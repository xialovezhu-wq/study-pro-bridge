import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import study_web_read as web_read


class WebReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sources = {}
        self.manifest = {
            "subject": "math",
            "schema": "study-published-library-v1",
            "files": [],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def add_source(self, relative, raw, *, kind="formal_history", storage="git"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        self.sources[relative] = raw
        row = {
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "storage": storage,
            "kind": kind,
        }
        self.manifest["files"].append(row)
        return row

    def build(self):
        (self.root / "PUBLISHED_LIBRARY.json").write_bytes(
            json.dumps(self.manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        return web_read.build_web_read(self.root, self.manifest)

    def descriptor(self, source):
        return json.loads((self.root / source["transport"]["path"]).read_bytes())

    def assert_restored(self, relative, raw):
        output = self.root / "restored" / relative
        receipt = web_read.restore_source(self.root, relative, output)
        self.assertEqual(output.read_bytes(), raw)
        self.assertEqual(receipt["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(receipt["bytes"], len(raw))
        self.assertEqual(receipt["visual_view_status"], "NOT_RUN")
        self.assertEqual(receipt["semantic_read_status"], "NOT_RUN")

    def test_utf8_restore_preserves_crlf_no_final_newline_and_long_unicode_line(self):
        originals = {
            "history/crlf.md": "第一轮\r\n第二轮\r\n".encode("utf-8"),
            "history/no-final-newline.jsonl": b'{"turn":1}\n{"turn":2}',
            "history/long-line.txt": ("题😀\t\"\\" * 50000).encode("utf-8"),
            "history/empty.md": b"",
        }
        for relative, raw in originals.items():
            self.add_source(relative, raw)
        result = self.build()
        sources = {row["path"]: row for row in result["sources"]}
        for relative, raw in originals.items():
            with self.subTest(path=relative):
                self.assertEqual(self.descriptor(sources[relative])["encoding"], "utf8")
                (self.root / relative).unlink()
                self.assert_restored(relative, raw)
        long_source = self.descriptor(sources["history/long-line.txt"])
        self.assertGreater(len(long_source["parts"]), 1)

    def test_images_and_exact_pdf_pages_have_binary_transport(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        page = b"%PDF-1.4\nexact page fixture\n%%EOF\n"
        self.add_source("originals/question.png", png, kind="question_image")
        self.add_source("originals/page-0008.pdf", page, kind="original_page_pdf")
        self.add_source("originals/book.pdf", b"%PDF-1.4\nwhole book\n%%EOF\n", kind="original_pdf")
        lfs = self.add_source("originals/lfs.png", png, kind="question_image", storage="git-lfs")
        (self.root / lfs["path"]).write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:placeholder\n")
        result = self.build()
        sources = {row["path"]: row for row in result["sources"]}
        for relative, raw in {"originals/question.png": png, "originals/page-0008.pdf": page}.items():
            with self.subTest(path=relative):
                self.assertEqual(sources[relative]["status"], "AVAILABLE")
                self.assertEqual(self.descriptor(sources[relative])["encoding"], "base64")
                self.assert_restored(relative, raw)
        for relative in ("originals/book.pdf", "originals/lfs.png"):
            with self.subTest(path=relative):
                self.assertEqual(sources[relative]["status"], "NATIVE_ORIGINAL_ONLY")
                self.assertTrue(sources[relative]["reason"])
                self.assertNotIn("transport", sources[relative])
                with self.assertRaisesRegex(web_read.WebReadError, "ORIGINAL_NOT_PROJECTED"):
                    web_read.restore_source(self.root, relative, self.root / "unexpected")

    def test_generated_files_catalog_and_payloads_obey_bounded_transport(self):
        # Many aliases exercise catalog partitioning without thousands of objects.
        for number in range(1100):
            self.add_source(f"history/long-source-name-{number:05d}.md", b"shared evidence\n")
        self.add_source("history/large.txt", ("中文😀\n" * 40000).encode("utf-8"))
        result = self.build()
        start = json.loads((self.root / result["start"]["path"]).read_bytes())
        self.assertNotIn("sources", start)
        self.assertLessEqual(result["start"]["bytes"], 16 * 1024)
        catalog = json.loads((self.root / start["catalog"]["path"]).read_bytes())
        self.assertGreater(len(catalog["parts"]), 1)
        public_sources = []
        for part in catalog["parts"]:
            rows = [json.loads(line) for line in (self.root / part["path"]).read_bytes().splitlines()]
            self.assertEqual(len(rows), part["count"])
            public_sources.extend(rows)
        self.assertEqual(public_sources, result["sources"])
        self.assertEqual(catalog["count"], len(public_sources))
        for item in result["generated_files"]:
            raw = (self.root / item["path"]).read_bytes()
            self.assertLessEqual(len(raw), 256 * 1024, item["path"])
            self.assertEqual(item["bytes"], len(raw))
            self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(item["git_blob_sha1"], hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest())
        seen = set()
        for source in public_sources:
            if source.get("status") != "AVAILABLE" or source["sha256"] in seen:
                continue
            seen.add(source["sha256"])
            descriptor = self.descriptor(source)
            offset, records, eof = 0, 0, 0
            for part in descriptor["parts"]:
                lines = (self.root / part["path"]).read_bytes().splitlines()
                self.assertEqual(len(lines), part["lines"])
                self.assertEqual(part["source_byte_start"], offset)
                self.assertEqual(part["first_record"], records + 1)
                for line in lines:
                    record = json.loads(line)
                    if record.get("eof"):
                        eof += 1
                        self.assertEqual(record["records"], records)
                        continue
                    self.assertEqual(record["offset"], offset)
                    payload = record["data"].encode("utf-8") if descriptor["encoding"] == "utf8" else base64.b64decode(record["data"], validate=True)
                    self.assertLessEqual(len(payload), 1024)
                    offset += len(payload)
                    records += 1
                self.assertEqual(part["source_byte_end_exclusive"], offset)
            self.assertEqual(eof, 1)
            self.assertEqual(offset, source["bytes"])
            self.assertEqual(records, descriptor["data_records"])

    def test_same_sha_reuses_one_object_and_repeated_build_is_deterministic(self):
        self.add_source("history/a.md", b"same original bytes\r\n")
        self.add_source("history/b.md", b"same original bytes\r\n")
        first = self.build()
        sources = {row["path"]: row for row in first["sources"]}
        self.assertEqual(sources["history/a.md"]["transport"], sources["history/b.md"]["transport"])
        self.assertEqual(first["counts"]["unique_objects"], 2)  # shared source and native manifest
        snapshot = {row["path"]: (self.root / row["path"]).read_bytes() for row in first["generated_files"]}
        second = self.build()
        self.assertEqual(first, second)
        self.assertEqual(snapshot, {row["path"]: (self.root / row["path"]).read_bytes() for row in second["generated_files"]})

    def test_source_hash_or_size_drift_rejects_build(self):
        row = self.add_source("history/source.md", b"original")
        for replacement in (b"tampered", b"different-size"):
            with self.subTest(replacement=replacement):
                (self.root / row["path"]).write_bytes(replacement)
                with self.assertRaisesRegex(web_read.WebReadError, "SOURCE_BYTES_MISMATCH"):
                    self.build()

    def test_duplicate_sha_locator_still_checks_its_own_source_bytes(self):
        self.add_source("history/a.md", b"shared")
        self.add_source("history/b.md", b"shared")
        (self.root / "history/b.md").write_bytes(b"drifts")
        with self.assertRaisesRegex(web_read.WebReadError, "SOURCE_BYTES_MISMATCH"):
            self.build()

    def test_changed_segment_is_rejected_without_overwriting_output(self):
        self.add_source("history/source.md", b"full original dialogue\n")
        result = self.build()
        source = next(row for row in result["sources"] if row["path"] == "history/source.md")
        part = self.descriptor(source)["parts"][0]
        path = self.root / part["path"]
        path.write_bytes(path.read_bytes().replace(b"original", b"tampered"))
        output = self.root / "previous-result.md"
        output.write_bytes(b"keep previous result")
        with self.assertRaisesRegex(web_read.WebReadError, "TRANSPORT_BYTES_MISMATCH"):
            web_read.restore_source(self.root, "history/source.md", output)
        self.assertEqual(output.read_bytes(), b"keep previous result")

    def test_missing_eof_is_rejected_even_if_segment_and_descriptor_hashes_match(self):
        self.add_source("history/source.md", b"full original dialogue\n")
        result = self.build()
        source = next(row for row in result["sources"] if row["path"] == "history/source.md")
        descriptor = self.descriptor(source)
        part = descriptor["parts"][0]
        path = self.root / part["path"]
        raw = b"\n".join(path.read_bytes().splitlines()[:-1]) + b"\n"
        path.write_bytes(raw)
        part.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), lines=part["lines"] - 1)
        descriptor_raw = json.dumps(descriptor, ensure_ascii=False).encode("utf-8")
        transport = dict(source["transport"])
        (self.root / transport["path"]).write_bytes(descriptor_raw)
        transport.update(sha256=hashlib.sha256(descriptor_raw).hexdigest(), bytes=len(descriptor_raw))
        output = self.root / "unexpected"
        with self.assertRaisesRegex(web_read.WebReadError, "RESTORED_SOURCE_MISMATCH_OR_MISSING_EOF"):
            web_read.restore_manifest(self.root, transport, output, expected_source=source)
        self.assertFalse(output.exists())

    def test_restore_rejects_parent_traversal_and_symlink_transport(self):
        self.add_source("history/source.md", b"evidence")
        result = self.build()
        source = next(row for row in result["sources"] if row["path"] == "history/source.md")
        transport = dict(source["transport"])
        transport["path"] = "../outside.json"
        with self.assertRaisesRegex(web_read.WebReadError, "UNSAFE_TRANSPORT_PATH"):
            web_read.restore_manifest(self.root, transport, self.root / "unexpected")
        descriptor_path = self.root / source["transport"]["path"]
        copied = self.root / "descriptor-copy.json"
        copied.write_bytes(descriptor_path.read_bytes())
        descriptor_path.unlink()
        descriptor_path.symlink_to(copied)
        with self.assertRaisesRegex(web_read.WebReadError, "TRANSPORT_SYMLINK"):
            web_read.restore_source(self.root, "history/source.md", self.root / "unexpected")


if __name__ == "__main__":
    unittest.main()
