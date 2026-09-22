"""Bounded synthetic tests: no CU attestation, models, network, or native writes."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

import study_bridge as bridge
import bridge_archives as archive
import test_study_bridge as fixture
import study_web_read as web_transport
from test_study_bridge import COMMIT, FakePublished, FakeOwners


class ReadGateTests(unittest.TestCase):
    setUp = fixture.BridgeTests.setUp
    quick = fixture.BridgeTests.quick
    prepare = fixture.BridgeTests.prepare
    sent = fixture.BridgeTests.sent
    observed = fixture.BridgeTests.observed
    output = fixture.BridgeTests.output
    advice = fixture.BridgeTests.advice
    ref = fixture.BridgeTests.ref
    fixture_source = fixture.BridgeTests.fixture_source
    b_documents = fixture.BridgeTests.b_documents

    def evidence(self, run, change=None):
        path = Path(run["web_evidence"]["path"])
        value = json.loads(path.read_text())
        if change:
            change(value)
            bridge.write(path, bridge.json_bytes(value))
        return path, value

    def test_mechanical_pass_is_partial_without_separate_local_CU_confirmation(self):
        run = self.prepare()
        self.observed(run)
        path, value = self.evidence(run)
        value.update(origin="root", actual_cu_observation_confirmed=True, status="PASS")
        bridge.write(path, bridge.json_bytes(value))
        result = self.bridge.inspect_web_read(run, path)
        self.assertEqual(result["scan"]["mechanical_status"], "PASS")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["business_admitted"])
        self.assertIsNone(result["root_cu_confirmation"])
        self.bridge.record_web_read(run, path)
        self.bridge.download(run, self.advice(run))
        self.assertEqual(run["stage"], "DOWNLOADED_AWAITING_WEB_EVIDENCE")
        self.assertFalse(self.owners.imports)

    def test_full_count_does_not_override_id_range_or_EOF_defects(self):
        run = self.prepare()
        self.observed(run)
        path, good = self.evidence(run)
        cases = {
            "missing_id": lambda e: e["scan"]["domains"][1].update(scanned_ids=[]),
            "duplicate_id": lambda e: e["scan"]["domains"][1].update(scanned_ids=["Q-1", "Q-1"]),
            "wrong_commit": lambda e: e["scan"].update(source_commit="b" * 40),
            "gap": lambda e: e["scan"]["sources"][0]["ranges"][0].update(start_line=2),
            "no_eof": lambda e: e["scan"]["sources"][0]["ranges"][0].update(eof=False),
            "fake_eof_marker": lambda e: e["scan"]["sources"][0]["ranges"][0].update(eof_marker="not observed"),
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                value = copy.deepcopy(good)
                change(value)
                bridge.write(path, bridge.json_bytes(value))
                result = self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(result["scan"]["mechanical_status"], "PARTIAL")

    def test_changed_observation_cannot_reuse_verified_read_receipt(self):
        run = self.prepare()
        self.observed(run)
        self.bridge.download(run, self.advice(run))
        _, value = self.evidence(run)
        observed = Path(value["read_operations"][0]["observation_file"])
        observed.write_text("changed actual observation")
        with self.assertRaisesRegex(bridge.BridgeError, "WEB_TOOL_OBSERVATION_MISMATCH"):
            self.bridge.validate_output(run)

    def test_current_conversation_snippet_does_not_count_as_complete_read(self):
        run = self.prepare()
        self.observed(run)
        def shorten(value):
            op = next(op for op in value["read_operations"] if op["path"].endswith("original/conversation.json"))
            op["content_quote"] = "actual fixture"
        path, _ = self.evidence(run, shorten)
        result = self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(any("TEXT_NOT_COMPLETELY" in issue for issue in result["current_input_issues"]))

    def split_observation(self, value, path, cuts):
        index = next(i for i, op in enumerate(value["read_operations"]) if op["path"] == path)
        original = value["read_operations"][index]
        data = original["content_quote"].encode()
        chunks = []
        for number, (start, end) in enumerate(zip(cuts, cuts[1:])):
            chunk = data[start:end].decode()
            saved = self.base / "observations" / f"segment-{number}.txt"
            terminal = end == len(data)
            saved.write_text(chunk + ("\nOBSERVED_EOF\n" if terminal else ""))
            chunks.append({**original, "content_quote": chunk, "content_sha256": bridge.sha(chunk.encode()),
                           "start_byte": start, "end_byte": end, "observation_file": str(saved),
                           "observation_sha256": bridge.file_hash(saved), "truncated": False,
                           "eof": terminal, "eof_marker": "OBSERVED_EOF" if terminal else None})
        value["read_operations"][index] = chunks[0]
        value["read_operations"].extend(chunks[1:])
        return chunks

    def test_segmented_current_conversation_passes_with_byte_hash_and_observed_EOF(self):
        run = self.prepare()
        self.observed(run)
        path, value = self.evidence(run)
        conversation = next(op["path"] for op in value["read_operations"] if op["path"].endswith("conversation.json"))
        original = next(op["content_quote"] for op in value["read_operations"] if op["path"] == conversation).encode()
        self.split_observation(value, conversation, [0, 7, len(original)])
        bridge.write(path, bridge.json_bytes(value))
        self.bridge.record_web_read(run, path, confirm_observed_cu=True)
        self.assertEqual(run["web_read_status"], "verified")
        self.bridge.download(run, self.advice(run))
        self.assertEqual(run["stage"], "AWAITING_LOCAL_DECISION")

    def test_segmented_current_conversation_rejects_gap_hash_and_missing_EOF(self):
        run = self.prepare()
        self.observed(run)
        path, original = self.evidence(run)
        conversation = next(op["path"] for op in original["read_operations"] if op["path"].endswith("conversation.json"))
        size = len(next(op["content_quote"] for op in original["read_operations"] if op["path"] == conversation).encode())
        for defect in ("gap", "hash", "eof", "truncated"):
            with self.subTest(defect=defect):
                value = copy.deepcopy(original)
                chunks = self.split_observation(value, conversation, [0, 7, size])
                if defect == "gap":
                    chunks[1]["start_byte"] += 1
                elif defect == "hash":
                    chunks[1]["content_sha256"] = "0" * 64
                elif defect == "eof":
                    chunks[1]["eof"] = False
                else:
                    chunks[1]["truncated"] = True
                bridge.write(path, bridge.json_bytes(value))
                checked = self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)
                self.assertEqual(checked["status"], "PARTIAL")

    def test_byte_offsets_reconcile_unicode_and_cross_segment_reference(self):
        data = "甲乙\n末轮。".encode()
        saved = self.base / "observations"
        saved.mkdir()
        value = {"read_operations": [{"path": "conversation.json", "content_quote": data.decode(),
                                       "observation_file": str(saved / "initial")} ]}
        self.split_observation(value, "conversation.json", [0, len("甲乙".encode()), len(data)])
        coverage = bridge.observed_text_coverage("conversation.json", data, value["read_operations"])
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["sha256"], bridge.sha(data))
        bridge.require_observed_reference("conversation.json", data, "乙\n末轮", value["read_operations"], complete=True)

    def test_B_reference_quote_must_be_in_observed_part_of_same_file(self):
        run = self.prepare("B")
        self.observed(run)
        path, value = self.evidence(run)
        op = next(op for op in value["read_operations"] if op["path"] == "history.md")
        op["content_quote"] = "old-event 2026-08-01 mechanism old\n"
        bridge.write(path, bridge.json_bytes(value))
        self.bridge.record_web_read(run, path, confirm_observed_cu=True)
        with self.assertRaisesRegex(bridge.BridgeError, "SOURCE_QUOTE_NOT_OBSERVED_IN_AUTHORIZED_TASK"):
            self.bridge.download(run, self.output(run, self.b_documents()))
        self.assertFalse(self.owners.imports)

    def test_B_conversation_reference_requires_complete_observed_bytes(self):
        self.fixture_source("history/conversation.json", "saved first turn\nsaved final turn\n")
        run = self.prepare("B")
        self.observed(run)
        path, value = self.evidence(run)
        op = next(op for op in value["read_operations"] if op["path"] == "history/conversation.json")
        op["content_quote"] = "saved first turn"
        bridge.write(path, bridge.json_bytes(value))
        self.bridge.record_web_read(run, path, confirm_observed_cu=True)
        docs = self.b_documents()
        item = json.loads(docs["questions.jsonl"])
        item["source_refs"].append(self.ref("history/conversation.json", "saved first turn"))
        docs["questions.jsonl"] = json.dumps(item) + "\n"
        with self.assertRaisesRegex(bridge.BridgeError, "REFERENCED_CONVERSATION_NOT_COMPLETELY_OBSERVED"):
            self.bridge.download(run, self.output(run, docs))
        self.assertFalse(self.owners.imports)

    def assert_B_original_guard(self, mode):
        run = self.prepare("B")
        self.observed(run)
        docs = self.b_documents()
        if mode == "no_binding":
            item = json.loads(docs["questions.jsonl"])
            item["source_refs"] = []
            docs["questions.jsonl"] = json.dumps(item) + "\n"
        else:
            path, value = self.evidence(run)
            index = next(i for i, op in enumerate(value["read_operations"]) if op.get("kind") == "image")
            value["read_operations"].pop(index)
            for source in value["scan"]["sources"]:
                for chunk in source["ranges"]:
                    if chunk["operation_index"] > index:
                        chunk["operation_index"] -= 1
            bridge.write(path, bridge.json_bytes(value))
            self.bridge.record_web_read(run, path, confirm_observed_cu=True)
        expected = "B_REQUIRED_ORIGINAL_REFERENCE_MISSING" if mode == "no_binding" else "SOURCE_NOT_OBSERVED_IN_AUTHORIZED_TASK"
        with self.assertRaisesRegex(bridge.BridgeError, expected):
            self.bridge.download(run, self.output(run, docs))
        self.assertFalse(self.owners.imports)

    def test_B_requires_original_image_binding(self):
        self.assert_B_original_guard("no_binding")

    def test_B_requires_actual_original_image_view(self):
        self.assert_B_original_guard("no_view")

    def test_B_unrelated_published_diagram_cannot_replace_required_question_image(self):
        self.fixture_source("assistant-diagram.png", b"\x89PNG\r\n\x1a\nsynthetic generated diagram")
        run = self.prepare("B")
        self.observed(run)
        docs = self.b_documents()
        item = json.loads(docs["questions.jsonl"])
        item["source_refs"] = [self.ref("assistant-diagram.png")]
        docs["questions.jsonl"] = json.dumps(item) + "\n"
        with self.assertRaisesRegex(bridge.BridgeError, "B_REQUIRED_ORIGINAL_REFERENCE_MISSING"):
            self.bridge.download(run, self.output(run, docs))
        self.assertFalse(self.owners.imports)

    def test_engineering_receipt_and_control_scan_failure_never_reach_import(self):
        for controls in ({"artifact_type": "engineering_read_receipt"}, {"scan": {"scan_complete": False}}):
            with self.subTest(controls=controls):
                folder = self.root / "control-envelope"
                folder.mkdir(exist_ok=True)
                bridge.write(folder / "advice.json", bridge.json_bytes({"items": []}))
                blockers = bridge.output_completion_blockers({"project": "A", **controls}, folder)
                self.assertTrue(blockers)
        run = self.prepare()
        self.sent(run)
        self.bridge.download(run, self.output(run, {"engineering_summary.json": {"status": "PARTIAL"}}))
        self.assertEqual(run["stage"], "DOWNLOADED_NONCOMPLETE")
        self.assertTrue(any("REQUIRED_BUSINESS_OUTPUT_MISSING:advice.json" in reason
                            for reason in run["output_admission"]["reasons"]))
        self.assertFalse(self.owners.imports)

    def transported_scan(self, omit_final_lf=False, separate_eof=False):
        # Real publisher output, including escaped newlines/quotes and enough
        # UTF-8 bytes to require multiple eight-line connector observations.
        self.fixture_source("native-index.json", bridge.json_bytes({"cards": [
            {"id": "Q-1", "summary": '用户原句："条件"\n下一行。' * 1200}]}).decode())
        run = self.prepare()
        self.observed(run)
        evidence_path, evidence = self.evidence(run)
        built = web_transport.build_web_read(self.published.root, self.published.manifest)
        for generated in built["generated_files"]:
            self.published.files[generated["path"]] = self.published.root / generated["path"]
        source = next(row for row in built["sources"] if row["path"] == "native-index.json")
        descriptor = json.loads(self.published.files[source["transport"]["path"]].read_bytes())
        original_index = next(i for i, op in enumerate(evidence["read_operations"]) if op["path"] == source["path"])
        chunks, ranges = [], []
        for part in descriptor["parts"]:
            lines = self.published.files[part["path"]].read_bytes().splitlines(keepends=True)
            data_end = len(lines) - 1 if separate_eof and json.loads(lines[-1]).get("eof") else len(lines)
            groups = [(first, min(first + 8, data_end)) for first in range(0, data_end, 8)]
            if data_end < len(lines):
                groups.append((data_end, len(lines)))
            for first, last in groups:
                raw = b"".join(lines[first:last])
                actual_raw = raw[:-1] if omit_final_lf else raw
                records = [json.loads(line) for line in raw.splitlines()]
                payloads = [record["data"].encode() for record in records if "data" in record]
                decoded = b"".join(payloads)
                offset = next((record["offset"] for record in records if "offset" in record), source["bytes"])
                saved = self.base / "observations" / f"transport-{len(chunks)}.txt"
                saved.write_bytes(b"ACTUAL_TOOL_JSONL_BODY\n" + actual_raw)
                operation = {"path": source["path"], "source_commit": COMMIT, "tool": "fixture-github-fetch-file",
                    "result_locator": f"fixture-return-{len(chunks)}", "observation_file": str(saved),
                    "observation_sha256": bridge.file_hash(saved), "truncated": False,
                    "projection": {"schema": "study-web-observation-v1", "source_sha256": source["sha256"],
                        "manifest_path": source["transport"]["path"], "part_path": part["path"],
                        "start_line": first + 1, "end_line": last,
                        "raw_quote": actual_raw.decode(), "raw_sha256": bridge.sha(actual_raw)}}
                index = original_index if not chunks else len(evidence["read_operations"]) + len(chunks) - 1
                ranges.append({"operation_index": index, "start_byte": offset, "end_byte": offset + len(decoded),
                               "content_sha256": bridge.sha(decoded)})
                chunks.append(operation)
        evidence["read_operations"][original_index] = chunks[0]
        evidence["read_operations"].extend(chunks[1:])
        scan_source = next(row for row in evidence["scan"]["sources"] if row["path"] == source["path"])
        scan_source.pop("ranges")
        scan_source["byte_ranges"] = ranges
        bridge.write(evidence_path, bridge.json_bytes(evidence))
        return run, evidence_path, evidence, scan_source

    def test_real_publisher_escaped_transport_reaches_read_gate_via_observed_byte_ranges(self):
        run, path, evidence, source = self.transported_scan()
        self.assertGreater(len(source["byte_ranges"]), 1)
        restored_text = self.published.files["native-index.json"].read_text()
        self.assertTrue(all(restored_text not in Path(op["observation_file"]).read_text()
                            for op in evidence["read_operations"] if op.get("projection")))
        checked = self.bridge.inspect_web_read(run, path)
        self.assertEqual(checked["scan"]["mechanical_status"], "PASS")
        self.assertFalse(checked["business_admitted"])
        self.bridge.record_web_read(run, path, confirm_observed_cu=True)
        offset = restored_text.index("用户")
        reference = self.ref("native-index.json", restored_text[offset:offset + 20])
        self.bridge.download(run, self.output(run, {"advice.json": {"items": [{
            "package_id": run["input_packages"][0]["package_id"], "question_id": "Q-1",
            "recommendation": "isolated fixture suggestion", "uncertainties": [], "evidence_refs": [reference]}]}}))
        self.assertEqual(run["stage"], "AWAITING_LOCAL_DECISION")

    def test_transport_missing_observed_segment_keeps_scan_partial(self):
        run, path, evidence, source = self.transported_scan()
        omitted = source["byte_ranges"][1]["operation_index"]
        evidence["read_operations"].pop(omitted)
        for row in evidence["scan"]["sources"]:
            for key in ("ranges", "byte_ranges"):
                if key in row:
                    row[key] = [chunk for chunk in row[key] if chunk["operation_index"] != omitted]
                    for chunk in row[key]:
                        if chunk["operation_index"] > omitted:
                            chunk["operation_index"] -= 1
        bridge.write(path, bridge.json_bytes(evidence))
        checked = self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)
        self.assertEqual(checked["status"], "PARTIAL")
        self.assertTrue(any(row["code"] == "SCAN_SOURCE_NOT_READ_THROUGH_EOF" for row in checked["scan"]["issues"]))

    def test_connector_omitted_final_lf_normalizes_exact_ranges_and_eof_only_line(self):
        run, path, evidence, _ = self.transported_scan(omit_final_lf=True, separate_eof=True)
        projected = [op for op in evidence["read_operations"] if op.get("projection")]
        self.assertTrue(any(op["projection"]["start_line"] == op["projection"]["end_line"]
                            and json.loads(op["projection"]["raw_quote"]).get("eof") is True for op in projected))
        self.assertTrue(all(not op["projection"]["raw_quote"].endswith("\n") for op in projected))
        checked = self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)
        self.assertEqual(checked["status"], "PASS")
        self.assertTrue(all(row["line_ending"] == "fetch_file_omits_final_lf"
                            and row["actual_raw_sha256"] != row["canonical_range_sha256"]
                            for row in checked["projection_normalizations"]))
        # An optional explicit declaration must describe the observed return.
        for op in projected:
            op["projection"]["line_ending"] = "fetch_file_omits_final_lf"
        bridge.write(path, bridge.json_bytes(evidence))
        self.assertEqual(self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)["status"], "PASS")

    def test_final_lf_compatibility_rejects_other_rewrites_and_canonical_raw_hash(self):
        run, path, original, _ = self.transported_scan(omit_final_lf=True, separate_eof=True)
        for defect in ("removed_second_newline", "content_changed", "added_space", "wrong_declaration", "canonical_hash"):
            with self.subTest(defect=defect):
                evidence = copy.deepcopy(original)
                op = next(op for op in evidence["read_operations"] if op.get("projection"))
                projection = op["projection"]
                if defect == "removed_second_newline":
                    projection["raw_quote"] = projection["raw_quote"].replace("\n", "", 1)
                elif defect == "content_changed":
                    projection["raw_quote"] = projection["raw_quote"].replace("用户", "伪造", 1)
                elif defect == "added_space":
                    projection["raw_quote"] += " "
                elif defect == "wrong_declaration":
                    projection["line_ending"] = "verbatim"
                projection["raw_sha256"] = bridge.sha((projection["raw_quote"] + ("\n" if defect == "canonical_hash" else "")).encode())
                observed = Path(op["observation_file"])
                observed.write_text(projection["raw_quote"])
                op["observation_sha256"] = bridge.file_hash(observed)
                bridge.write(path, bridge.json_bytes(evidence))
                expected = "WEB_PROJECTION_LINE_ENDING_DECLARATION_MISMATCH" if defect == "wrong_declaration" else "WEB_PROJECTION_RAW_RANGE_MISMATCH"
                with self.assertRaisesRegex(bridge.BridgeError, expected):
                    self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)

    def test_transport_wrong_original_hash_is_rejected(self):
        run, path, evidence, _ = self.transported_scan()
        op = next(op for op in evidence["read_operations"] if op.get("projection"))
        op["projection"]["source_sha256"] = "0" * 64
        bridge.write(path, bridge.json_bytes(evidence))
        with self.assertRaisesRegex(bridge.BridgeError, "WEB_PROJECTION_SOURCE_BINDING_MISMATCH"):
            self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)

    def test_self_consistent_forged_projection_is_rejected_against_pinned_part(self):
        run, path, evidence, _ = self.transported_scan()
        op = next(op for op in evidence["read_operations"] if op.get("projection"))
        projection = op["projection"]
        projection["raw_quote"] = projection["raw_quote"].replace("用户", "伪造", 1)
        projection["raw_sha256"] = bridge.sha(projection["raw_quote"].encode())
        observed = Path(op["observation_file"])
        observed.write_text(projection["raw_quote"])
        op["observation_sha256"] = bridge.file_hash(observed)
        bridge.write(path, bridge.json_bytes(evidence))
        with self.assertRaisesRegex(bridge.BridgeError, "WEB_PROJECTION_RAW_RANGE_MISMATCH"):
            self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)

    def test_submitted_decoding_marker_is_not_a_projection_verification(self):
        run, path, evidence, _ = self.transported_scan()
        op = next(op for op in evidence["read_operations"] if op.get("projection"))
        projection = op.pop("projection")
        op["_bridge_projection_raw"] = projection["raw_quote"]
        op["content_quote"] = self.published.files["native-index.json"].read_text()
        bridge.write(path, bridge.json_bytes(evidence))
        with self.assertRaisesRegex(bridge.BridgeError, "OBSERVATION_QUOTE_NOT_IN_SOURCE_AND_TOOL_RESULT"):
            self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)

    def test_real_base64_transport_restoration_does_not_count_as_image_view(self):
        run, path, evidence, _ = self.transported_scan()
        start = json.loads(self.published.files["WEB_READ_START.json"].read_bytes())
        catalog = json.loads(self.published.files[start["catalog"]["path"]].read_bytes())
        rows = [json.loads(line) for part in catalog["parts"]
                for line in self.published.files[part["path"]].read_bytes().splitlines()]
        source = next(row for row in rows if row["path"] == "problem.png")
        descriptor = json.loads(self.published.files[source["transport"]["path"]].read_bytes())
        self.assertEqual(descriptor["encoding"], "base64")
        part = descriptor["parts"][0]
        raw = self.published.files[part["path"]].read_bytes()
        saved = self.base / "observations" / "downloaded-image-transport.txt"
        saved.write_bytes(raw)
        op = next(op for op in evidence["read_operations"] if op["path"] == "problem.png")
        op.update(observation_file=str(saved), observation_sha256=bridge.file_hash(saved), truncated=False,
                  projection={"schema": "study-web-observation-v1", "source_sha256": source["sha256"],
                      "manifest_path": source["transport"]["path"], "part_path": part["path"],
                      "start_line": 1, "end_line": part["lines"], "raw_quote": raw.decode(), "raw_sha256": bridge.sha(raw)})
        bridge.write(path, bridge.json_bytes(evidence))
        checked = self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)
        self.assertIn("problem.png", checked["read_paths"])
        self.assertNotIn("problem.png", checked["image_paths"])

    def test_structured_advice_uses_only_declared_refs_and_exact_current_members(self):
        run = self.prepare()
        self.observed(run)
        with self.bridge.input_materials(run) as current:
            name = next(name for name in current if name.endswith("original/conversation.json"))
            original = current[name]["local_source"].read_bytes()
            reference = {"path": name, "sha256": current[name]["sha256"], "quote": original.decode()}
        recommendation = {"proposed_file": {"path": "future/new-file.json", "sha256": "not-evidence"}, "action": "needs local review"}
        item = {"package_id": run["input_packages"][0]["package_id"], "question_id": "Q-1",
                "recommendation": recommendation, "uncertainties": [], "evidence_refs": [reference]}
        self.assertEqual(bridge.collect_references(item), [reference])
        self.bridge.download(run, self.output(run, {"advice.json": {"items": [item]}}))
        self.assertEqual(run["stage"], "AWAITING_LOCAL_DECISION")
        saved = json.loads((Path(run["output_dir"]) / "advice.json").read_text())
        self.assertEqual(saved["items"][0]["recommendation"], recommendation)
        self.assertFalse(self.owners.imports)

    def test_exact_output_copy_resolves_but_wrong_package_and_alias_are_rejected(self):
        run = self.prepare()
        self.observed(run)
        with self.bridge.input_materials(run) as current:
            name = next(name for name in current if name.endswith("original/conversation.json"))
            original = current[name]["local_source"].read_bytes()
            ref = {"path": "copied/current.json", "sha256": bridge.sha(original)}
            root = self.root / "copies"
            bridge.write(root / ref["path"], original)
            item = {"package_id": run["input_packages"][0]["package_id"], "question_id": "Q-1"}
            self.assertEqual(bridge.resolve_current_reference(ref, item, current, root, run), name)
            with self.assertRaisesRegex(bridge.BridgeError, "PACKAGE_ITEM_OR_HASH_MISMATCH"):
                bridge.resolve_current_reference(dict(ref, package_id="another-package"), item, current, root, run)
            with self.assertRaisesRegex(bridge.BridgeError, "NOT_REGISTERED"):
                bridge.resolve_current_reference(dict(ref, path="absent.json", source_type="current_input"), item, current, root, run)
            item2 = {**item, "package_id": None}
            with self.assertRaisesRegex(bridge.BridgeError, "IDENTITY_MISSING"):
                bridge.resolve_current_reference(ref, item2, current, root, run)
            metadata_copy = {"path": "copied/metadata.json", "sha256": bridge.sha(b"{}")}
            bridge.write(root / metadata_copy["path"], b"{}")
            with self.assertRaisesRegex(bridge.BridgeError, "ORIGIN_AMBIGUOUS"):
                bridge.resolve_current_reference(metadata_copy, item, current, root, run)
            resolved = bridge.resolve_current_reference(dict(metadata_copy, member_path="original/source.json"), item, current, root, run)
            self.assertTrue(resolved.endswith("original/source.json"))

    def test_A_without_original_package_identity_is_blocked_even_if_not_declared_partial(self):
        run = self.prepare()
        self.observed(run)
        output = self.output(run, {"advice.json": {"items": [{"package_id": None, "question_id": "Q-1",
            "recommendation": {"proposal": "unresolved source"}, "uncertainties": []}]}})
        with self.assertRaisesRegex(bridge.BridgeError, "A_ADVICE_PACKAGE_COVERAGE_MISMATCH"):
            self.bridge.download(run, output)
        self.assertEqual(run["stage"], "DOWNLOADED")
        self.assertTrue((Path(run["output_dir"]).parent / "download.zip").is_file())
        self.assertFalse(self.owners.imports)

    def test_explicit_partial_download_can_be_rejected_locally_without_import(self):
        run = self.prepare()
        self.observed(run)
        output = self.output(run, {"advice.json": {"status": "PARTIAL", "ready_for_formal_intake": False,
            "items": [{"package_id": None, "recommendation": {"proposal": "unresolved source"}}]}})
        self.bridge.download(run, output)
        self.assertEqual(run["stage"], "DOWNLOADED_NONCOMPLETE")
        self.assertEqual(archive.file_hash(Path(run["output_dir"]).parent / "download.zip"), archive.file_hash(output))
        self.bridge.record_a_disposition(run, "rejected", "Current package identities and scan are incomplete")
        self.bridge.record_a_disposition(run, "needs_review", "Await actual original evidence")
        self.assertEqual(len(run["local_review_dispositions"]), 2)
        self.assertNotIn("local_decision", run)
        self.assertFalse(self.owners.imports)
        with self.assertRaisesRegex(bridge.BridgeError, "OUTPUT_EXPLICITLY_NONCOMPLETE"):
            self.bridge.validate_output(run)

    def test_accept_binding_rechecks_gate_but_committed_recovery_does_not_replay(self):
        run = self.prepare()
        self.observed(run)
        self.bridge.download(run, self.advice(run))
        receipt = {"package_ids": [run["input_packages"][0]["package_id"]], "publication_event_id": "fixture-pub",
                   "decisions": [{"outcome": "accepted"}]}
        self.owners.verify_a = lambda *args: receipt
        run["web_read_status"] = "PARTIAL"
        with self.assertRaisesRegex(bridge.BridgeError, "FULL_HISTORY_SCAN_NOT_VERIFIED"):
            self.bridge.bind_a_decision(run, self.root / "fixture-receipt.json")
        run.update(stage="LOCAL_DECIDED_CLOUD_PENDING", local_decision={"receipts": [receipt]})
        with mock.patch.object(self.bridge, "validate_output", side_effect=AssertionError("must not re-admit committed work")):
            self.bridge.bind_a_decision(run, self.root / "fixture-receipt.json")
        self.assertEqual(run["stage"], "LOCAL_DECIDED_PUBLISHED")
        self.assertFalse(self.owners.imports)

    def test_408_history_domains_do_not_force_canonical_originals_and_unsupported_cell(self):
        self.published = FakePublished(self.root / "formal408", "408")
        self.owners = FakeOwners(self.published)
        self.bridge = bridge.Bridge(self.base, self.published, self.owners)
        run = self.prepare("D", "408")
        self.observed(run)
        path, _ = self.evidence(run)
        result = self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)
        domains = {row["name"]: row for row in result["scan"]["expected_domains"]}
        self.assertEqual(domains["formal_node_ids"]["expected_count"], 1)
        self.assertEqual(domains["history_package_ids"]["expected_count"], 1)
        self.assertNotIn("canonical_question_ids", domains)
        self.assertNotIn("canonical.csv", {row["path"] for row in result["scan"]["sources"]})
        inspection_context = {**run, "project": "C"}
        result = self.bridge.inspect_web_read(inspection_context, path, confirm_observed_cu=True)
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["business_admitted"])
        with self.assertRaisesRegex(bridge.BridgeError, "INVALID_PROJECT_SUBJECT"):
            self.bridge.prepare("C", "408", "2026-09-07", "unsupported")
        self.assertFalse(self.owners.imports)

    def test_408_history_status_comes_from_native_fields_not_legacy_prefix(self):
        native = FakePublished(self.root / "status408", "408")
        native.manifest["history_index"] = [
            {"package_id": "LEGACY-complete", "conversation_complete": True},
            {"package_id": "LEGACY-incomplete", "conversation_complete": False},
            {"package_id": "PENDING", "formal_status": "pending_local_judgment"},
        ]
        texts = {path: native.files[path].read_text() for path in bridge.scan_source_paths("408", native.manifest)}
        domains = bridge.scan_id_domains("408", native.manifest, texts)
        self.assertEqual(domains["history_complete_package_ids"], ["LEGACY-complete"])
        self.assertEqual(domains["history_incomplete_package_ids"], ["LEGACY-incomplete"])
        self.assertEqual(domains["history_pending_package_ids"], ["PENDING"])
        native.manifest["history_index"].append(native.manifest["history_index"][0])
        with self.assertRaisesRegex(bridge.BridgeError, "NATIVE_SCAN_DUPLICATE_RECORD_ID"):
            bridge.scan_id_domains("408", native.manifest, texts)

    def test_english_formal_bank_is_required_but_generic_corpus_and_inventory_are_not(self):
        self.published = FakePublished(self.root / "formalenglish", "english")
        self.owners = FakeOwners(self.published)
        self.bridge = bridge.Bridge(self.base, self.published, self.owners)
        run = self.prepare("A", "english")
        self.observed(run)
        required = bridge.scan_source_paths("english", self.published.manifest)
        self.assertIn("bank/master_bank.csv", required)
        self.assertIn("bank/sentence_patterns.md", required)
        domains = {row["name"]: row["expected_count"] for row in run["web_evidence"]["scan"]["expected_domains"]}
        self.assertEqual(domains["formal_bank_ids"], 1)
        self.assertEqual(domains["sentence_pattern_ids"], 1)
        modified = copy.deepcopy(self.published.manifest)
        modified["files"].extend([{"path": "raw/reference_relations/writing-vocabulary-foundation/nodes.jsonl"},
                                  {"path": "raw/articles/exam-reading-corpus/2011/text-3.json"}])
        modified["file_inventory"] = [{"path": "published-inventory/fixture.jsonl", "sha256": "f" * 64}]
        self.assertEqual(bridge.scan_source_paths("english", modified), required)
        modified["files"] = [row for row in modified["files"] if row["path"] != "bank/master_bank.csv"]
        with self.assertRaisesRegex(bridge.BridgeError, "FORMAL_HISTORY_SCOPE_GAP"):
            bridge.scan_source_paths("english", modified)


if __name__ == "__main__":
    unittest.main()
