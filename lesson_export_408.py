#!/usr/bin/env python3
"""Read-only, cutoff-bound 408 lesson evidence export. No grading or intake writes."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from bridge_archives import (BridgeError, check_manifest, file_hash, inventory, packages, read_json,
                             require, safe_path, source_file, write_zip)

SCHEMA = "cs408-lesson-export-v1"
LEDGER = Path("wiki/study_vaults/408-full/state/review-loop/events.jsonl")


def encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def instant(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "TIMEZONE_REQUIRED")
    return parsed.astimezone(dt.timezone.utc)


def checked_file(path: Path, root: Path | None = None) -> Path:
    path = Path(path).expanduser().absolute()
    require(".." not in path.parts and not any(p.is_symlink() for p in [path, *path.parents]), "UNSAFE_SOURCE_PATH")
    return source_file(path, root)


def load(path: Path, root: Path | None = None) -> dict:
    checked_file(path, root)
    value = read_json(path)
    require(isinstance(value, dict), "JSON_OBJECT_REQUIRED")
    return value


def turns_valid(turns: Any) -> list:
    require(isinstance(turns, list), "INVALID_LEARNING_TURNS")
    for t in turns:
        require(isinstance(t, dict) and t.get("role") in {"user", "assistant"}
                and isinstance(t.get("kind"), str) and isinstance(t.get("text"), str)
                and bool(t["text"].strip()) and "observed_at" in t, "INVALID_LEARNING_TURN")
        if t["observed_at"] is not None:
            instant(t["observed_at"])
    return turns


def run_of(event: dict) -> str | None:
    return event.get("campaign_run_id") or event.get("session_id")


def validate_coverage(expected: list[str], actual: list[str]) -> None:
    require(len(set(expected)) == len(expected) and len(set(actual)) == len(actual), "DUPLICATE_COVERAGE_ID")
    require(set(expected) == set(actual), "LESSON_COVERAGE_MISMATCH",
            {"missing": sorted(set(expected) - set(actual)), "unexpected": sorted(set(actual) - set(expected))})


def export_lesson(repo: str | Path, private_root: str | Path, *, output: str | Path,
                  snapshot_id: str, run_id: str | None = None, date: str | None = None,
                  cutoff: str | None = None, quick_pack_paths=(), supplement_paths=(),
                  expected_event_ids: list[str] | None = None) -> dict[str, Any]:
    """Export one run, or all campaign/standalone evidence on a Shanghai study date.

    snapshot_id is the caller's pinned Library MCP version, not a claim that the
    live event ledger is already published in that snapshot. Explicit cutoff is
    recommended when replaying an export. Existing output is immutable.
    """
    repo, private_root = Path(repo).absolute(), Path(private_root).absolute()
    output = Path(output).expanduser().absolute()
    require(bool(run_id) or bool(date), "RUN_ID_OR_DATE_REQUIRED")
    require(isinstance(snapshot_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", snapshot_id)), "SNAPSHOT_ID_REQUIRED")
    if date:
        require(dt.date.fromisoformat(date).isoformat() == date, "INVALID_DATE")
    if run_id:
        require(bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", run_id)), "INVALID_RUN_ID")
    require(not output.is_relative_to(repo) and not output.is_relative_to(private_root), "OUTPUT_MUST_BE_EXTERNAL")
    require(".." not in output.parts and not any(p.is_symlink() for p in [output, *output.parents]), "UNSAFE_OUTPUT_PATH")
    cutoff_dt = instant(cutoff) if cutoff else dt.datetime.now(dt.timezone.utc)
    cutoff = cutoff_dt.isoformat()
    ledger = repo / LEDGER
    raw = checked_file(ledger, repo).read_bytes() if ledger.exists() else b""
    require(not raw or raw.endswith(b"\n"), "INCOMPLETE_EVENT_LEDGER_LINE")
    events, seen = [], set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event_kind") not in {"outcome", "repair"} or not event.get("item_id") or not run_of(event):
            continue
        if run_id and run_of(event) != run_id:
            continue
        if date and (event.get("session_review_date") or event.get("observed_date")) != date:
            continue
        if instant(event["event_time"]) > cutoff_dt:
            continue
        eid = event.get("event_id")
        require(isinstance(eid, str) and eid and eid not in seen, "DUPLICATE_EVENT_ID")
        seen.add(eid)
        events.append(event)
    if expected_event_ids is not None:
        validate_coverage(expected_event_ids, [e["event_id"] for e in events])
    groups: dict[tuple[str, str], list] = {}
    for e in events:
        groups.setdefault((run_of(e), e["item_id"]), []).append(e)
    for es in groups.values():
        es.sort(key=lambda e: instant(e["event_time"]))
        require(len({e.get("formal_node_id") for e in es}) == 1, "ITEM_FORMAL_ID_DRIFT")
        commitments = {e["campaign_item_commitment_sha256"] for e in es if e.get("campaign_item_commitment_sha256")}
        require(len(commitments) <= 1, "ITEM_COMMITMENT_DRIFT")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lesson-export-", dir=output.parent) as temporary:
        stage = Path(temporary)
        files: dict[str, Path] = {}
        def add_json(name: str, value: Any) -> str:
            target = stage / safe_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded(value))
            files[name] = target
            return name
        def add_source(name: str, path: Path, root: Path | None = None) -> str:
            checked_file(path, root)
            name = safe_path(name)
            require(name not in files or file_hash(files[name]) == file_hash(path), "EXPORT_PATH_COLLISION")
            # Freeze bytes so the inventory and ZIP see the same source version.
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            files[name] = target
            return name
        def assets(rows: list) -> list:
            refs = []
            for asset in rows:
                p = checked_file(Path(asset["path"]))
                digest = file_hash(p)
                require(digest == asset.get("sha256"), "QUESTION_ASSET_HASH_MISMATCH")
                expected_size = asset.get("size_bytes", asset.get("size"))
                if expected_size is not None:
                    require(p.stat().st_size == expected_size, "QUESTION_ASSET_SIZE_MISMATCH")
                name = "question_assets/" + digest + p.suffix.lower()
                refs.append({"path": add_source(name, p), "sha256": digest, "source_ref": asset.get("ref")})
            return refs

        # Verified native packages can be turned into transport ZIPs in staging;
        # supplied quick-pack ZIPs are embedded byte-for-byte.
        inputs = [Path(p) for p in quick_pack_paths]
        bound_packages = {}
        for e in events:
            binding = e.get("source_package") or {}
            locator = binding.get("locator", "")
            if locator:
                require(locator.startswith("cs408-conversation-package://"), "INVALID_PACKAGE_LOCATOR")
                pid = locator.split("://", 1)[1]
                require(re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", pid), "INVALID_PACKAGE_ID")
                require(pid not in bound_packages or bound_packages[pid] == binding["sha256"], "PACKAGE_VERSION_CONFLICT")
                bound_packages[pid] = binding["sha256"]
        supplied_ids = set()
        if inputs:
            with packages(inputs, expected_subject="408") as supplied:
                supplied_ids = {p["manifest"]["package_id"] for p in supplied}
        for pid in sorted(set(bound_packages) - supplied_ids):
            candidates = list(private_root.glob("????-??-??/" + pid))
            require(len(candidates) == 1, "BOUND_PACKAGE_MISSING", pid)
            from study_bridge import pack
            packed = pack("408", candidates[0], repo, base=stage / "native-transport")
            inputs.append(Path(packed["path"]))
        package_refs = {}
        with (packages(inputs, expected_subject="408") if inputs else nullcontext([])) as packed:
            for p in packed:
                m = p["manifest"]
                pid = m["package_id"]
                if pid in bound_packages:
                    require(m["version"] == bound_packages[pid], "EVENT_PACKAGE_HASH_MISMATCH")
                name = add_source("quick_packs/" + pid + ".zip", p["zip_path"])
                package_refs[pid] = {"package_id": pid, "path": name, "sha256": p["zip_sha256"],
                                     "version": m["version"]}
                # Keep the native conversation intact, including repeated words.
                add_source("package_conversations/" + pid + ".json", p["root"] / "original/conversation.json")

        evidence: dict[tuple, list] = {k: [] for k in groups}
        displays: dict[tuple, dict] = {}
        request_seen = set()
        journal = private_root / ".turn-runner-v1"
        for p in sorted((journal / "results").glob("*.json")):
            saved = load(p, journal)
            outcome = saved.get("outcome", {})
            key = (outcome.get("run_id"), outcome.get("item_id"))
            if key not in groups or outcome.get("status") == "blocked":
                continue
            require(p.stem == file_hash(p), "RESULT_HASH_MISMATCH")
            request_path = checked_file(Path(saved["request_file"]), journal / "requests")
            request = load(request_path, journal / "requests")
            require(file_hash(request_path) == saved.get("request_sha256"), "REQUEST_HASH_MISMATCH")
            require((request.get("run_id"), request.get("item_id")) == key, "REQUEST_IDENTITY_MISMATCH")
            body = request["input"]
            # Buffered append operations have no canonical timestamp/event. Their
            # complete dialogue is exported through a selected terminal package,
            # never attached to an earlier cutoff just by matching run/item.
            if request.get("command") == "append-turn":
                continue
            if body.get("event_time") and instant(body["event_time"]) > cutoff_dt:
                continue
            if request.get("command") == "finalize-appended-and-next" and not outcome.get("correction_review_event_id"):
                continue
            display_path = checked_file(Path(body["display_receipt_path"]), private_root / "turns")
            digest = file_hash(display_path)
            require(digest == body.get("display_receipt_sha256") == display_path.stem, "DISPLAY_HASH_MISMATCH")
            display = load(display_path, private_root / "turns")
            require((display.get("run_id"), display.get("item_id")) == key, "DISPLAY_IDENTITY_MISMATCH")
            commitments = {e["campaign_item_commitment_sha256"] for e in groups[key] if e.get("campaign_item_commitment_sha256")}
            require(not commitments or display.get("campaign_item_commitment_sha256") in commitments, "DISPLAY_COMMITMENT_MISMATCH")
            require(display.get("formal_node_id") == groups[key][0].get("formal_node_id"), "DISPLAY_FORMAL_ID_MISMATCH")
            # Terminal correction linkage is stronger than run/item alone.
            correction = outcome.get("correction_review_event_id")
            if correction and correction not in {e["event_id"] for e in groups[key]}:
                continue  # terminal result is later than this export cutoff
            result_ref = add_source("raw/results/" + p.name, p, journal)
            request_ref = add_source("raw/requests/" + request_path.name, request_path, journal)
            display_ref = add_source("raw/displays/" + display_path.name, display_path, private_root)
            if key not in displays:
                displays[key] = {"display_ref": display_ref, "learner_surface": display.get("learner_surface"),
                                 "question_provenance": display.get("question_provenance"),
                                 "assets": assets(display.get("question_assets", []))}
            if saved["request_sha256"] in request_seen:
                continue
            request_seen.add(saved["request_sha256"])
            turns = request.get("learning_turns", body.get("new_turns", []))
            turns_valid(turns)
            evidence[key].append({"source": "immutable_turn_request", "request_ref": request_ref,
                                  "result_ref": result_ref, "request_sha256": saved["request_sha256"],
                                  "display_ref": display_ref, "turns": turns})

        by_id = {e["event_id"]: e for e in events}
        supplements_root = private_root / ".learning-evidence-v1/supplements"
        supplement_inputs = [(p, True) for p in sorted(supplements_root.glob("*.json"))]
        supplement_inputs += [(Path(p), False) for p in supplement_paths]
        supplement_seen = set()
        for p, automatic in supplement_inputs:
            supplement = load(p, supplements_root if automatic else None)
            if automatic and supplement.get("event_id") not in by_id:
                continue
            digest = file_hash(p)
            if automatic:
                require(p.stem == digest, "SUPPLEMENT_FILE_HASH_MISMATCH")
            if digest in supplement_seen:
                continue
            require(supplement.get("schema") == "cs408-lesson-evidence-supplement-v1", "SUPPLEMENT_SCHEMA_INVALID")
            e = by_id.get(supplement.get("event_id"))
            require(e is not None, "SUPPLEMENT_EVENT_OUTSIDE_SCOPE")
            require(supplement.get("event_sha256") == sha(encoded(e)), "SUPPLEMENT_EVENT_HASH_MISMATCH")
            key = (run_of(e), e["item_id"])
            require((supplement.get("run_id"), supplement.get("item_id")) == key
                    and supplement.get("formal_node_id") == e.get("formal_node_id"), "SUPPLEMENT_IDENTITY_MISMATCH")
            require(supplement.get("provenance") == "recovered_from_current_conversation", "SUPPLEMENT_PROVENANCE_INVALID")
            turns_valid(supplement["turns"])
            ref = add_source("raw/supplements/" + digest + ".json", p)
            supplement_seen.add(digest)
            evidence[key].append({"source": supplement["provenance"], "event_id": e["event_id"],
                                  "supplement_ref": ref, "turns": supplement["turns"]})

        rows = []
        for key, es in groups.items():
            pids = sorted({e["source_package"]["locator"].split("://", 1)[1] for e in es if e.get("source_package")})
            gaps = []
            turn_refs = evidence[key]
            for pid in pids:
                turn_refs.append({"source": "sealed_native_conversation", "conversation_ref": "package_conversations/" + pid + ".json", "package_id": pid})
            if not pids and not any(t.get("turns") for t in turn_refs):
                gaps.append("raw_learning_dialogue_missing: recorded classifications are not verbatim reasoning")
            if key not in displays:
                gaps.append("verified_question_display_missing")
            row = {"schema": SCHEMA, "review_id": "LR-" + sha(encoded(key))[:24], "run_id": key[0],
                   "item_id": key[1], "event_id": es[0]["event_id"], "event_ids": [e["event_id"] for e in es],
                   "coverage_ids": [e["event_id"] for e in es], "formal_node_id": es[0].get("formal_node_id"),
                   "package_id": pids[0] if len(pids) == 1 else None, "package_ids": pids,
                   "first_result": es[0], "final_result": es[-1], "events": es,
                   "question": displays.get(key), "raw_turns": turn_refs, "evidence_gaps": gaps}
            rows.append(row)

        standalone_ids = []
        if date and not run_id:
            for p in sorted((private_root / ".learning-evidence-v1/records").glob("*.json")):
                record = load(p, private_root)
                if record.get("study_date") != date or instant(record["recorded_at"]) > cutoff_dt:
                    continue
                require(record.get("schema") == "cs408-light-learning-evidence-v1", "LIGHT_EVIDENCE_SCHEMA_INVALID")
                require(p.stem == file_hash(p), "LIGHT_EVIDENCE_HASH_MISMATCH")
                eid = record.get("evidence_id")
                require(isinstance(eid, str) and eid and eid not in standalone_ids, "DUPLICATE_EVIDENCE_ID")
                standalone_ids.append(eid)
                turns_valid(record["turns"])
                ref = add_source("raw/standalone/" + p.name, p, private_root)
                question = dict(record["question"])
                question["assets"] = assets(question.get("assets", []))
                rows.append({"schema": SCHEMA, "review_id": "LR-" + sha(encoded(eid))[:24],
                             "run_id": None, "lesson_id": record.get("lesson_id"), "item_id": None,
                             "event_id": None, "event_ids": [], "evidence_id": eid, "coverage_ids": [eid],
                             "formal_node_id": record.get("formal_node_id"), "source_id": record.get("source_id"),
                             "package_id": None, "package_ids": [], "first_result": record["result"],
                             "final_result": record["result"], "events": [], "question": question,
                             "raw_turns": [{"source": "immutable_light_learning_evidence", "record_ref": ref, "turns": record["turns"]}],
                             "evidence_gaps": [] if record["turns"] else ["raw_learning_dialogue_missing"]})
        require(rows, "NO_LESSON_EVIDENCE")
        expected = [e["event_id"] for e in events] + standalone_ids
        actual = [eid for row in rows for eid in row["coverage_ids"]]
        validate_coverage(expected, actual)
        require(len({r["review_id"] for r in rows}) == len(rows), "DUPLICATE_REVIEW_ID")
        results = stage / "lesson_results.jsonl"
        results.write_bytes(b"".join(encoded(r) for r in rows))
        files["lesson_results.jsonl"] = results
        add_json("lesson_manifest.json", {"schema": SCHEMA, "subject": "408", "lesson_id": run_id or date, "run_id": run_id, "study_date": date,
                 "snapshot_id": snapshot_id, "snapshot_binding": "caller_pinned_library_version",
                 "canonical_events_source": str(LEDGER), "ledger_read_sha256": sha(raw), "export_cutoff": cutoff,
                 "selected_events_sha256": sha(b"".join(encoded(e) for e in events)),
                 "event_sha256": {e["event_id"]: sha(encoded(e)) for e in events},
                 "expected_event_ids": [e["event_id"] for e in events], "actual_event_ids": [e for r in rows for e in r["event_ids"]],
                 "expected_coverage_ids": expected, "actual_coverage_ids": actual,
                 "review_ids": [r["review_id"] for r in rows], "review_count": len(rows),
                 "event_count": len(events), "standalone_count": len(standalone_ids), "quick_packs": list(package_refs.values()),
                 "evidence_gaps": [{"review_id": r["review_id"], "gaps": r["evidence_gaps"]} for r in rows if r["evidence_gaps"]],
                 "formal_write_count": 0, "learner_evidence_write_count": 0})
        readme = stage / "README.md"
        readme.write_text("先读 lesson_manifest.json 核对课时、截止时间与完整 coverage IDs，再逐行读 lesson_results.jsonl。\n"
                          "每行是一道真实课时题；首次错误与提示后纠正保留为不同事件，但不重复计题。正确题没有 package_id 是正常情况。\n"
                          "raw_turns 和 package_conversations 保留原话；evidence_gaps 表示缺失，禁止从 sound 等判定补造推理。\n"
                          "quick_packs 中 ZIP 保持既有原 bytes。原题图片位于 question_assets，question 给出相对路径。\n"
                          "GPT A 必须逐项回显 review_updates，覆盖每个 review_id 及其全部 event_ids/coverage_ids；提示后理解不能判作独立掌握。\n"
                          "snapshot_id 是调用方固定的资料库版本；本课原始事件另按 canonical ledger 和截止时间绑定。\n")
        files["README.md"] = readme
        add_json("manifest.json", {"schema_version": 1, "kind": "lesson_review_bundle", "subject": "408",
                 "lesson_schema": SCHEMA, "lesson_id": run_id or date, "snapshot_id": snapshot_id, "files": inventory(files)})
        # Reject any concurrent canonical ledger change; the next export can retry.
        if ledger.exists():
            require(checked_file(ledger, repo).read_bytes() == raw, "EVENT_LEDGER_CHANGED_DURING_EXPORT")
        write_zip(files, output)
    return {"status": "exported", "path": str(output), "zip_sha256": file_hash(output), "schema": SCHEMA,
            "review_count": len(rows), "event_count": len(events), "standalone_count": len(standalone_ids),
            "review_ids": [r["review_id"] for r in rows], "coverage_ids": actual, "cutoff": cutoff,
            "evidence_gaps": [{"review_id": r["review_id"], "gaps": r["evidence_gaps"]} for r in rows if r["evidence_gaps"]],
            "formal_write_count": 0, "learner_evidence_write_count": 0}


def verify_lesson_directory(root: Path) -> dict[str, Any]:
    """Validate a fully extracted lesson bundle, including event/row coverage."""
    outer = check_manifest(root)
    require(outer.get("kind") == "lesson_review_bundle" and outer.get("subject") == "408", "NOT_A_408_LESSON_BUNDLE")
    manifest = load(root / "lesson_manifest.json", root)
    require(manifest.get("schema") == SCHEMA and manifest.get("snapshot_id") == outer.get("snapshot_id")
            and manifest.get("lesson_id") == outer.get("lesson_id"), "LESSON_MANIFEST_IDENTITY_MISMATCH")
    data = checked_file(root / "lesson_results.jsonl", root).read_bytes()
    require(data.endswith(b"\n"), "INCOMPLETE_LESSON_RESULTS")
    rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    require(all(r.get("schema") == SCHEMA for r in rows), "LESSON_ROW_SCHEMA_INVALID")
    event_ids = [eid for r in rows for eid in r["event_ids"]]
    coverage_ids = [eid for r in rows for eid in r["coverage_ids"]]
    validate_coverage(manifest["expected_event_ids"], event_ids)
    validate_coverage(manifest["actual_event_ids"], event_ids)
    validate_coverage(manifest["expected_coverage_ids"], coverage_ids)
    validate_coverage(manifest["actual_coverage_ids"], coverage_ids)
    validate_coverage(manifest["review_ids"], [r["review_id"] for r in rows])
    require(manifest["review_count"] == len(rows) and manifest["event_count"] == len(event_ids), "LESSON_COUNT_MISMATCH")
    actual_events = {e["event_id"]: e for r in rows for e in r["events"]}
    require(manifest.get("event_sha256") == {eid: sha(encoded(e)) for eid, e in actual_events.items()}, "LESSON_EVENT_HASH_MISMATCH")
    require(sha(b"".join(encoded(actual_events[eid]) for eid in manifest["expected_event_ids"])) == manifest["selected_events_sha256"], "LESSON_EVENT_SET_HASH_MISMATCH")
    for r in rows:
        if r["events"]:
            require(all(run_of(e) == r["run_id"] and e["item_id"] == r["item_id"]
                        and e.get("formal_node_id") == r.get("formal_node_id") for e in r["events"]), "LESSON_EVENT_IDENTITY_MISMATCH")
            require(r["first_result"] == r["events"][0] and r["final_result"] == r["events"][-1], "LESSON_RESULT_DERIVATION_MISMATCH")
        require([e["event_id"] for e in r["events"]] == r["event_ids"], "LESSON_ROW_EVENT_MISMATCH")
        require(r["coverage_ids"] == (r["event_ids"] or [r.get("evidence_id")]), "LESSON_ROW_COVERAGE_MISMATCH")
        require(bool(r["event_ids"]) or r.get("event_id") is None, "STANDALONE_FAKE_EVENT_ID")
        for evidence in r["raw_turns"]:
            for key, value in evidence.items():
                if key.endswith("_ref"):
                    checked_file(root / safe_path(value), root)
        if r.get("question"):
            q = r["question"]
            if q.get("display_ref"):
                checked_file(root / safe_path(q["display_ref"]), root)
            for asset in q.get("assets", []):
                require(file_hash(checked_file(root / safe_path(asset["path"]), root)) == asset["sha256"], "LESSON_ASSET_HASH_MISMATCH")
    return {"manifest": manifest, "results": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("repo", "private-root", "output", "snapshot-id"):
        parser.add_argument("--" + field, required=True)
    for field in ("run-id", "date", "cutoff"):
        parser.add_argument("--" + field)
    parser.add_argument("--quick-pack", action="append", default=[])
    parser.add_argument("--supplement", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        result = export_lesson(args.repo, args.private_root, output=args.output, snapshot_id=args.snapshot_id,
                               run_id=args.run_id, date=args.date, cutoff=args.cutoff,
                               quick_pack_paths=args.quick_pack, supplement_paths=args.supplement)
    except (BridgeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
