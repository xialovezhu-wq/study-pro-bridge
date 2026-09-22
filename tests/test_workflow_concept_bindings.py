"""Temporary source/ZIP fixtures; no real queues, learner writes or publisher."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
import zipfile
from unittest import mock

import bridge_archives as archives
import study_bridge as bridge
import study_mcp as mcp


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class ConceptWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, relative, value):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value if isinstance(value, bytes) else canonical(value))
        return target

    def math_fixture(self):
        names = ["数学一回滚复习系统/" + n for n in
                 ("复习单元.json", "复习记录.jsonl", "学习进度.json", "学习前5题记录.jsonl", "快速入库事件.jsonl")]
        names += ["错题知识网络/错题卡/GS-001.md",
                  "数学一回滚复习系统/知识点复盘/正式会话/fixture/session.json"]
        scripts = ["数学一回滚复习系统/scripts/scheduler.py", "错题知识网络/scripts/math_concept_index.py"]
        files = {}
        for name in names + scripts:
            path = self.write(name, b"fixture")
            files[name] = {"path": name, "sha256": archives.file_hash(path)}
        personal = {"version": "personal-v1", "formal_version": "formal-v1"}
        index = "错题知识网络/个人知识点索引/index.sqlite3"
        index_path = self.write(index, b"fixture database")
        contract = {"schema": "native-math-workflow-bindings-v1", "personalization": personal,
                    "installed_index": {"path": index, "sha256": archives.file_hash(index_path), "published": False},
                    "inputs": [files[n] for n in names], "components": [files[n] for n in scripts],
                    "component_directories": ["数学一回滚复习系统/scripts", "错题知识网络/scripts"]}
        contract["version"] = hashlib.sha256(canonical(contract)).hexdigest()
        snap = SimpleNamespace(files=files, library={"native_math_workflow": contract, "personalization": personal},
                               commit="a" * 40, identity=lambda: {"source_commit": "a" * 40})
        store = object.__new__(mcp.LibraryStore)
        store.published = SimpleNamespace(current_sources=lambda subject, files, paths:
            {p: {"source_path": str(self.root / p)} for p in paths})
        store.lock = threading.RLock()
        store.workflow_cache = {("C", snap.commit, "2026-09-08"): {"candidates": [{"id": "fixture"}]}}
        store.snapshot = lambda *args: snap
        return store, snap, personal, names[-1]

    def test_old_snapshot_rejects_changed_concept_even_on_cache_hit(self):
        store, snap, personal, source = self.math_fixture()
        with mock.patch.dict(bridge.REPOS, math=self.root), mock.patch.object(mcp.study_personalization, "native_call",
                return_value={"status": "ready", **personal}):
            self.assertEqual(1, store.workflow("C", "math", "2026-09-08")["result"]["collections"]["candidates"])
            self.write(source, b"new actual point fact")
            with self.assertRaisesRegex(archives.BridgeError, "NATIVE_MATH_DIFFERS_FROM_PUBLICATION"):
                store.workflow("C", "math", "2026-09-08")

    def test_verified_hash_cache_reuses_bytes_but_rejects_restored_mtime_edit(self):
        store, snap, personal, source = self.math_fixture()
        with mock.patch.dict(bridge.REPOS, math=self.root), mock.patch.object(mcp.study_personalization, "native_call",
                return_value={"status": "ready", **personal}):
            store._math_bindings(snap)
            with mock.patch.object(mcp, "file_hash", wraps=mcp.file_hash) as hash_call:
                store._math_bindings(snap)
                self.assertEqual(hash_call.call_count, 0)
            path = self.root / source
            before = path.stat()
            path.write_bytes(b"changed")  # Same length as b"fixture".
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(archives.BridgeError, "NATIVE_MATH_DIFFERS_FROM_PUBLICATION"):
                store._math_bindings(snap)

    def test_verified_hash_cache_rejects_atomic_replacement_with_old_mtime(self):
        store, snap, personal, source = self.math_fixture()
        with mock.patch.dict(bridge.REPOS, math=self.root), mock.patch.object(mcp.study_personalization, "native_call",
                return_value={"status": "ready", **personal}):
            store._math_bindings(snap)
            path = self.root / source
            before = path.stat()
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(b"changed")
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, path)
            with self.assertRaisesRegex(archives.BridgeError, "NATIVE_MATH_DIFFERS_FROM_PUBLICATION"):
                store._math_bindings(snap)

    def test_workflow_file_compresses_without_losing_native_bytes_or_hash(self):
        store, snap, _, _ = self.math_fixture()
        native = {'candidate_bundle_sha256':'b' * 64,
                  'eligible_candidates':[{'id':'GS-001','evidence':'完整证据' * 30000}]}
        key = ('C', snap.commit, '2026-09-08')
        store.workflow_cache[key] = native
        store.workflow = lambda *args, **kwargs: {
            'source_commit':snap.commit, 'target_date':'2026-09-08',
            'result':{'candidate_bundle_sha256':'b' * 64, 'collections':{'eligible_candidates':1}}}
        meta, raw, mime, uri = store.workflow_file('C','math','2026-09-08')
        self.assertEqual(mime, 'application/zip')
        self.assertTrue(uri.endswith('.zip'))
        self.assertEqual(meta['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(meta['native_candidate_bundle_sha256'], 'b' * 64)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(archive.namelist(), ['candidate-bundle.json'])
            complete = archive.read('candidate-bundle.json')
        self.assertEqual(complete, mcp.encoded(native))
        self.assertEqual(meta['content_sha256'], hashlib.sha256(complete).hexdigest())
        self.assertEqual(meta['content_bytes'], len(complete))
        self.assertLess(len(raw), len(complete) // 10)

    def test_small_workflow_file_preserves_json_resource_contract(self):
        store, snap, _, _ = self.math_fixture()
        store.workflow = lambda *args, **kwargs: {'source_commit':snap.commit,
            'target_date':'2026-09-08','result':{'collections':{'candidates':1}}}
        meta, raw, mime, uri = store.workflow_file('C','math','2026-09-08')
        self.assertEqual(mime, 'application/json')
        self.assertEqual(meta['compression'], 'none')
        self.assertEqual(meta['sha256'], meta['content_sha256'])
        self.assertEqual(json.loads(raw), {'candidates':[{'id':'fixture'}]})

    def test_algorithm_and_personalization_changes_are_not_old_snapshot_results(self):
        store, snap, personal, source = self.math_fixture()
        with mock.patch.dict(bridge.REPOS, math=self.root), mock.patch.object(mcp.study_personalization, "native_call",
                return_value={"status": "ready", **personal, "version": "personal-v2"}):
            with self.assertRaisesRegex(archives.BridgeError, "PERSONALIZATION_DIFFERS"):
                store._math_bindings(snap)
        self.write("错题知识网络/scripts/new_dependency.py", b"new")
        with mock.patch.dict(bridge.REPOS, math=self.root):
            with self.assertRaisesRegex(archives.BridgeError, "ALGORITHM_SET_CHANGED"):
                store._math_bindings(snap)
        snap.library.pop("native_math_workflow")
        with self.assertRaisesRegex(archives.BridgeError, "BINDINGS_NOT_PUBLISHED"):
            store._math_bindings(snap)

    def test_database_is_bound_without_becoming_a_published_binary(self):
        store, snap, personal, source = self.math_fixture()
        index = snap.library["native_math_workflow"]["installed_index"]["path"]
        self.assertNotIn(index, snap.files)
        self.write(index, b"changed index")
        with mock.patch.dict(bridge.REPOS, math=self.root):
            with self.assertRaisesRegex(archives.BridgeError, "INDEX_DIFFERS_FROM_PUBLICATION"):
                store._math_bindings(snap)

    def test_compact_published_workflow_entry_reads_hash_bound_sidecar(self):
        store, snap, personal, source = self.math_fixture()
        contract = snap.library["native_math_workflow"]
        path = "support/cmath/manifest.json"
        file = self.write(path, contract)
        sha = archives.file_hash(file)
        snap.files[path] = {"path": path, "sha256": sha}
        snap.text = lambda name: (self.root / name).read_text()
        snap.library["native_math_workflow"] = {"schema": contract["schema"], "version": contract["version"],
                                               "path": path, "sha256": sha}
        with mock.patch.dict(bridge.REPOS, math=self.root), mock.patch.object(mcp.study_personalization, "native_call",
                return_value={"status": "ready", **personal}):
            self.assertTrue(store._math_bindings(snap))
        snap.library["native_math_workflow"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(archives.BridgeError, "MANIFEST_BINDING_MISMATCH"):
            store._math_contract(snap)

    def test_d_snapshot_replay_returns_full_context_and_legacy_remains_readable(self):
        support = {"components": [{"published_path": "support/d408/read_only_due.py"}], "inputs": []}
        paths = {"support/d408/manifest.json": self.write("support/d408/manifest.json", support),
                 "PUBLISHED_LIBRARY.json": self.write("PUBLISHED_LIBRARY.json", {}),
                 "support/d408/read_only_due.py": self.write("support/d408/read_only_due.py", b"# fixture")}
        snap = SimpleNamespace(text=lambda p: paths[p].read_text(), path=lambda p: paths[p], library={"file_inventory": []})
        old = {"due_version": "v1", "formal_write_count": 0, "learner_evidence_write_count": 0}
        full = {**old, "concept_priority_version": "cp-v1", "concept_requests": [{"request_id": "CR-1"}],
                "review_concept_ids": ["OS.PAGE"]}
        def runner(args, **kwargs):
            output = Path(args[args.index("--output-dir") + 1])
            output.mkdir()
            (output / "native-due-run.json").write_bytes(canonical(old))
            if new_support:
                (output / "due-context.json").write_bytes(canonical(full))
            return SimpleNamespace(returncode=0, stderr="")
        store = object.__new__(mcp.LibraryStore)
        for new_support in (True, False):
            with mock.patch.object(mcp.subprocess, "run", side_effect=runner):
                self.assertEqual(full if new_support else old, store._calculate(snap, "D", "2026-09-08"))

    def test_d_native_collections_paginate_and_summary_never_embeds_full_arrays(self):
        due = [{"formal_node_id": f"OS_2024_{i:03d}", "source_refs": [{"path": "fixture", "sha256": "a" * 64}]}
               for i in range(5)]
        raw = {"due_version": "due-v1", "concept_requests": [{"request_id": "R"}],
               "coverage": {"cohort_total": 5, "due_formal_node_ids": [r["formal_node_id"] for r in due], "blocked": []},
               "native_due_run": {"run_sha256": "due-v1", "due_items": due, "blocked": [], "included": ["A", "B"],
                                  "source_bindings": {"files": [{"path": "fixture", "sha256": "a" * 64}]}}}
        store = object.__new__(mcp.LibraryStore)
        snap = SimpleNamespace(commit="a" * 40, identity=lambda: {"source_commit": "a" * 40})
        store.snapshot = lambda *args: snap
        store.lock = threading.RLock()
        store.workflow_cache = {("D", snap.commit, "2026-09-08"): raw}
        first = store.workflow("D", "408", "2026-09-08", collection="due_items", limit=2)
        second = store.workflow("D", "408", "2026-09-08", collection="due_items", offset=first["next_offset"], limit=2)
        self.assertEqual(due[:2], first["records"])
        self.assertEqual(due[2:4], second["records"])
        self.assertEqual(5, first["total_records"])
        summary = store.workflow("D", "408", "2026-09-08", limit=2)["result"]
        self.assertEqual(5, summary["collections"]["due_items"])
        self.assertEqual(2, summary["collections"]["included"])
        self.assertEqual("due-v1", summary["native_due_run"]["run_sha256"])
        self.assertEqual({"collection": "due_items", "count": 5}, summary["native_due_run"]["due_items"])
        def assert_no_arrays(value):
            self.assertNotIsInstance(value, list)
            if isinstance(value, dict):
                for child in value.values():
                    assert_no_arrays(child)
        assert_no_arrays(summary)
        self.assertEqual(due, raw["native_due_run"]["due_items"])
        bindings = store.workflow("D", "408", "2026-09-08", collection="files", limit=2)
        self.assertEqual(raw["native_due_run"]["source_bindings"]["files"], bindings["records"])

    def test_c_zip_payload_preserves_v2_through_subject_validation(self):
        math_root = bridge.REPOS["math"]
        with mock.patch.object(sys, "path", [str(math_root / "数学一回滚复习系统/scripts"),
                                            str(math_root / "错题知识网络/scripts"), *sys.path]):
            import math_review
        key = "MATH-CONCEPT-" + "a" * 24
        ref = {"path": "point/session.json", "sha256": "b" * 64}
        basis = {"schema_version": "warmup-candidate-bundle-v2", "candidate_bundle_sha256": "d" * 64,
                 "concept_evidence_versions": [{"concept_key": key, "point_source_version": "c" * 64, "source_refs": [ref]}]}
        questions = [{"question_id": f"GS-{i:03}", "anchor_id": key, "formal_card_sha256": "e" * 64,
                      "state_source_version": "f" * 64, "public_value_reason": "fixture", "problem_path": "problem.md",
                      "solution_path": "solution.md"} for i in range(1, 6)]
        files = {name: self.write(name, value) for name, value in {
            "selected_questions.json": {"questions": questions, "selection_basis": basis},
            "problem.md": b"fixture question", "solution.md": b"fixture solution"}.items()}
        archive = self.root / "C.zip"
        archives.write_zip(files, archive)
        extracted = archives.extract_zip(archive, self.root / "unzipped", archives.ArchiveBudget(self.root))
        run = {"project": "C", "subject": "math", "run_id": "C-fixture", "source_commit": "a" * 40,
               "target_date": "2026-09-08", "output_dir": str(extracted)}
        payload = object.__new__(bridge.Bridge).queue_payload(run)
        normalized, _ = math_review.normalized_source_request(self.root, payload)
        self.assertEqual(basis, normalized["selection_basis"])
        point = {"concept_key": key, "formal_ids": ["GS-001"], "point_source_version": "c" * 64,
                 "events": [{"source_refs": [ref]}]}
        bundle = {"concept_evidence": [point], "anchors": [{"anchor_id": key, "anchor_kind": "concept_review"}]}
        with mock.patch.object(sys, "path", [str(math_root / "数学一回滚复习系统/scripts"), *sys.path]):
            proof = math_review.validate_c_selection_basis(normalized, bundle, {ref["path"]: ref})
        self.assertEqual([key], proof["concept_keys"])

    def test_d_zip_payload_preserves_context_supplements_and_refuses_conflicts(self):
        context = {"concept_priority_version": "cp-v1", "concept_requests": [{"request_id": "CR-1"}],
                   "review_concept_ids": ["OS.PAGE"]}
        documents = {"manifest.json": context, "due_concepts.json": {"concept_ids": []},
                     "selected_items.jsonl": b"", "unresolved.jsonl": b"",
                     "supplemental_items.jsonl": canonical({"request_id": "CR-1", "question_id": "OS_2024_001"}) + b"\n",
                     "coverage_report.json": {"covered_concept_ids": ["OS.PAGE"], "unresolved_concept_ids": []}}
        files = {name: self.write(name, value) for name, value in documents.items()}
        archive = self.root / "D.zip"
        archives.write_zip(files, archive)
        extracted = archives.extract_zip(archive, self.root / "unzipped", archives.ArchiveBudget(self.root))
        run = {"project": "D", "subject": "408", "run_id": "D-fixture", "source_commit": "a" * 40,
               "target_date": "2026-09-08", "due_version": "due-v1", "output_dir": str(extracted)}
        payload = object.__new__(bridge.Bridge).queue_payload(run)
        self.assertEqual(context, {k: payload[k] for k in context})
        self.assertEqual("CR-1", payload["supplemental_items"][0]["request_id"])
        (extracted / "due_concepts.json").write_bytes(canonical({"concept_ids": [], "concept_priority_version": "other"}))
        with self.assertRaisesRegex(archives.BridgeError, "D_CONCEPT_CONTEXT_CONFLICT"):
            object.__new__(bridge.Bridge).queue_payload(run)

    def test_d_zip_payload_reaches_real_subject_import_in_temporary_repository(self):
        # Separate interpreter keeps the two subjects' module names isolated.
        script = r'''
import json,sys
from pathlib import Path
from unittest import mock
sys.path[:0] = [str(Path(sys.argv[1])/'scripts'), str(Path(sys.argv[1])/'tests'), sys.argv[2]]
from test_study_bridge_408 import StudyBridge408Tests
import study_bridge_408 as native
import study_bridge as shared
import bridge_archives as archives
fixture = StudyBridge408Tests()
fixture.setUp()
try:
    body, current, ledger = fixture.d_fixture()
    current.update(concept_priority_version='fixture-priority', concept_requests=[{'request_id':'R','concept_id':'OS01-01'}])
    context = {key: current[key] for key in ('concept_priority_version','concept_requests','review_concept_ids')}
    output = Path(body['output_root']).resolve()
    documents = {'manifest.json': context, 'due_concepts.json': {'concept_ids':body['due_concept_ids']},
                 'coverage_report.json': body['coverage']}
    for name,value in documents.items():
        (output/name).write_text(json.dumps(value))
    for name,rows in [('selected_items.jsonl',body['selected_items']),('unresolved.jsonl',[]),('supplemental_items.jsonl',[])]:
        (output/name).write_text(''.join(json.dumps(row)+'\n' for row in rows))
    zipped=output.parent/'D-shared.zip'
    archives.write_zip({p.name:p for p in output.iterdir()},zipped)
    extracted=archives.extract_zip(zipped,output.parent/'D-extracted',archives.ArchiveBudget(output.parent))
    run={**body,'run_id':'D-shared-fixture','output_dir':str(extracted)}
    payload=object.__new__(shared.Bridge).queue_payload(run)
    payload['fixture_publication_manifest']=body['fixture_publication_manifest']
    with mock.patch.object(native,'due',return_value=current):
        result=native.import_d(payload,repo=fixture.repo,details_root=fixture.native.details)
    assert result['status']=='IMPORTED_FOR_NATIVE_PREPARATION', result
    assert ledger.read_bytes()==b''
    print(json.dumps({'status':result['status'],'learner_event_bytes':len(ledger.read_bytes())}))
finally:
    fixture.doCleanups()
'''
        process = subprocess.run([mcp.NATIVE_PYTHON, "-c", script, str(bridge.REPOS["408"]), str(mcp.BASE)],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual(0, json.loads(process.stdout)["learner_event_bytes"])


if __name__ == "__main__":
    unittest.main()
