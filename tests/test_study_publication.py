import json
import fcntl
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

import study_publication as p


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote, self.seed, self.source = [self.root / n for n in ("remote.git", "seed", "source")]
        self.remote.mkdir()
        self.seed.mkdir()
        self.source.mkdir()
        git(self.remote, "init", "--bare", "-b", "main")
        git(self.seed, "init", "-b", "main")
        git(self.seed, "config", "user.name", "test")
        git(self.seed, "config", "user.email", "test@example.invalid")
        (self.seed / "README.md").write_text("existing unrelated remote code\n")
        git(self.seed, "add", "README.md")
        git(self.seed, "commit", "-m", "baseline")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        git(self.seed, "push", "origin", "main")
        self.publisher = p.Publisher("math", self.source, self.root / "state", str(self.remote), test_mode=True)
        (self.source / "card.md").write_text("题目及完整历史\n")
        self.desc = {"subject": "math", "local_version": "formal-1", "sources": [
            {"source_path": str(self.source / "card.md"), "publish_path": "cards/GS-001.md",
             "kind": "formal-card", "formal_ids": ["GS-001"]}],
            "entrypoints": {"cards": "cards/GS-001.md"}, "gaps": [{"id": "legacy", "code": "missing_original"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_preserves_remote_and_is_event_idempotent(self):
        self.desc["sources"][0].update(visibility="protected", state_source_version="formal-state-v1")
        prepared = self.publisher.execute("formal:1", self.desc, prepare_only=True)
        staged_stamp = p.stamp(self.publisher.checkout / "cards/GS-001.md")
        with patch.object(p.shutil, "copyfileobj", side_effect=AssertionError("Unchanged prepared bytes must be reused")):
            prepared = self.publisher.execute("formal:1", self.desc, prepare_only=True)
        self.assertEqual(prepared["changed_files"], 0)
        self.assertEqual(p.stamp(self.publisher.checkout / "cards/GS-001.md"), staged_stamp)
        with self.assertRaisesRegex(p.PublicationError, "PREPARED_MANIFEST_BINDING_MISMATCH"):
            self.publisher.execute("formal:1", prepared_manifest_sha256="0" * 64)
        with patch.object(p, "describe", side_effect=AssertionError("Reviewed snapshot must not re-export")):
            result = self.publisher.execute("formal:1", prepared_manifest_sha256=prepared["manifest_sha256"])
        self.assertEqual(result["status"], "PUBLISHED")
        stored = p.read_json(self.publisher.receipt_path("formal:1"))
        self.assertNotIn("sources", stored)
        self.assertEqual(stored["source_cache"]["count"], 1)
        commit = result["source_commit"]
        self.assertEqual(git(self.remote, "rev-parse", "main"), commit)
        self.assertEqual(git(self.remote, "show", "main:README.md"), "existing unrelated remote code")
        manifest = json.loads(git(self.remote, "show", "main:PUBLISHED_LIBRARY.json"))
        self.assertNotIn("source_commit", manifest)
        self.assertEqual(manifest["coverage_status"], "PARTIAL")
        self.assertEqual(manifest["files"][0]["visibility"], "protected")
        self.assertEqual(manifest["files"][0]["state_source_version"], "formal-state-v1")
        (self.source / "card.md").write_text("later formal state must use a new event\n")
        replay = self.publisher.execute("formal:1", self.desc)
        self.assertEqual(replay["source_commit"], commit)

    def test_incremental_retains_history(self):
        self.publisher.execute("formal:1", self.desc)
        (self.source / "other.md").write_text("new history\n")
        desc = {"subject": "math", "sources": [{"source_path": str(self.source / "other.md"),
                "publish_path": "cards/GS-002.md", "kind": "formal-card"}]}
        result = self.publisher.execute("formal:2", desc)
        self.assertEqual(result["counts"]["retained_files"], 1)
        self.assertEqual(git(self.remote, "show", "main:cards/GS-001.md"), "题目及完整历史")

    def test_crlf_and_literal_glob_filename_preserve_original_bytes(self):
        original = b"full original dialogue\r\nunchanged bytes\r\n"
        (self.source / "card.md").write_bytes(original)
        self.desc["sources"][0]["publish_path"] = "cards/[2026] 原件.md"
        self.publisher.execute("formal:1", self.desc)
        raw = subprocess.check_output(["git", "-C", str(self.remote), "show", "main:cards/[2026] 原件.md"])
        self.assertEqual(raw, original)

    @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs optional runtime not installed")
    def test_lfs_publishes_and_downloads_real_bytes(self):
        self.desc["sources"][0]["storage"] = "git-lfs"
        result = self.publisher.execute("formal:1", self.desc, lfs_verified=True)
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertIn("lfs_remote_bytes_verified_at", result)
        pointer = git(self.remote, "show", "main:cards/GS-001.md")
        self.assertTrue(pointer.startswith("version https://git-lfs.github.com/spec/v1"))
        oid = p.file_hash(self.source / "card.md")
        self.assertEqual((self.publisher.state / "lfs-readback/objects" / oid[:2] / oid[2:4] / oid).read_bytes(),
                         (self.source / "card.md").read_bytes())
        (self.source / "later.md").write_text("A later text-only study update")
        self.desc["sources"].append({"source_path": str(self.source / "later.md"),
                                      "publish_path": "history/later.md", "kind": "formal_history"})
        real_git = p.git
        def no_lfs_transfer(checkout, *args, **kw):
            if "lfs" in args and "fetch" in args:
                raise AssertionError("Previously verified original must not download again")
            if "push" in args:
                self.assertEqual(kw.get("extra_env"), {"GIT_LFS_SKIP_PUSH": "1"})
            return real_git(checkout, *args, **kw)
        with patch.object(p, "git", side_effect=no_lfs_transfer):
            later = self.publisher.execute("formal:2", self.desc, lfs_verified=False)
        self.assertEqual(later["status"], "PUBLISHED")
        self.assertTrue(later["lfs_readback_reused"])
        (self.source / "card.md").write_text("A different large original requires fresh zero-budget evidence")
        with self.assertRaisesRegex(p.PublicationError, "LFS_BUDGET_UNVERIFIED"):
            self.publisher.execute("formal:3", self.desc, lfs_verified=False)

    def test_source_drift_prevents_commit(self):
        self.publisher.remote_metadata()
        self.publisher.initialize()
        receipt = self.publisher.prepare("formal:1", p.normalize(self.desc, "math"))
        (self.source / "card.md").write_text("changed after snapshot")
        with self.assertRaisesRegex(p.PublicationError, "SOURCE_CHANGED_SINCE_PREPARE"):
            self.publisher.publish_prepared(receipt, False)

    def test_web_transport_is_committed_and_restores_exact_remote_source(self):
        original = ("完整正文\\n与原始换行\r\n" * 6000).encode("utf-8")
        (self.source / "card.md").write_bytes(original)
        result = self.publisher.execute("web:1", self.desc)
        cache = p.read_json(self.publisher.state / result["web_transport"]["path"])
        for row in cache["generated_files"]:
            remote_bytes = subprocess.check_output(["git", "-C", str(self.remote), "show",
                                                    f"{result['source_commit']}:{row['path']}"])
            self.assertEqual(p.hashlib.sha256(remote_bytes).hexdigest(), row["sha256"])
        target = self.root / "restored.md"
        proof = p.study_web_read.restore_source(self.publisher.checkout, "cards/GS-001.md", target)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(proof["visual_view_status"], "NOT_RUN")
        self.assertEqual(proof["semantic_read_status"], "NOT_RUN")

    def test_web_transport_tamper_blocks_prepared_commit(self):
        receipt = self.publisher.execute("web:1", self.desc, prepare_only=True)
        cache = p.read_json(self.publisher.state / receipt["web_transport"]["path"])
        row = next(x for x in cache["generated_files"] if x["path"].endswith(".jsonl"))
        (self.publisher.checkout / row["path"]).write_bytes(b"changed transport\n")
        with self.assertRaisesRegex(p.PublicationError, "WEB_TRANSPORT_BYTES_CHANGED"):
            self.publisher.publish_prepared(receipt, False)

    def test_scheduler_snapshot_keeps_live_input_binding_until_commit(self):
        live = self.source / "scheduler-state.json"
        live.write_text('{"state":1}\n')
        self.desc["additional_source_bindings"] = [{"source_path": str(live), "sha256": p.file_hash(live),
                                                   "bytes": live.stat().st_size}]
        receipt = self.publisher.execute("scheduler:1", self.desc, prepare_only=True)
        live.write_text('{"state":2}\n')
        with self.assertRaisesRegex(p.PublicationError, "SOURCE_CHANGED_SINCE_PREPARE"):
            self.publisher.publish_prepared(receipt, False)

    def test_scheduler_absent_input_becoming_present_blocks_commit(self):
        live = self.source / "optional-admissions.jsonl"
        self.desc["additional_absent_bindings"] = [{"source_path": str(live)}]
        receipt = self.publisher.execute("scheduler:1", self.desc, prepare_only=True)
        live.write_text('{"event":"new admission"}\n')
        with self.assertRaisesRegex(p.PublicationError, "DUE_SUPPORT_ABSENT_INPUT_APPEARED"):
            self.publisher.publish_prepared(receipt, False)

    def test_large_inventory_uses_small_root_without_duplicate_full_list(self):
        with patch.object(p, "ROOT_MANIFEST_LIMIT", 3500), patch.object(p, "INVENTORY_LIMIT", 1500):
            self.desc["sources"] = []
            self.desc["original_mappings"] = [{"question_id": str(n), "original_path": "a" * 200} for n in range(20)]
            for number in range(20):
                path = self.source / f"{number}.md"
                path.write_text(f"real source {number}")
                self.desc["sources"].append({"source_path": str(path),
                                             "publish_path": f"cards/{number:03d}.md", "kind": "formal"})
            self.desc["sources"][0]["publish_path"] = "000.md"
            self.desc["sources"][1]["publish_path"] = "001.md"
            self.publisher.execute("formal:1", self.desc)
            root = p.read_json(self.publisher.checkout / "PUBLISHED_LIBRARY.json")
            self.assertNotIn("files", root)
            self.assertIn("file_inventory", root)
            self.assertEqual(sum(s["count"] for s in root["file_inventory"] if s["source_directory"] == "."), 2)
            self.assertNotIn("original_mappings", root)
            self.assertIn("original_mapping_inventory", root)
            resolved = p.load_library(self.publisher.checkout)
            self.assertEqual(len(resolved["files"]), 20)
            self.assertEqual(resolved["original_mappings"], self.desc["original_mappings"])
            for shard in root["file_inventory"]:
                self.assertLessEqual(shard["bytes"], 1500)
            self.assertLess((self.publisher.checkout / "PUBLISHED_LIBRARY.json").stat().st_size, 3500)

    def test_old_event_retry_does_not_rewind_latest(self):
        first = self.publisher.execute("formal:1", self.desc)
        (self.source / "card.md").write_text("a later formal revision\n")
        second = self.publisher.execute("formal:2", self.desc)
        self.assertNotEqual(first["source_commit"], second["source_commit"])
        self.publisher.execute("formal:1", self.desc)
        self.assertEqual(p.read_json(self.publisher.state / "latest.json")["source_commit"], second["source_commit"])

    def test_unknown_push_resolves_remote_before_retry(self):
        real_git = p.git
        pushed = []
        def interrupted(checkout, *args, **kw):
            result = real_git(checkout, *args, **kw)
            command = list(args)
            while command[:1] == ["-c"]:
                command = command[2:]
            if command[:1] == ["push"]:
                pushed.append(1)
                raise p.PublicationError("COMMAND_TIMEOUT")
            return result
        with patch.object(p, "git", side_effect=interrupted):
            result = self.publisher.execute("formal:1", self.desc)
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(len(pushed), 1)

    def test_pdf_cache_reuse_does_not_open_or_generate_pdf(self):
        original = self.source / "original.pdf"
        original.write_bytes(b"original fixture")
        sha = p.file_hash(original)
        cache = self.root / "page-cache"
        page = cache / sha / "page-0001.pdf"
        page.parent.mkdir(parents=True)
        page.write_bytes(b"previously verified page fixture")
        p.write_json(cache / "index.json", {sha + ":1": {"path": str(page), "stamp": p.stamp(page),
                      "sha256": p.file_hash(page), "bytes": page.stat().st_size}})
        description = self.root / "page-source.json"
        p.write_json(description, {"original_mappings": [{"source_path": str(original), "sha256": sha,
                     "source_id": "fixture-original", "visibility": "protected", "pages": [
                     {"page_number": 1, "publish_path": "published-originals/fixture/page-0001.pdf"}]}]})
        import types
        stub = types.ModuleType("pypdf")
        def forbidden(*args, **kwargs):
            raise AssertionError("Cached pages must not reopen PDF or create a writer")
        stub.PdfReader = stub.PdfWriter = forbidden
        before = p.stamp(page)
        with patch.dict("sys.modules", {"pypdf": stub}):
            result = p.export_pdf_pages(description, cache, self.root / "pages.json")
        self.assertEqual(result["created_pages"], 0)
        self.assertEqual(p.stamp(page), before)
        self.assertEqual(p.read_json(self.root / "pages.json")["sources"][0]["original_sha256"], sha)

    def test_secret_not_printed_or_pushed(self):
        secret = "ghp_" + "X" * 36
        (self.source / "card.md").write_text(secret)
        before = git(self.remote, "rev-parse", "main")
        with self.assertRaises(p.PublicationError) as caught:
            self.publisher.execute("formal:1", self.desc)
        self.assertEqual(caught.exception.code, "SECRET_PATTERN")
        self.assertNotIn(secret, str(caught.exception.detail))
        self.assertEqual(before, git(self.remote, "rev-parse", "main"))

    def test_escaping_path_rejected(self):
        self.desc["sources"][0]["publish_path"] = "../outside"
        with self.assertRaisesRegex(p.PublicationError, "UNSAFE_PUBLISH_PATH"):
            p.normalize(self.desc, "math")

    def test_remote_symlink_cannot_overwrite_source(self):
        (self.seed / "cards").symlink_to(self.source, target_is_directory=True)
        git(self.seed, "add", "cards")
        git(self.seed, "commit", "-m", "symlink collision")
        git(self.seed, "push", "origin", "main")
        with self.assertRaisesRegex(p.PublicationError, "REMOTE_SYMLINK_COLLISION"):
            self.publisher.execute("formal:1", self.desc)

    def test_lfs_budget_blocks_before_commit(self):
        self.desc["sources"][0]["storage"] = "lfs"
        before = git(self.remote, "rev-parse", "main")
        with self.assertRaisesRegex(p.PublicationError, "LFS_BUDGET_UNVERIFIED"):
            self.publisher.execute("formal:1", self.desc)
        self.assertEqual(before, git(self.remote, "rev-parse", "main"))

    def test_pending_is_local_and_repeatable(self):
        with patch.object(p, "run", side_effect=AssertionError("must not network")):
            a = p.mark_pending("math", "event", self.root / "pending")
            b = p.mark_pending("math", "event", self.root / "pending")
        self.assertEqual(a, b)
        self.assertEqual(a["status"], "PENDING")

    def test_request_fixture_never_starts_network(self):
        with patch.object(p.subprocess, "Popen", side_effect=AssertionError("no child allowed")):
            result = p.request_publication("math", "event", self.source, self.root / "state")
        self.assertEqual(result["status"], "NOT_PRODUCTION_REPO")

    def test_busy_launcher_creates_no_waiting_process(self):
        state = self.root / "async"
        state.joinpath("math").mkdir(parents=True)
        fd = os.open(state / "math/launch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with patch.dict(p.SUBJECTS["math"], root=self.source), patch.object(
                    p.subprocess, "Popen", side_effect=AssertionError("no duplicate child")):
                waiting = p.request_publication("math", "event", self.source, state)
                self.assertEqual(waiting["status"], "PENDING")
                self.assertTrue(waiting["baseline_review_required"])
                p.write_json(state / "math/latest.json", {"status": "PUBLISHED"})
                result = p.request_publication("math", "event", self.source, state)
            self.assertEqual(result["status"], "ALREADY_RUNNING")
            self.assertTrue(p.read_json(Path(result["receipt_path"]))["automatic_requested"])
        finally:
            os.close(fd)

    def test_direct_publish_dispatches_only_already_pending_event(self):
        state = self.root / "after-direct"
        p.mark_pending("math", "real-event", state)
        publisher = p.Publisher("math", self.source, state)
        path = publisher.receipt_path("real-event")
        row = p.read_json(path)
        row.update(automatic_requested=True, requested_at=p.now())
        p.write_json(path, row)
        with patch.object(p, "request_publication", return_value={"status": "STARTED"}) as request:
            result = p.dispatch_pending("math", self.source, state)
        self.assertEqual(result["status"], "STARTED")
        request.assert_called_once_with("math", "real-event", self.source, state)

    def test_worker_coalesces_pending_without_repeat_publish(self):
        state = self.root / "async"
        p.mark_pending("math", "earlier-failed", state)
        old_publisher = p.Publisher("math", self.source, state)
        failed_path = old_publisher.receipt_path("earlier-failed")
        failed = p.read_json(failed_path)
        failed.update(status="CLOUD_PENDING", request_origin="postcommit", requested_at="2000-01-01T00:00:00+00:00")
        p.write_json(failed_path, failed)
        for event in ("old", "new"):
            p.mark_pending("math", event, state)
            pub = p.Publisher("math", self.source, state)
            path = pub.receipt_path(event)
            row = p.read_json(path)
            row.update(automatic_requested=True, requested_at=event)
            p.write_json(path, row)
        fd = os.open(state / "math/launch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        calls = []
        def execute(publisher, event, **kw):
            self.assertTrue(kw.get("wait_for_lock"))
            calls.append(event)
            result = {"status": "PUBLISHED", "subject": "math", "event_id": event,
                      "source_commit": "a" * 40}
            p.write_json(publisher.receipt_path(event), result)
            return result
        with patch.object(p.Publisher, "execute", execute):
            result = p.drain_pending("math", state, fd)
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(p.read_json(pub.receipt_path("new"))["status"], "PUBLISHED")
        self.assertEqual(p.read_json(pub.receipt_path("old"))["status"], "PUBLISHED")
        self.assertEqual(p.read_json(failed_path)["status"], "PUBLISHED")
        self.assertIn("coalesced_into_event_id", p.read_json(failed_path))

    def test_worker_failure_stops_once(self):
        state = self.root / "failed"
        p.mark_pending("math", "event", state)
        pub = p.Publisher("math", self.source, state)
        path = pub.receipt_path("event")
        row = p.read_json(path)
        row["automatic_requested"] = True
        p.write_json(path, row)
        fd = os.open(state / "math/launch.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with patch.object(p.Publisher, "execute", side_effect=p.PublicationError("NETWORK_DOWN")) as execute:
            result = p.drain_pending("math", state, fd)
        self.assertEqual(result, 1)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(p.read_json(path)["status"], "CLOUD_PENDING")


if __name__ == "__main__":
    unittest.main()
