import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bridge_archives import BridgeError
from study_mcp import LibraryStore


MASTER_HEADER = [
    "id", "date", "type", "item", "source_article", "source_sentence", "meaning",
    "usage", "writing_value", "tags", "review_note", "appear_count", "last_seen",
]
MASTERED_HEADER = [
    "item", "matched_id", "matched_type", "mastered_date", "evidence_sentence",
    "evidence_context", "proof_note",
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def object_sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def json_bytes(value):
    return canonical(value) + b"\n"


def csv_bytes(header, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


class EnglishFixture:
    def __init__(self, with_binary_attachment=False):
        self.temp = tempfile.TemporaryDirectory(prefix="study-mcp-english-test-")
        self.base = Path(self.temp.name)
        repo = self.base / "publication/english/checkout"
        repo.mkdir(parents=True)
        package_id = "EN-PKG-20260913-AAAAAAAAAAAAAAAA"
        conversation = json_bytes({"messages": [{"role": "user", "content": "I chose B."}]})
        source = json_bytes({"identity": {"source_id": "FIXTURE-SOURCE", "sentence_id": "S1"}})
        package_files = {
            "conversation.json": {"sha256": hashlib.sha256(conversation).hexdigest()},
            "source.json": {"sha256": hashlib.sha256(source).hexdigest()},
        }
        package_core = {"schema_version": "english_conversation_package_v1", "package_id": package_id,
                        "subject": "english", "study_date": "2026-09-13", "files": package_files}
        manifest = {**package_core, "attachments": []}
        package_sha = object_sha(package_core)
        event = {
            "event_key": "fixture-protected-answer", "kind": "answer", "origin": "daily",
            "task_id": None, "source_commit": None, "source_id": "FIXTURE-SOURCE", "unit_id": "S1",
            "bank_ids": ["FIX-001"], "sentence_pattern_ids": [], "concept_ids": ["bank:FIX-001"],
            "observed_at": "2026-09-13T08:00:00+08:00", "outcome": "incorrect",
            "hint_dependence": "not_observed", "reasoning_status": "invalid",
            "user_response": "I chose B.", "reasoning": "fixture reasoning", "correction": "fixture correction",
        }
        event_core = {
            "schema_version": "english_formal_learning_event_v1", "subject": "english", "sequence": 1,
            "event_id": "EN-LEARN-" + object_sha({"event_key": event["event_key"]})[:24].upper(),
            "event": event,
            "evidence_refs": [{"package_id": package_id, "package_sha256": package_sha}],
            "visibility": "protected", "previous_sha256": "0" * 64,
        }
        event_row = {**event_core, "record_sha256": object_sha(event_core)}
        master = csv_bytes(MASTER_HEADER, [{
            "id": "FIX-001", "date": "2026-09-13", "type": "单词", "item": "fixtureword",
            "source_article": "fixture", "source_sentence": "a fixture word", "meaning": "测试词义",
            "usage": "a fixture word", "writing_value": "一般", "tags": "fixture",
            "review_note": "fixture", "appear_count": "1", "last_seen": "2026-09-13",
        }])
        mastered = csv_bytes(MASTERED_HEADER, [])
        patterns = b"# Sentence Patterns\n"
        events = json_bytes(event_row)
        article = b"# Fixture article\n\nA stable fixture passage.\n"
        prefix = f"intake/packages/2026-09-13/{package_id}"
        files = {
            "bank/master_bank.csv": master,
            "bank/mastered_items.csv": mastered,
            "bank/sentence_patterns.md": patterns,
            "bank/learning_events.jsonl": events,
            "articles/2026-04-24-article.md": article,
            f"{prefix}/manifest.json": json_bytes(manifest),
            f"{prefix}/conversation.json": conversation,
            f"{prefix}/source.json": source,
        }
        if with_binary_attachment:
            files[f"{prefix}/attachments/001-return.zip"] = b"PK\x03\x04\x00\xff\x00"
        formal_hashes = {
            "bank/master_bank.csv": hashlib.sha256(master).hexdigest(),
            "bank/mastered_items.csv": hashlib.sha256(mastered).hexdigest(),
            "bank/sentence_patterns.md": hashlib.sha256(patterns).hexdigest(),
            "bank/review_exclusion_ledger.jsonl": None,
            "bank/learning_events.jsonl": hashlib.sha256(events).hexdigest(),
            "bank/concept_aliases.json": None,
        }
        self.formal_hashes = formal_hashes
        manifest_root = {
            "subject": "english", "repository": "fixture/english", "branch": "main",
            "local_version": "fixture-v1", "coverage_status": "COMPLETE",
            "counts": {"files": len(files)}, "history_index": ["bank/master_bank.csv", "bank/sentence_patterns.md"] + (["intake/packages"] if with_binary_attachment else []),
            "entrypoints": ["bank/master_bank.csv", "bank/sentence_patterns.md"],
            "formal_source_hashes": formal_hashes, "formal_version": object_sha(formal_hashes),
            "files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                      for path, data in files.items()],
        }
        root_bytes = json_bytes(manifest_root)
        files["PUBLISHED_LIBRARY.json"] = root_bytes
        for relative, data in files.items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for command in (["git", "init", "-q", "-b", "main"], ["git", "add", "."],
                        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-qm", "fixture"]):
            subprocess.run(command, cwd=repo, check=True, capture_output=True)
        self.commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        receipt = {"status": "PUBLISHED", "subject": "english", "branch": "main",
                   "source_commit": self.commit, "repository": "fixture/english", "local_version": "fixture-v1",
                   "manifest_sha256": hashlib.sha256(root_bytes).hexdigest()}
        state = repo.parent
        (state / "receipts").mkdir()
        (state / "latest.json").write_bytes(json_bytes(receipt))
        (state / "receipts/fixture.json").write_bytes(json_bytes(receipt))

    def close(self):
        self.temp.cleanup()


class EnglishContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = EnglishFixture()
        self.store = LibraryStore(self.fixture.base)

    def tearDown(self):
        self.store.close()
        self.fixture.close()

    def request(self, value, **kwargs):
        value = {**value, "allow_historical": True}
        return self.store.query("english", "english_learning_context",
                                source_commit=self.fixture.commit, contains=json.dumps(value), **kwargs)

    def test_library_keeps_binary_returns_out_of_text_history_scan(self):
        fixture = EnglishFixture(with_binary_attachment=True)
        store = LibraryStore(fixture.base)
        try:
            with patch.object(store, 'freshness', return_value={'status':'current'}):
                result = store.library('english', fixture.commit, limit=1)
            self.assertEqual(result['domains']['vocabulary']['records'], 1)
            binary = result['non_text_history_files']
            self.assertEqual(len(binary), 1)
            self.assertTrue(binary[0]['path'].endswith('/001-return.zip'))
            self.assertNotIn(binary[0]['path'], {r['path'] for r in result['full_history_source_scan']})
            self.assertEqual(store.snapshot('english', fixture.commit).path(binary[0]['path']).read_bytes(), b"PK\x03\x04\x00\xff\x00")
        finally:
            store.close()
            fixture.close()

    def test_snapshot_formal_record_is_version_bound_and_paginated(self):
        result = self.request({"bank_ids": ["FIX-001"], "view": "before_attempt",
                               "collection": "formal_records"}, limit=1, max_bytes=4096)
        self.assertEqual(result["source_commit"], self.fixture.commit)
        self.assertEqual(result["english_learning_context"]["formal_version"], object_sha(self.fixture.formal_hashes))
        self.assertEqual(result["records"][0]["record"]["item"], "fixtureword")
        self.assertTrue(result["eof"])
        self.assertTrue(all(row["source_commit"] == self.fixture.commit for row in result["formal_source_refs"]))

    def test_protected_history_requires_explicit_question_review(self):
        before = self.request({"bank_ids": ["FIX-001"], "view": "before_attempt",
                               "collection": "learning_history"})
        self.assertEqual(before["records"], [])
        self.assertEqual(before["english_learning_context"]["protected_event_count"], 1)
        review = self.request({"bank_ids": ["FIX-001"], "view": "question_review",
                               "collection": "learning_history"})
        self.assertEqual(len(review["records"]), 1)
        self.assertEqual(review["records"][0]["visibility"], "protected")

    def test_source_hash_constraint_is_not_silently_refreshed(self):
        result = self.request({"source_article": "articles/2026-04-24-article.md", "sentence_id": "S1",
                               "source_hash": "0" * 64, "view": "before_attempt"})
        self.assertEqual(result["english_learning_context"]["status"], "unavailable")
        self.assertEqual(result["english_learning_context"]["reason"], "source_changed")
        self.assertEqual(result["source_commit"], self.fixture.commit)

    def test_explicit_historical_snapshot_never_reads_live_english_identity(self):
        latest_path = self.fixture.base / "publication/english/latest.json"
        latest = json.loads(latest_path.read_text())
        latest["source_commit"] = "b" * 40
        latest_path.write_bytes(json_bytes(latest))
        with patch("study_mcp.study_personalization.native_call",
                   side_effect=AssertionError("historical query touched live English data")):
            result = self.request({"bank_ids": ["FIX-001"], "view": "before_attempt",
                                   "collection": "formal_records"})
        self.assertEqual(result["freshness"]["status"], "historical")
        self.assertEqual(result["records"][0]["record"]["item"], "fixtureword")

    def test_manifest_formal_hash_mismatch_is_rejected(self):
        snapshot = self.store.snapshot("english", self.fixture.commit)
        snapshot.library["formal_source_hashes"]["bank/master_bank.csv"] = "f" * 64
        with self.assertRaises(BridgeError) as caught:
            self.request({"bank_ids": ["FIX-001"], "view": "before_attempt"})
        self.assertEqual(caught.exception.code, "ENGLISH_FORMAL_SOURCE_DIFFERS_FROM_SNAPSHOT")

    def test_library_advertises_existing_query_domain(self):
        library = self.store.library("english", self.fixture.commit)
        self.assertEqual(library["english_context_query"]["domain"], "english_learning_context")
        self.assertIn("question_review", library["english_context_query"]["views"])


if __name__ == "__main__":
    unittest.main()
