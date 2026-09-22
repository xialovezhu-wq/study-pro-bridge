"""Synthetic engineering fixtures only: no production study files, network or models."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_archives as archive
import study_bridge as bridge

COMMIT = "a" * 40


class FakePublished:
    def __init__(self, root: Path, subject: str = "math"):
        self.root, self.subject = root, subject
        root.mkdir(parents=True)
        self.files = {}
        self.add("history.md", "old-event 2026-08-01 mechanism old\ncurrent-event 2026-09-07 mechanism current\nfixture solution\n")
        self.add("problem.png", b"\x89PNG\r\n\x1a\nsynthetic isolated test source")
        self.add("Q-1.md", "engineering formal card Q-1")
        self.add("错题知识网络/教学投影/Q-1/learning-state.json", '{"engineering_fixture":true}')
        entries, history = {}, ["history.md"]
        if subject == "math":
            self.add("native-index.json", '{"cards":[{"id":"Q-1"}]}\n')
            entries, history = {"history_index": "native-index.json"}, "native-index.json"
        elif subject == "english":
            self.add("bank/master_bank.csv", "\ufeffid,word\nB-1,fixture\n")
            self.add("bank/sentence_patterns.md", "## SP-001 fixture\nsynthetic sentence pattern\n")
            entries = ["bank/master_bank.csv", "bank/sentence_patterns.md"]
        elif subject == "408":
            self.add("formal-nodes.md", "|ID|label|\n|---|---|\n|Q-1|fixture|\n")
            self.add("canonical.csv", '\ufeffexam_question_id,text\nQ-1,"fixture\nmultiline"\n')
            self.add("events.jsonl", '{"event_id":"E-1"}\n')
            entries = {"formal_nodes": "formal-nodes.md", "canonical_exam_index": "canonical.csv",
                       "learning_events": "events.jsonl", "historical_questions": "history.md"}
            history = [{"package_id": "CS408-history-fixture"}]
        self.manifest = {"schema_version": 1, "subject": subject, "repository": "xialovezhu-wq/kaoyan-" + subject,
                         "branch": "main", "coverage_status": "COMPLETE", "gaps": [], "history_index": history, "entrypoints": entries,
                         "files": [{"path": name, "sha256": archive.file_hash(path), "bytes": path.stat().st_size}
                                   for name, path in self.files.items()]}
        self.add("PUBLISHED_LIBRARY.json", bridge.json_bytes(self.manifest))
        for row in self.manifest["files"]:
            if row["path"] == "Q-1.md":
                row.update(kind="formal_card", formal_ids=["Q-1"])
            if row["path"] == "problem.png":
                row.update(kind="original_conversation_evidence", formal_ids=[f"Q-{i}" for i in range(5)], role="question")
            if row["path"].endswith("learning-state.json"):
                row.update(state_source_version="c" * 64)
        self.add("PUBLISHED_LIBRARY.json", bridge.json_bytes(self.manifest))
        self.receipt = {"status": "PUBLISHED", "subject": subject, "source_commit": COMMIT,
                        "manifest_sha256": archive.file_hash(self.files["PUBLISHED_LIBRARY.json"]),
                        "event_id": "fixture-publication", "web_read_status": "NOT_YET_VERIFIED"}
        self.pending = False

    def add(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode() if isinstance(value, str) else value)
        self.files[name] = path

    def latest(self, subject):
        bridge.require(not self.pending, "LOCAL_COMMIT_CLOUD_PENDING")
        return self.receipt

    def verify(self, subject, commit):
        bridge.require(subject == self.subject and commit == COMMIT, "SOURCE_COMMIT_MISMATCH")
        return self.receipt, self.manifest

    def contains(self, subject, earlier, later):
        return earlier == later == COMMIT

    def event_receipt(self, subject, event_id):
        return {**self.receipt, "event_id": event_id, "status": "PENDING" if self.pending else "PUBLISHED"}

    def materialize(self, subject, commit, name, destination, expected_sha256=None):
        self.verify(subject, commit)
        archive.copy_file(self.files[name], destination)
        bridge.require(not expected_sha256 or archive.file_hash(destination) == expected_sha256, "PUBLISHED_SOURCE_HASH_MISMATCH")
        return destination


class FakeOwners:
    def __init__(self, published):
        self.published, self.imports = published, []
        self.reject = None
        self.due_version = "due-v1"

    def sources(self, subject, run_id):
        return {"sources": [{"publish_path": path, "source_path": str(source)} for path, source in self.published.files.items()]}

    def due(self, target_date):
        return {"due_version": self.due_version}

    def import_queue(self, project, subject, payload, path):
        if self.reject:
            raise bridge.BridgeError(self.reject)
        if project == "D":
            bridge.require(payload["due_version"] == self.due_version, "D_DUE_VERSION_STALE")
        self.imports.append(payload)
        return {"status": "recorded" if project in {"B", "C"} else "IMPORTED_FOR_NATIVE_PREPARATION",
                "queue_id": None if project == "B" else "engineering-fixture-only", "learning_events_written": 0}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.base = self.root / "bridge"
        self.base.mkdir()
        self.published = FakePublished(self.root / "formal")
        self.owners = FakeOwners(self.published)
        self.bridge = bridge.Bridge(self.base, self.published, self.owners)

    def quick(self, subject="math", package_id="MATHPKG-fixture", text="all actual fixture turns"):
        directory = self.root / (subject + package_id + str(len(list(self.root.iterdir()))))
        directory.mkdir()
        (directory / "original").mkdir()
        (directory / "original/conversation.json").write_text(text)
        version = bridge.sha(text.encode())
        native = {"subject": "cs408" if subject == "408" else subject, "package_id": package_id,
                  "study_date": "2026-09-07", "canonical_sha256": version}
        bridge.write(directory / "original/manifest.json", bridge.json_bytes(native))
        bridge.write(directory / "original/source.json", b"{}")
        bridge.write(directory / "original/receipt.json", b"{}")
        files = {p.relative_to(directory).as_posix(): p for p in directory.rglob("*") if p.is_file()}
        manifest = {"schema_version": 1, "kind": "quick_pack", "subject": subject, "package_id": package_id,
                    "study_date": "2026-09-07", "version": version,
                    "native_manifest_sha256": archive.file_hash(directory / "original/manifest.json"),
                    "questions": [{"question_id": "Q-1", "conversation_path": "original/conversation.json"}],
                    "files": archive.inventory(files)}
        bridge.write(directory / "manifest.json", bridge.json_bytes(manifest))
        output = directory.with_suffix(".zip")
        archive.write_zip({p.relative_to(directory).as_posix(): p for p in directory.rglob("*") if p.is_file()}, output)
        return output

    def prepare(self, project="A", subject="math"):
        return self.bridge.prepare(project, subject, "2026-09-07", "fixture-" + project,
                                   "2026-09-08" if project in {"C", "D"} else None,
                                   [self.quick(subject)] if project == "A" else [],
                                   skip_a_reason="Engineering fixture has no new packages" if project != "A" else None)

    def sent(self, run):
        return self.bridge.record_send(run, "https://chatgpt.com/c/engineering-fixture", "isolated-unit-test-only")

    def observed(self, run):
        self.sent(run)
        directory = self.base / "observations"
        directory.mkdir(exist_ok=True)
        operations = []
        for path, source in self.published.files.items():
            saved = directory / path
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, saved)
            row = {"tool": "isolated-test-double", "result_locator": "engineering-fixture:result", "path": path,
                   "source_commit": COMMIT, "observation_file": str(saved), "observation_sha256": archive.file_hash(saved)}
            if path.endswith(".png"):
                row.update(kind="image", original_sha256=archive.file_hash(source))
            else:
                row["content_quote"] = source.read_text()
                row["truncated"] = False
                row.update(eof=True, eof_marker="ENGINEERING_FIXTURE_EOF")
                saved.write_text(row["content_quote"] + "\nENGINEERING_FIXTURE_EOF\n")
                row["observation_sha256"] = archive.file_hash(saved)
            operations.append(row)
        with self.bridge.input_materials(run) as current:
            for number, (path, item) in enumerate(current.items()):
                saved = directory / ("input-" + str(number) + ".txt")
                shutil.copyfile(item["local_source"], saved)
                quote = saved.read_text()
                saved.write_text(quote + "\nENGINEERING_FIXTURE_EOF\n")
                operations.append({"tool": "isolated-test-double", "result_locator": "engineering-fixture:input",
                                   "path": path, "source_commit": COMMIT, "observation_file": str(saved),
                                   "observation_sha256": archive.file_hash(saved), "content_quote": quote,
                                   "truncated": False, "eof": True, "eof_marker": "ENGINEERING_FIXTURE_EOF"})
        evidence = {key: run[key] for key in ("run_id", "subject", "source_commit", "task_url", "authorization_ref")}
        evidence.update(kind="observed_authorized_task_tool_reads", engineering_fixture=True, read_operations=operations)
        texts = {path: self.published.files[path].read_text() for path in bridge.scan_source_paths(run["subject"], self.published.manifest)}
        domains = bridge.scan_id_domains(run["subject"], self.published.manifest, texts)
        evidence["scan"] = {"source_commit": COMMIT, "domains": [{"name": name, "expected_count": len(ids),
            "expected_ids_sha256": bridge.ids_sha256(ids), "scanned_count": len(ids), "scanned_ids": ids,
            "scanned_ids_sha256": bridge.ids_sha256(ids)} for name, ids in domains.items()], "sources": [
                {"path": path, "sha256": bridge.sha(text.encode()), "ranges": [{
                    "start_line": 1 if text else 0, "end_line": len(text.splitlines()),
                    "operation_index": next(i for i, op in enumerate(operations) if op["path"] == path),
                    "content_sha256": bridge.sha(text.encode()), "eof": True,
                    "eof_marker": "ENGINEERING_FIXTURE_EOF"}]} for path, text in texts.items()]}
        target = directory / "evidence.json"
        bridge.write(target, bridge.json_bytes(evidence))
        self.bridge.record_web_read(run, target, confirm_observed_cu=True)

    def ref(self, name="history.md", quote=None):
        return {"path": name, "sha256": archive.file_hash(self.published.files[name]),
                **({"quote": quote} if quote else {})}

    def fixture_source(self, name, text):
        self.published.add(name, text)
        rows = self.published.manifest["files"]
        row = next((row for row in rows if row["path"] == name), None)
        if row is None:
            row = {"path": name}
            rows.append(row)
        row.update(sha256=archive.file_hash(self.published.files[name]), bytes=self.published.files[name].stat().st_size)
        self.published.add("PUBLISHED_LIBRARY.json", bridge.json_bytes(self.published.manifest))
        self.published.receipt["manifest_sha256"] = archive.file_hash(self.published.files["PUBLISHED_LIBRARY.json"])

    def output(self, run, documents):
        folder = self.root / "output-build"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir()
        for relative, content in documents.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else
                             bridge.json_bytes(content) if isinstance(content, (dict, list)) else content.encode())
        files = {p.relative_to(folder).as_posix(): p for p in folder.rglob("*") if p.is_file()}
        manifest = {"schema_version": 1, **{key: run[key] for key in ("project", "subject", "run_id", "source_commit")},
                    "files": archive.inventory(files)}
        for key in ("target_date", "input_packages", "due_version"):
            if key in run:
                manifest[key] = run[key]
        bridge.write(folder / "manifest.json", bridge.json_bytes(manifest))
        files["manifest.json"] = folder / "manifest.json"
        target = self.root / ("response-" + str(len(list(self.root.glob("response*")))) + ".zip")
        archive.write_zip(files, target)
        return target

    def advice(self, run):
        return self.output(run, {"advice.json": {"items": [{"package_id": run["input_packages"][0]["package_id"],
                "question_id": "Q-1", "recommendation": "fixture suggestion; local review required", "evidence_refs": [],
                "uncertainties": ["engineering fixture is not learner evidence"]}]}})

    def b_documents(self, current="2026-09-07", earlier="2026-08-01"):
        item = {"question_id": "Q-1", "current_event": {"event_id": "current-event", "event_date": current,
                "evidence_ref": self.ref(quote=f"current-event {current} mechanism current")},
                "historical_event": {"event_id": "old-event", "event_date": earlier,
                "evidence_ref": self.ref(quote=f"old-event {earlier} mechanism old")},
                "common_mechanism": {"explanation": "fixture comparison", "evidence_refs": []},
                "difference": "fixture difference"}
        item["common_mechanism"]["evidence_refs"] = [item["current_event"]["evidence_ref"], item["historical_event"]["evidence_ref"]]
        item["source_refs"] = [self.ref("problem.png")]
        return {"review_plan.json": {"question_ids": ["Q-1"]}, "questions.jsonl": json.dumps(item) + "\n",
                "answer_rubrics.jsonl": '{"question_id":"Q-1","rubric":"fixture"}\n',
                "historical_basis.jsonl": '{"question_id":"Q-1"}\n', "current_only_not_eligible.jsonl": "",
                "misconception_map.json": {"Q-1": "fixture mechanism"}, "summary.md": "fixture document"}

    def selected_documents(self, run, count=5):
        items, documents = [], {"rationale.md": "engineering fixture only"}
        for i in range(count):
            prefix = f"questions/Q-{i}/"
            item = {"question_id": f"Q-{i}", "source_label": "synthetic fixture",
                    "problem_path": prefix + "problem.png", "solution_path": prefix + "solution.md",
                    "history_path": prefix + "history_summary.md", "image_paths": [prefix + "problem.png"],
                    "source_refs": [self.ref("problem.png"), self.ref(quote="fixture solution")], "concept_ids": [f"K-{i}"],
                    "formal_card_sha256": "b" * 64, "state_source_version": "state-fixture", "public_value_reason": "fixture"}
            documents[prefix + "problem.png"] = self.published.files["problem.png"].read_bytes()
            documents[prefix + "solution.md"] = "fixture solution"
            documents[prefix + "history_summary.md"] = "fixture history"
            items.append(item)
        if run["project"] == "C":
            documents["selected_questions.json"] = {"questions": items}
        else:
            for i, item in enumerate(items):
                item.update(review_target_concept_ids=[f"K-{i}"], suppressed_same_concept_question_ids=[])
            documents.update({"due_concepts.json": {"concept_ids": [f"K-{i}" for i in range(count)]},
                              "selected_items.jsonl": "".join(json.dumps(x) + "\n" for x in items),
                              "unresolved.jsonl": "", "coverage_report.json": {"covered_concept_ids": [f"K-{i}" for i in range(count)],
                                                                                 "unresolved_concept_ids": []}})
        return documents

    def test_total_zip_preserves_originals_and_separates_subjects(self):
        math, english = self.quick(), self.quick("english", "EN-PKG-fixture")
        total = self.root / "total.zip"
        archive.write_zip({"math/a.zip": math, "english/e.zip": english}, total)
        with archive.packages([total]) as found:
            self.assertEqual({x["manifest"]["subject"] for x in found}, {"math", "english"})
            self.assertEqual({x["zip_sha256"] for x in found}, {archive.file_hash(math), archive.file_hash(english)})
        run = self.bridge.prepare("A", "math", "2026-09-07", "combined-input", packages=[total])
        self.assertEqual(len(run["input_packages"]), 1)
        self.assertEqual(run["web_read_status"], "web_read_unverified")

    def test_finder_container_metadata_does_not_drop_real_packages(self):
        quick = self.quick()
        target = self.root / "finder-total.zip"
        with zipfile.ZipFile(target, "w") as out:
            out.write(quick, "Today/math/input.zip")
            out.writestr("Today/.DS_Store", b"Finder metadata")
            out.writestr("__MACOSX/Today/math/._input.zip", b"AppleDouble metadata")
        with archive.packages([target]) as found:
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["zip_sha256"], archive.file_hash(quick))

    def test_cross_subject_folder_and_conflicting_package_id_rejected(self):
        english = self.quick("english", "EN-PKG-fixture")
        target = self.root / "cross.zip"
        archive.write_zip({"math/input.zip": english}, target)
        with self.assertRaisesRegex(bridge.BridgeError, "CROSS_SUBJECT"):
            with archive.packages([target]):
                pass

    def test_quick_wrapper_cannot_relabel_a_native_package_to_another_subject(self):
        original = self.quick()
        unpacked = archive.extract_zip(original, self.root / "relabel", archive.ArchiveBudget(self.root))
        manifest = archive.read_json(unpacked / "manifest.json")
        manifest.update(subject="english", package_id="EN-PKG-FORGED-LABEL")
        bridge.write(unpacked / "manifest.json", bridge.json_bytes(manifest))
        forged = self.root / "relabeled.zip"
        archive.write_zip({p.relative_to(unpacked).as_posix(): p for p in unpacked.rglob("*") if p.is_file()}, forged)
        with self.assertRaisesRegex(bridge.BridgeError, "QUICK_NATIVE_IDENTITY_MISMATCH"):
            with archive.packages([forged]):
                pass
        one, two = self.quick(text="one"), self.quick(text="two")
        with self.assertRaisesRegex(bridge.BridgeError, "DUPLICATE_PACKAGE_ID_CONFLICT"):
            with archive.packages([one, two]):
                pass

    def test_traversal_symlink_and_duplicate_paths_rejected(self):
        for names in [("../outside",), ("Case", "case")]:
            target = self.root / "bad.zip"
            with zipfile.ZipFile(target, "w") as out:
                for name in names:
                    out.writestr(name, "data")
            with self.assertRaises(bridge.BridgeError):
                archive.extract_zip(target, self.root / "extract", archive.ArchiveBudget(self.root))
            self.assertFalse((self.root / "outside").exists())
        target = self.root / "link.zip"
        with zipfile.ZipFile(target, "w") as out:
            entry = zipfile.ZipInfo("link")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            out.writestr(entry, "/etc/passwd")
        with self.assertRaisesRegex(bridge.BridgeError, "ARCHIVE_SPECIAL_FILE"):
            archive.extract_zip(target, self.root / "extract-link", archive.ArchiveBudget(self.root))

    def test_zip_budget_preserves_input_and_large_files_are_streamed(self):
        material = self.root / "large.bin"
        with material.open("wb") as out:
            for _ in range(12):
                out.write(b"engineering-fixture" * 55000)
        target = self.root / "large.zip"
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read forbidden")):
            archive.write_zip({"large.bin": material}, target)
            extracted = archive.extract_zip(target, self.root / "streamed", archive.ArchiveBudget(self.root))
            self.assertEqual(archive.file_hash(extracted / "large.bin"), archive.file_hash(material))
        before = archive.file_hash(target)
        with self.assertRaisesRegex(bridge.BridgeError, "ARCHIVE_LIMIT_EXCEEDED"):
            archive.extract_zip(target, self.root / "too-small", archive.ArchiveBudget(self.root, max_bytes=1024))
        self.assertEqual(before, archive.file_hash(target))

    def test_pack_preserves_full_conversation_and_is_immutable(self):
        source = self.root / "native"
        source.mkdir()
        native = {"subject": "math", "package_id": "MATHPKG-fixture", "study_date": "2026-09-07",
                  "canonical_sha256": "c" * 64, "missing_fields": []}
        for name, text in {"manifest.json": "native original manifest", "receipt.json": "native receipt",
                           "conversation.json": "begin\nall intermediate turns\nend", "source.json": "real source"}.items():
            (source / name).write_text(text)
        bridge.write(source / "manifest.json", bridge.json_bytes(native))
        paths = {p.name: p for p in source.iterdir()}
        with mock.patch.object(bridge, "native_package", return_value=(native, paths, {"conversation.json": "conversation"})):
            result = bridge.pack("math", source, self.root, self.base)
            with archive.packages([Path(result["path"])]) as found:
                self.assertEqual((found[0]["root"] / "original/conversation.json").read_bytes(),
                                 (source / "conversation.json").read_bytes())
            (source / "conversation.json").write_text("different new content")
            with self.assertRaisesRegex(bridge.BridgeError, "EXISTING_CONTENT_CONFLICT"):
                bridge.pack("math", source, self.root, self.base)

    def test_prepare_blocks_pending_or_missing_dependency(self):
        self.published.pending = True
        with self.assertRaisesRegex(bridge.BridgeError, "LOCAL_COMMIT_CLOUD_PENDING"):
            self.prepare()
        self.published.pending = False
        with self.assertRaisesRegex(bridge.BridgeError, "A_DEPENDENCY"):
            self.bridge.prepare("B", "math", "2026-09-07", "missing-a")

    def test_unknown_send_resume_never_resends(self):
        run = self.prepare()
        self.bridge.record_send(run, None, "unit-test-only", uncertain=True)
        self.assertEqual(run["stage"], "SEND_RESULT_UNKNOWN")
        self.bridge.record_send(run, "https://chatgpt.com/c/engineering-fixture", "unit-test-only", uncertain=True)
        again = self.prepare()
        self.assertEqual(again["stage"], "SEND_RESULT_UNKNOWN")
        self.assertEqual(again["task_url"], run["task_url"])
        with self.assertRaisesRegex(bridge.BridgeError, "RUN_ALREADY_BOUND"):
            self.bridge.record_send(run, "https://chatgpt.com/c/different", "unit-test-only")
        with self.assertRaisesRegex(bridge.BridgeError, "RUN_INPUT_VERSION_CONFLICT"):
            self.bridge.prepare("A", "math", "2026-09-07", run["run_id"], packages=[self.quick(text="changed input")])

    def test_download_before_web_evidence_does_not_write_learning(self):
        run = self.prepare()
        self.sent(run)
        output = self.advice(run)
        self.bridge.download(run, output)
        self.assertEqual(run["stage"], "DOWNLOADED_AWAITING_WEB_EVIDENCE")
        self.assertEqual(self.owners.imports, [])
        self.assertTrue((Path(run["output_dir"]).parent / "download.zip").exists())
        self.observed(run)
        self.bridge.download(run, output)
        self.assertEqual(run["stage"], "AWAITING_LOCAL_DECISION")
        self.assertEqual(self.owners.imports, [])

    def test_web_manifest_claim_cannot_replace_actual_observation(self):
        run = self.prepare()
        self.sent(run)
        evidence = self.base / "fake-claim.json"
        bridge.write(evidence, bridge.json_bytes({**run, "kind": "web_manifest_claim", "read_operations": []}))
        with self.assertRaisesRegex(bridge.BridgeError, "ACTUAL_WEB_TOOL_READS_REQUIRED"):
            self.bridge.record_web_read(run, evidence)

    def test_b_requires_real_earlier_event_and_imports_documents_only(self):
        run = self.prepare("B")
        self.observed(run)
        self.bridge.download(run, self.output(run, self.b_documents()))
        self.assertEqual(run["stage"], "DOCUMENT_IMPORTED")
        self.assertEqual(run["learning_events_written"], 0)
        self.assertEqual(len(self.owners.imports), 1)
        self.assertEqual(self.owners.imports[0]["project"], "B")
        self.assertIsNone(run["native_import_receipt"]["queue_id"])

    def test_b_current_event_cannot_be_its_own_earlier_history(self):
        run = self.prepare("B")
        self.observed(run)
        with self.assertRaisesRegex(bridge.BridgeError, "B_HISTORY_NOT_EARLIER"):
            self.bridge.download(run, self.output(run, self.b_documents(earlier="2026-09-07")))
        self.assertEqual(self.owners.imports, [])

    def test_b_legacy_history_without_event_id_can_use_real_date_and_quote(self):
        self.fixture_source("legacy.md", "2026-08-01 mechanism old; legacy record has no event identifier.")
        run = self.prepare("B")
        self.observed(run)
        documents = self.b_documents()
        item = json.loads(documents["questions.jsonl"])
        item["historical_event"] = {"event_id": None, "event_date": "2026-08-01",
                                    "evidence_ref": self.ref("legacy.md", "2026-08-01 mechanism old; legacy record has no event identifier.")}
        item["common_mechanism"]["evidence_refs"] = [item["current_event"]["evidence_ref"], item["historical_event"]["evidence_ref"]]
        documents["questions.jsonl"] = json.dumps(item) + "\n"
        self.bridge.download(run, self.output(run, documents))
        self.assertEqual(run["stage"], "DOCUMENT_IMPORTED")

    def test_b_null_cannot_hide_an_existing_event_id_outside_the_excerpt(self):
        self.fixture_source("events.jsonl", json.dumps({"event_id": "EVENT-OLD", "date": "2026-08-01", "note": "mechanism old"}) + "\n")
        run = self.prepare("B")
        self.observed(run)
        documents = self.b_documents()
        item = json.loads(documents["questions.jsonl"])
        item["historical_event"] = {"event_id": None, "event_date": "2026-08-01",
                                    "evidence_ref": self.ref("events.jsonl", '"date": "2026-08-01", "note": "mechanism old"')}
        item["common_mechanism"]["evidence_refs"] = [item["current_event"]["evidence_ref"], item["historical_event"]["evidence_ref"]]
        documents["questions.jsonl"] = json.dumps(item) + "\n"
        with self.assertRaisesRegex(bridge.BridgeError, "B_EXISTING_EVENT_ID_MUST_BE_USED"):
            self.bridge.download(run, self.output(run, documents))

    def test_c_exactly_five_images_native_validation_and_duplicate_download(self):
        run = self.prepare("C")
        self.observed(run)
        output = self.output(run, self.selected_documents(run))
        self.bridge.download(run, output)
        self.assertEqual(run["stage"], "QUEUE_IMPORTED")
        self.assertEqual(len(self.owners.imports[0]["questions"]), 5)
        self.assertTrue(Path(self.owners.imports[0]["questions"][0]["question_path"]).is_file())
        self.bridge.download(run, output)
        self.assertEqual(len(self.owners.imports), 1)

    def test_c_four_questions_block_before_native_import(self):
        run = self.prepare("C")
        self.observed(run)
        with self.assertRaisesRegex(bridge.BridgeError, "C_REQUIRES_FIVE"):
            self.bridge.download(run, self.output(run, self.selected_documents(run, 4)))
        self.assertEqual(self.owners.imports, [])

    def test_c_native_ineligible_rejection_is_not_overridden(self):
        run = self.prepare("C")
        self.observed(run)
        self.owners.reject = "NATIVE_DAY_0_7_EXCLUDED"
        with self.assertRaisesRegex(bridge.BridgeError, "NATIVE_DAY_0_7_EXCLUDED"):
            self.bridge.download(run, self.output(run, self.selected_documents(run)))
        self.assertNotEqual(run["stage"], "QUEUE_IMPORTED")

    def test_review_cli_rejects_another_question_before_native_writer(self):
        run = self.prepare("C")
        self.observed(run)
        self.bridge.download(run, self.output(run, self.selected_documents(run)))
        payload = {"subject": "math", "project": "C", "run_id": run["run_id"], "source_commit": COMMIT,
                   "formal_id": "Q-NOT-IN-SOURCE", "attempt_id": "engineering-attempt", "user_answer_text": "Engineering fixture only."}
        request = self.root / "review-request.json"
        bridge.write(request, bridge.json_bytes(payload))
        with mock.patch.object(bridge, "Bridge", return_value=self.bridge), mock.patch.object(self.owners, "record_review", create=True) as native:
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = bridge.main(["--base", str(self.base), "record-review", "--run", str(self.bridge.run_path(run["date"], run["run_id"])),
                                    "--payload-file", str(request)])
            self.assertEqual(code, 2)
            self.assertIn("REVIEW_QUESTION_NOT_IN_IMPORTED_SOURCE", captured.getvalue())
            native.assert_not_called()

    def test_temporary_bridge_payload_cannot_silently_write_production_subject(self):
        owners = bridge.Owners()
        path = self.root / "fixture-native-request.json"
        bridge.write(path, b"{}")
        with mock.patch.object(bridge, "invoke") as invoke:
            with self.assertRaisesRegex(bridge.BridgeError, "PRODUCTION_NATIVE_INPUT_REQUIRES_CANONICAL_BRIDGE_INBOX"):
                owners.record_review("408", path)
            with self.assertRaisesRegex(bridge.BridgeError, "PRODUCTION_NATIVE_INPUT_REQUIRES_CANONICAL_BRIDGE_INBOX"):
                owners.import_queue("C", "math", {}, path)
            invoke.assert_not_called()

    def test_d_due_coverage_and_native_drift_rejection(self):
        self.published = FakePublished(self.root / "formal408", "408")
        self.owners = FakeOwners(self.published)
        self.bridge = bridge.Bridge(self.base, self.published, self.owners)
        run = self.prepare("D", "408")
        self.observed(run)
        output = self.output(run, self.selected_documents(run, 2))
        self.owners.due_version = "new-learning-event-due-v2"
        with self.assertRaisesRegex(bridge.BridgeError, "D_DUE_VERSION_STALE"):
            self.bridge.download(run, output)
        self.assertEqual(self.owners.imports, [])

    def test_d_unresolved_coverage_blocks_before_native_import(self):
        self.published = FakePublished(self.root / "formal408", "408")
        self.owners = FakeOwners(self.published)
        self.bridge = bridge.Bridge(self.base, self.published, self.owners)
        run = self.prepare("D", "408")
        self.observed(run)
        documents = self.selected_documents(run, 2)
        documents["unresolved.jsonl"] = '{"concept_id":"K-2","reason":"missing original"}\n'
        with self.assertRaisesRegex(bridge.BridgeError, "D_REQUIRED_COVERAGE_UNRESOLVED"):
            self.bridge.download(run, self.output(run, documents))
        self.assertEqual(self.owners.imports, [])

    def test_d_nothing_due_is_skipped_before_any_authorized_send(self):
        self.published = FakePublished(self.root / "formal408", "408")
        self.owners = FakeOwners(self.published)
        self.bridge = bridge.Bridge(self.base, self.published, self.owners)
        with mock.patch.object(self.owners, "due", return_value={"due_version": "empty-due", "due_concept_ids": [],
                                                               "coverage": {"due_formal_node_ids": []}}):
            run = self.prepare("D", "408")
        self.assertEqual(run["stage"], "SKIPPED_NOTHING_DUE")
        with self.assertRaisesRegex(bridge.BridgeError, "RUN_SEND_STATE_CONFLICT"):
            self.sent(run)

    def test_related_local_change_rejects_older_output_without_resending(self):
        run = self.prepare("B")
        self.observed(run)
        current = self.root / "newer-local-history.md"
        current.write_text("new real local event after the published snapshot")
        with mock.patch.object(self.owners, "sources", return_value={"sources": [{"publish_path": "history.md", "source_path": str(current)}]}):
            with self.assertRaisesRegex(bridge.BridgeError, "RELATED_SOURCE_STALE"):
                self.bridge.download(run, self.output(run, self.b_documents()))
        self.assertEqual(self.owners.imports, [])

    def test_publisher_cache_locates_only_needed_sources_and_rejects_ambiguous_aliases(self):
        current = self.root / "current-card.md"
        current.write_text("current source")
        state = self.root / "publication-fixture"
        cache = {str(current): {"stage_stamps": {"cards/current.md": [1]}},
                 "/Volumes/NOT-MOUNTED/unrelated-history.json": {"stage_stamps": {"history/unrelated.json": [1]}}}
        bridge.write(state / "408/hash-cache.json", bridge.json_bytes(cache))
        published = bridge.Published(state)
        known = {"cards/current.md": {"kind": "formal_card", "sha256": archive.file_hash(current)}}
        result = published.current_sources("408", known, ["cards/current.md"])
        self.assertEqual(result["cards/current.md"]["source_path"], str(current))
        cache[str(self.root / "other.md")] = {"stage_stamps": {"cards/current.md": [2]}}
        bridge.write(state / "408/hash-cache.json", bridge.json_bytes(cache))
        with self.assertRaisesRegex(bridge.BridgeError, "PUBLISHED_SOURCE_MAPPING_AMBIGUOUS"):
            published.current_sources("408", known, ["cards/current.md"])

    def test_cli_scan_and_dispatch_are_real_entrypoints(self):
        path = self.quick()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bridge.main(["--base", str(self.base), "scan-packages", str(path)]), 0)
            self.assertEqual(bridge.main(["--base", str(self.base), "dispatch", "--date", "2026-09-07"]), 0)
        self.assertIn("MATHPKG-fixture", out.getvalue())
        self.assertTrue((self.base / "outbox/2026-09-07/TODAY_DISPATCH.md").is_file())

    def test_actual_cs408_owner_seals_only_temp_fixture_then_bridge_packs(self):
        repo = bridge.REPOS["408"]
        module = bridge.module_at("bridge_test_real_cs408_package", repo / "scripts/conversation_package_408.py")
        private = self.root / "isolated-cs408-packages"
        result = module.record_package(study_date="2026-09-07", session_id=None, item_id=None,
                                       source_id="ENGINEERING_FIXTURE_NOT_A_QUESTION",
                                       source={"engineering_fixture": True},
                                       conversation=[{"role": "user", "text": "Synthetic engineering fixture, not learner evidence."},
                                                     {"role": "assistant", "text": "Fixture-only response."}],
                                       frozen_feedback="Fixture only.", seal=True, private_root=private)
        packages = list(private.glob("2026-09-07/CS408-*"))
        self.assertEqual(len(packages), 1)
        output = bridge.pack("408", packages[0], repo, self.base)
        with archive.packages([Path(output["path"])]) as found:
            self.assertEqual(archive.file_hash(found[0]["root"] / "original/conversation.json"),
                             archive.file_hash(packages[0] / "conversation.json"))
        self.assertEqual(output["subject"], "408")

    def test_actual_english_owner_seals_only_temp_fixture_then_bridge_packs(self):
        repo = bridge.REPOS["english"]
        sys.path.insert(0, str(repo))
        from english_pipeline.packages import create_conversation_package
        package, _ = create_conversation_package(self.root / "isolated-english-intake", {
            "idempotency_key": "engineering-fixture-only", "segment_key": "engineering-fixture-only",
            "thread_ref": "engineering-fixture-not-real-study", "occurred_at": "2026-09-07T10:00:00+08:00",
            "source": {"source_kind": "engineering_fixture", "answer_exposure": "answer_free",
                       "source_id": "fixture", "question_id": "ENGINEERING-FIXTURE", "source_hash": "f" * 64},
            "conversation": [{"role": "user", "content": "Engineering test, not a learner answer."},
                             {"role": "assistant", "content": "Engineering test only."}]})
        output = bridge.pack("english", package, repo, self.base)
        with archive.packages([Path(output["path"])]) as found:
            self.assertEqual(archive.file_hash(found[0]["root"] / "original/conversation.json"),
                             archive.file_hash(package / "conversation.json"))

    def test_actual_math_owner_validates_only_temp_fixture_then_bridge_packs(self):
        actual_repo = bridge.REPOS["math"]
        module = bridge.module_at("bridge_test_real_math_package", actual_repo / "数学一回滚复习系统/scripts/quick_intake.py")
        isolated_repo = self.root / "isolated-math"
        module.REPO_ROOT = isolated_repo
        module.SOURCE_STAGING_ROOT = isolated_repo / "数学一回滚复习系统/快速入库来源"
        package_id = "MATHPKG-" + "e" * 24
        package = module.SOURCE_STAGING_ROOT / "2026-09-07" / package_id
        package.mkdir(parents=True)
        payload = {"study_date": "2026-09-07", "package_id": package_id, "timezone": "Asia/Shanghai",
                   "source_locator": "engineering-fixture-only", "source": {"source_locator": "engineering-fixture-only"},
                   "conversation": [{"sequence": 1, "role": "user", "text": "Engineering fixture, not real learner evidence."},
                                    {"sequence": 2, "role": "assistant", "text": "Engineering fixture only."}],
                   "artifacts": [], "missing_fields": ["question_image", "solution_image"]}
        documents = module.conversation_package_documents(payload)
        for name, document in zip(("manifest.json", "conversation.json", "source.json", "receipt.json"), documents):
            (package / name).write_bytes(module.canonical_json_bytes(document))
        # Only the module's explicit temporary repository roots differ. Its real
        # manifest/hash/receipt validator and Bridge pack implementation execute.
        with mock.patch.object(bridge, "module_at", return_value=module):
            result = bridge.pack("math", package, isolated_repo, self.base)
        with archive.packages([Path(result["path"])]) as found:
            self.assertEqual(archive.file_hash(found[0]["root"] / "original/conversation.json"),
                             archive.file_hash(package / "conversation.json"))

    def test_actual_408_due_and_import_cli_use_only_synthetic_temporary_repo(self):
        repo = self.root / "isolated-native408"
        repo.mkdir()
        (repo / "scripts").symlink_to(bridge.REPOS["408"] / "scripts", target_is_directory=True)
        native_tables = bridge.module_at("bridge_test_native408_tables", bridge.REPOS["408"] / "scripts/intake_lib_408.py")
        columns = native_tables.MASTER_COLS
        ids = [f"DS_2099_{number:03d}" for number in range(1, 455)]
        master = ["# Synthetic engineering fixture, never real study", "",
                  "|" + "|".join(columns) + "|", "|" + "|".join("---" for _ in columns) + "|"]
        tracks = ["# Synthetic engineering fixture", "", "|正式节点ID|主复习单元ID|原题轨状态|待补事项|", "|---|---|---|---|"]
        for number, fid in enumerate(ids):
            concept = "DS01-01" if number % 2 == 0 else "DS01-02"
            values = {"ID": fid, "来源ID": "VISUAL_PENDING", "科目": "数据结构", "主模块": "DS01",
                      "主知识点": concept + " fixture", "首次做题日期": "2026-08-01", "最近复做日期": "未记录"}
            master.append("|" + "|".join(values.get(column, "—") for column in columns) + "|")
            tracks.append(f"|{fid}|RU_{fid}|awaiting_primary_baseline|无|")
        (repo / "节点总表.md").write_text("\n".join(master) + "\n")
        (repo / "原题复做轨总表.md").write_text("\n".join(tracks) + "\n")
        ledger = repo / "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.touch()
        owners = bridge.Owners({**bridge.REPOS, "408": repo})
        due = owners.due("2026-09-08")
        self.assertEqual(due["formal_write_count"], 0)
        self.assertEqual(due["coverage"]["cohort_total"], 454)
        self.assertEqual(set(due["due_concept_ids"]), {"DS01-01", "DS01-02"})
        output = self.root / "native408-output"
        output.mkdir()
        for name in ("problem.md", "solution.md", "history.md"):
            (output / name).write_text("Synthetic engineering fixture, not real exam material.")
        selected = []
        for group in due["due_concepts"]:
            fid = group["formal_node_ids"][0]
            formal = repo / (fid + ".md")
            formal.write_text("# Engineering fixture only\n\n" + fid + " is not a real study event.\n")
            selected.append({"question_id": fid, "review_target_concept_ids": [group["concept_id"]],
                             "suppressed_same_concept_question_ids": group["formal_node_ids"][1:],
                             "source_label": "engineering fixture", "problem_path": "problem.md", "solution_path": "solution.md",
                             "history_path": "history.md", "image_paths": [],
                             "source_refs": [{"path": fid + ".md", "sha256": archive.file_hash(formal)}]})
        published = self.root / "native408-published-fixture.json"
        bridge.write(published, bridge.json_bytes({"subject": "408", "files": [
            {"path": row["question_id"] + ".md", "sha256": archive.file_hash(repo / (row["question_id"] + ".md")),
             "formal_ids": [row["question_id"]]} for row in selected]}))
        payload = {"subject": "408", "project": "D", "task_id": "engineering-fixture-D", "source_commit": COMMIT,
                   "target_date": "2026-09-08", "due_version": due["due_version"], "due_concept_ids": due["due_concept_ids"],
                   "selected_items": selected, "unresolved": [], "coverage": {"covered_concept_ids": due["due_concept_ids"],
                                                                             "unresolved_concept_ids": []},
                   "output_root": str(output), "fixture_publication_manifest": str(published)}
        result = owners.import_queue("D", "408", payload, self.root / "native408-input.json")
        self.assertEqual(result["status"], "IMPORTED_FOR_NATIVE_PREPARATION")
        self.assertEqual(result["learner_evidence_write_count"], 0)
        repeated = owners.import_queue("D", "408", payload, self.root / "native408-input.json")
        self.assertEqual(repeated["status"], "ALREADY_IMPORTED")
        self.assertEqual(result["receipt_sha256"], repeated["receipt_sha256"])
        self.assertEqual(ledger.read_bytes(), b"")

    def test_actual_english_adjudication_receipt_and_advice_attachment_binding(self):
        actual = bridge.REPOS["english"]
        sys.path.insert(0, str(actual))
        helpers = bridge.module_at("bridge_test_english_receipt_helpers", actual / "tests/english_pipeline/test_direct_packages.py")
        from english_pipeline.packages import create_conversation_package
        from english_pipeline.nightly import freeze_nightly
        from english_pipeline.constants import MASTER_HEADER, MASTERED_HEADER
        from english_pipeline.util import file_sha256
        repo = self.root / "isolated-english-writer"
        bank = repo / "bank"
        bank.mkdir(parents=True)
        (repo / "scripts").symlink_to(actual / "scripts", target_is_directory=True)
        (bank / "master_bank.csv").write_text(",".join(MASTER_HEADER) + "\n")
        (bank / "mastered_items.csv").write_text(",".join(MASTERED_HEADER) + "\n")
        (bank / "sentence_patterns.md").write_text("# Synthetic engineering fixture\n")
        state = repo / "intake"
        advice = self.root / "engineering-advice.zip"
        with zipfile.ZipFile(advice, "w") as out:
            out.writestr("advice.json", '{"engineering_fixture":true,"not_real_learning":true}')
        identity = {"source_id": "ENGINEERING-EN-SOURCE", "source_hash": "a" * 64, "source_kind": "article",
                    "answer_exposure": "answer_free", "sentence_id": "S-FIXTURE", "source_sentence": "Engineering fixture only."}
        def request(key, supplement=False):
            return {"idempotency_key": key, "segment_key": key, "thread_ref": "engineering:" + key,
                    "occurred_at": "2026-09-07T10:00:00+08:00",
                    "source": {**identity, **({"web_result": {"origin": "project-A", "task_id": "engineering-A", "source_commit": COMMIT}} if supplement else {})},
                    "conversation": [{"role": "user", "content": "Synthetic engineering local rejection; not a learner answer."},
                                     {"role": "assistant", "content": "Synthetic engineering fixture only."}],
                    "attachments": [{"path": str(advice), "role": "other_attachment"}] if supplement else []}
        _, original = create_conversation_package(state, request("engineering-original"))
        _, supplement = create_conversation_package(state, request("engineering-supplement", True))
        manifest_path, manifest = freeze_nightly(state, repo, study_date="2026-09-07")
        refs = [{"package_id": receipt["package_id"], "package_sha256": receipt["package_sha256"],
                 "pointer": "conversation.json#/messages/0", "kind": "user_message"} for receipt in (original, supplement)]
        refs.append({"package_id": supplement["package_id"], "package_sha256": supplement["package_sha256"],
                     "pointer": "manifest.json#/attachments/0", "kind": "other_attachment"})
        event = {"event_key": "engineering-local-decision", "kind": "adjudication", "origin": "project-A",
                 "task_id": "engineering-A", "source_commit": COMMIT, "source_id": identity["source_id"],
                 "unit_id": identity["sentence_id"], "bank_ids": [], "sentence_pattern_ids": [], "concept_ids": [],
                 "observed_at": "2026-09-07T10:00:00+08:00", "outcome": "rejected", "hint_dependence": "not_observed",
                 "reasoning_status": "not_observed", "user_response": "", "reasoning": "", "correction": ""}
        actions = {"schema_version": "english_sol_actions_v2", "action_set_id": "EN-ACTIONS-ENGINEERING-FIXTURE",
                   "batch_id": manifest["batch_id"], "batch_manifest_sha256": file_sha256(manifest_path),
                   "created_at": "2026-09-07T10:01:00+08:00", "producer": {"role": "sol_nightly_reviewer", "model": "test", "prompt_version": "engineering-fixture-only"},
                   "formal_write_count": 0, "formal_writeback": "none", "actions": [{"action_id": "ENGINEERING-REJECT",
                   "action_type": "learning_event_append", "reason": "Isolated engineering fixture, never production learning.",
                   "evidence_refs": refs, "event": event}], "unresolved": []}
        action_path = self.root / "english-fixture-actions.json"
        bridge.write(action_path, bridge.json_bytes(actions))
        helper = helpers.DirectConversationPackageTests()
        receipt_path, receipt = helper._apply_with_support_fixture(state, repo, manifest_path=manifest_path,
                                                                actions_path=action_path, authorization=manifest["batch_id"])
        run = {"run_id": "engineering-A", "subject": "english", "source_commit": COMMIT,
               "download_sha256": archive.file_hash(advice), "input_packages": [{"package_id": original["package_id"],
                                                                              "version": original["package_sha256"]}]}
        owners = bridge.Owners({**bridge.REPOS, "english": repo})
        result = owners.verify_a("english", receipt_path, run)
        self.assertEqual(result["decisions"][0]["outcome"], "rejected")
        self.assertEqual(result["publication_event_id"], receipt["receipt_id"])
        published = FakePublished(self.root / "english-publisher-fixture", "english")
        integration = bridge.Bridge(self.base, published, owners)
        run.update(project="A", date="2026-09-07", stage="AWAITING_LOCAL_DECISION")
        before_events = (repo / "bank/learning_events.jsonl").read_bytes()
        published.pending = True
        integration.bind_a_decision(run, receipt_path)
        self.assertEqual(run["stage"], "LOCAL_DECIDED_CLOUD_PENDING")
        published.pending = False
        integration.bind_a_decision(run, receipt_path)
        self.assertEqual(run["stage"], "LOCAL_DECIDED_PUBLISHED")
        self.assertEqual(before_events, (repo / "bank/learning_events.jsonl").read_bytes())
        run["input_packages"].append({"package_id": "EN-PKG-ENGINEERING-UNHANDLED", "version": "b" * 64})
        with self.assertRaisesRegex(bridge.BridgeError, "A_LOCAL_DECISION_SET_INCOMPLETE"):
            integration.bind_a_decision(run, [receipt_path])
        run["input_packages"].pop()
        run["download_sha256"] = "0" * 64
        with self.assertRaisesRegex(bridge.BridgeError, "NATIVE_A_DECISION_NOT_BOUND_TO_DOWNLOADED_ADVICE"):
            owners.verify_a("english", receipt_path, run)

    def test_408_mapping_requires_actual_advice_adjudication_and_proposal_hash(self):
        package_id = "CS408-20260907-ffffffffffffffff"
        output = self.root / "cs408-advice-fixture"
        output.mkdir()
        item = {"package_id": package_id, "question_id": package_id, "recommendation": "Engineering fixture only.",
                "evidence_refs": [], "uncertainties": []}
        bridge.write(output / "advice.json", bridge.json_bytes({"items": [item]}))
        run = {"run_id": "engineering-A", "source_commit": COMMIT, "output_dir": str(output),
               "input_packages": [{"package_id": package_id, "version": "c" * 64}]}
        checked = {"status": "verified", "subject": "408", "formal_complete": True, "task_id": run["run_id"],
                   "source_commit": COMMIT, "package_id": package_id, "evidence_sha256": "c" * 64,
                   "proposal_sha256": bridge.publication.digest(item), "advice_adjudication": "modified",
                   "publication_event_id": "engineering-terminal-only", "terminal_sha256": "d" * 64,
                   "decision": "redo", "formal_id": "DS_2099_001"}
        owners = bridge.Owners()
        with mock.patch.object(bridge, "invoke", return_value=checked):
            result = owners.verify_a("408", self.root / "fixture-verify-request.json", run)
            self.assertEqual(result["decisions"][0]["outcome"], "modified")
            checked["advice_adjudication"] = None
            with self.assertRaisesRegex(bridge.BridgeError, "NATIVE_A_ADVICE_ADJUDICATION_MISSING"):
                owners.verify_a("408", self.root / "fixture-verify-request.json", run)
            checked["advice_adjudication"] = "modified"
            checked["proposal_sha256"] = "0" * 64
            with self.assertRaisesRegex(bridge.BridgeError, "NATIVE_A_DECISION_NOT_BOUND_TO_DOWNLOADED_ADVICE"):
                owners.verify_a("408", self.root / "fixture-verify-request.json", run)

    def test_actual_math_B_C_registration_eligibility_and_outcome_in_temporary_repo(self):
        actual = bridge.REPOS["math"]
        helpers = bridge.module_at("bridge_test_math_warmup_helpers", actual / "tests/test_scheduler_warmup_v5.py")
        fixture = helpers.SchedulerWarmupV5Tests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.repo
        scheduler = bridge.module_at("bridge_test_real_math_review_scheduler", actual / "数学一回滚复习系统/scripts/scheduler.py")
        review = bridge.module_at("bridge_test_real_math_review", actual / "数学一回滚复习系统/scripts/math_review.py")
        units, candidates, identities = [], [], []
        for number in range(9901, 9906):
            identity = f"GS-{number}"
            path = fixture.write_card(identity, wrong_history=["2026-08-01 engineering fixture"], source="工程隔离样本", private_text="Engineering fixture, not a real question.")
            unit = fixture.unit(identity, first_date="2026-06-01", recurrence_date="2026-08-01")
            unit.update({"类型": "错题", "下次复习日期": "2026-12-01"})
            units.append(unit)
            candidates.append(fixture.candidate(identity, path, source="工程隔离样本"))
            identities.append(identity)
        bridge.write(root / "数学一回滚复习系统/复习单元.json", bridge.json_bytes(units))
        log = root / "数学一回滚复习系统/复习记录.jsonl"
        log.touch()
        rows, questions = [], []
        for identity in identities:
            current = review.state.load_state(root, identity)
            card = root / current["formal_card_path"]
            rows.append({"path": current["formal_card_path"], "kind": "formal_card", "formal_ids": [identity], "sha256": archive.file_hash(card)})
            rows.append({"path": f"错题知识网络/教学投影/{identity}/learning-state.json", "kind": "learning_state", "formal_ids": [identity],
                         "sha256": bridge.sha(review.state.canonical(current)), "state_source_version": current["source_version"]})
            item = {"formal_id": identity, "formal_card_sha256": current["formal_card_sha256"],
                    "state_source_version": current["source_version"], "public_value_reason": "engineering fixture only"}
            for role in ("question", "solution"):
                path = root / f"{role}_{identity}.png"
                path.write_bytes(b"\x89PNG\r\n\x1a\nEngineering fixture " + (role + identity).encode())
                item[role + "_path"] = str(path)
                rows.append({"path": path.name, "kind": "original", "role": role, "formal_ids": [identity], "sha256": archive.file_hash(path)})
            questions.append(item)
        manifest = root / "published-fixture.json"
        bridge.write(manifest, bridge.json_bytes({"subject": "math", "files": rows}))
        proof = {"verified": True, "status": "PUBLISHED", "subject": "math", "repository": "xialovezhu-wq/kaoyan-math",
                 "branch": "main", "source_commit": COMMIT, "resolved_manifest_path": str(manifest), "manifest_sha256": archive.file_hash(manifest)}
        common = {"schema_version": "math-review-source-v1", "subject": "math", "repository": "xialovezhu-wq/kaoyan-math",
                  "branch": "main", "source_commit": COMMIT, "target_date": "2026-09-08", "questions": questions}
        legacy = fixture.legacy(candidates=candidates, units=units)
        native = SimpleNamespace(**legacy)
        native.LOG_PATH = log
        native.score_attempt_identity = scheduler.score_attempt_identity
        native.safe_parse_date = scheduler.safe_parse_date
        native.load_review_logs = lambda: json.loads("[" + ",".join(log.read_text().splitlines()) + "]") if log.read_text().strip() else []
        native.append_log = lambda event: legacy["append_jsonl_entry"](log, event)
        b = review.import_source(root, {**common, "project": "B", "run_id": "engineering-math-B"}, native, verifier=lambda _: proof)
        self.assertIsNone(b["queue_id"])
        self.assertEqual(log.read_bytes(), b"")
        c_document = {**common, "project": "C", "run_id": "engineering-math-C"}
        c = review.import_source(root, c_document, native, verifier=lambda _: proof)
        self.assertTrue(c["queue_id"])
        self.assertEqual(review.import_source(root, c_document, native, verifier=lambda _: proof)["status"], "noop")
        self.assertEqual(log.read_bytes(), b"")
        shown = review.show_source(root, c["run_id"], native, helpers.date(2026, 9, 8))
        self.assertEqual(shown["status"], "DISPLAY_READY")
        self.assertEqual(shown["delivery_events_written"], 1)
        public_text = Path(shown["display_path"]).read_text()
        registered = review.read_source(root, c["run_id"])
        self.assertTrue(all(row["solution_file"] not in public_text for row in registered["questions"]))
        self.assertNotIn("engineering fixture only", public_text)
        self.assertEqual(review.show_source(root, c["run_id"], native, helpers.date(2026, 9, 8))["delivery_events_written"], 0)
        self.assertEqual(review.show_source(root, b["run_id"], native, helpers.date(2026, 9, 8))["status"], "B_DOCUMENT_REQUIRES_LOCAL_QUESTION_SELECTION")
        outcome = {"schema_version": "math-review-outcome-v1", "subject": "math", "project": "C", "run_id": c["run_id"],
                   "source_commit": COMMIT, "formal_id": questions[0]["formal_id"], "formal_card_sha256": questions[0]["formal_card_sha256"],
                   "state_source_version": questions[0]["state_source_version"], "date": "2026-09-08", "attempt_id": "engineering-only-attempt",
                   "score": 2, "user_answer_text": "Synthetic engineering observation, not a real learner answer.",
                   "correction_text": "Synthetic engineering correction only.", "hint_dependency": "l2", "independent_correct": False,
                   "evidence_origin": "user_observed", "queue_id": c["queue_id"], "queue_item_id": c["questions"][0]["queue_item_id"]}
        with self.assertRaisesRegex(review.ReviewError, "score_and_independence_evidence_disagree"):
            review.normalize_outcome(root, {**outcome, "score": 4})
        result = review.record_outcome(root, outcome, native)
        self.assertEqual(result["new_learning_events"], 1)
        self.assertFalse(result["advances_long_term"])
        self.assertEqual(result["postcommit"]["publication"]["status"], "ISOLATED_NO_PUBLICATION")
        again = review.record_outcome(root, outcome, native)
        self.assertEqual(again["status"], "noop")
        self.assertEqual(len(native.load_review_logs()), 1)

    def test_actual_math_A_receipt_backend_maps_without_inferring_acceptance(self):
        actual = bridge.REPOS["math"]
        sys.path.insert(0, str(actual / "tests"))
        helpers = bridge.module_at("bridge_test_math_advice_helpers", actual / "tests/test_math_review.py")
        fixture = helpers.AdviceDecisionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = helpers.review.record_advice_decision(fixture.root, fixture.payload, fixture.quick)
        receipt = Path(result["decision_receipt_path"])
        owners = bridge.Owners({**bridge.REPOS, "math": fixture.root})

        def isolated_verify(command, repo):
            self.assertEqual(command[-3:], ["verify-advice-decision", "--run-id", fixture.run["run_id"]])
            self.assertEqual(repo, fixture.root)
            return helpers.review.verify_advice_decision(fixture.root, fixture.run["run_id"], fixture.quick)

        with mock.patch.object(bridge, "invoke", side_effect=isolated_verify):
            checked = owners.verify_a("math", receipt, fixture.run)
            self.assertEqual(checked["decisions"][0]["outcome"], "rejected")
            self.assertEqual(checked["publication_event_id"], result["publication_event_id"])
            self.assertEqual(checked["native_receipt_sha256"], archive.file_hash(receipt))
            wrong = {**fixture.run, "download_sha256": "0" * 64}
            with self.assertRaisesRegex(bridge.BridgeError, "NATIVE_A_TASK_ADVICE_BINDING_MISMATCH"):
                owners.verify_a("math", receipt, wrong)
        self.assertFalse(fixture.fixture.events_path.exists())


if __name__ == "__main__":
    unittest.main()
