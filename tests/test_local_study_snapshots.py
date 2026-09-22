import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import study_publication as p
from study_bridge import Published
from study_mcp import LibraryStore


class LocalSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.state = self.root / "publication"
        p.write_json(self.state / "config.json", {"target": "local"})
        self.pub = p.Publisher("math", self.source, self.state)
        self.original = "正式正文与完整历史\r\n".encode()
        (self.source / "card.md").write_bytes(self.original)
        self.desc = {"subject": "math", "local_version": "native-v1", "sources": [
            {"source_path": str(self.source / "card.md"), "publish_path": "cards/GS-001.md", "kind": "formal-card"}]}
        real_run = p.run
        def local_only(args, **kwargs):
            if args[0] == "gh" or any(arg in {"fetch", "push", "ls-remote"} for arg in args):
                raise AssertionError("Local snapshots must not use a remote command: " + str(args[:4]))
            return real_run(args, **kwargs)
        self.commands = patch.object(p, "run", side_effect=local_only)
        self.commands.start()
        self.transport = patch.object(p.study_web_read, "build_web_read", side_effect=AssertionError("No legacy Base64 transport"))
        self.transport.start()

    def tearDown(self):
        self.transport.stop()
        self.commands.stop()
        self.tmp.cleanup()

    def test_publish_and_verify_without_github_or_a_remote_baseline(self):
        result = self.pub.execute("local:1", self.desc)
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["publication_target"], "local")
        self.assertFalse(result["network_started"])
        self.assertFalse(result["github_backup_performed"])
        self.assertNotIn("web_transport", result)
        self.assertNotIn("remote_ref_verified_at", result)
        verified = p.verify_published("math", result["source_commit"], self.state)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["verification_transport"], "local")
        self.assertFalse(verified["network_started"])
        self.assertEqual(p.git(self.pub.checkout, "rev-parse", "refs/study-snapshots/" + result["source_commit"]),
                         result["source_commit"])

    def test_old_versions_and_history_remain_readable(self):
        first = self.pub.execute("local:1", self.desc)
        (self.source / "second.md").write_text("新增记录\n")
        later = {"subject": "math", "sources": [{"source_path": str(self.source / "second.md"),
                                                  "publish_path": "cards/GS-002.md", "kind": "formal-card"}]}
        second = self.pub.execute("local:2", later)
        self.assertEqual(second["counts"]["retained_files"], 1)
        self.assertNotEqual(first["source_commit"], second["source_commit"])
        owner = Published(self.state)
        old = owner.materialize("math", first["source_commit"], "cards/GS-001.md", self.root / "old.md")
        self.assertEqual(old.read_bytes(), self.original)
        replay = self.pub.execute("local:1", self.desc)
        self.assertEqual(replay["source_commit"], first["source_commit"])
        self.assertEqual(p.read_json(self.pub.state / "latest.json")["source_commit"], second["source_commit"])

    def test_source_changes_between_prepare_and_commit_still_fail(self):
        result = self.pub.execute("local:1", self.desc, prepare_only=True)
        (self.source / "card.md").write_text("新版本不得混入旧快照")
        with self.assertRaisesRegex(p.PublicationError, "SOURCE_CHANGED_SINCE_PREPARE"):
            self.pub.publish_prepared(result, False)
        self.assertFalse((self.pub.state / "latest.json").exists())

    def test_committed_snapshot_recovery_does_not_repeat_formal_or_remote_work(self):
        with patch.object(self.pub, "finish_local", side_effect=p.PublicationError("interrupted_after_commit")):
            with self.assertRaises(p.PublicationError):
                self.pub.execute("local:1", self.desc)
        prior = p.read_json(self.pub.receipt_path("local:1"))
        with patch.object(p, "describe", side_effect=AssertionError("Do not rebuild committed source")):
            result = self.pub.execute("local:1")
        self.assertEqual(result["source_commit"], prior["candidate_commit"])
        self.assertEqual(result["status"], "PUBLISHED")

    @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs unavailable")
    def test_new_lfs_bytes_work_without_upload_or_budget_evidence(self):
        self.desc["sources"][0]["storage"] = "git-lfs"
        result = self.pub.execute("local:lfs", self.desc, lfs_verified=False)
        self.assertEqual(result["publication_target"], "local")
        self.assertNotIn("LFS_BUDGET_UNVERIFIED", [r.get("code") for r in result["gaps"]])
        sha = p.file_hash(self.source / "card.md")
        local = p.local_lfs_object(self.pub.state, sha)
        self.assertEqual(local.read_bytes(), self.original)
        recovered = Published(self.state).materialize("math", result["source_commit"], "cards/GS-001.md",
                                                      self.root / "restored.md", sha)
        self.assertEqual(recovered.read_bytes(), self.original)

    def test_mcp_reports_local_identity_and_reads_original(self):
        result = self.pub.execute("local:1", self.desc)
        store = LibraryStore(self.root)
        try:
            text = store.fetch("math", "cards/GS-001.md")
            self.assertEqual(text["text"].encode(), self.original)
            self.assertEqual(text["source_role"], "local_immutable_study_snapshot")
            self.assertEqual(text["snapshot_id"], result["source_commit"])
            self.assertFalse(text["github_backup_required"])
        finally:
            store.close()

    def test_local_cli_does_not_require_legacy_lfs_budget_evidence(self):
        from contextlib import redirect_stdout
        import io
        missing = self.root / "obsolete-github-budget.json"
        with patch.object(p, "describe", return_value=p.normalize(self.desc, "math")), redirect_stdout(io.StringIO()):
            status = p.main(["publish", "--subject", "math", "--repo", str(self.source),
                             "--state-dir", str(self.state), "--event-id", "local:cli",
                             "--lfs-budget-evidence", str(missing)])
        self.assertEqual(status, 0)
        self.assertEqual(p.read_json(self.pub.state / "latest.json")["publication_target"], "local")


if __name__ == "__main__":
    unittest.main()
