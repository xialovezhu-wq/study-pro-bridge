#!/usr/bin/env python3
"""Small, explicit file boundary between the four web projects and their owners.

No model calls, browser sends, history selection, grading, or automatic A decisions.
Run files describe delivery only; formal state remains in each subject repository.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import io
import shutil
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

import study_publication as publication
from bridge_archives import (ArchiveBudget, BridgeError, DEFAULT_LIMIT, check_manifest, copy_file,
                             extract_zip, file_hash, inventory, collect_inputs, packages as collect_packages,
                             read_json as load, require, safe_path, source_file, write_zip)

BASE = Path(__file__).resolve().parent
SUBJECTS = ("math", "english", "408")
PROJECT_SUBJECTS = {"A": ("math",), "B": ("math",), "C": ("math",), "D": ("408",)}
REPOS = {s: Path.home() / "Documents" / f"kaoyan-{s}" for s in SUBJECTS}
ROLES = {"question_image": "problem", "problem_image": "problem", "solution_image": "solution",
         "explanation_image": "solution", "solution_text": "solution", "conversation": "conversation"}
VISUAL_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".pdf"}
BUSINESS_FILES = {
    "A": ["advice.json"],
    "B": ["review_plan.json", "questions.jsonl", "answer_rubrics.jsonl", "historical_basis.jsonl",
          "current_only_not_eligible.jsonl", "misconception_map.json", "summary.md"],
    "C": ["selected_questions.json", "rationale.md"],
    "D": ["due_concepts.json", "selected_items.jsonl", "unresolved.jsonl", "coverage_report.json", "rationale.md"],
}
A_MATH_CONTRACT = "math-final-semantics-and-profiles-v2"
B_MATH_CONTRACT = "math-independent-review-v2"
C_MATH_CONTRACT = "math-gpt6-handoff-selection-v2"
C_HANDOFF_FILES = {"START_HERE.md", "handoff.json", "refs.json"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def write(path: Path, data: bytes, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in [path, *path.parents]), "SYMLINK_DESTINATION")
    if path.exists() and immutable:
        require(path.read_bytes() == data, "EXISTING_CONTENT_CONFLICT", str(path))
        return
    fd, tmp = tempfile.mkstemp(prefix=".bridge-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def valid_id(value: str) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value), "INVALID_ID")
    return value


def date(value: str) -> str:
    require(isinstance(value, str), "INVALID_DATE")
    try:
        require(dt.date.fromisoformat(value).isoformat() == value, "INVALID_DATE")
    except ValueError as exc:
        raise BridgeError("INVALID_DATE") from exc
    return value


def module_at(name: str, path: Path) -> Any:
    require(path.is_file(), "NATIVE_ADAPTER_UNAVAILABLE", str(path))
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec and spec.loader, "NATIVE_ADAPTER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def native_package(subject: str, package_dir: Path, repo: Path) -> tuple[dict[str, Any], dict[str, Path], dict[str, str]]:
    """Reopen only the one user-specified native package through its actual owner."""
    manifest = load(package_dir / "manifest.json")
    original, roles = {}, {}
    if subject == "math":
        module = module_at("bridge_math_quick_intake", repo / "数学一回滚复习系统/scripts/quick_intake.py")
        try:
            module.validate_conversation_package_manifest(manifest, (package_dir / "manifest.json").resolve(), "Bridge")
        except module.QuickIntakeError as exc:
            raise BridgeError("NATIVE_PACKAGE_VALIDATION_FAILED", {"subject": subject, "reason": str(exc)}) from exc
        for key, row in manifest["files"].items():
            path = repo / row["path"]
            require(path.is_file(), "CURRENT_PACKAGE_ORIGINAL_MISSING", row["path"])
            original[path.name] = path
            roles[path.name] = key
        for row in manifest.get("artifacts", []):
            path = repo / row["path"]
            require(path.is_file(), "CURRENT_PACKAGE_ORIGINAL_MISSING", row["path"])
            original["attachments/" + path.name] = path
            roles["attachments/" + path.name] = row["role"]
    elif subject == "english":
        sys.path.insert(0, str(repo))
        from english_pipeline.packages import validate_conversation_package
        from english_pipeline.errors import PipelineError
        try:
            manifest = validate_conversation_package(package_dir)
        except PipelineError as exc:
            raise BridgeError("NATIVE_PACKAGE_VALIDATION_FAILED", {"subject": subject, "reason": str(exc)}) from exc
        for path, row in manifest["files"].items():
            original[safe_path(path)] = package_dir / path
            roles[path] = row.get("role", "support")
    else:
        module = module_at("bridge_cs408_packages", repo / "scripts/conversation_package_408.py")
        require(manifest.get("subject") == "cs408" and manifest.get("sealed") is True, "UNSEALED_OR_WRONG_SUBJECT_PACKAGE")
        try:
            module.verify_package_binding(module.LOCATOR_PREFIX + manifest["package_id"],
                                          expected_canonical_sha256=manifest["canonical_sha256"],
                                          require_sealed=True, private_root=package_dir.parent.parent)
        except module.ConversationPackageError as exc:
            raise BridgeError("NATIVE_PACKAGE_VALIDATION_FAILED", {"subject": subject, "reason": str(exc)}) from exc
        for row in manifest["files"]:
            path = safe_path(row["relative_path"])
            original[path] = package_dir / path
            roles[path] = row.get("role", "support")
    for path in ("manifest.json", "receipt.json"):
        original[path] = package_dir / path
        roles[path] = "native_receipt" if path == "receipt.json" else "native_manifest"
    require("conversation.json" in original, "FULL_CONVERSATION_REQUIRED")
    return manifest, original, roles


def pack(subject: str, package_dir: Path, repo: Path, base: Path = BASE,
         question_id: str | None = None) -> dict[str, Any]:
    native, paths, roles = native_package(subject, package_dir, repo)
    require(native.get("subject") == ("cs408" if subject == "408" else subject), "CROSS_SUBJECT_TASK")
    package_id, study_date = valid_id(native["package_id"]), date(native["study_date"])
    identity = native.get("source_identity", {})
    question_id = valid_id(question_id or identity.get("formal_id") or identity.get("question_id") or package_id)
    files = {"original/" + key: source_file(path, package_dir) for key, path in paths.items()}
    output_roles = {"original/" + key: value for key, value in roles.items()}
    question = {"question_id": question_id, "identity_kind": "package_locator" if question_id == package_id else "source_identity",
                "conversation_path": "original/conversation.json", "problem_paths": [], "solution_paths": [],
                "source_path": "original/source.json"}
    for path, role in output_roles.items():
        normalized = ROLES.get(role)
        if normalized in {"problem", "solution"}:
            question[normalized + "_paths"].append(path)
    version = native.get("canonical_sha256", native.get("package_canonical_sha256"))
    require(isinstance(version, str) and re.fullmatch(r"[0-9a-f]{64}", version), "NATIVE_PACKAGE_VERSION_REQUIRED")
    # Scan only the exact current package's declared files; never print matches.
    for path in files.values():
        publication.file_hash(path, scan=True)
    manifest = {"schema_version": 1, "kind": "quick_pack", "subject": subject,
                "package_id": package_id, "study_date": study_date, "version": version,
                "native_manifest_sha256": file_hash(files["original/manifest.json"]),
                "questions": [question], "missing_fields": native.get("missing_fields", []),
                "files": inventory(files, output_roles)}
    target = base / "outbox" / study_date / "quick-packs" / subject / f"{study_date}_{subject}_{package_id}.zip"
    with tempfile.TemporaryDirectory(prefix="study-bridge-pack-") as temporary:
        manifest_path = Path(temporary).resolve() / "manifest.json"
        write(manifest_path, json_bytes(manifest))
        files["manifest.json"] = manifest_path
        write_zip(files, target)
    return {"path": str(target), "package_id": package_id, "subject": subject,
            "version": version, "zip_sha256": file_hash(target), "missing_fields": manifest["missing_fields"]}


def invoke(argv: list[str], repo: Path) -> dict[str, Any]:
    result = subprocess.run(argv, cwd=repo, capture_output=True, timeout=300)
    try:
        output = json.loads(result.stdout)
    except ValueError as exc:
        raise BridgeError("NATIVE_ADAPTER_NON_JSON", {"command": argv[1:3], "exit_code": result.returncode}) from exc
    require(result.returncode == 0, "NATIVE_ADAPTER_REJECTED", output)
    return output


class Owners:
    def __init__(self, repos: dict[str, Path] | None = None):
        self.repos = repos or REPOS

    def due(self, target_date: str) -> dict[str, Any]:
        repo = self.repos["408"]
        require(Path("/usr/local/bin/python3").is_file(), "VERIFIED_408_PYTHON_UNAVAILABLE")
        return invoke(["/usr/local/bin/python3", str(repo / "scripts/study_bridge_408.py"), "due", "--repo", str(repo),
                       "--date", target_date], repo)

    def sources(self, subject: str, run_id: str) -> dict[str, Any]:
        return publication.describe(subject, self.repos[subject], "bridge-read:" + run_id)

    def import_queue(self, project: str, subject: str, payload: dict[str, Any], payload_path: Path) -> dict[str, Any]:
        repo = self.repos[subject]
        require(repo.resolve() != REPOS[subject].resolve() or payload_path.resolve().is_relative_to(BASE / "inbox"),
                "PRODUCTION_NATIVE_INPUT_REQUIRES_CANONICAL_BRIDGE_INBOX")
        require(subject != "math" or repo.resolve() == REPOS["math"].resolve(),
                "ISOLATED_MATH_REQUIRES_BACKEND_WITH_INJECTED_VERIFIER")
        write(payload_path, json_bytes(payload), immutable=True)
        if subject == "math" and project in {"B", "C"}:
            return invoke([sys.executable, str(repo / "数学一回滚复习系统/scripts/scheduler.py"),
                           "import-review-source", "--manifest-file", str(payload_path)], repo)
        return invoke(["/usr/local/bin/python3", str(repo / "scripts/study_bridge_408.py"), "import-d",
                       "--repo", str(repo), "--input-json", str(payload_path)], repo)

    def record_review(self, subject: str, payload_path: Path) -> dict[str, Any]:
        repo = self.repos[subject]
        require(repo.resolve() != REPOS[subject].resolve() or payload_path.resolve().is_relative_to(BASE / "inbox"),
                "PRODUCTION_NATIVE_INPUT_REQUIRES_CANONICAL_BRIDGE_INBOX")
        require(subject != "math" or repo.resolve() == REPOS["math"].resolve(),
                "ISOLATED_MATH_REQUIRES_BACKEND_WITH_INJECTED_VERIFIER")
        if subject == "math":
            command = [sys.executable, str(repo / "数学一回滚复习系统/scripts/scheduler.py"),
                       "record-review-outcome", "--payload-file", str(payload_path)]
        elif subject == "408":
            command = ["/usr/local/bin/python3", str(repo / "scripts/study_bridge_408.py"), "record-review",
                       "--repo", str(repo), "--input-json", str(payload_path)]
        else:
            raise BridgeError("ENGLISH_REQUIRES_NATIVE_TYPED_DECISION", "Use the subject-owned capture/freeze/typed writer; suggestions are not events.")
        return invoke(command, repo)

    def verify_a(self, subject: str, receipt_path: Path, run: dict[str, Any]) -> dict[str, Any]:
        repo = self.repos[subject]
        if subject == "math":
            expected_path = repo / "数学一回滚复习系统/A建议裁决" / (run["run_id"] + ".json")
            require(receipt_path.resolve() == expected_path.resolve(), "NATIVE_A_RECEIPT_PATH_MISMATCH")
            checked = invoke([sys.executable, str(repo / "数学一回滚复习系统/scripts/quick_intake.py"),
                              "verify-advice-decision", "--run-id", run["run_id"]], repo)
            require(checked.get("status") == "LOCAL_DECISION_COMPLETE" and checked.get("verified") is True
                    and checked.get("run_id") == run["run_id"] and checked.get("source_commit") == run["source_commit"]
                    and checked.get("advice_zip_sha256") == run["download_sha256"], "NATIVE_A_TASK_ADVICE_BINDING_MISMATCH")
            require(Path(checked["decision_receipt_path"]).resolve() == expected_path.resolve(), "NATIVE_A_RECEIPT_PATH_MISMATCH")
            expected = {p["package_id"]: p["version"] for p in run["input_packages"]}
            actual = {p["package_id"]: p["package_version"] for p in checked["packages"]}
            require(expected == actual and len(actual) == len(checked["packages"]), "NATIVE_A_PACKAGE_VERSION_MISMATCH")
            pairs = {(q["package_id"], q["question_id"]) for q in run["input_questions"]}
            decisions = checked["decisions"]
            require(len(decisions) == len(pairs) and {(q["package_id"], q["question_id"]) for q in decisions} == pairs,
                    "NATIVE_A_PACKAGE_COVERAGE_MISMATCH")
            require(all(q.get("outcome") in {"accepted", "modified", "rejected", "no_change"} and q.get("local_reason") for q in decisions),
                    "NATIVE_A_ADVICE_ADJUDICATION_MISSING")
            require(checked.get("publication_event_id"), "NATIVE_A_TERMINAL_PUBLICATION_EVENT_MISSING")
            return {"publication_event_id": checked["publication_event_id"], "decisions": decisions,
                    "native_receipt_sha256": file_hash(expected_path), "record_sha256": checked["record_sha256"],
                    "package_ids": sorted(expected)}
        if subject == "english":
            checked = invoke([sys.executable, str(repo / "scripts/english_learning_pipeline.py"),
                              "validate-formal-receipt", "--repo-root", str(repo), "--state-dir", str(repo / "intake"),
                              "--writer-receipt", str(receipt_path)], repo)
            require(checked.get("status") == "PASS" and checked.get("subject") == subject, "NATIVE_A_RECEIPT_REJECTED")
            expected = {x["package_id"]: x["version"] for x in run["input_packages"] if x["package_id"] in checked["package_ids"]}
            require(expected, "NATIVE_A_PACKAGE_COVERAGE_MISMATCH")
            raw_hashes = checked["package_sha256s"]
            require(isinstance(raw_hashes, list) and len(raw_hashes) == len(checked["package_ids"])
                    and len(set(checked["package_ids"])) == len(checked["package_ids"]), "NATIVE_A_PACKAGE_VERSION_MISMATCH")
            hashes = dict(zip(checked["package_ids"], raw_hashes))
            require(all(hashes.get(key) == value for key, value in expected.items()),
                    "NATIVE_A_PACKAGE_VERSION_MISMATCH")
            decisions, covered = [], set()
            for record in checked["decisions"]:
                event = record["event"]
                if (event.get("task_id"), event.get("source_commit"), event.get("origin"), event.get("kind")) != (
                        run["run_id"], run["source_commit"], "project-A", "adjudication"):
                    continue
                require(event["outcome"] in {"accepted", "modified", "rejected"}, "A_ADJUDICATION_IS_NOT_MASTERY")
                covered.update(ref["package_id"] for ref in record["evidence_refs"] if ref.get("package_id") in expected)
                decisions.append({"event_id": record["event_id"], "outcome": event["outcome"], "unit_id": event["unit_id"]})
            require(set(expected) <= covered, "NATIVE_A_DECISIONS_NOT_BOUND_TO_INPUT_PACKAGES")
            decision_ids = {row["event_id"] for row in decisions}
            require(any(row.get("decision_event_id") in decision_ids and row.get("sha256") == run["download_sha256"]
                        for row in checked.get("verified_attachments", [])), "NATIVE_A_DECISION_NOT_BOUND_TO_DOWNLOADED_ADVICE")
            return {"publication_event_id": checked["publication_event_id"], "decisions": decisions,
                    "native_receipt_sha256": checked["writer_receipt_sha256"], "package_ids": sorted(expected)}
        if subject == "408":
            checked = invoke(["/usr/local/bin/python3", str(repo / "scripts/study_bridge_408.py"), "verify-a",
                              "--repo", str(repo), "--input-json", str(receipt_path)], repo)
            require(checked.get("status") == "verified" and checked.get("subject") == subject
                    and checked.get("formal_complete") is True, "NATIVE_A_RECEIPT_REJECTED")
            require(checked.get("task_id") == run["run_id"] and
                    (checked.get("source_commit") or checked.get("library_version")) == run["source_commit"], "NATIVE_A_TASK_VERSION_MISMATCH")
            expected = {x["package_id"]: x["version"] for x in run["input_packages"]}
            package_id = checked.get("package_id")
            require(package_id in expected and checked.get("evidence_sha256") == expected[package_id], "NATIVE_A_PACKAGE_VERSION_MISMATCH")
            advice = load(Path(run["output_dir"]) / "advice.json")
            hashes = {publication.digest(advice)} | {publication.digest(item) for item in advice["items"] if item["package_id"] == package_id}
            require(checked.get("proposal_sha256") in hashes, "NATIVE_A_DECISION_NOT_BOUND_TO_DOWNLOADED_ADVICE")
            require(checked.get("advice_adjudication") in {"accepted", "modified", "rejected"}, "NATIVE_A_ADVICE_ADJUDICATION_MISSING")
            require(checked.get("publication_event_id"), "NATIVE_A_TERMINAL_PUBLICATION_EVENT_MISSING")
            return {"publication_event_id": checked["publication_event_id"], "package_ids": [package_id],
                    "native_receipt_sha256": checked["terminal_sha256"], "decisions": [{"package_id": package_id,
                    "outcome": checked["advice_adjudication"], "formal_id": checked["formal_id"], "native_decision": checked["decision"]}]}
        raise BridgeError("NATIVE_A_RECEIPT_ADAPTER_NOT_READY", subject)


class Published:
    def __init__(self, state: Path):
        self.state = state

    def local_pending_receipts(self, subject: str):
        """Only native local work can hold a local reader. Untagged receipts predate local publication."""
        for path in (self.state / subject / "receipts").glob("*.json"):
            row = load(path)
            if row.get("publication_target") == "local" and row.get("status") != "PUBLISHED":
                yield row

    def latest(self, subject: str) -> dict[str, Any]:
        latest = load(self.state / subject / "latest.json")
        require(latest.get("status") == "PUBLISHED" and latest.get("subject") == subject,
                "LATEST_PUBLICATION_UNAVAILABLE")
        for path in (self.state / subject / "receipts").glob("*.json"):
            row = load(path)
            require(row.get("status") == "PUBLISHED" or (not row.get("automatic_requested") and
                    row.get("status") not in {"PENDING", "PREPARED", "COMMITTED_PENDING_PUSH", "COMMITTED_PENDING_LOCAL_VERIFY",
                                              "PUSH_RESULT_UNKNOWN", "CLOUD_PENDING", "LOCAL_SNAPSHOT_PENDING"}),
                    "LOCAL_SNAPSHOT_PENDING" if publication.configured_target(self.state) == "local" else "LOCAL_COMMIT_CLOUD_PENDING",
                    {"event_id": row.get("event_id"), "status": row.get("status")})
        return latest

    def event_receipt(self, subject: str, event_id: str) -> dict[str, Any]:
        publisher = publication.Publisher(subject, REPOS[subject], self.state)
        path = publisher.receipt_path(event_id)
        row = load(path)
        require(row.get("subject") == subject and row.get("event_id") == event_id, "PUBLICATION_EVENT_RECEIPT_MISMATCH")
        return dict(row, receipt_path=str(path))

    def verify(self, subject: str, commit: str) -> tuple[dict[str, Any], dict[str, Any]]:
        receipt = publication.verify_published(subject, commit, self.state)
        return receipt, load(Path(receipt.get("resolved_manifest_path", receipt["manifest_path"])))

    def inspect_version(self, subject: str, commit: str, *,
                        resolve_inventory: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read a previously published fixed commit without refreshing/pushing or writing caches.

        ``resolve_inventory=False`` keeps the byte inventories as shard
        descriptors instead of parsing all of them. A caller that only needs one
        subject area (for example the personalization objects) can then load a
        single shard with ``inventory_shard`` instead of reading every shard.
        """
        require(re.fullmatch(r"[0-9a-f]{40}", commit), "INVALID_SOURCE_COMMIT")
        rows = [load(path) for path in (self.state / subject / "receipts").glob("*.json")]
        rows = [row for row in rows if row.get("status") == "PUBLISHED" and row.get("subject") == subject
                and row.get("source_commit") == commit and row.get("branch") == "main"]
        require(rows, "NO_CANONICAL_PUBLICATION_RECEIPT")
        receipt = rows[0]
        with tempfile.TemporaryDirectory(prefix="study-bridge-index-inspection-") as temporary:
            raw = self.materialize(subject, commit, "PUBLISHED_LIBRARY.json", Path(temporary) / "root.json", receipt["manifest_sha256"])
            library = load(raw)
            require(library.get("subject") == subject and library.get("local_version") == receipt.get("local_version")
                    and library.get("repository") == receipt.get("repository"), "RECEIPT_VERSION_MISMATCH")
            if not resolve_inventory:
                return receipt, library
            for key, destination in (("file_inventory", "files"), ("original_mapping_inventory", "original_mappings")):
                if library.get(key):
                    resolved = []
                    for index, shard in enumerate(library[key]):
                        path = self.materialize(subject, commit, shard["path"], Path(temporary) / (key + str(index)), shard["sha256"])
                        records = json_lines(path)
                        require(len(records) == shard["count"], "INVENTORY_COUNT_MISMATCH", shard["path"])
                        resolved.extend(records)
                    library[destination] = resolved
        return receipt, library

    def inventory_shard(self, subject: str, commit: str, shard: dict[str, Any]) -> list[dict[str, Any]]:
        """Load exactly one published byte-inventory shard, verified by its own hash."""
        require(re.fullmatch(r"[0-9a-f]{40}", commit), "INVALID_SOURCE_COMMIT")
        path = Path(shard.get("path") or "")
        require(path.name not in {"", ".", ".."} and not path.is_absolute()
                and ".." not in path.parts, "INVALID_INVENTORY_SHARD")
        with tempfile.TemporaryDirectory(prefix="study-bridge-shard-") as temporary:
            target = self.materialize(subject, commit, shard["path"], Path(temporary) / "shard.json", shard["sha256"])
            records = json_lines(target)
        require(len(records) == shard["count"], "INVENTORY_COUNT_MISMATCH", shard["path"])
        return records

    def contains(self, subject: str, earlier: str, later: str) -> bool:
        return subprocess.run(["git", "-C", str(self.state / subject / "checkout"), "merge-base", "--is-ancestor", earlier, later],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0

    def current_sources(self, subject: str, manifest_files: dict[str, Any], paths: list[str]) -> dict[str, dict[str, str]]:
        """Use the publisher's existing cache only as an exact path locator.

        Content freshness still comes from the actual affected files below. Old
        stage aliases may remain, so never choose between ambiguous candidates.
        """
        cache_path = self.state / subject / "hash-cache.json"
        cache = load(cache_path) if cache_path.is_file() else {}
        result = {}
        for path in set(paths):
            safe_path(path)
            require(path in manifest_files, "SOURCE_REFERENCE_NOT_IN_VERSION", path)
            candidates = {source for source, row in cache.items() if path in row.get("stage_stamps", {})}
            require(len(candidates) <= 1, "PUBLISHED_SOURCE_MAPPING_AMBIGUOUS", path)
            if candidates:
                source = Path(next(iter(candidates)))
            else:
                original = manifest_files[path].get("original_reference")
                source = Path(original) if isinstance(original, str) and Path(original).is_absolute() else REPOS[subject] / path
            require(source.is_absolute(), "CURRENT_SOURCE_PATH_MUST_BE_ABSOLUTE")
            result[path] = {"publish_path": path, "source_path": str(source)}
        return result

    def materialize(self, subject: str, commit: str, path: str, destination: Path,
                    expected_sha256: str | None = None) -> Path:
        safe_path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as out:
            result = subprocess.run(["git", "-C", str(self.state / subject / "checkout"), "show", f"{commit}:{path}"],
                                    stdout=out, stderr=subprocess.PIPE, timeout=60)
        require(result.returncode == 0, "PUBLISHED_SOURCE_UNAVAILABLE", path)
        with destination.open("rb") as inp:
            pointer = inp.read(250)
        if pointer.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            match = re.search(rb"oid sha256:([0-9a-f]{64})", pointer)
            require(match and expected_sha256 == match.group(1).decode(), "LFS_POINTER_HASH_MISMATCH", path)
            oid = match.group(1).decode()
            original = publication.local_lfs_object(self.state / subject, oid)
            copy_file(original, destination, immutable=False)
        if expected_sha256:
            require(file_hash(destination) == expected_sha256, "PUBLISHED_SOURCE_HASH_MISMATCH", path)
        return destination


def published_metadata(library: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["path"]: row for key in ("file_inventory", "original_mapping_inventory")
            for row in library.get(key, [])}


def scan_source_paths(subject: str, library: dict[str, Any]) -> list[str]:
    """Native index scope, independent of a model's claimed scan count.

    File identities are explicitly a separate domain from record identities.
    Legacy records without IDs still require a complete read of their file.
    """
    files = {row["path"] for row in library["files"]}
    entries, history = library.get("entrypoints", {}), library.get("history_index")
    paths = set()
    if subject == "math":
        require(isinstance(entries, dict) and isinstance(history, str), "NATIVE_SCAN_SCOPE_UNAVAILABLE")
        paths.add(history)
        paths.update(entries[key] for key in ("review_events", "capture_events") if entries.get(key))
    elif subject == "408":
        require(isinstance(entries, dict) and isinstance(history, list), "NATIVE_SCAN_SCOPE_UNAVAILABLE")
        # The package history index is embedded in the root manifest. The full
        # canonical exam corpus is an original-source oracle, not summary scope.
        paths.add("PUBLISHED_LIBRARY.json")
        for key in ("formal_nodes", "learning_events", "historical_questions"):
            require(entries.get(key), "NATIVE_SCAN_SCOPE_UNAVAILABLE", key)
            paths.add(entries[key])
        # The variant bank index is the only extra summary surface. Individual
        # variant surfaces and plan requests stay out of the scan scope so a
        # query keeps reading a bounded set; callers fetch them by path.
        if entries.get("variant_bank"):
            paths.add(entries["variant_bank"])
    else:
        require(isinstance(history, list) and history and all(isinstance(p, str) for p in history),
                "NATIVE_SCAN_SCOPE_UNAVAILABLE")
        for prefix in history:
            selected = {path for path in files if path == prefix or path.startswith(prefix.rstrip("/") + "/")}
            require(selected, "NATIVE_SCAN_SCOPE_UNAVAILABLE", prefix)
            # Capture directories also contain ZIP returns and original images.
            # Those remain addressable source files, not UTF-8 history records.
            paths.update(path for path in selected
                         if Path(path).suffix.lower() in {".json", ".jsonl", ".md", ".csv", ".tsv", ".txt", ".yaml", ".yml"})
        # These are formal learning records even though older root manifests
        # only name articles/review/events under history_index.
        for path in ("bank/master_bank.csv", "bank/sentence_patterns.md"):
            require(path in files, "FORMAL_HISTORY_SCOPE_GAP", path)
            paths.add(path)
    require(paths - {"PUBLISHED_LIBRARY.json"} <= files, "NATIVE_SCAN_SOURCE_NOT_PUBLISHED")
    return sorted(paths)


def scan_id_domains(subject: str, library: dict[str, Any], texts: dict[str, str],
                    jsonl_records: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, list[str]]:
    jsonl_records = jsonl_records or {}
    domains = {"source_files": sorted(set(texts) | set(jsonl_records))}
    entries, history = library.get("entrypoints", {}), library.get("history_index")

    def add(name: str, values: list[Any]):
        require(all(isinstance(value, str) and value.strip() for value in values), "NATIVE_SCAN_RECORD_ID_MISSING", name)
        require(len(values) == len(set(values)), "NATIVE_SCAN_DUPLICATE_RECORD_ID", name)
        domains[name] = sorted(set(values))

    def records(path: str):
        if path in jsonl_records:
            return jsonl_records[path]
        return [json.loads(line) for line in texts[path].split("\n") if line.strip()]

    if subject == "math":
        cards = json.loads(texts[history]).get("cards")
        require(isinstance(cards, list), "NATIVE_SCAN_SCOPE_UNAVAILABLE")
        add("formal_card_ids", [row.get("id") for row in cards])
        if entries.get("capture_events"):
            add("capture_event_ids", [row.get("event_id") for row in records(entries["capture_events"])])
        # The old review ledger has unit_id, but no independent event_id.
        # Its source file must reach EOF; unit_id is never relabeled an event.
    elif subject == "408":
        rows = [line.split("|")[1:-1] for line in texts[entries["formal_nodes"]].splitlines() if line.startswith("|")]
        require(rows and rows[0][0].strip() == "ID", "NATIVE_SCAN_SCOPE_UNAVAILABLE")
        add("formal_node_ids", [row[0].strip() for row in rows[2:] if row and row[0].strip()])
        add("review_event_ids", [row.get("event_id") for row in records(entries["learning_events"])])
        add("history_package_ids", [row.get("package_id") for row in history])
        groups = {"complete": [], "incomplete": [], "pending": [], "unknown": []}
        for row in history:
            state = ("pending" if row.get("formal_status") == "pending_local_judgment" else
                     "complete" if row.get("conversation_complete") is True else
                     "incomplete" if row.get("conversation_complete") is False else "unknown")
            groups[state].append(row.get("package_id"))
        for state, identities in groups.items():
            add("history_" + state + "_package_ids", identities)
        if entries.get("variant_bank"):
            bank = json.loads(texts[entries["variant_bank"]])
            add("variant_surface_ids", [row.get("variant_id") for row in bank.get("entries", [])])
    else:
        event_paths = [path for path in texts if path.startswith("intake/events/") and path.endswith(".json")]
        add("intake_event_ids", [json.loads(texts[path]).get("event_id") for path in event_paths])
        add("article_file_paths", [path for path in texts if path.startswith("articles/")])
        add("formal_bank_ids", [row.get("id") for row in csv.DictReader(io.StringIO(texts["bank/master_bank.csv"].lstrip("\ufeff")))])
        add("sentence_pattern_ids", re.findall(r"(?m)^##\s+(SP-\d+)\b", texts["bank/sentence_patterns.md"]))
    return domains


def ids_sha256(ids: list[str]) -> str:
    """Canonical set hash: UTF-8 sorted unique IDs, one ID plus LF per line."""
    return sha("".join(identity + "\n" for identity in sorted(set(ids))).encode())


def observed_text_coverage(path: str, data: bytes, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconcile actual tool text against source bytes without requiring one huge return.

    Byte offsets are zero-based and end-exclusive. A whole-file operation remains
    supported; segmented operations must bind their exact offsets and chunk hash.
    EOF belongs to an observed terminal operation, never to a model's summary.
    """
    spans, eof, observed_quotes = [], False, []
    for operation in operations:
        if operation.get("path") != path or operation.get("kind") == "image" or operation.get("truncated") is not False:
            continue
        quote = operation.get("content_quote")
        if not isinstance(quote, str):
            continue
        raw = quote.encode("utf-8")
        observation = Path(operation["observation_file"])
        retained = operation.get("_bridge_projection_raw", quote)
        if file_hash(observation) != operation.get("observation_sha256") or retained not in observation.read_text():
            continue
        if raw in data:
            observed_quotes.append(quote)
        start, end = operation.get("start_byte"), operation.get("end_byte")
        if start is None and end is None and raw == data:
            start, end = 0, len(data)
        elif not (type(start) is int and type(end) is int and 0 <= start <= end <= len(data)
                  and operation.get("content_sha256") == sha(raw)):
            continue
        if data[start:end] != raw:
            continue
        spans.append((start, end))
        marker = operation.get("eof_marker")
        if (end == len(data) and operation.get("eof") is True and isinstance(marker, str) and marker.strip()
                and marker in observation.read_text()):
            eof = True
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    complete = merged == [[0, len(data)]] and eof
    assembled = b"".join(data[start:end] for start, end in merged)
    return {"complete": complete, "ranges": merged, "eof": eof,
            "sha256": sha(assembled) if complete else None, "observed_quotes": observed_quotes}


def require_observed_reference(path: str, data: bytes, quote: str | None,
                               operations: list[dict[str, Any]], complete: bool = False) -> None:
    coverage = observed_text_coverage(path, data, operations)
    if complete:
        require(coverage["complete"] and coverage["sha256"] == sha(data),
                "REFERENCED_CONVERSATION_NOT_COMPLETELY_OBSERVED", path)
    if quote:
        raw = quote.encode("utf-8")
        require(any(quote in part for part in coverage["observed_quotes"])
                or any(raw in data[start:end] for start, end in coverage["ranges"]),
                "SOURCE_QUOTE_NOT_OBSERVED_IN_AUTHORIZED_TASK", path)


def validate_scan(subject: str, commit: str, library: dict[str, Any], texts: dict[str, str],
                  scan: Any, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure mechanical check. PASS does not attest that CU was actually used."""
    issues, expected, source_results = [], {}, []
    try:
        expected = scan_id_domains(subject, library, texts)
    except (BridgeError, ValueError, KeyError, TypeError) as exc:
        issues.append({"code": "NATIVE_SCAN_ID_DOMAIN_UNAVAILABLE", "detail": str(exc)})
    scan = scan if isinstance(scan, dict) else {}
    if scan.get("source_commit") != commit:
        issues.append({"code": "SCAN_COMMIT_MISSING_OR_MISMATCH"})
    declared = scan.get("domains", [])
    declared = declared if isinstance(declared, list) else []
    by_name = {row.get("name"): row for row in declared if isinstance(row, dict)}
    if len(by_name) != len(declared) or set(by_name) != set(expected):
        issues.append({"code": "SCAN_ID_DOMAIN_SET_MISMATCH"})
    for name, ids in expected.items():
        row = by_name.get(name, {})
        scanned = row.get("scanned_ids", [])
        valid = isinstance(scanned, list) and all(isinstance(value, str) for value in scanned)
        if not (valid and len(scanned) == len(set(scanned)) and set(scanned) == set(ids)
                and row.get("expected_count") == len(ids) and row.get("scanned_count") == len(ids)
                and row.get("expected_ids_sha256") == ids_sha256(ids)
                and row.get("scanned_ids_sha256") == ids_sha256(scanned)):
            issues.append({"code": "SCAN_UNIQUE_ID_SET_INCOMPLETE", "domain": name})
    declared_sources = scan.get("sources", [])
    declared_sources = declared_sources if isinstance(declared_sources, list) else []
    by_path = {row.get("path"): row for row in declared_sources if isinstance(row, dict)}
    if len(by_path) != len(declared_sources) or set(by_path) != set(texts):
        issues.append({"code": "SCAN_SOURCE_SET_MISMATCH"})
    for path, text in texts.items():
        row, cursor = by_path.get(path, {}), 1
        lines = text.splitlines(keepends=True)
        if "byte_ranges" in row:
            try:
                ranges = row["byte_ranges"]
                require(isinstance(ranges, list) and ranges and row.get("sha256") == sha(text.encode()),
                        "SCAN_INVALID_BYTE_RANGES")
                selected, indexes = [], set()
                for chunk in ranges:
                    index = chunk["operation_index"]
                    require(type(index) is int and 0 <= index < len(operations) and index not in indexes,
                            "SCAN_INVALID_BYTE_OPERATION")
                    indexes.add(index)
                    operation = operations[index]
                    require(operation.get("path") == path and operation.get("source_commit") == commit
                            and all(chunk.get(key) == operation.get(key) for key in
                                    ("start_byte", "end_byte", "content_sha256")), "SCAN_BYTE_RANGE_BINDING_MISMATCH")
                    selected.append(operation)
                coverage = observed_text_coverage(path, text.encode(), selected)
                require(coverage["complete"] and coverage["sha256"] == sha(text.encode()),
                        "SCAN_SOURCE_NOT_READ_THROUGH_EOF")
                complete = True
            except (BridgeError, KeyError, TypeError, ValueError, OSError, IndexError) as exc:
                complete = False
                issues.append({"code": "SCAN_SOURCE_NOT_READ_THROUGH_EOF", "path": path, "detail": str(exc)})
            source_results.append({"path": path, "sha256": sha(text.encode()), "source_line_count": len(lines),
                                   "complete": complete, "coordinate": "source_utf8_bytes"})
            continue
        ranges = row.get("ranges", [])
        complete, eof = bool(ranges), False
        complete &= row.get("sha256") == sha(text.encode())
        for chunk in ranges:
            try:
                index, start, end = chunk["operation_index"], chunk["start_line"], chunk["end_line"]
                require(type(index) is int and 0 <= index < len(operations) and type(start) is int and type(end) is int,
                        "SCAN_INVALID_RANGE")
                operation = operations[index]
                empty = not lines and start == end == 0 and len(ranges) == 1
                require(empty or start == cursor and start <= end <= len(lines), "SCAN_RANGE_GAP_OVERLAP_OR_OVERFLOW")
                observed = "" if empty else "".join(lines[start - 1:end])
                require(operation.get("path") == path and operation.get("source_commit") == commit
                        and operation.get("content_quote") == observed and chunk.get("content_sha256") == sha(observed.encode()),
                        "SCAN_RANGE_NOT_EXACT_OBSERVED_SOURCE_TEXT")
                observation = Path(operation["observation_file"]).read_text()
                require(file_hash(Path(operation["observation_file"])) == operation.get("observation_sha256")
                        and operation.get("_bridge_projection_raw", observed) in observation, "SCAN_OBSERVATION_CHANGED")
                cursor = end + 1
                if chunk.get("eof") is True:
                    marker = chunk.get("eof_marker")
                    require((empty or end == len(lines)) and isinstance(marker, str) and marker.strip()
                            and marker in observation and operation.get("truncated") is False,
                            "SCAN_EOF_NOT_OBSERVED")
                    eof = True
            except (BridgeError, KeyError, TypeError, ValueError, OSError, IndexError) as exc:
                complete = False
                issues.append({"code": "SCAN_RANGE_INCOMPLETE", "path": path, "detail": str(exc)})
        complete &= (cursor == len(lines) + 1 or not lines and cursor == 1) and eof
        if not complete:
            issues.append({"code": "SCAN_SOURCE_NOT_READ_THROUGH_EOF", "path": path})
        source_results.append({"path": path, "sha256": sha(text.encode()), "source_line_count": len(lines),
                               "complete": bool(complete)})
    return {"mechanical_status": "PASS" if not issues else "PARTIAL", "issues": issues,
            "expected_domains": [{"name": name, "expected_count": len(ids), "expected_ids": ids,
                                  "expected_ids_sha256": ids_sha256(ids)} for name, ids in expected.items()],
            "sources": source_results, "actual_cu_observation_confirmed": False}


def lesson_binding(item: dict[str, Any]) -> dict[str, Any]:
    manifest = item["lesson"]["manifest"]
    return {"lesson_id": item["manifest"]["lesson_id"], "snapshot_id": manifest["snapshot_id"],
            "zip_sha256": item["zip_sha256"], "review_ids": manifest["review_ids"],
            "event_ids": manifest["actual_event_ids"], "coverage_ids": manifest["actual_coverage_ids"]}


def lesson_members(row: dict[str, Any]) -> set[str]:
    """The complete result stream plus this item's actual originals."""
    paths = {"lesson_results.jsonl", "lesson_manifest.json"}
    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("_ref") and key != "source_ref" and isinstance(child, str) and not "://" in child:
                    paths.add(safe_path(child))
                elif key == "path" and isinstance(child, str):
                    paths.add(safe_path(child))
                elif isinstance(child, (dict, list)):
                    visit(child)
    visit(row.get("raw_turns", []))
    visit(row.get("question") or {})
    return paths


def validate_review_updates(advice: dict[str, Any], run: dict[str, Any]) -> list[dict[str, Any]]:
    expected = {row["review_id"]: row for row in run.get("input_review_updates", [])}
    reviews = advice.get("review_updates", [])
    require(isinstance(reviews, list) and all(isinstance(row, dict) for row in reviews), "A_REVIEW_UPDATES_REQUIRED")
    ids = [row.get("review_id") for row in reviews]
    require(all(isinstance(value, str) and value for value in ids) and
            len(ids) == len(set(ids)) and set(ids) == set(expected), "A_REVIEW_UPDATE_COVERAGE_MISMATCH")
    for row in reviews:
        source = expected[row["review_id"]]
        for field in ("event_ids", "coverage_ids"):
            values = row.get(field)
            require(isinstance(values, list) and all(isinstance(value, str) and value for value in values) and
                    len(values) == len(set(values)) and set(values) == set(source[field]), "A_REVIEW_EVENT_COVERAGE_MISMATCH", row["review_id"])
        require(row.get("lesson_id") == source["lesson_id"] and row.get("package_id") == source.get("package_id"),
                "A_REVIEW_IDENTITY_MISMATCH")
        require(row.get("first_result") == source["first_result"] and row.get("final_result") == source["final_result"],
                "A_REVIEW_RESULT_ECHO_MISMATCH", row["review_id"])
        require((isinstance(row.get("recommendation"), str) and row["recommendation"].strip() or
                 isinstance(row.get("recommendation"), dict) and row["recommendation"]) and
                isinstance(row.get("uncertainties"), list), "A_ADVICE_FIELDS_MISSING")
        refs = collect_references(row)
        result_path = "lessons/" + source["lesson_id"] + "/lesson_results.jsonl"
        require(any(ref["path"] == result_path and isinstance(ref.get("quote"), str) and
                    source["review_id"] in ref["quote"] for ref in refs), "A_REVIEW_CURRENT_RESULT_REFERENCE_REQUIRED")
    return reviews


def validate_math_profile_updates(value: Any) -> list[dict[str, Any]]:
    """Validate the web-authored semantic profile payload before local adjudication."""
    require(isinstance(value, dict) and value.get("schema") == "math-learning-profile-update-v1",
            "A_MATH_PROFILE_UPDATE_SCHEMA_REQUIRED")
    updates = value.get("updates")
    require(isinstance(updates, list) and updates, "A_MATH_PROFILE_UPDATES_REQUIRED")
    keys = []
    for update in updates:
        require(isinstance(update, dict), "A_MATH_PROFILE_UPDATE_OBJECT_REQUIRED")
        concept_key = update.get("concept_key")
        keys.append(concept_key)
        require(isinstance(concept_key, str) and concept_key.strip()
                and isinstance(update.get("overview"), str) and update["overview"].strip()
                and isinstance(update.get("short_summary"), str) and update["short_summary"].strip()
                and isinstance(update.get("progress"), list)
                and update.get("next_check") is not None, "A_MATH_PROFILE_UPDATE_FIELDS_MISSING", concept_key)
        refs = update.get("source_refs")
        require(isinstance(refs, list) and refs
                and all(isinstance(ref, dict) and isinstance(ref.get("path"), str)
                        and isinstance(ref.get("sha256"), str) and len(ref["sha256"]) == 64 for ref in refs),
                "A_MATH_PROFILE_SOURCE_REFS_REQUIRED", concept_key)
        require("timeline" not in update or update["timeline"] is not None,
                "A_MATH_PROFILE_TIMELINE_MUST_NOT_BE_NULL", concept_key)
    require(len(keys) == len(set(keys)), "A_MATH_PROFILE_CONCEPT_DUPLICATE")
    return updates


def validate_c_handoff_root(root: Path, archive_sha256: str) -> dict[str, Any]:
    files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    require(files == C_HANDOFF_FILES, "C_HANDOFF_FILE_SET_MISMATCH", sorted(files))
    handoff, refs = load(root / "handoff.json"), load(root / "refs.json")
    publication_current = handoff.get("publication_current", {})
    profile_receipt = handoff.get("profile_receipt", {})
    formal_readback = handoff.get("formal_readback", {})
    constraints = handoff.get("constraints", {})
    after_a = handoff.get("source_project") == "A"
    require(handoff.get("schema") == "math-c-handoff-v1" and handoff.get("project") == "C"
            and handoff.get("subject") == "math" and handoff.get("source_project") in {"A", "B"},
            "C_HANDOFF_IDENTITY_MISMATCH")
    require(isinstance(handoff.get("source_session_id"), str) and handoff["source_session_id"]
            and isinstance(handoff.get("source_evidence_id"), str) and handoff["source_evidence_id"]
            and isinstance(handoff.get("formal_event_id"), str) and handoff["formal_event_id"],
            "C_HANDOFF_SOURCE_IDENTITIES_REQUIRED")
    require(formal_readback.get("event_id") == handoff["formal_event_id"]
            and profile_receipt.get("status") in {"accepted", "noop"}
            and publication_current.get("status") == "PUBLISHED_CURRENT"
            and isinstance(publication_current.get("source_commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", publication_current["source_commit"]),
            "C_HANDOFF_NOT_FORMALIZED_AND_CURRENT")
    if after_a:
        event_ids = handoff.get("formal_event_ids")
        run_ids = handoff.get("source_run_ids")
        require(isinstance(handoff.get("source_batch_id"), str) and handoff["source_batch_id"]
                and handoff.get("source_kind") == "A_formal_batch"
                and handoff.get("b_review", {}).get("status") == "not_performed"
                and isinstance(handoff.get("b_review", {}).get("reason"), str)
                and handoff["b_review"]["reason"].strip()
                and isinstance(event_ids, list) and event_ids
                and all(isinstance(value, str) and value.startswith("MFI-CLOSE-") for value in event_ids)
                and len(event_ids) == len(set(event_ids))
                and handoff["formal_event_id"] == event_ids[-1]
                and formal_readback.get("event_ids") == event_ids
                and profile_receipt.get("event_ids") == event_ids
                and formal_readback.get("status") == "verified"
                and isinstance(run_ids, list) and run_ids and len(run_ids) == len(set(run_ids))
                and all(isinstance(value, str) and value for value in run_ids),
                "C_AFTER_A_FORMAL_BATCH_BINDING_REQUIRED")
    require(constraints.get("questions") == "existing_formal_cards_only"
            and constraints.get("answer_protection") is True
            and constraints.get("generated_B_questions_are_not_formal_cards") is True
            and constraints.get("do_not_score_source_or_generated_B_questions") is True,
            "C_HANDOFF_CONSTRAINTS_MISMATCH")
    sources = refs.get("sources")
    require(refs.get("schema") == "math-c-handoff-refs-v1"
            and refs.get("latest_snapshot") == publication_current
            and isinstance(sources, list) and sources
            and all(isinstance(row, dict) and isinstance(row.get("path"), str)
                    and isinstance(row.get("sha256"), str) and len(row["sha256"]) == 64 for row in sources),
            "C_HANDOFF_REFS_MISMATCH")
    require(isinstance(handoff.get("evidence_manifest"), dict)
            and handoff["evidence_manifest"].get("path")
            and isinstance(handoff["evidence_manifest"].get("sha256"), str)
            and len(handoff["evidence_manifest"]["sha256"]) == 64,
            "C_HANDOFF_EVIDENCE_MANIFEST_REQUIRED")
    binding = {"schema": handoff["schema"], "zip_sha256": archive_sha256,
            "source_session_id": handoff["source_session_id"],
            "source_evidence_id": handoff["source_evidence_id"],
            "formal_event_id": handoff["formal_event_id"],
            "source_commit": publication_current["source_commit"],
            "snapshot_id": publication_current.get("snapshot_id", publication_current["source_commit"]),
            "publication_current": publication_current,
            "evidence_manifest": handoff["evidence_manifest"],
            "member_sha256": {name: file_hash(root / name) for name in sorted(C_HANDOFF_FILES)}}
    if after_a:
        binding.update(source_project="A", source_kind="A_formal_batch",
                       source_batch_id=handoff["source_batch_id"], source_run_ids=run_ids,
                       formal_event_ids=event_ids, b_review=handoff["b_review"])
    return binding


def inspect_c_handoff_archive(path: Path) -> dict[str, Any]:
    source_file(path)
    require(path.suffix.lower() == ".zip", "C_HANDOFF_ZIP_REQUIRED")
    with tempfile.TemporaryDirectory(prefix="study-bridge-c-handoff-") as temporary:
        root = extract_zip(path, Path(temporary), ArchiveBudget(Path(temporary), max_bytes=64 * 1024 ** 2))
        return validate_c_handoff_root(root, file_hash(path))


class Bridge:
    def __init__(self, base: Path = BASE, published: Any = None, owners: Any = None):
        self.base = base.resolve()
        self.published = published or Published(self.base / "publication")
        self.owners = owners or Owners()

    def verify_lesson_snapshot(self, rows, subject, commit, library):
        events = {event["event_id"]: event for row in rows for event in row.get("events", [])}
        if not events:
            return
        entry = library.get("entrypoints", {}).get("learning_events")
        paths = entry if isinstance(entry, list) else [entry] if isinstance(entry, str) else []
        require(paths, "LESSON_SNAPSHOT_EVENTS_UNAVAILABLE")
        known = {row["path"]: row for row in library["files"]}
        actual = {}
        with tempfile.TemporaryDirectory(prefix="lesson-snapshot-") as temporary:
            for number, path in enumerate(paths):
                require(path in known, "LESSON_SNAPSHOT_EVENTS_UNAVAILABLE")
                source = self.published.materialize(subject, commit, path, Path(temporary).resolve() / str(number), known[path]["sha256"])
                for event in json_lines(source):
                    if event.get("event_id") in events:
                        require(event["event_id"] not in actual, "LESSON_SNAPSHOT_DUPLICATE_EVENT")
                        actual[event["event_id"]] = event
        require(actual == events, "LESSON_SOURCE_NEWER_THAN_SNAPSHOT", {"missing_event_ids": sorted(set(events) - set(actual)),
                "action": "Publish current subject sources through the existing publisher, then export again."})

    def export_lesson(self, repo, private_root, *, output, run_id=None, study_date=None,
                      cutoff=None, quick_packs=(), supplements=()):
        from lesson_export_408 import export_lesson
        output = Path(output).expanduser().absolute()
        require(not output.resolve().is_relative_to(Path(repo).resolve()) and
                not output.resolve().is_relative_to(Path(private_root).resolve()), "OUTPUT_MUST_BE_EXTERNAL")
        require(".." not in output.parts and not any(path.is_symlink() for path in [output, *output.parents]),
                "UNSAFE_OUTPUT_PATH")
        latest = self.published.latest("408")
        receipt, library = self.published.verify("408", latest["source_commit"])
        require(receipt.get("publication_target") == "local" and receipt.get("snapshot_id"),
                "LESSON_REQUIRES_VERIFIED_LOCAL_SNAPSHOT")
        # Stage before publishing the user-facing ZIP: stale events never escape
        # as an apparently accepted export.
        with tempfile.TemporaryDirectory(prefix="lesson-export-bridge-") as temporary:
            archive = Path(temporary).resolve() / "lesson.zip"
            result = export_lesson(repo, private_root, output=archive, snapshot_id=receipt["snapshot_id"],
                    run_id=run_id, date=study_date, cutoff=cutoff, quick_pack_paths=quick_packs, supplement_paths=supplements)
            with collect_inputs([archive], "408") as items:
                lesson = next(item for item in items if item["manifest"]["kind"] == "lesson_review_bundle")
                self.verify_lesson_snapshot(lesson["lesson"]["results"], "408", latest["source_commit"], library)
            copy_file(archive, output)
        return dict(result, path=str(output.resolve()), zip_sha256=file_hash(output), snapshot_id=receipt["snapshot_id"])

    def run_path(self, study_date: str, run_id: str) -> Path:
        return self.base / "outbox" / date(study_date) / "runs" / (valid_id(run_id) + ".json")

    def save_run(self, run: dict[str, Any]) -> None:
        write(self.run_path(run["date"], run["run_id"]), json_bytes(run))

    @contextlib.contextmanager
    def input_materials(self, run: dict[str, Any]):
        with self._package_input_materials(run) as package_materials:
            handoff = run.get("input_handoff")
            if not handoff:
                yield package_materials
                return
            archive = Path(handoff["archive_path"])
            with tempfile.TemporaryDirectory(prefix="study-bridge-c-handoff-read-") as temporary:
                root = extract_zip(archive, Path(temporary), ArchiveBudget(Path(temporary), max_bytes=64 * 1024 ** 2))
                current = validate_c_handoff_root(root, file_hash(archive))
                require(all(current.get(key) == handoff.get(key) for key in current),
                        "C_HANDOFF_BINDING_CHANGED")
                prefix = "c-handoff/" + handoff["source_evidence_id"] + "/"
                materials = dict(package_materials)
                for name in sorted(C_HANDOFF_FILES):
                    materials[prefix + name] = {"sha256": current["member_sha256"][name],
                                                "local_source": root / name,
                                                "handoff_id": handoff["source_evidence_id"],
                                                "member_path": name}
                yield materials

    @contextlib.contextmanager
    def _package_input_materials(self, run: dict[str, Any]):
        if not run["input_packages"] and not run.get("input_lessons"):
            yield {}
            return
        days = {q["package_id"]: q["study_date"] for q in run["input_questions"]}
        paths = [Path(path) for path in run["input_archives"]] if run.get("input_archives") else [self.base / "outbox" / days[p["package_id"]] / "quick-packs" / run["subject"] /
                 f"{days[p['package_id']]}_{run['subject']}_{p['package_id']}.zip" for p in run["input_packages"]]
        expected = {p["package_id"]: p for p in run["input_packages"]}
        lessons = {p["lesson_id"]: p for p in run.get("input_lessons", [])}
        with collect_inputs(paths, run["subject"]) as supplied:
            result = {}
            packages = [p for p in supplied if p["manifest"]["kind"] == "quick_pack"]
            lesson_inputs = [p for p in supplied if p["manifest"]["kind"] == "lesson_review_bundle"]
            require({p["manifest"]["package_id"] for p in packages} == set(expected), "CURRENT_INPUT_PACKAGE_SET_MISMATCH")
            require({p["manifest"]["lesson_id"] for p in lesson_inputs} == set(lessons), "CURRENT_INPUT_LESSON_SET_MISMATCH")
            for package in supplied:
                manifest = package["manifest"]
                is_lesson = manifest["kind"] == "lesson_review_bundle"
                identity = manifest["lesson_id" if is_lesson else "package_id"]
                binding = lessons[identity] if is_lesson else expected[identity]
                if is_lesson:
                    require(lesson_binding(package) == binding, "CURRENT_INPUT_LESSON_BINDING_CHANGED")
                    actual_rows = [dict(row, lesson_id=identity) for row in package["lesson"]["results"]]
                    require(actual_rows == [row for row in run.get("input_review_updates", []) if row["lesson_id"] == identity],
                            "CURRENT_INPUT_REVIEW_BINDING_CHANGED")
                require(package["zip_sha256"] == binding["zip_sha256"] and
                        (is_lesson or manifest["version"] == binding["version"]), "CURRENT_INPUT_VERSION_CHANGED")
                for row in manifest["files"]:
                    key = ("lessons/" if is_lesson else "quick-packs/") + identity + "/" + row["path"]
                    result[key] = {"sha256": row["sha256"], "local_source": package["root"] / row["path"],
                                   "lesson_id" if is_lesson else "package_id": identity, "member_path": row["path"]}
            yield result

    def prepare(self, project: str, subject: str, study_date: str, run_id: str,
                target_date: str | None = None, packages: list[Path] | None = None,
                after_a: Path | None = None, skip_a_reason: str | None = None,
                c_handoff: Path | None = None) -> dict[str, Any]:
        require(subject in PROJECT_SUBJECTS.get(project, ()), "INVALID_PROJECT_SUBJECT")
        path = self.run_path(study_date, run_id)
        if path.exists():
            prior = load(path)
            require((prior["project"], prior["subject"], prior.get("target_date")) ==
                    (project, subject, target_date), "RUN_ID_CONFLICT")
            if packages:
                with collect_inputs(packages) as supplied:
                    bindings = [{"package_id": x["manifest"]["package_id"], "version": x["manifest"]["version"],
                                 "zip_sha256": x["zip_sha256"]} for x in supplied if x["manifest"]["subject"] == subject and x["manifest"]["kind"] == "quick_pack"]
                    require(bindings == prior["input_packages"], "RUN_INPUT_VERSION_CONFLICT")
                    lessons = [lesson_binding(x) for x in supplied if x["manifest"]["subject"] == subject and x["manifest"]["kind"] == "lesson_review_bundle"]
                    require(lessons == prior.get("input_lessons", []), "RUN_INPUT_LESSON_VERSION_CONFLICT")
            if c_handoff:
                binding = inspect_c_handoff_archive(c_handoff)
                existing_handoff = prior.get("input_handoff")
                if existing_handoff:
                    require(binding["zip_sha256"] == existing_handoff.get("zip_sha256"),
                            "RUN_C_HANDOFF_VERSION_CONFLICT")
                else:
                    prior_dependency = prior.get("dependency", {})
                    compatible_dependency = (
                        prior_dependency.get("project") == "A"
                        and prior_dependency.get("stage") == "LOCAL_DECIDED_PUBLISHED"
                    ) or (
                        prior_dependency.get("status") == "no_new_packages_declared"
                        and prior_dependency.get("no_new_packages_declared") is True
                        and isinstance(prior_dependency.get("reason"), str)
                        and bool(prior_dependency["reason"].strip())
                    )
                    safe_to_attach = (project == "C" and subject == "math" and prior.get("stage") == "NOT_SENT"
                                      and not prior.get("task_url") and not prior.get("authorization_ref")
                                      and not prior.get("download_sha256") and not prior.get("output_dir")
                                      and compatible_dependency)
                    require(safe_to_attach, "RUN_C_HANDOFF_MISSING_CREATE_NEW_RUN",
                            "Existing run was already externally dispatched; create a new run ID.")
                    latest = self.published.latest(subject)
                    require(prior.get("source_commit") == latest.get("source_commit")
                            and self.published.contains(subject,binding['source_commit'],latest['source_commit']),
                            "RUN_C_HANDOFF_PUBLICATION_CHANGED_CREATE_NEW_RUN")
                    destination = self.base / "outbox" / study_date / "handoffs" / (
                        binding["source_evidence_id"] + ".zip")
                    copy_file(c_handoff, destination)
                    binding["archive_path"] = str(destination.resolve())
                    prior["input_handoff"] = binding
                    self.save_run(prior)
            return prior  # Even unknown sends retain the same run; never reset it.
        if project in {"C", "D"}:
            require(target_date and date(target_date) == (dt.date.fromisoformat(study_date) + dt.timedelta(days=1)).isoformat(),
                    "TARGET_MUST_BE_NEXT_DAY")
            require(not packages, "C_D_DO_NOT_ACCEPT_QUICK_PACKAGES")
        require(not c_handoff or project == "C" and subject == "math", "C_HANDOFF_REQUIRES_MATH_C")
        require(project != "C" or subject != "math" or c_handoff,
                "C_MATH_REQUIRES_GPT6_B_REVIEW_HANDOFF")
        latest = self.published.latest(subject)
        receipt, library = self.published.verify(subject, latest["source_commit"])
        require(library.get("subject") == subject and library.get("repository") == f"xialovezhu-wq/kaoyan-{subject}"
                and library.get("branch") == "main", "PUBLISHED_LIBRARY_IDENTITY_MISMATCH")
        handoff_binding = None
        if c_handoff:
            handoff_binding = inspect_c_handoff_archive(c_handoff)
            self.published.verify(subject, handoff_binding['source_commit'])
            require(self.published.contains(subject,handoff_binding['source_commit'],latest['source_commit']),
                    "C_HANDOFF_PUBLICATION_NOT_IN_CURRENT_HISTORY")
            destination = self.base / "outbox" / study_date / "handoffs" / (
                handoff_binding["source_evidence_id"] + ".zip")
            copy_file(c_handoff, destination)
            handoff_binding["archive_path"] = str(destination.resolve())
        dependency = None
        if project == "C" and handoff_binding and handoff_binding.get("source_project") == "A":
            require(not after_a and not skip_a_reason, "C_AFTER_A_HANDOFF_ALREADY_BINDS_A_DEPENDENCY")
            dependency = {"project": "A", "subject": "math", "stage": "LOCAL_DECIDED_PUBLISHED",
                          "run_id": handoff_binding["source_batch_id"],
                          "run_ids": handoff_binding["source_run_ids"],
                          "source_kind": "A_formal_batch",
                          "formal_event_ids": handoff_binding["formal_event_ids"],
                          "publication_after_decision": handoff_binding["publication_current"]}
        elif project != "A":
            require(bool(after_a) != bool(skip_a_reason), "A_DEPENDENCY_OR_EXPLICIT_NO_NEW_PACKAGES_REQUIRED")
            if after_a:
                dependency = load(after_a)
                require(dependency.get("project") == "A" and dependency.get("subject") == subject
                        and dependency.get("stage") == "LOCAL_DECIDED_PUBLISHED", "A_NOT_LOCALLY_DECIDED_AND_PUBLISHED")
                require(self.published.contains(subject, dependency["publication_after_decision"]["source_commit"], latest["source_commit"]),
                        "LATEST_PUBLICATION_PRECEDES_A_DECISION")
            else:
                dependency = {"status": "no_new_packages_declared", "reason": skip_a_reason,
                              "skip_a_reason": skip_a_reason, "no_new_packages_declared": True}
        require(project != "A" or packages, "A_REQUIRES_CURRENT_PACKAGES")
        input_bindings, input_questions, input_lessons, input_reviews, input_archives = [], [], [], [], []
        with collect_inputs(packages) if packages else contextlib.nullcontext([]) as all_items:
            items = [item for item in all_items if item["manifest"]["subject"] == subject]
            require(not packages or items, "NO_INPUT_FOR_SUBJECT", subject)
            for item in items:
                manifest = item["manifest"]
                if manifest["kind"] == "lesson_review_bundle":
                    require(project == "A" and subject == "408", "LESSON_INPUT_REQUIRES_408_A")
                    binding = lesson_binding(item)
                    require(binding["snapshot_id"] == receipt.get("snapshot_id", latest["source_commit"]), "LESSON_SNAPSHOT_MISMATCH")
                    input_lessons.append(binding)
                    rows = json_lines(item["root"] / "lesson_results.jsonl")
                    input_reviews.extend(dict(row, lesson_id=binding["lesson_id"]) for row in rows)
                    destination = self.base / "outbox" / study_date / "lessons" / (binding["lesson_id"] + ".zip")
                    copy_file(item["zip_path"], destination)
                    input_archives.append(str(destination))
                    continue
                input_bindings.append({"package_id": manifest["package_id"], "version": manifest["version"],
                                       "zip_sha256": item["zip_sha256"]})
                input_questions.extend(dict(question, package_id=manifest["package_id"], study_date=manifest["study_date"])
                                       for question in manifest["questions"])
                destination = self.base / "outbox" / manifest["study_date"] / "quick-packs" / subject / (
                    f"{manifest['study_date']}_{subject}_{manifest['package_id']}.zip")
                copy_file(item["zip_path"], destination)
                input_archives.append(str(destination))
        require(len({row["review_id"] for row in input_reviews}) == len(input_reviews), "DUPLICATE_INPUT_REVIEW_ID")
        if input_lessons:
            self.verify_lesson_snapshot(input_reviews, subject, latest["source_commit"], library)
        run = {"schema_version": 1, "project": project, "subject": subject, "date": date(study_date),
               "run_id": valid_id(run_id), "target_date": target_date, "source_commit": latest["source_commit"],
               "input_packages": input_bindings, "input_questions": input_questions,
               "stage": "NOT_SENT", "publication": receipt, "readiness": "READY_FOR_AUTHORIZED_SEND",
               "coverage_status": library.get("coverage_status"), "published_gaps": library.get("gaps", []),
               "publication_state": "local_snapshot_ready" if receipt.get("publication_target") == "local" else "pushed",
               "publication_target": receipt.get("publication_target", "github"),
               "web_read_status": "web_read_unverified", "dependency": dependency}
        if handoff_binding:
            run["input_handoff"] = handoff_binding
        if input_lessons:
            run.update(input_lessons=input_lessons, input_review_updates=input_reviews, input_archives=input_archives)
        if project == "D":
            due = self.owners.due(target_date)
            run["due_version"] = due.get("due_version", due.get("manifest", {}).get("due_version"))
            require(run["due_version"], "NATIVE_DUE_VERSION_MISSING")
            for field in ("concept_priority_version", "concept_requests", "review_concept_ids"):
                if field in due:
                    run[field] = due[field]
            coverage = due.get("coverage", {})
            if coverage.get("unmapped_due_formal_node_ids"):
                run.update(readiness="BLOCKED_CONCEPT_MAPPING", stage="BLOCKED",
                           blocking_formal_ids=coverage["unmapped_due_formal_node_ids"])
            elif due.get("due_concept_ids") == [] and coverage.get("due_formal_node_ids") == [] and not due.get("concept_requests"):
                run.update(readiness="SKIPPED", stage="SKIPPED_NOTHING_DUE")
        self.save_run(run)
        return run

    def record_send(self, run: dict[str, Any], task_url: str | None, authorization_ref: str,
                    uncertain: bool = False) -> dict[str, Any]:
        require((not task_url and uncertain) or (task_url and re.match(r"^https://chatgpt\.com/(?:c/|g/)[^\s]+$", task_url)),
                "ACTUAL_TASK_URL_OR_UNCERTAIN_SEND_REQUIRED")
        require(authorization_ref.strip(), "ACTUAL_TASK_AUTHORIZATION_REQUIRED")
        if run.get("task_url"):
            require(run["task_url"] == task_url, "RUN_ALREADY_BOUND_TO_TASK")
            return run
        require(run["stage"] in {"NOT_SENT", "SEND_RESULT_UNKNOWN"}, "RUN_SEND_STATE_CONFLICT")
        if run.get("authorization_ref"):
            require(run["authorization_ref"] == authorization_ref, "RUN_AUTHORIZATION_CONFLICT")
        run.update(task_url=task_url, authorization_ref=authorization_ref,
                   stage="SEND_RESULT_UNKNOWN" if uncertain else "SENT")
        self.save_run(run)
        return run

    def normalize_web_operations(self, run: dict[str, Any], library: dict[str, Any], known: dict[str, Any],
                                 operations: list[dict[str, Any]], temporary: Path) -> list[dict[str, Any]]:
        """Bind observed JSONL transport to the pinned publisher, then decode.

        The private retained-raw marker is stripped from all submitted records.
        Restoring a file alone never creates an operation or a semantic-read claim.
        """
        temporary = temporary.resolve()
        cache, parsed, verified = {}, {}, set()

        def fetch(relative: str, expected: dict[str, Any] | None = None) -> bytes:
            safe_path(relative)
            if relative not in cache:
                target = temporary / ("projection-" + str(len(cache)))
                file = self.published.materialize(run["subject"], run["source_commit"], relative, target,
                                                  expected.get("sha256") if expected else None)
                require(file.stat().st_size <= 64 * 1024 ** 2, "WEB_PROJECTION_SOURCE_TOO_LARGE", relative)
                cache[relative] = file.read_bytes()
            raw = cache[relative]
            if expected:
                binding = (relative, expected.get("sha256"), expected.get("bytes"))
                if binding not in verified:
                    require(sha(raw) == expected.get("sha256") and len(raw) == expected.get("bytes"),
                            "WEB_PROJECTION_BYTES_MISMATCH", relative)
                    verified.add(binding)
            return raw

        def metadata(relative: str, expected: dict[str, Any] | None = None) -> Any:
            raw = fetch(relative, expected)
            if relative not in parsed:
                parsed[relative] = ([json.loads(line) for line in raw.splitlines()] if relative.endswith(".jsonl")
                                    else json.loads(raw))
            return parsed[relative]

        normalized = []
        for submitted in operations:
            operation = {key: value for key, value in submitted.items() if not key.startswith("_bridge_")}
            projection = operation.get("projection")
            if projection is None:
                normalized.append(operation)
                continue
            require(isinstance(projection, dict) and projection.get("schema") == "study-web-observation-v1",
                    "WEB_PROJECTION_OBSERVATION_SCHEMA_INVALID")
            path = safe_path(operation["path"])
            require(path in known and operation.get("source_commit") == run["source_commit"]
                    and projection.get("source_sha256") == known[path]["sha256"], "WEB_PROJECTION_SOURCE_BINDING_MISMATCH", path)
            try:
                start = metadata("WEB_READ_START.json")
                require(start.get("schema") == "study-web-read-v1" and start.get("subject") == run["subject"]
                        and start.get("repository") == library.get("repository"), "WEB_PROJECTION_START_MISMATCH")
                catalog = metadata(start["catalog"]["path"], start["catalog"])
                require(catalog.get("schema") == "study-web-catalog-v1" and catalog.get("eof") is True,
                        "WEB_PROJECTION_CATALOG_INVALID")
                matches = []
                for part in catalog["parts"]:
                    if part["first_path"] <= path <= part["last_path"]:
                        rows = metadata(part["path"], part)
                        require(len(rows) == part["count"], "WEB_PROJECTION_CATALOG_COUNT_MISMATCH")
                        matches.extend(row for row in rows if row.get("path") == path)
                require(len(matches) == 1, "WEB_PROJECTION_SOURCE_NOT_UNIQUE", path)
                source = matches[0]
                require(source.get("status") == "AVAILABLE" and source.get("sha256") == known[path]["sha256"],
                        "WEB_PROJECTION_SOURCE_BINDING_MISMATCH", path)
                require(projection.get("manifest_path") == source["transport"]["path"], "WEB_PROJECTION_MANIFEST_BINDING_MISMATCH")
                descriptor = metadata(source["transport"]["path"], source["transport"])
                require(descriptor.get("schema") == "study-web-source-v1" and descriptor.get("encoding") in {"utf8", "base64"}
                        and all(descriptor.get(key) == source.get(key) for key in ("sha256", "bytes")),
                        "WEB_PROJECTION_DESCRIPTOR_BINDING_MISMATCH")
                parts = [part for part in descriptor["parts"] if part["path"] == projection.get("part_path")]
                require(len(parts) == 1, "WEB_PROJECTION_PART_NOT_BOUND")
                part = parts[0]
                lines = fetch(part["path"], part).splitlines(keepends=True)
                first, last = projection.get("start_line"), projection.get("end_line")
                require(len(lines) == part["lines"] and type(first) is int and type(last) is int
                        and 1 <= first <= last <= len(lines) and last - first < start.get("max_fetch_lines", 8),
                        "WEB_PROJECTION_OBSERVED_RANGE_INVALID")
                raw = b"".join(lines[first - 1:last])
                raw_quote = projection.get("raw_quote")
                require(isinstance(raw_quote, str), "WEB_PROJECTION_RAW_RANGE_MISMATCH")
                actual_raw = raw_quote.encode()
                # fetch_file's content field omits precisely the final LF of
                # the requested line range. Preserve/hash that actual return;
                # only the same-commit canonical range restores its final LF.
                if actual_raw == raw:
                    line_ending = "verbatim"
                elif raw.endswith(b"\n") and actual_raw == raw[:-1]:
                    line_ending = "fetch_file_omits_final_lf"
                else:
                    raise BridgeError("WEB_PROJECTION_RAW_RANGE_MISMATCH")
                require(projection.get("raw_sha256") == sha(actual_raw), "WEB_PROJECTION_RAW_RANGE_MISMATCH")
                require(projection.get("line_ending", line_ending) == line_ending,
                        "WEB_PROJECTION_LINE_ENDING_DECLARATION_MISMATCH")
                observation = source_file(Path(operation["observation_file"]))
                require(observation.resolve().is_relative_to(self.base)
                        and not observation.resolve().is_relative_to(self.base / "inbox"), "DOWNLOADED_CLAIMS_ARE_NOT_WEB_EVIDENCE")
                require(file_hash(observation) == operation.get("observation_sha256") and raw_quote in observation.read_text()
                        and operation.get("truncated") is False, "WEB_PROJECTION_NOT_ACTUALLY_OBSERVED")
                native = fetch(path, source)
                payloads, offset, end, eof, marker = [], None, None, False, None
                for line in lines[first - 1:last]:
                    record = json.loads(line)
                    require(not eof, "WEB_PROJECTION_DATA_AFTER_EOF")
                    if record.get("eof") is True:
                        require(record == {"eof": True, "sha256": source["sha256"], "bytes": source["bytes"],
                                           "records": descriptor["data_records"]}, "WEB_PROJECTION_EOF_MISMATCH")
                        require(end is None or end == len(native), "WEB_PROJECTION_PREMATURE_EOF")
                        offset = len(native) if offset is None else offset
                        end, eof, marker = len(native), True, line.decode().rstrip("\r\n")
                        continue
                    begin, value = record["offset"], record["data"]
                    payload = value.encode("utf-8") if descriptor["encoding"] == "utf8" else base64.b64decode(value, validate=True)
                    require(type(begin) is int and 0 <= begin <= len(native) and len(payload) <= descriptor["payload_bytes_max"]
                            and (end is None or begin == end), "WEB_PROJECTION_SOURCE_OFFSET_MISMATCH")
                    require(native[begin:begin + len(payload)] == payload, "WEB_PROJECTION_DECODED_SOURCE_MISMATCH")
                    offset = begin if offset is None else offset
                    end = begin + len(payload)
                    payloads.append(payload)
                decoded = b"".join(payloads)
                operation.update(start_byte=offset, end_byte=end, content_sha256=sha(decoded), eof=eof,
                                 eof_marker=marker, _bridge_projection_raw=raw_quote,
                                 _bridge_projection_line_ending=line_ending, _bridge_projection_canonical_sha256=sha(raw))
                if descriptor["encoding"] == "utf8":
                    operation.update(kind="text", content_quote=decoded.decode("utf-8"))
                else:
                    operation.update(kind="binary_transport", content_quote=None)
            except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                if isinstance(exc, BridgeError):
                    raise
                raise BridgeError("WEB_PROJECTION_INVALID", str(exc)) from exc
            normalized.append(operation)
        return normalized

    def inspect_web_read(self, run: dict[str, Any], evidence_path: Path,
                         confirm_observed_cu: bool = False) -> dict[str, Any]:
        """Operator-recorded observations from the already authorized real task.

        This never substitutes an output manifest or a push receipt for tool evidence.
        """
        evidence = load(evidence_path)
        require(run.get("authorization_ref") and run.get("task_url"), "NO_AUTHORIZED_WEB_TASK")
        for field in ("run_id", "subject", "source_commit", "task_url", "authorization_ref"):
            require(evidence.get(field) == run.get(field), "WEB_EVIDENCE_BINDING_MISMATCH", field)
        require(evidence.get("kind") == "observed_authorized_task_tool_reads" and evidence.get("read_operations"),
                "ACTUAL_WEB_TOOL_READS_REQUIRED")
        require(evidence_path.resolve().is_relative_to(self.base), "WEB_EVIDENCE_MUST_BE_LOCAL_EXECUTION_RECORD")
        inbox = self.base / "inbox"
        require(not evidence_path.resolve().is_relative_to(inbox), "DOWNLOADED_CLAIMS_ARE_NOT_WEB_EVIDENCE")
        reader = getattr(self.published, "inspect_version", self.published.verify)
        receipt, library = reader(run["subject"], run["source_commit"])
        known = {row["path"]: row for row in library["files"]}
        known.update(published_metadata(library))
        known["PUBLISHED_LIBRARY.json"] = {"sha256": receipt["manifest_sha256"]}
        paths, images, current_issues = set(), set(), []
        with tempfile.TemporaryDirectory(prefix="study-bridge-web-check-") as temporary, self.input_materials(run) as current:
            known.update(current)
            operations = self.normalize_web_operations(run, library, known, evidence["read_operations"], Path(temporary))
            for number, operation in enumerate(operations):
                require(operation.get("tool") and operation.get("result_locator"), "WEB_TOOL_RESULT_LOCATOR_REQUIRED")
                path = safe_path(operation["path"])
                require(path in known and operation.get("source_commit") == run["source_commit"],
                        "WEB_TOOL_SOURCE_MISMATCH", path)
                observation = source_file(Path(operation["observation_file"]))
                require(observation.resolve().is_relative_to(self.base) and not observation.resolve().is_relative_to(inbox),
                        "DOWNLOADED_CLAIMS_ARE_NOT_WEB_EVIDENCE")
                require(file_hash(observation) == operation.get("observation_sha256"), "WEB_TOOL_OBSERVATION_MISMATCH")
                if path in current:
                    source = current[path]["local_source"]
                else:
                    source = self.published.materialize(run["subject"], run["source_commit"], path,
                                                       Path(temporary).resolve() / str(number), known[path]["sha256"])
                if operation.get("kind") == "image":
                    # A saved CU screenshot is observation evidence, while the
                    # original hash proves which library image it refers to.
                    with observation.open("rb") as inp:
                        magic = inp.read(16)
                    require(magic.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
                            and operation.get("original_sha256") == known[path]["sha256"],
                            "CU_IMAGE_OBSERVATION_REQUIRED", path)
                    images.add(path)
                elif operation.get("kind") == "binary_transport":
                    require(operation.get("_bridge_projection_raw"), "UNVERIFIED_BINARY_TRANSPORT")
                else:
                    quote = operation.get("content_quote")
                    require(isinstance(quote, str) and (quote.strip() or source.stat().st_size == 0
                            or operation.get("_bridge_projection_raw")), "OBSERVED_CONTENT_QUOTE_REQUIRED", path)
                    require(source.stat().st_size <= 64 * 1024 ** 2 and observation.stat().st_size <= 64 * 1024 ** 2,
                            "TEXT_EVIDENCE_TOO_LARGE")
                    require(quote in source.read_text() and operation.get("_bridge_projection_raw", quote) in observation.read_text(),
                            "OBSERVATION_QUOTE_NOT_IN_SOURCE_AND_TOOL_RESULT", path)
                paths.add(path)
            if run["project"] == "A":
                for question in run["input_questions"]:
                    prefix = "quick-packs/" + question["package_id"] + "/"
                    for member in [question["conversation_path"], *question.get("problem_paths", []), *question.get("solution_paths", [])]:
                        path = prefix + member
                        if path in current and Path(member).suffix.lower() not in VISUAL_SUFFIXES:
                            original = current[path]["local_source"].read_bytes()
                            coverage = observed_text_coverage(path, original, operations)
                            if not coverage["complete"] or coverage["sha256"] != sha(original):
                                current_issues.append("A_CURRENT_TEXT_NOT_COMPLETELY_OBSERVED:" + member)
            if run["project"] == "A":
                for review in run.get("input_review_updates", []):
                    prefix = "lessons/" + review["lesson_id"] + "/"
                    for member in lesson_members(review):
                        path = prefix + member
                        if path not in current:
                            current_issues.append("A_CURRENT_LESSON_ORIGINAL_MISSING:" + path)
                        elif Path(member).suffix.lower() in VISUAL_SUFFIXES:
                            if path not in images:
                                current_issues.append("A_CURRENT_IMAGE_OBSERVATION_REQUIRED:" + path)
                        else:
                            original = current[path]["local_source"].read_bytes()
                            coverage = observed_text_coverage(path, original, operations)
                            if not coverage["complete"] or coverage["sha256"] != sha(original):
                                current_issues.append("A_CURRENT_TEXT_NOT_COMPLETELY_OBSERVED:" + path)
            try:
                texts = {}
                for number, path in enumerate(scan_source_paths(run["subject"], library)):
                    source = self.published.materialize(run["subject"], run["source_commit"], path,
                                                       Path(temporary).resolve() / ("scan-" + str(number)), known[path]["sha256"])
                    require(source.stat().st_size <= 64 * 1024 ** 2, "SCAN_INDEX_TOO_LARGE")
                    texts[path] = source.read_bytes().decode("utf-8")
                scan = validate_scan(run["subject"], run["source_commit"], library, texts,
                                     evidence.get("scan"), operations)
            except (BridgeError, ValueError, KeyError, TypeError) as exc:
                scan = {"mechanical_status": "PARTIAL", "issues": [{"code": "NATIVE_SCAN_SCOPE_UNAVAILABLE", "detail": str(exc)}],
                        "actual_cu_observation_confirmed": False}
        require("PUBLISHED_LIBRARY.json" in paths and len(paths) >= 2, "WEB_LIBRARY_AND_CONTENT_READ_REQUIRED")
        if run["project"] == "A":
            if (not run["input_questions"] or not run["input_packages"]) and not run.get("input_review_updates"):
                current_issues.append("A_CURRENT_PACKAGE_IDENTITIES_MISSING")
            for question in run["input_questions"]:
                prefix = "quick-packs/" + question["package_id"] + "/"
                if prefix + question["conversation_path"] not in paths:
                    current_issues.append("A_COMPLETE_CURRENT_CONVERSATION_READ_REQUIRED")
                for original in question.get("problem_paths", []) + question.get("solution_paths", []):
                    if prefix + original not in paths:
                        current_issues.append("A_CURRENT_ORIGINAL_READ_REQUIRED:" + original)
                    if Path(original).suffix.lower() in VISUAL_SUFFIXES:
                        if prefix + original not in images:
                            current_issues.append("A_CURRENT_IMAGE_OBSERVATION_REQUIRED:" + original)
        result = {"path": str(evidence_path.resolve()), "sha256": file_hash(evidence_path),
                  "read_paths": sorted(paths), "image_paths": sorted(images), "scan": scan,
                  "current_input_issues": sorted(set(current_issues)), "root_cu_confirmation": None,
                  "verification_method": "mechanical_source_checks_separate_from_explicit_local_CU_confirmation"}
        result["projection_normalizations"] = [
            {"operation_index": number, "path": operation["path"],
             "line_ending": operation["_bridge_projection_line_ending"],
             "actual_raw_sha256": operation["projection"]["raw_sha256"],
             "canonical_range_sha256": operation["_bridge_projection_canonical_sha256"]}
            for number, operation in enumerate(operations) if "_bridge_projection_line_ending" in operation]
        if confirm_observed_cu:
            result["root_cu_confirmation"] = {"evidence_sha256": result["sha256"],
                "confirmed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "statement": "The local operator independently inspected the actual authorized task CU/tool results, range and EOF observations, and required original image views. This is an explicit operator assertion, not a conclusion inferred from these files."}
        result["status"] = "PASS" if scan["mechanical_status"] == "PASS" and not current_issues and confirm_observed_cu else "PARTIAL"
        result["business_supported"] = run["subject"] in PROJECT_SUBJECTS.get(run["project"], ())
        result["business_admitted"] = result["status"] == "PASS" and result["business_supported"]
        return result

    def record_web_read(self, run: dict[str, Any], evidence_path: Path,
                        confirm_observed_cu: bool = False) -> dict[str, Any]:
        evidence = self.inspect_web_read(run, evidence_path, confirm_observed_cu)
        run.update(web_read_status="verified" if evidence["business_admitted"] else "PARTIAL", web_evidence=evidence)
        if run["stage"] == "SEND_RESULT_UNKNOWN" and evidence["business_admitted"]:
            run["stage"] = "SENT"
        self.save_run(run)
        return run

    def require_web_admission(self, run: dict[str, Any]) -> None:
        evidence = run.get("web_evidence", {})
        require(run.get("web_read_status") == "verified" and evidence.get("scan", {}).get("mechanical_status") == "PASS",
                "FULL_HISTORY_SCAN_NOT_VERIFIED")
        require(evidence.get("path") and file_hash(Path(evidence["path"])) == evidence.get("sha256"), "WEB_EVIDENCE_CHANGED")
        require((evidence.get("root_cu_confirmation") or {}).get("evidence_sha256") == evidence["sha256"],
                "ACTUAL_CU_OBSERVATION_REQUIRES_EXPLICIT_LOCAL_CONFIRMATION")
        # Reopen observations and the same-commit native index. A cached PASS
        # cannot authorize a later import after any evidence file was changed.
        checked = self.inspect_web_read(run, Path(evidence["path"]), confirm_observed_cu=False)
        require(checked["scan"]["mechanical_status"] == "PASS" and not checked["current_input_issues"],
                "FULL_HISTORY_OR_CURRENT_INPUT_EVIDENCE_CHANGED")

    def download(self, run: dict[str, Any], archive: Path, max_bytes: int = DEFAULT_LIMIT) -> dict[str, Any]:
        require(run.get("task_url") and run.get("authorization_ref"), "NO_AUTHORIZED_WEB_TASK")
        digest = file_hash(archive)
        if run.get("download_sha256"):
            require(run["download_sha256"] == digest, "RUN_OUTPUT_CONFLICT")
            if run["stage"] in {"DOCUMENT_IMPORTED", "QUEUE_IMPORTED", "NO_DUE_CONCEPTS", "AWAITING_LOCAL_DECISION",
                                "LOCAL_DECIDED_PUBLISHED", "LOCAL_DECIDED_CLOUD_PENDING"}:
                check_manifest(Path(run["output_dir"]))
                require(file_hash(Path(run["output_dir"]).parent / "download.zip") == digest, "STORED_DOWNLOAD_CHANGED")
                return run
        day = run.get("target_date") if run["project"] in {"C", "D"} else run["date"]
        destination = self.base / "inbox" / day / ("project-" + run["project"]) / run["subject"] / run["run_id"]
        with tempfile.TemporaryDirectory(prefix="study-bridge-download-") as temporary:
            extracted = extract_zip(archive, Path(temporary).resolve() / "output", ArchiveBudget(Path(temporary).resolve(), max_bytes=max_bytes))
            manifest = check_manifest(extracted)
            for field in ("project", "subject", "run_id", "source_commit"):
                require(manifest.get(field) == run[field], "OUTPUT_RUN_BINDING_MISMATCH", field)
            require(manifest.get("project") in PROJECT_SUBJECTS and run["subject"] in PROJECT_SUBJECTS[run["project"]],
                    "INVALID_PROJECT_SUBJECT")
            if run["project"] in {"C", "D"}:
                require(manifest.get("target_date") == run["target_date"], "TARGET_DATE_MISMATCH")
            if run["project"] == "A":
                require(manifest.get("input_packages") == run["input_packages"], "A_INPUT_VERSION_MISMATCH")
                require(manifest.get("input_lessons", []) == run.get("input_lessons", []), "A_INPUT_LESSON_VERSION_MISMATCH")
            copy_file(archive, destination / "download.zip")
            for member in extracted.rglob("*"):
                if member.is_file():
                    copy_file(member, destination / "output" / member.relative_to(extracted))
        run.update(download_sha256=digest, output_dir=str(destination / "output"), stage="DOWNLOADED")
        self.save_run(run)
        blockers = output_completion_blockers(manifest, destination / "output")
        if blockers:
            run.update(stage="DOWNLOADED_NONCOMPLETE", output_admission={"status": "PARTIAL", "reasons": blockers})
            self.save_run(run)
            return run
        if run.get("web_read_status") != "verified":
            run["stage"] = "DOWNLOADED_AWAITING_WEB_EVIDENCE"
            self.save_run(run)
            return run
        self.validate_output(run)
        if run["project"] == "A":
            run["stage"] = "AWAITING_LOCAL_DECISION"
            if run.get("input_lessons"):
                run.update(review_updates_status="review_updates_pending_local_adjudication", learning_events_written=0)
        elif run["project"] == "B":
            if run["subject"] == "math":
                native = self.owners.import_queue("B", "math", self.queue_payload(run), destination / "native-import.json")
                require(native.get("status") in {"recorded", "noop"} and native.get("queue_id") is None,
                        "B_SOURCE_REGISTRATION_NOT_CONFIRMED", native)
                run["native_import_receipt"] = native
            run["stage"] = "DOCUMENT_IMPORTED"
            run["learning_events_written"] = 0
        else:
            payload = self.queue_payload(run)
            native = self.owners.import_queue(run["project"], run["subject"], payload, destination / "native-import.json")
            native_status = native.get("status")
            allowed_status = {"recorded", "noop"} if run["project"] == "C" else {"IMPORTED_FOR_NATIVE_PREPARATION", "ALREADY_IMPORTED", "NO_DUE_CONCEPTS"}
            require(native_status in allowed_status,
                    "NATIVE_IMPORT_NOT_ADMITTED", native)
            run.update(stage="NO_DUE_CONCEPTS" if native_status == "NO_DUE_CONCEPTS" else "QUEUE_IMPORTED",
                       native_import_receipt=native, learning_events_written=0)
        self.save_run(run)
        return run

    def bind_a_decision(self, run: dict[str, Any], native_receipt: Path | list[Path]) -> dict[str, Any]:
        recovery = run.get("stage") in {"LOCAL_DECIDED_CLOUD_PENDING", "LOCAL_DECIDED_PUBLISHED"} and bool(run.get("local_decision"))
        require(run["project"] == "A" and run.get("download_sha256") and run["stage"] in {
                "AWAITING_LOCAL_DECISION", "LOCAL_DECIDED_CLOUD_PENDING", "LOCAL_DECIDED_PUBLISHED", "DOWNLOADED_NONCOMPLETE",
                "DOWNLOADED_AWAITING_WEB_EVIDENCE", "DOWNLOADED"},
                "A_DOWNLOAD_NOT_READY_FOR_LOCAL_DECISION_BINDING")
        require(not run.get("input_lessons"), "review_updates_pending_local_adjudication")
        receipts = list(dict.fromkeys(native_receipt if isinstance(native_receipt, list) else [native_receipt]))
        checked = [dict(self.owners.verify_a(run["subject"], path, run), native_receipt_path=str(path.resolve())) for path in receipts]
        expected = {item["package_id"] for item in run["input_packages"]}
        require({package_id for row in checked for package_id in row["package_ids"]} == expected,
                "A_LOCAL_DECISION_SET_INCOMPLETE")
        accepts = any(decision.get("outcome") in {"accepted", "modified"} for row in checked for decision in row["decisions"])
        if accepts and not recovery:
            self.validate_output(run)
        run["local_decision"] = {"receipts": checked, "package_ids": sorted(expected)}
        try:
            published = [self.published.event_receipt(run["subject"], row["publication_event_id"]) for row in checked]
            require(all(row["status"] == "PUBLISHED" for row in published), "LOCAL_COMMIT_CLOUD_PENDING")
            for row in published:
                self.published.verify(run["subject"], row["source_commit"])
            latest = self.published.latest(run["subject"])
            require(all(self.published.contains(run["subject"], row["source_commit"], latest["source_commit"]) for row in published),
                    "LATEST_PUBLICATION_PRECEDES_A_DECISION")
            verified, _ = self.published.verify(run["subject"], latest["source_commit"])
        except (BridgeError, publication.PublicationError, OSError) as exc:
            run.update(stage="LOCAL_DECIDED_CLOUD_PENDING", publication_state="local_commit",
                       pending_reason=getattr(exc, "code", "PUBLICATION_RECEIPT_UNAVAILABLE"))
            self.save_run(run)
            return run
        run.update(stage="LOCAL_DECIDED_PUBLISHED", publication_after_decision=verified,
                   publication_state="local_snapshot_ready" if verified.get("publication_target") == "local" else "pushed",
                   publication_target=verified.get("publication_target", "github"))
        run.pop("pending_reason", None)
        self.save_run(run)
        return run

    def record_a_disposition(self, run: dict[str, Any], decision: str, reason: str) -> dict[str, Any]:
        require(run["project"] == "A" and run.get("download_sha256") and decision in {"rejected", "needs_review"}
                and isinstance(reason, str) and reason.strip(), "LOCAL_A_DISPOSITION_REQUIRED")
        row = {"decision": decision, "reason": reason, "download_sha256": run["download_sha256"],
               "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(), "formal_writes": 0,
               "meaning": "Local review disposition only; does not mark a subject decision or formal intake complete."}
        run.setdefault("local_review_dispositions", []).append(row)
        self.save_run(run)
        return run

    def validate_output(self, run: dict[str, Any]) -> None:
        root = Path(run["output_dir"])
        manifest = check_manifest(root)
        require(not output_completion_blockers(manifest, root), "OUTPUT_EXPLICITLY_NONCOMPLETE")
        self.require_web_admission(run)
        evidence = run.get("web_evidence", {})
        require(evidence.get("path") and file_hash(Path(evidence["path"])) == evidence.get("sha256"),
                "WEB_EVIDENCE_CHANGED")
        version_receipt, library = self.published.verify(run["subject"], run["source_commit"])
        known = {row["path"]: row for row in library["files"]}
        project = run["project"]
        contract = manifest.get("contract_version")
        a_math_v2 = project == "A" and run["subject"] == "math" and contract == A_MATH_CONTRACT
        b_math_v2 = project == "B" and run["subject"] == "math" and contract == B_MATH_CONTRACT
        c_math_v2 = project == "C" and run["subject"] == "math" and contract == C_MATH_CONTRACT
        required = list(BUSINESS_FILES[project])
        if a_math_v2:
            required.append("profile_updates.json")
        if b_math_v2:
            required.extend(["START_HERE.md", "student/questions.jsonl", "private/hints.jsonl"])
        require(all((root / name).is_file() for name in required), "REQUIRED_OUTPUT_MISSING", required)
        contract_references = []
        if project == "A":
            require(manifest.get("input_lessons", []) == run.get("input_lessons", []), "A_INPUT_LESSON_VERSION_MISMATCH")
            advice = load(root / "advice.json")
            items = advice.get("items")
            expected = {(x["package_id"], x["question_id"]) for x in run["input_questions"]}
            require(isinstance(items, list) and {(x.get("package_id"), x.get("question_id")) for x in items} == expected
                    and len(items) == len(expected), "A_ADVICE_PACKAGE_COVERAGE_MISMATCH")
            require(all((isinstance(x.get("recommendation"), str) and x["recommendation"].strip()
                         or isinstance(x.get("recommendation"), dict) and x["recommendation"])
                        and isinstance(x.get("uncertainties"), list) for x in items), "A_ADVICE_FIELDS_MISSING")
            profile_updates = []
            if a_math_v2:
                require(all(isinstance(x.get("recommendation"), dict)
                            and isinstance(x["recommendation"].get("formal_card"), dict)
                            and x["recommendation"]["formal_card"]
                            and isinstance(x["recommendation"].get("exam_writeup"), str)
                            and x["recommendation"]["exam_writeup"].strip()
                            and isinstance(x["recommendation"].get("observations"), list)
                            and (x["recommendation"]["observations"] or
                                 (x['recommendation'].get('observation_scope') == 'no_observation' and
                                  isinstance(x['recommendation'].get('no_observation_reason'), str) and
                                  x['recommendation']['no_observation_reason'].strip()))
                            for x in items), "A_MATH_FINAL_SEMANTIC_CONTENT_REQUIRED")
                profile_updates = validate_math_profile_updates(load(root / "profile_updates.json"))
            reviews = validate_review_updates(advice, run)
            items = items + reviews
        elif project == "B":
            items = json_lines(root / "questions.jsonl")
            require(items, "EMPTY_B_REVIEW")
            rubrics = json_lines(root / "answer_rubrics.jsonl")
            historical = json_lines(root / "historical_basis.jsonl")
            require(load(root / "review_plan.json") and load(root / "misconception_map.json")
                    and (root / "summary.md").read_text().strip(), "B_REVIEW_DOCUMENT_INCOMPLETE")
            require(all(row.get("rubric") or row.get("criteria") for row in rubrics), "B_RUBRIC_REQUIRED")
            ids = [x.get("question_id") for x in items]
            require(len(ids) == len(set(ids)) and set(ids) == {x.get("question_id") for x in rubrics}
                    == {x.get("question_id") for x in historical}, "B_QUESTION_RUBRIC_HISTORY_PAIRING")
            if b_math_v2:
                student = json_lines(root / "student/questions.jsonl")
                hints = json_lines(root / "private/hints.jsonl")
                start_here = (root / "START_HERE.md").read_text()
                require(set(ids) == {x.get("question_id") for x in student}
                        == {x.get("question_id") for x in hints}, "B_STUDENT_PRIVATE_PAIRING")
                require(all(isinstance(x.get("time_budget_seconds"), int) and x["time_budget_seconds"] > 0
                            and isinstance(x.get("prompt"), str) and x["prompt"].strip() for x in student),
                        "B_PREWRITTEN_TASK_REQUIRED")
                require(0 < sum(x["time_budget_seconds"] for x in student) <= 30 * 60,
                        "B_REVIEW_TIME_BUDGET_EXCEEDS_30_MINUTES")
                require(all(isinstance(x.get("hints"), list) and x["hints"] for x in hints),
                        "B_PRIVATE_HINTS_REQUIRED")
                roles = {row["path"]: row["role"] for row in manifest["files"]}
                require(all("private" in roles.get(path, "").lower()
                            for path in ("answer_rubrics.jsonl", "private/hints.jsonl")),
                        "B_RUBRICS_AND_HINTS_MUST_BE_PRIVATE")
                require("kaoyan-independent-review" in start_here
                        and "GPT-6" in start_here and "Capture" in start_here,
                        "B_START_HERE_EXECUTION_AND_AUDIT_HANDOFF_REQUIRED")
                require(manifest.get("local_skill") == "kaoyan-independent-review"
                        and manifest.get("capture_policy") == "disabled"
                        and manifest.get("formal_return_policy") == "gpt6_review_audit_after_evidence_closeout"
                        and manifest.get("generated_question_intake") is False,
                        "B_MATH_LOCAL_EXECUTION_CONTRACT_REQUIRED")
            for item in items:
                current, earlier = item.get("current_event", {}), item.get("historical_event")
                current_only = b_math_v2 and earlier is None
                if current_only:
                    require(item.get("history_status") == "current_only"
                            and isinstance(item.get("history_reason"), str) and item["history_reason"].strip(),
                            "B_CURRENT_ONLY_REASON_REQUIRED", item.get("question_id"))
                    require(item.get("common_mechanism") is None,
                            "B_CURRENT_ONLY_MUST_NOT_CLAIM_COMMON_HISTORY", item.get("question_id"))
                else:
                    require(isinstance(earlier, dict)
                            and event_time(earlier.get("event_date")) < event_time(current.get("event_date")),
                            "B_HISTORY_NOT_EARLIER_THAN_CURRENT_EVENT", item.get("question_id"))
                    require(not b_math_v2 or item.get("history_status") == "paired",
                            "B_PAIRED_HISTORY_STATUS_REQUIRED", item.get("question_id"))
                    require(not current.get("event_id") or not earlier.get("event_id")
                            or current["event_id"] != earlier["event_id"], "B_CURRENT_EVENT_IS_NOT_HISTORY")
                for event in (current,) if current_only else (current, earlier):
                    reference = event.get("evidence_ref", {})
                    quote = reference.get("quote", "")
                    event_id = event.get("event_id")
                    require(event_id is None or isinstance(event_id, str) and event_id.strip(), "B_INVALID_EVENT_ID")
                    require(str(event["event_date"]) in quote and (event_id is None or event_id in quote),
                            "B_EVENT_ID_DATE_NOT_BOUND_TO_EVIDENCE")
                if not current_only:
                    mechanism = item.get("common_mechanism", {})
                    require(mechanism.get("explanation") and len(mechanism.get("evidence_refs", [])) >= 2
                            and item.get("difference"), "B_COMMON_MECHANISM_AND_DIFFERENCE_REQUIRED")
                    require(all(event["evidence_ref"] in mechanism["evidence_refs"] for event in (current, earlier)),
                            "B_COMMON_MECHANISM_MUST_CITE_BOTH_EVENTS")
                if b_math_v2:
                    source_ids = item.get("source_formal_ids")
                    require(isinstance(source_ids, list) and source_ids
                            and len(source_ids) == len(set(source_ids))
                            and item.get("question_id") not in source_ids
                            and all(isinstance(value, str) and value for value in source_ids),
                            "B_SOURCE_FORMAL_IDS_REQUIRED", item.get("question_id"))
            # Imported text is a source-backed document. Mechanism interpretation
            # still belongs to the subject reviewer and never becomes mastery.
        elif project == "C":
            selection = load(root / "selected_questions.json")
            items = selection.get("questions", [])
            require(len(items) == 5 and len({x.get("question_id") for x in items}) == 5, "C_REQUIRES_FIVE_UNIQUE_REAL_QUESTIONS")
            require(all(x.get("formal_card_sha256") and x.get("state_source_version")
                        and x.get("public_value_reason") for x in items), "C_NATIVE_STATE_BINDINGS_REQUIRED")
            if c_math_v2:
                inputs = selection.get("selection_inputs", {})
                require(inputs.get("schema") == "math-c-selection-input-v1"
                        and inputs.get("source_commit") == run["source_commit"],
                        "C_CURRENT_SOURCE_HANDOFF_REQUIRED")
                handoff = run.get("input_handoff")
                after_a_handoff = bool(handoff and handoff.get("source_project") == "A")
                source = inputs.get("a_formal_handoff" if after_a_handoff else "b_review_handoff", {})
                require(handoff and source.get("status") == "ready"
                        and all(source.get(key) == handoff.get(key) for key in
                                ("schema", "source_session_id", "source_evidence_id", "formal_event_id", "source_commit"))
                        and source.get("publication_current") == handoff.get("publication_current"),
                        "C_B_HANDOFF_BINDING_MISMATCH")
                if after_a_handoff:
                    require(inputs.get("b_review_handoff", {}).get("status") == "not_performed"
                            and inputs["b_review_handoff"].get("reason") == handoff["b_review"]["reason"]
                            and source.get("source_batch_id") == handoff["source_batch_id"]
                            and source.get("formal_event_ids") == handoff["formal_event_ids"]
                            and source.get("source_run_ids") == handoff["source_run_ids"],
                            "C_AFTER_A_SOURCE_AND_NO_B_BINDING_REQUIRED")
                prefix = "c-handoff/" + handoff["source_evidence_id"] + "/"
                handoff_refs = source.get("source_refs")
                require(isinstance(handoff_refs, list)
                        and {(ref.get("path"), ref.get("sha256")) for ref in handoff_refs}
                        >= {(prefix + name, handoff["member_sha256"][name]) for name in ("handoff.json", "refs.json")},
                        "C_B_HANDOFF_MEMBER_REFS_REQUIRED")
                dependency = run.get("dependency", {})
                current_a = inputs.get("current_a", {})
                if dependency.get("project") == "A":
                    require(dependency.get("stage") == "LOCAL_DECIDED_PUBLISHED"
                            and current_a.get("status") == "formalized"
                            and current_a.get("run_id") == dependency.get("run_id")
                            and isinstance(current_a.get("source_refs"), list) and current_a["source_refs"],
                            "C_CURRENT_A_BINDING_REQUIRED")
                else:
                    require(dependency.get("status") == "no_new_packages_declared"
                            and dependency.get("no_new_packages_declared") is True
                            and isinstance(dependency.get("reason"), str) and dependency["reason"].strip()
                            and current_a.get("status") == "no_new_packages_declared"
                            and current_a.get("reason") == dependency["reason"]
                            and isinstance(current_a.get("source_refs"), list)
                            and current_a["source_refs"] == [],
                            "C_CURRENT_A_NO_NEW_PACKAGES_BINDING_REQUIRED")
                mcp_current = inputs.get("mcp_current", {})
                require(mcp_current.get("status") == "current"
                        and mcp_current.get("source_commit") == run["source_commit"]
                        and isinstance(mcp_current.get("source_refs"), list) and mcp_current["source_refs"],
                        "C_MCP_CURRENT_BINDING_REQUIRED")
                contract_references = collect_references(inputs)
                for item in items:
                    for field in ("last_actual_activity", "anchor_source_floor"):
                        value = item.get(field)
                        require(isinstance(value, dict) and value.get("status") in {"known", "unrecorded"},
                                "C_ACTUAL_ACTIVITY_AND_SOURCE_FLOOR_REQUIRED", {"question_id": item.get("question_id"), "field": field})
                        if value["status"] == "known":
                            event_time(value.get("date"))
                            ref = value.get("source_ref")
                            require(isinstance(ref, dict) and ref.get("path") and ref.get("sha256"),
                                    "C_ACTIVITY_DATE_SOURCE_REQUIRED", {"question_id": item.get("question_id"), "field": field})
                        else:
                            require(value.get("date") is None and isinstance(value.get("reason"), str) and value["reason"].strip(),
                                    "C_UNRECORDED_ACTIVITY_REASON_REQUIRED", {"question_id": item.get("question_id"), "field": field})
                    activity = item["last_actual_activity"]
                    floor = item["anchor_source_floor"]
                    if activity["status"] == "known":
                        target = dt.date.fromisoformat(run["target_date"])
                        require(event_time(activity["date"]).date() <= target - dt.timedelta(days=8),
                                "C_DAY_0_7_ACTIVITY_NOT_ELIGIBLE", item.get("question_id"))
                    if activity["status"] == floor["status"] == "known":
                        require(event_time(floor["date"]) <= event_time(activity["date"]),
                                "C_SOURCE_FLOOR_AFTER_SELECTED_ACTIVITY", item.get("question_id"))
        else:
            items = json_lines(root / "selected_items.jsonl")
            due = load(root / "due_concepts.json").get("concept_ids")
            coverage = load(root / "coverage_report.json")
            unresolved = json_lines(root / "unresolved.jsonl")
            require(manifest.get("due_version") == run["due_version"], "D_DUE_VERSION_MISMATCH")
            require(isinstance(due, list) and len(due) == len(set(due)), "D_INVALID_DUE_CONCEPT_SET")
            require(not unresolved and not coverage.get("unresolved_concept_ids"), "D_REQUIRED_COVERAGE_UNRESOLVED")
            covered = [concept for row in items for concept in row.get("review_target_concept_ids", [])]
            require(len(covered) == len(set(covered)) and set(covered) == set(due), "D_COVERAGE_MISMATCH")
            require(len(items) == len({x.get("question_id") for x in items}), "D_DUPLICATE_QUESTION")
            extra = self._d_concept_payload(root)
            for field in ("concept_priority_version", "concept_requests", "review_concept_ids"):
                if field in run:
                    require(extra.get(field) == run[field], "D_CONCEPT_CONTEXT_MISMATCH", field)
            supplemental = extra.get("supplemental_items", [])
            items = items + supplemental
            review = extra.get("review_concept_ids", due)
            all_covered = {concept for row in items for concept in row.get("review_target_concept_ids", [])}
            require(all_covered == set(review) == set(coverage.get("covered_concept_ids", [])),
                    "D_REVIEW_CONCEPT_COVERAGE_MISMATCH")
        references = collect_references(items) + contract_references
        require(project == "A" or (project == "D" and not items and not due) or references, "PUBLISHED_EVIDENCE_REQUIRED")
        verified_hashes = set()
        with tempfile.TemporaryDirectory(prefix="study-bridge-source-check-") as temporary, self.input_materials(run) as inputs:
            observation_sources = {**known, **published_metadata(library), **inputs,
                                   "PUBLISHED_LIBRARY.json": {"sha256": version_receipt["manifest_sha256"]}}
            operations = self.normalize_web_operations(run, library, observation_sources,
                load(Path(evidence["path"]))["read_operations"], Path(temporary))
            resolved = []
            for item in items:
                for reference in collect_references(item):
                    path = safe_path(reference["path"])
                    require(reference.get("source_commit", run["source_commit"]) == run["source_commit"],
                            "SOURCE_REFERENCE_COMMIT_MISMATCH", path)
                    current_path = resolve_current_reference(reference, item, inputs, root, run) if project == "A" else None
                    if current_path:
                        path = current_path
                    else:
                        require(path in known and known[path]["sha256"] == reference.get("sha256"),
                                "SOURCE_REFERENCE_NOT_IN_VERSION", path)
                    resolved.append((reference, path, current_path))
            for reference in contract_references:
                path = safe_path(reference["path"])
                require(reference.get("source_commit", run["source_commit"]) == run["source_commit"],
                        "SOURCE_REFERENCE_COMMIT_MISMATCH", path)
                if path in inputs and inputs[path].get("handoff_id"):
                    require(inputs[path]["sha256"] == reference.get("sha256"),
                            "C_HANDOFF_MEMBER_REFERENCE_MISMATCH", path)
                    resolved.append((reference, path, path))
                else:
                    require(path in known and known[path]["sha256"] == reference.get("sha256"),
                            "SOURCE_REFERENCE_NOT_IN_VERSION", path)
                    resolved.append((reference, path, None))
            historical_paths = [path for _, path, current_path in resolved if not current_path]
            if hasattr(self.published, "current_sources"):
                current_sources = self.published.current_sources(run["subject"], known, historical_paths)
            else:
                current_sources = {row["publish_path"]: row for row in self.owners.sources(run["subject"], run["run_id"])["sources"]}
            for number, (reference, path, current_path) in enumerate(resolved):
                require(path in evidence["read_paths"], "SOURCE_NOT_OBSERVED_IN_AUTHORIZED_TASK", path)
                file = inputs[path]["local_source"] if current_path else self.published.materialize(
                    run["subject"], run["source_commit"], path, Path(temporary).resolve() / str(number), reference["sha256"])
                quote = reference.get("quote")
                visual = Path(path).suffix.lower() in VISUAL_SUFFIXES
                if visual:
                    require(path in evidence["image_paths"], "ORIGINAL_IMAGE_NOT_OBSERVED_IN_WEB_TASK", path)
                elif quote:
                    require(file.stat().st_size <= 64 * 1024 ** 2 and quote in file.read_text(), "SOURCE_QUOTE_MISMATCH", path)
                if not visual:
                    require_observed_reference(path, file.read_bytes(), quote, operations,
                                               complete=Path(path).name == "conversation.json")
                if project == "B":
                    for item in items:
                        events = (item["current_event"],)
                        if item.get("historical_event") is not None:
                            events += (item["historical_event"],)
                        for event in events:
                            if event["evidence_ref"] == reference:
                                known_ids = declared_event_ids(file, event["event_date"], quote or "")
                                require(not known_ids or event.get("event_id") in known_ids,
                                        "B_EXISTING_EVENT_ID_MUST_BE_USED", {"path": path, "event_ids": sorted(known_ids)})
                if not current_path:
                    current = current_sources.get(path)
                    require(current, "RELATED_CURRENT_SOURCE_UNAVAILABLE", path)
                    require(file_hash(Path(current["source_path"])) == reference["sha256"], "RELATED_SOURCE_STALE", path)
                elif visual:
                    require(path in evidence["image_paths"], "A_CURRENT_IMAGE_OBSERVATION_REQUIRED", path)
                verified_hashes.add(reference["sha256"])
            if a_math_v2:
                for update in profile_updates:
                    for reference in update["source_refs"]:
                        path = safe_path(reference["path"])
                        if path in inputs:
                            require(inputs[path]["sha256"] == reference["sha256"],
                                    "A_MATH_PROFILE_CURRENT_SOURCE_MISMATCH", path)
                        else:
                            require(path in known and known[path]["sha256"] == reference["sha256"],
                                    "A_MATH_PROFILE_SNAPSHOT_SOURCE_MISMATCH", path)
                        require(path in evidence["read_paths"], "A_MATH_PROFILE_SOURCE_NOT_OBSERVED", path)
        if project == "B":
            for item in items:
                refs = collect_references(item)
                cited_images = {ref["path"] for ref in refs if Path(ref["path"]).suffix.lower() in VISUAL_SUFFIXES}
                source_ids = set(item.get("source_formal_ids", [])) if b_math_v2 else {item["question_id"]}
                required_images = {path for path, row in known.items()
                                   if source_ids & set(row.get("formal_ids", []))
                                   and Path(path).suffix.lower() in VISUAL_SUFFIXES and row.get("required") is not False}
                for mapping in library.get("original_mappings", []):
                    mapping_ids = {mapping.get("question_id"), *mapping.get("formal_ids", [])}
                    if source_ids & mapping_ids:
                        path = mapping.get("publish_path", mapping.get("path", ""))
                        if Path(path).suffix.lower() in VISUAL_SUFFIXES:
                            required_images.add(path)
                require(required_images, "B_ORIGINAL_IMAGE_BINDING_REQUIRED", item["question_id"])
                require(required_images <= cited_images, "B_REQUIRED_ORIGINAL_REFERENCE_MISSING",
                        sorted(required_images - cited_images))
                require(required_images <= set(evidence["image_paths"]), "B_REQUIRED_ORIGINAL_IMAGE_NOT_OBSERVED",
                        sorted(required_images - set(evidence["image_paths"])))
        if project in {"C", "D"}:
            for item in items:
                valid_id(item["question_id"])
                require(item.get("source_label") and item.get("image_paths"), "ORIGINAL_SOURCE_LABEL_AND_IMAGES_REQUIRED")
                item_refs = collect_references(item)
                item_hashes = {ref["sha256"] for ref in item_refs}
                for field in ("problem_path", "solution_path", "history_path"):
                    source_file(root / safe_path(item[field]), root)
                for field in ("problem_path", "solution_path"):
                    original = root / item[field]
                    if file_hash(original) not in item_hashes:
                        require(original.stat().st_size <= 64 * 1024 ** 2, "UNBOUND_QUESTION_MATERIAL", item[field])
                        try:
                            content = original.read_text().strip()
                        except UnicodeError as exc:
                            raise BridgeError("UNBOUND_QUESTION_MATERIAL", item[field]) from exc
                        require(content and any(ref.get("quote", "").strip() == content for ref in item_refs),
                                "QUESTION_OR_SOLUTION_NOT_AN_ORIGINAL_OR_EXACT_EXCERPT", item[field])
                for raw in item["image_paths"]:
                    image = source_file(root / safe_path(raw), root)
                    image_hash = file_hash(image)
                    require(image_hash in verified_hashes & item_hashes, "IMAGE_NOT_BOUND_TO_PUBLISHED_ORIGINAL", raw)
                    matching = {ref["path"] for ref in item_refs if ref["sha256"] == image_hash}
                    require(matching & set(evidence.get("image_paths", [])), "ORIGINAL_IMAGE_NOT_OBSERVED_IN_WEB_TASK", raw)
                    paired_ids = {formal_id for path in matching for formal_id in known[path].get("formal_ids", [])}
                    if paired_ids:
                        allowed_ids = {item["question_id"]}
                        for mapping in library.get("original_mappings", []):
                            if mapping.get("question_id") == item["question_id"]:
                                allowed_ids.update(mapping.get("formal_ids", []))
                        require(allowed_ids & paired_ids, "QUESTION_IMAGE_PAIRING_MISMATCH", item["question_id"])
        profile_reference_count = (sum(len(update["source_refs"]) for update in profile_updates)
                                   if a_math_v2 else 0)
        run["validation"] = {"source_commit": run["source_commit"],
                             "reference_count": len(references) + profile_reference_count,
                             "subject_decision_required": project in {"A", "B"}, "learning_events_written": 0,
                             "contract_version": contract,
                             "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                             "download_sha256": run["download_sha256"]}
        if project == "B" and run["subject"] == "math":
            bindings = []
            for item in items:
                sources = []
                for identity in item.get("source_formal_ids", [item["question_id"]]):
                    cards = [entry for entry in known.values() if entry.get("kind") == "formal_card" and identity in entry.get("formal_ids", [])]
                    state = known.get(f"错题知识网络/教学投影/{identity}/learning-state.json", {})
                    require(len(cards) == 1 and state.get("state_source_version"), "B_NATIVE_STATE_BINDINGS_UNAVAILABLE", identity)
                    sources.append({"formal_id": identity, "formal_card_sha256": cards[0]["sha256"],
                                    "state_source_version": state["state_source_version"]})
                bindings.append({"question_id": item["question_id"], "sources": sources})
            run["validation"]["math_source_questions"] = bindings

    def queue_payload(self, run: dict[str, Any]) -> dict[str, Any]:
        root = Path(run["output_dir"])
        common = {"subject": run["subject"], "project": run["project"], "run_id": run["run_id"],
                  "source_commit": run["source_commit"], "target_date": run.get("target_date") or run["date"]}
        if run["project"] == "B" and run["subject"] == "math":
            if run["validation"].get("contract_version") == B_MATH_CONTRACT:
                return dict(common, schema_version="math-independent-review-source-v2",
                            local_skill="kaoyan-independent-review", capture_policy="disabled",
                            formal_return_policy="gpt6_review_audit_after_evidence_closeout",
                            generated_question_intake=False,
                            repository="xialovezhu-wq/kaoyan-math", branch="main",
                            questions=[{**question,
                                        "question_path": str(root / "student/questions.jsonl"),
                                        "solution_path": str(root / "answer_rubrics.jsonl"),
                                        "hints_path": str(root / "private/hints.jsonl")}
                                       for question in run["validation"]["math_source_questions"]])
            return dict(common, schema_version="math-review-source-v1", repository="xialovezhu-wq/kaoyan-math", branch="main",
                        questions=[{**question["sources"][0], "public_value_reason": "", "question_path": str(root / "questions.jsonl"),
                                    "solution_path": str(root / "answer_rubrics.jsonl")}
                                   for question in run["validation"]["math_source_questions"]])
        if run["project"] == "C":
            selection = load(root / "selected_questions.json")
            questions = selection["questions"]
            return dict(common, schema_version="math-review-source-v2" if "selection_basis" in selection else "math-review-source-v1",
                        **({"selection_basis": selection["selection_basis"]} if "selection_basis" in selection else {}),
                        **({"selection_inputs": selection["selection_inputs"]} if "selection_inputs" in selection else {}),
                        repository="xialovezhu-wq/kaoyan-math", branch="main",
                        questions=[{"formal_id": q["question_id"], "formal_card_sha256": q["formal_card_sha256"],
                                    "state_source_version": q["state_source_version"], "public_value_reason": q["public_value_reason"],
                                    **({"anchor_id": q["anchor_id"]} if "anchor_id" in q else {}),
                                    **({"last_actual_activity": q["last_actual_activity"],
                                        "anchor_source_floor": q["anchor_source_floor"]}
                                       if "last_actual_activity" in q and "anchor_source_floor" in q else {}),
                                    "question_path": str(root / q["problem_path"]), "solution_path": str(root / q["solution_path"])}
                                   for q in questions])
        return dict(common, task_id=run["run_id"], due_version=run["due_version"],
                    due_concept_ids=load(root / "due_concepts.json")["concept_ids"],
                    selected_items=json_lines(root / "selected_items.jsonl"), unresolved=json_lines(root / "unresolved.jsonl"),
                    coverage=load(root / "coverage_report.json"), output_root=str(root), **self._d_concept_payload(root))

    @staticmethod
    def _d_concept_payload(root: Path) -> dict[str, Any]:
        envelopes = [load(root / "manifest.json"), load(root / "due_concepts.json")]
        result = {}
        for field in ("concept_priority_version", "concept_requests", "review_concept_ids"):
            values = [row[field] for row in envelopes if field in row]
            require(not values or all(value == values[0] for value in values), "D_CONCEPT_CONTEXT_CONFLICT", field)
            if values:
                result[field] = values[0]
        supplemental = root / "supplemental_items.jsonl"
        if supplemental.is_file():
            result["supplemental_items"] = json_lines(supplemental)
        return result


def json_lines(path: Path) -> list[dict[str, Any]]:
    result = []
    with source_file(path).open() as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                require(isinstance(row, dict), "JSONL_OBJECT_REQUIRED", path.name)
                result.append(row)
    return result


def event_time(value: str | None) -> dt.datetime:
    require(isinstance(value, str) and value, "EVENT_DATE_REQUIRED")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeError("INVALID_EVENT_DATE") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))


def declared_event_ids(path: Path, event_date: str, quote: str) -> set[str]:
    """Find declared event_id fields, never manufacture IDs from question names."""
    text = path.read_text()
    ids = set(re.findall(r'''["']?event_id["']?\s*:\s*["']?([A-Za-z0-9_.:-]+)''', quote))
    compact_quote = re.sub(r"\s+", "", quote)

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            identity = value.get("event_id")
            if isinstance(identity, str) and any(event_date in field for field in value.values() if isinstance(field, str)):
                compact_record = re.sub(r"\s+", "", json.dumps(value, ensure_ascii=False))
                if compact_quote in compact_record or compact_record in compact_quote:
                    ids.add(identity)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
    try:
        visit(json.loads(text))
    except ValueError:
        for line in text.splitlines():
            try:
                visit(json.loads(line))
            except ValueError:
                pass
        if text.startswith("---\n"):
            header = text.split("---", 2)[1]
            if event_date in header:
                ids.update(re.findall(r'''(?m)^event_id:\s*["']?([A-Za-z0-9_.:-]+)''', header))
    return ids


def output_completion_blockers(manifest: dict[str, Any], root: Path) -> list[str]:
    blockers = []
    envelopes = [("manifest", manifest)]
    for filename in ("advice.json", "review_plan.json", "selected_questions.json", "coverage_report.json",
                     "engineering_summary.json", "engineering_receipt.json", "read_receipt.json"):
        if (root / filename).is_file():
            value = load(root / filename)
            if isinstance(value, dict):
                envelopes.append((filename, value))
    required = list(BUSINESS_FILES.get(manifest.get("project"), []))
    contract = manifest.get("contract_version")
    if contract == A_MATH_CONTRACT:
        required.append("profile_updates.json")
    elif contract == B_MATH_CONTRACT:
        required.extend(["START_HERE.md", "student/questions.jsonl", "private/hints.jsonl"])
    for filename in required:
        if not (root / filename).is_file():
            blockers.append("REQUIRED_BUSINESS_OUTPUT_MISSING:" + filename)
    # These are control envelopes, never arbitrary recommendations or quoted
    # source records whose old statuses may legitimately be PARTIAL.
    for name, value in list(envelopes):
        for key in ("scan", "read_gate", "engineering", "business", "completion"):
            if isinstance(value.get(key), dict):
                envelopes.append((name + "." + key, value[key]))
    for name, value in envelopes:
        for key in ("kind", "artifact_kind", "artifact_type", "output_type"):
            kind = value.get(key)
            if isinstance(kind, str) and any(word in kind.lower() for word in ("engineering", "read_receipt")):
                blockers.append("ENGINEERING_RECEIPT_NOT_BUSINESS_OUTPUT:" + name + "." + key)
        for key in ("status", "state", "completion_status"):
            status = value.get(key)
            if isinstance(status, str) and any(word in status.upper() for word in
                    ("BLOCKED", "PARTIAL", "INCOMPLETE", "NEEDS_REVIEW", "UNSUPPORTED", "NOT_RUN")):
                blockers.append(name + "." + key + "=" + status)
        for key in ("ready_for_formal_intake", "ready_for_import", "complete", "scan_complete", "business_admitted"):
            if value.get(key) is False:
                blockers.append(name + "." + key + "=false")
    return blockers


def resolve_current_reference(reference: dict[str, Any], item: dict[str, Any], inputs: dict[str, Any],
                              output_root: Path, run: dict[str, Any]) -> str | None:
    """Only registered package members or byte-identical, unique output copies."""
    path = safe_path(reference["path"])
    if item.get("review_id"):
        review = next((row for row in run.get("input_review_updates", []) if row["review_id"] == item["review_id"]), None)
        require(review, "A_CURRENT_REVIEW_IDENTITY_MISSING")
        lesson_id = review["lesson_id"]
        allowed = {"lessons/" + lesson_id + "/" + member for member in lesson_members(review)}
        if path in inputs:
            require(path in allowed and inputs[path].get("lesson_id") == lesson_id and
                    reference.get("sha256") == inputs[path]["sha256"], "A_CURRENT_REVIEW_REFERENCE_MISMATCH")
            require(reference.get("lesson_id", lesson_id) == lesson_id, "A_CURRENT_REVIEW_REFERENCE_MISMATCH")
            return path
        require(not path.startswith(("lessons/", "quick-packs/")) and reference.get("source_type") not in
                {"current", "current_input", "current_lesson"}, "A_CURRENT_REFERENCE_NOT_REGISTERED")
        return None
    package_id = item.get("package_id")
    question = next((row for row in run["input_questions"] if (row["package_id"], row["question_id"])
                     == (package_id, item.get("question_id"))), None)
    require(question, "A_CURRENT_ITEM_IDENTITY_MISSING")
    allowed = {question["conversation_path"], "original/manifest.json", "original/source.json", "original/receipt.json"}
    allowed.update(question.get("problem_paths", []) + question.get("solution_paths", []))
    candidates = [name for name, row in inputs.items() if row.get("package_id") == package_id
                  and row["member_path"] in allowed and row["sha256"] == reference.get("sha256")]
    if path in inputs:
        require(path in candidates, "A_CURRENT_REFERENCE_PACKAGE_ITEM_OR_HASH_MISMATCH", path)
        matched = path
    elif (output_root / path).is_file() and candidates:
        require(file_hash(source_file(output_root / path, output_root)) == reference.get("sha256"),
                "A_CURRENT_COPY_HASH_MISMATCH", path)
        if reference.get("member_path"):
            candidates = [name for name in candidates if inputs[name]["member_path"] == reference["member_path"]]
        require(len(candidates) == 1, "A_CURRENT_COPY_ORIGIN_AMBIGUOUS", path)
        matched = candidates[0]
    else:
        require(not path.startswith("quick-packs/") and reference.get("source_type") not in {"current", "current_input", "current_package"},
                "A_CURRENT_REFERENCE_NOT_REGISTERED", path)
        return None
    require(reference.get("package_id", package_id) == package_id, "A_CURRENT_REFERENCE_PACKAGE_ITEM_OR_HASH_MISMATCH")
    expected = next(row for row in run["input_packages"] if row["package_id"] == package_id)
    require(reference.get("package_version", expected["version"]) == expected["version"], "A_CURRENT_REFERENCE_VERSION_MISMATCH")
    return matched


REFERENCE_FIELDS = {"evidence_refs", "source_refs", "evidence_ref", "source_ref", "history_evidence_refs", "current_evidence_refs"}


def collect_references(value: Any) -> list[dict[str, Any]]:
    references = []
    if isinstance(value, list):
        for item in value:
            references.extend(collect_references(item))
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in REFERENCE_FIELDS:
                rows = child if isinstance(child, list) else [child]
                require(all(isinstance(row, dict) and "path" in row and "sha256" in row for row in rows),
                        "INVALID_DECLARED_EVIDENCE_REFERENCE", key)
                references.extend(rows)
            else:
                references.extend(collect_references(child))
    return references


def dispatch(base: Path, study_date: str) -> Path:
    """One human-readable navigation list; creating it cannot send or upload."""
    day = base / "outbox" / date(study_date)
    projects_path = base / "web-projects/projects.json"
    projects = load(projects_path) if projects_path.exists() else {}
    # Project configuration is root-owned. Only read known URLs; do not invent
    # a project from a matching display name or overwrite the configuration.
    def project_url(project: str) -> str:
        rows = projects.get("projects", projects) if isinstance(projects, dict) else projects
        if isinstance(rows, dict):
            row = rows.get(project, rows.get("project-" + project, {}))
            return row.get("url", row.get("project_url", "尚未绑定")) if isinstance(row, dict) else "尚未绑定"
        if isinstance(rows, list):
            for row in rows:
                if row.get("project") == project or row.get("key") == project:
                    return row.get("url", row.get("project_url", "尚未绑定"))
        return "尚未绑定"

    rows = [load(path) for path in sorted((day / "runs").glob("*.json"))]
    text = [f"{study_date} 网页任务投递清单", "", "点击只导航。发送需当前批次明确授权；未知发送先恢复原任务，不创建替代 run_id。", "",
            "| 项目 | 学科 | 输入包 | GitHub source_commit | 目标日期 | 状态 | 网页 |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        url = row.get("task_url") or project_url(row["project"])
        link = f"[打开]({url})" if url.startswith("https://chatgpt.com/") else url
        text.append("| " + " | ".join([row["project"], row["subject"],
                    ", ".join(x["package_id"] for x in row["input_packages"]) or "无当前包",
                    row["source_commit"], row.get("target_date") or "—", row["stage"], link]) + " |")
    if not rows:
        text.extend(["", "尚无已准备任务。A、B、C均只处理数学；D按其独立入口处理。"])
    text.extend(["", "当前快速包", ""])
    for path in sorted((day / "quick-packs").glob("*/*.zip")):
        text.append(f"- [{path.name}]({path.resolve()})")
    output = day / "TODAY_DISPATCH.md"
    write(output, ("\n".join(text) + "\n").encode())
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE, help="Bridge workspace; fixtures use a temporary directory")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("pack", help="Pack one exact, already sealed native conversation package")
    create.add_argument("--subject", choices=SUBJECTS, required=True)
    create.add_argument("--package-dir", type=Path, required=True)
    create.add_argument("--repo", type=Path)
    create.add_argument("--question-id")
    lesson = sub.add_parser("export-lesson", aliases=["pack-lesson"], help="Export all real 408 lesson outcomes against the latest verified local snapshot")
    lesson.add_argument("--repo", type=Path, default=REPOS["408"])
    lesson.add_argument("--private-root", type=Path, default=Path.home() / ".codex/kaoyan-408-conversation-packages")
    lesson.add_argument("--output", required=True, type=Path)
    scope = lesson.add_mutually_exclusive_group(required=True)
    scope.add_argument("--run-id")
    scope.add_argument("--date")
    lesson.add_argument("--cutoff")
    lesson.add_argument("--quick-pack", action="append", type=Path, default=[])
    lesson.add_argument("--supplement", action="append", type=Path, default=[])
    scan = sub.add_parser("scan-packages", help="Inspect explicit ZIPs/folders, including multi-subject total ZIPs")
    scan.add_argument("inputs", nargs="+", type=Path)
    scan.add_argument("--max-unpacked-bytes", type=int, default=DEFAULT_LIMIT)
    bundle = sub.add_parser("bundle", help="Combine original package ZIPs without changing their bytes")
    bundle.add_argument("inputs", nargs="+", type=Path)
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--max-unpacked-bytes", type=int, default=DEFAULT_LIMIT)
    prepare = sub.add_parser("prepare", help="Pin the latest successful publication; this does not send")
    prepare.add_argument("--project", choices=PROJECT_SUBJECTS, required=True)
    prepare.add_argument("--subject", choices=SUBJECTS, required=True)
    prepare.add_argument("--date", required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--target-date")
    prepare.add_argument("--package", action="append", type=Path, default=[])
    prepare.add_argument("--c-handoff", type=Path,
                         help="Subject-owned math-c-handoff-v1 ZIP; valid only for project C math")
    prepare.add_argument("--after-a-run", type=Path)
    prepare.add_argument("--skip-a-reason", help="Explicit no-new-package declaration, retained in this run")
    listing = sub.add_parser("dispatch", help="Refresh TODAY_DISPATCH; links never send")
    listing.add_argument("--date", required=True)
    status = sub.add_parser("status", help="Read the existing run before any retry")
    status.add_argument("--run", required=True, type=Path)
    sent = sub.add_parser("record-send", help="Record an already authorized actual GUI send; does not send")
    sent.add_argument("--run", required=True, type=Path)
    sent.add_argument("--task-url", help="Actual task URL; may be omitted only with --uncertain before the GUI click")
    sent.add_argument("--authorization-ref", required=True)
    sent.add_argument("--uncertain", action="store_true")
    observed = sub.add_parser("record-web-read", help="Bind genuine CU/tool observations from the authorized task")
    observed.add_argument("--run", required=True, type=Path)
    observed.add_argument("--evidence-file", required=True, type=Path)
    observed.add_argument("--confirm-observed-cu", action="store_true",
                          help="Explicit local operator assertion after independently inspecting the actual task CU results, EOF, and originals; never set from a model report")
    inspection = sub.add_parser("inspect-web-read", help="Read-only mechanical inspection for any of the 12 project/subject cells; never prepares or imports a business run")
    inspection.add_argument("--context-file", required=True, type=Path)
    inspection.add_argument("--evidence-file", required=True, type=Path)
    inspection.add_argument("--confirm-observed-cu", action="store_true", help="Same explicit operator assertion as record-web-read")
    inspection.add_argument("--output", type=Path, help="Optional engineering inspection receipt; outside the business inbox")
    download = sub.add_parser("download-import", help="Preserve a download, validate it, then call the subject owner if ready")
    download.add_argument("--run", required=True, type=Path)
    download.add_argument("--zip", required=True, type=Path)
    download.add_argument("--max-unpacked-bytes", type=int, default=DEFAULT_LIMIT)
    review = sub.add_parser("record-review", help="Forward an explicit real answer through the existing subject writer")
    review.add_argument("--run", required=True, type=Path)
    review.add_argument("--payload-file", required=True, type=Path)
    decision = sub.add_parser("bind-a-decision", help="Verify an existing subject-owned decision/closeout; never apply advice")
    decision.add_argument("--run", required=True, type=Path)
    decision.add_argument("--native-receipt", required=True, type=Path, action="append", help="Repeat for the subject-owned receipts covering all input packages")
    disposition = sub.add_parser("record-a-disposition", help="Record rejected/needs_review locally even when admission is blocked; no formal writer")
    disposition.add_argument("--run", required=True, type=Path)
    disposition.add_argument("--decision", choices=("rejected", "needs_review"), required=True)
    disposition.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        bridge = Bridge(args.base)
        if args.command == "pack":
            result = pack(args.subject, args.package_dir.resolve(), args.repo or REPOS[args.subject], args.base, args.question_id)
        elif args.command in {"export-lesson", "pack-lesson"}:
            result = bridge.export_lesson(args.repo, args.private_root, output=args.output, run_id=args.run_id,
                    study_date=args.date, cutoff=args.cutoff, quick_packs=args.quick_pack, supplements=args.supplement)
        elif args.command in {"scan-packages", "bundle"}:
            with collect_inputs(args.inputs, max_bytes=args.max_unpacked_bytes) as found:
                result = [dict(subject=x["manifest"]["subject"], **lesson_binding(x)) if x["manifest"]["kind"] == "lesson_review_bundle"
                          else {"subject": x["manifest"]["subject"], "package_id": x["manifest"]["package_id"],
                           "version": x["manifest"]["version"], "zip_sha256": x["zip_sha256"]} for x in found]
                if args.command == "bundle":
                    write_zip({x["manifest"]["subject"] + "/" +
                               ("lesson-" + x["manifest"]["lesson_id"] if x["manifest"]["kind"] == "lesson_review_bundle" else x["manifest"]["package_id"]) + ".zip": x["zip_path"]
                               for x in found}, args.output)
                    result = {"path": str(args.output.resolve()), "sha256": file_hash(args.output), "packages": result}
        elif args.command == "prepare":
            result = bridge.prepare(args.project, args.subject, args.date, args.run_id, args.target_date,
                                    args.package, args.after_a_run, args.skip_a_reason, args.c_handoff)
        elif args.command == "dispatch":
            result = {"path": str(dispatch(args.base, args.date)), "sends": 0}
        elif args.command == "inspect-web-read":
            context = load(args.context_file)
            require(context.get("project") in PROJECT_SUBJECTS and context.get("subject") in PROJECT_SUBJECTS[context.get('project')],
                    "INVALID_INSPECTION_PROJECT_SUBJECT")
            for key in ("input_packages", "input_questions"):
                context.setdefault(key, [])
            result = bridge.inspect_web_read(context, args.evidence_file, args.confirm_observed_cu)
            if args.output:
                require(args.output.resolve().is_relative_to(bridge.base)
                        and not args.output.resolve().is_relative_to(bridge.base / "inbox"), "INSPECTION_RECEIPT_MUST_STAY_OUTSIDE_BUSINESS_INBOX")
                write(args.output, json_bytes(result))
        else:
            run = load(args.run)
            require(args.run.resolve() == bridge.run_path(run["date"], run["run_id"]), "RUN_PATH_IDENTITY_MISMATCH")
            if args.command == "status":
                result = run
            elif args.command == "record-send":
                result = bridge.record_send(run, args.task_url, args.authorization_ref, args.uncertain)
            elif args.command == "record-web-read":
                result = bridge.record_web_read(run, args.evidence_file, args.confirm_observed_cu)
            elif args.command == "download-import":
                result = bridge.download(run, args.zip, args.max_unpacked_bytes)
            elif args.command == "bind-a-decision":
                result = bridge.bind_a_decision(run, args.native_receipt)
            elif args.command == "record-a-disposition":
                result = bridge.record_a_disposition(run, args.decision, args.reason)
            else:
                require(run["stage"] in {"DOCUMENT_IMPORTED", "QUEUE_IMPORTED"}, "REVIEW_SOURCE_NOT_IMPORTED")
                payload = load(args.payload_file)
                require(payload.get("subject") == run["subject"] and payload.get("project") in
                        {run["project"], "project-" + run["project"]}, "REVIEW_SUBJECT_PROJECT_MISMATCH")
                require(payload.get("run_id", payload.get("task_id")) == run["run_id"]
                        and payload.get("source_commit", payload.get("library_version")) == run["source_commit"],
                        "REVIEW_RUN_VERSION_MISMATCH")
                root = Path(run["output_dir"])
                check_manifest(root)
                if run["project"] == "C":
                    items = load(root / "selected_questions.json")["questions"]
                else:
                    items = json_lines(root / ("selected_items.jsonl" if run["project"] == "D" else "questions.jsonl"))
                allowed = sorted({item["question_id"] for item in items})
                require(run["subject"] != "math" or payload.get("formal_id") in allowed, "REVIEW_QUESTION_NOT_IN_IMPORTED_SOURCE")
                payload["project"] = run["project"]
                if run["subject"] == "408":
                    require("allowed_formal_ids" not in payload or payload["allowed_formal_ids"] == allowed,
                            "REVIEW_ALLOWED_QUESTION_SET_MISMATCH")
                    payload["allowed_formal_ids"] = allowed
                forwarded = root.parent / ("review-input-" + publication.digest(payload) + ".json")
                write(forwarded, json_bytes(payload), immutable=True)
                result = bridge.owners.record_review(run["subject"], forwarded)
                run["last_review_result"] = {"input_path": str(forwarded), "input_sha256": file_hash(forwarded), "native_result": result}
                bridge.save_run(run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BridgeError, publication.PublicationError) as exc:
        print(json.dumps({"status": "BLOCKED", "code": exc.code, "detail": exc.detail,
                          "automatic_resends": 0, "originals_preserved": True}, ensure_ascii=False))
        return 2
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "BLOCKED", "code": "INVALID_OR_UNAVAILABLE_INPUT", "error_type": type(exc).__name__,
                          "automatic_resends": 0, "originals_preserved": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
