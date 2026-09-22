import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bridge_archives import BridgeError
from study_mcp import LibraryStore, BASE


def raw_json(value):
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


class Fixture:
    def __init__(self, personalized=False, subject="math"):
        self.temp = tempfile.TemporaryDirectory(prefix="study-mcp-test-")
        self.base = Path(self.temp.name)
        repo = self.base / f"publication/{subject}/checkout"
        repo.mkdir(parents=True)
        image = io.BytesIO()
        Image.new("RGB", (3, 2), "white").save(image, format="PNG")
        self.text = ("题目中文边界。\n" * 10000).encode()
        files = {
            "formal/index.json": raw_json({"cards": [{"id": "GS-001", "meta": {"status": "待复做"}},
                                                     {"id": "LA-001", "meta": {"status": "待确认"}}]}),
            "capture.jsonl": raw_json({"event_id": "CAP-001", "fact": "真实事件"}),
            "review.jsonl": raw_json({"unit_id": "U1", "result": "提示后正确"}),
            "large.md": self.text,
            "image.png": image.getvalue(),
            "empty.txt": b"",
            "unsafe.bin": b"\x00\xff\x01",
        }
        repository = ("xialovezhu-wq/kaoyan-math" if subject == "math"
                      else "xialovezhu-wq/kaoyan-408")
        manifest = {"subject": subject, "repository": repository, "branch": "main",
                    "local_version": "v1", "coverage_status": "PARTIAL", "counts": {"files": len(files)},
                    "history_index": "formal/index.json", "entrypoints": {"history_index": "formal/index.json",
                    "capture_events": "capture.jsonl", "review_events": "review.jsonl"},
                    "files": [{"path": p, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
                              for p, b in files.items()]}
        if subject == "408":
            manifest["question_refs"] = {
                "CO_2012_005": {"formal_id": "CO_2012_005", "source_id": "EXAM408-2012-Q15",
                    "safe_card": "CO_2012_005.md", "question_images": ["question.png"],
                    "formal_state": {"formal_row_sha256": "1" * 64, "safe_card_sha256": "2" * 64}},
                "OS_2021_002": {"formal_id": "OS_2021_002", "source_id": "EXAM408-2021-Q24",
                    "safe_card": "OS_2021_002.md", "question_images": ["question-2.png"],
                    "formal_state": {"formal_row_sha256": "3" * 64, "safe_card_sha256": "4" * 64}}}
        if personalized:
            name = f'personalization/{subject}/manifest.json'
            files[name] = raw_json({'version': 'personal-v1', 'formal_version': 'formal-v1'})
            manifest['entrypoints']['personalization'] = name
            manifest['personalization'] = {'version': 'personal-v1', 'formal_version': 'formal-v1'}
            manifest['files'].append({'path': name, 'sha256': hashlib.sha256(files[name]).hexdigest(), 'bytes': len(files[name])})
        files["PUBLISHED_LIBRARY.json"] = raw_json(manifest)
        for p, data in files.items():
            target = repo / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "."],
                    ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"]):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
        self.commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        receipt = {"status": "PUBLISHED", "subject": subject, "branch": "main", "source_commit": self.commit,
                   "repository": manifest["repository"], "local_version": "v1",
                   "manifest_sha256": hashlib.sha256(files["PUBLISHED_LIBRARY.json"]).hexdigest()}
        state = repo.parent
        (state / "receipts").mkdir()
        (state / "latest.json").write_bytes(raw_json(receipt))
        (state / "receipts/fixture.json").write_bytes(raw_json(receipt))

    def close(self):
        self.temp.cleanup()


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = Fixture()
        cls.store = LibraryStore(cls.fixture.base)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.fixture.close()

    def test_large_utf8_read_continues_without_losing_bytes(self):
        chunks, offset = [], 0
        while True:
            page = self.store.fetch("math", "large.md", self.fixture.commit, offset, 10003)
            block = page["text"].encode()
            self.assertEqual(hashlib.sha256(block).hexdigest(), page["returned_sha256"])
            full_sha256 = hashlib.sha256(self.fixture.text).hexdigest()
            self.assertEqual(page["source_ref"], {
                "path": f"study-library://{self.fixture.commit}/large.md", "sha256": full_sha256})
            self.assertEqual(page["sha256"], full_sha256)
            self.assertNotEqual(page["returned_sha256"], full_sha256)
            self.assertEqual(page["hash_scope"], {"sha256": "complete_file",
                "source_ref.sha256": "complete_file", "returned_sha256": "returned_utf8_bytes"})
            self.assertEqual(offset + len(block), page["end_byte_exclusive"])
            chunks.append(block)
            if page["eof"]:
                break
            self.assertGreater(page["next_start_byte"], offset)
            offset = page["next_start_byte"]
        self.assertEqual(b"".join(chunks), self.fixture.text)

    def test_full_domain_and_null_event_identity(self):
        status = self.store.library("math", limit=2)
        self.assertEqual(status["domains"]["formal_cards"]["records"], 2)
        self.assertEqual(status["domains"]["review_events"]["null_ids"], 1)
        self.assertFalse(status["model_semantic_scan_complete"])
        a = self.store.query("math", "formal_cards", limit=1)
        b = self.store.query("math", "formal_cards", self.fixture.commit, offset=a["next_offset"], limit=1)
        self.assertEqual([a["records"][0]["id"], b["records"][0]["id"]], ["GS-001", "LA-001"])
        self.assertTrue(b["eof"])

    def test_cross_subject_id_and_arbitrary_paths_are_refused(self):
        for path in ("../latest.json", "/etc/passwd", ".git/config", "missing.md",
                     "formal/../large.md", "./large.md", "large.md/", "formal//index.json",
                     f"study-library://{self.fixture.commit}/large.md"):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                self.store.fetch("math", path)
        with self.assertRaises(BridgeError):
            self.store.query("math", "formal_cards", ids=["CO_2017_002"])
        with self.assertRaises(BridgeError):
            self.store.library("math", "0" * 40)

    def test_manifest_source_ref_uses_actual_manifest_hash(self):
        fixture = Fixture(subject="408")
        store = LibraryStore(fixture.base)
        try:
            raw = (fixture.base / "publication/408/checkout/PUBLISHED_LIBRARY.json").read_bytes()
            full_sha256 = hashlib.sha256(raw).hexdigest()
            for max_bytes in (100, len(raw)):
                result = store.fetch("408", "PUBLISHED_LIBRARY.json", fixture.commit, max_bytes=max_bytes)
                self.assertEqual(result["source_ref"], {
                    "path": f"study-library://{fixture.commit}/PUBLISHED_LIBRARY.json", "sha256": full_sha256})
                self.assertEqual(result["sha256"], full_sha256)
                self.assertEqual(result["returned_sha256"], hashlib.sha256(result["text"].encode()).hexdigest())
                self.assertEqual(result["eof"], max_bytes == len(raw))
            self.assertEqual(json.loads(result["text"])["subject"], "408")
        finally:
            store.close()
            fixture.close()

    def test_empty_file_is_explicit_eof_but_binary_is_not_empty_text(self):
        result = self.store.fetch("math", "empty.txt")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["total_bytes"], 0)
        self.assertTrue(result["eof"])
        with self.assertRaises(BridgeError):
            self.store.fetch("math", "unsafe.bin")
        with self.assertRaises(BridgeError):
            self.store.fetch("math", "large.md", max_bytes=1)

    def test_cache_tamper_is_detected(self):
        snapshot = self.store.snapshot("math")
        path = snapshot.path("empty.txt")
        path.chmod(0o600)
        path.write_bytes(b"modified")
        with self.assertRaises(BridgeError):
            snapshot.path("empty.txt")
        path.write_bytes(b"")
        path.chmod(0o400)

    def test_search_parses_records_even_without_github_code_index(self):
        result = self.store.search("math", "提示后正确")
        self.assertEqual(result["total_records"], 1)
        self.assertIsNone(result["records"][0]["id"])
        self.assertEqual(result["records"][0]["domain"], "review_events")

    def test_large_record_requires_sufficient_explicit_budget(self):
        row = {"id": "large-event", "record": "中" * 24000, "source_path": "capture.jsonl"}
        with self.assertRaises(BridgeError):
            self.store.page([row], 0, 1)
        page = self.store.page([row], 0, 1, 128 * 1024)
        self.assertEqual(page["records"], [row])
        self.assertTrue(page["eof"])
        with self.assertRaises(BridgeError):
            self.store.query("math", "formal_cards", max_bytes=1024 * 1024)

    def test_actual_image_decodes_and_preserves_original_hash(self):
        meta, data, mime = self.store.image("math", "image.png")
        self.assertEqual(mime, "image/png")
        self.assertEqual((meta["width"], meta["height"]), (3, 2))
        self.assertEqual(meta["source_sha256"], hashlib.sha256(data).hexdigest())

    def test_408_question_refs_are_pinned_paginated_and_do_not_scan_library(self):
        fixture = Fixture(subject="408")
        store = LibraryStore(fixture.base)
        try:
            snapshot = store.snapshot("408", fixture.commit)
            with patch.object(snapshot, "domains", side_effect=AssertionError("full scan must not run")):
                first = store.query("408", "question_refs", fixture.commit,
                                    ids=["CO_2012_005"], limit=1, max_bytes=24000)
                ids = store.query("408", "question_refs", fixture.commit,
                                  offset=1, limit=1, ids_only=True)
            self.assertEqual(first["source_commit"], fixture.commit)
            self.assertEqual(first["records"][0]["id"], "CO_2012_005")
            self.assertEqual(first["records"][0]["record"]["formal_state"],
                             {"formal_row_sha256": "1" * 64, "safe_card_sha256": "2" * 64})
            self.assertEqual(first["records"][0]["source_sha256"],
                             hashlib.sha256((fixture.base / "publication/408/checkout/PUBLISHED_LIBRARY.json").read_bytes()).hexdigest())
            self.assertFalse(first["native_source_scan_performed"])
            self.assertTrue(first["eof"])
            self.assertEqual(ids["records"], [{"id": "OS_2021_002", "source_path": "PUBLISHED_LIBRARY.json",
                                                "record_ordinal": 2}])
            with self.assertRaisesRegex(BridgeError, "IDS_NOT_IN_SUBJECT_DOMAIN"):
                store.query("408", "question_refs", fixture.commit, ids=["CO_2099_999"])
        finally:
            store.close()
            fixture.close()


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_handshake_tools_read_and_image(self):
        fixture = Fixture()
        try:
            params = StdioServerParameters(command=sys.executable,
                args=[str(BASE / "study_mcp.py"), "--base", str(fixture.base)], cwd=str(BASE))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    self.assertEqual({t.name for t in tools}, {"library", "query", "fetch", "search", "view_image", "workflow", "personal_context"})
                    self.assertTrue(all(t.annotations.readOnlyHint for t in tools))
                    fetch_description = next(t.description for t in tools if t.name == "fetch")
                    self.assertIn("Copy source_ref unchanged", fetch_description)
                    self.assertIn("complete-file hash", fetch_description)
                    result = await session.call_tool("fetch", {"subject": "math", "path": "large.md", "max_bytes": 1000})
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent["source_commit"], fixture.commit)
                    source_ref = {"path": f"study-library://{fixture.commit}/large.md",
                                  "sha256": hashlib.sha256(fixture.text).hexdigest()}
                    self.assertEqual(result.structuredContent["source_ref"], source_ref)
                    text_payload = json.loads(next(c.text for c in result.content if c.type == "text"))
                    self.assertEqual(text_payload["source_ref"], source_ref)
                    image = await session.call_tool("view_image", {"subject": "math", "path": "image.png"})
                    frames = [c for c in image.content if c.type == "image"]
                    self.assertEqual(len(frames), 1)
                    self.assertEqual(hashlib.sha256(base64.b64decode(frames[0].data)).hexdigest(),
                                     image.structuredContent["source_sha256"])
                    original = await session.call_tool("fetch", {"subject": "math", "path": "image.png"})
                    resources = [c.resource for c in original.content if c.type == "resource"]
                    self.assertEqual(len(resources), 1)
                    raw = base64.b64decode(resources[0].blob, validate=True)
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), original.structuredContent["sha256"])
                    self.assertFalse(original.structuredContent["host_file_materialization_verified"])
                    bundled = await session.call_tool("query", {"subject": "math", "domain": "source_files",
                                                               "ids": ["image.png", "large.md"]})
                    self.assertFalse(bundled.isError)
                    data = base64.b64decode(next(c.resource.blob for c in bundled.content if c.type == "resource"), validate=True)
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        self.assertEqual(archive.read("sources/image.png"), raw)
                        self.assertEqual(archive.read("sources/large.md"), fixture.text)
                        manifest = json.loads(archive.read("source_manifest.json"))
                        self.assertEqual(manifest["source_commit"], fixture.commit)
                    rejected = await session.call_tool("query", {"subject": "math", "domain": "source_files",
                                                                "ids": ["../unpublished.png"]})
                    self.assertFalse(rejected.structuredContent.get("ok", True))
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
