"""Synthetic lesson integration: real archive/coverage/read gates, no production writes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import bridge_archives as archive
import study_bridge as bridge
import lesson_export_408 as lesson
import test_study_bridge as fixtures
import test_lesson_export_408 as lesson_fixtures


class LessonBridgeTests(unittest.TestCase):
    quick = fixtures.BridgeTests.quick
    sent = fixtures.BridgeTests.sent
    observed = fixtures.BridgeTests.observed
    fixture_source = fixtures.BridgeTests.fixture_source

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.base = self.root / 'bridge'
        self.base.mkdir()
        self.published = fixtures.FakePublished(self.root / 'formal', '408')
        self.published.receipt.update(publication_target='local', snapshot_id=fixtures.COMMIT)
        self.owners = fixtures.FakeOwners(self.published)
        self.bridge = bridge.Bridge(self.base, self.published, self.owners)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.private = self.root / 'private'; self.private.mkdir()
        self.ledger = self.repo / lesson.LEDGER
        self.ledger.parent.mkdir(parents=True)
        self.event = lesson_fixtures.LessonExportTests.event(self)
        self.ledger.write_bytes(lesson.encoded(self.event))
        self.fixture_source('events.jsonl', lesson.encoded(self.event).decode())
        lesson_fixtures.LessonExportTests.request(self, [{'role': 'user', 'kind': 'answer', 'text': 'Real fixture reasoning', 'observed_at': None}])
        self.lesson_zip = self.root / 'lesson.zip'
        lesson.export_lesson(self.repo, self.private, output=self.lesson_zip, snapshot_id=fixtures.COMMIT,
                             run_id='RUN-1', cutoff='2026-09-08T10:00:00+00:00')

    def prepare(self, inputs=None):
        return self.bridge.prepare('A', '408', '2026-09-08', 'A-lesson', packages=inputs or [self.lesson_zip])

    def advice(self, run):
        result = copy.deepcopy(run['input_review_updates'][0])
        # A returns a proposed update, not a copied raw evidence container.
        result = {key: result[key] for key in ('review_id', 'lesson_id', 'package_id', 'event_ids',
                                              'coverage_ids', 'first_result', 'final_result')}
        with self.bridge.input_materials(run) as current:
            path = 'lessons/RUN-1/lesson_results.jsonl'
            result.update(recommendation='Retain independent correct evidence for local review', uncertainties=[],
                          current_evidence_refs=[{'path': path, 'sha256': current[path]['sha256'],
                                                 'quote': current[path]['local_source'].read_text()}])
        return {'items': [], 'review_updates': [result]}

    def output(self, run, advice, lessons=True):
        original = fixtures.BridgeTests.output(self, run, {'advice.json': advice})
        folder = self.root / 'output-build'
        manifest = bridge.load(folder / 'manifest.json')
        if lessons:
            manifest['input_lessons'] = run['input_lessons']
        bridge.write(folder / 'manifest.json', bridge.json_bytes(manifest))
        result = self.root / ('lesson-' + original.name)
        archive.write_zip({p.relative_to(folder).as_posix(): p for p in folder.rglob('*') if p.is_file()}, result)
        return result

    def test_all_correct_admitted_and_remains_local_adjudication(self):
        run = self.prepare()
        self.assertEqual(run['input_packages'], [])
        self.assertIsNone(run['input_review_updates'][0]['package_id'])
        self.observed(run)
        self.assertEqual(run['web_read_status'], 'verified')
        self.bridge.download(run, self.output(run, self.advice(run)))
        self.assertEqual(run['stage'], 'AWAITING_LOCAL_DECISION')
        self.assertEqual(run['review_updates_status'], 'review_updates_pending_local_adjudication')
        self.assertEqual(run['learning_events_written'], 0)
        with self.assertRaisesRegex(archive.BridgeError, 'review_updates_pending_local_adjudication'):
            self.bridge.bind_a_decision(run, [])
        self.assertEqual(self.owners.imports, [])

    def test_missing_duplicate_event_and_result_echo_rejected(self):
        run = self.prepare()
        self.observed(run)
        base = self.advice(run)
        changes = [([], 'A_REVIEW_UPDATE_COVERAGE_MISMATCH'),
                   (base['review_updates'] * 2, 'A_REVIEW_UPDATE_COVERAGE_MISMATCH')]
        for field, value, code in [('event_ids', [], 'A_REVIEW_EVENT_COVERAGE_MISMATCH'),
                                   ('coverage_ids', ['fake'], 'A_REVIEW_EVENT_COVERAGE_MISMATCH'),
                                   ('first_result', {}, 'A_REVIEW_RESULT_ECHO_MISMATCH'),
                                   ('final_result', {}, 'A_REVIEW_RESULT_ECHO_MISMATCH'),
                                   ('package_id', 'fake-package', 'A_REVIEW_IDENTITY_MISMATCH'),
                                   ('current_evidence_refs', [], 'A_REVIEW_CURRENT_RESULT_REFERENCE_REQUIRED')]:
            rows = copy.deepcopy(base['review_updates']); rows[0][field] = value
            changes.append((rows, code))
        for rows, code in changes:
            with self.subTest(code=code), self.assertRaisesRegex(archive.BridgeError, code):
                bridge.validate_review_updates({'review_updates': rows}, run)

    def test_output_must_bind_entire_input_lesson(self):
        run = self.prepare(); self.observed(run)
        with self.assertRaisesRegex(archive.BridgeError, 'A_INPUT_LESSON_VERSION_MISMATCH'):
            self.bridge.download(run, self.output(run, self.advice(run), lessons=False))

    def test_complete_raw_text_read_is_required(self):
        run = self.prepare(); self.observed(run)
        path = Path(run['web_evidence']['path'])
        evidence = bridge.load(path)
        evidence['read_operations'] = [op for op in evidence['read_operations'] if not op['path'].startswith('lessons/RUN-1/raw/')]
        bridge.write(path, bridge.json_bytes(evidence))
        result = self.bridge.inspect_web_read(run, path, confirm_observed_cu=True)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertTrue(any('A_CURRENT_TEXT_NOT_COMPLETELY_OBSERVED:lessons/' in issue for issue in result['current_input_issues']))

    def test_stale_snapshot_blocks_prepare_and_export_without_output(self):
        self.fixture_source('events.jsonl', '{"event_id":"older"}\n')
        with self.assertRaisesRegex(archive.BridgeError, 'LESSON_SOURCE_NEWER_THAN_SNAPSHOT'):
            self.prepare()
        output = self.root / 'must-not-exist.zip'
        with self.assertRaisesRegex(archive.BridgeError, 'LESSON_SOURCE_NEWER_THAN_SNAPSHOT'):
            self.bridge.export_lesson(self.repo, self.private, output=output, run_id='RUN-1', cutoff='2026-09-08T10:00:00+00:00')
        self.assertFalse(output.exists())

    def test_export_and_existing_pure_package_compatibility(self):
        with self.assertRaisesRegex(archive.BridgeError, 'OUTPUT_MUST_BE_EXTERNAL'):
            self.bridge.export_lesson(self.repo, self.private, output=self.repo / 'blocked.zip', run_id='RUN-1')
        output = self.root / 'verified.zip'
        result = self.bridge.export_lesson(self.repo, self.private, output=output, run_id='RUN-1', cutoff='2026-09-08T10:00:00+00:00')
        self.assertEqual(result['snapshot_id'], fixtures.COMMIT)
        with archive.collect_inputs([output]) as inputs:
            self.assertEqual(len(inputs), 1)
        with self.assertRaisesRegex(archive.BridgeError, 'NOT_A_QUICK_PACKAGE'):
            with archive.packages([output]):
                pass
        quick = self.quick('408', 'CS408-fixture')
        pure = self.bridge.prepare('A', '408', '2026-09-08', 'A-old', packages=[quick])
        self.assertNotIn('input_lessons', pure)
        with archive.packages([quick]) as inputs:
            self.assertEqual(inputs[0]['manifest']['package_id'], 'CS408-fixture')
        with self.bridge.input_materials(pure) as inputs:
            self.assertIn('quick-packs/CS408-fixture/original/conversation.json', inputs)

    def test_embedded_quick_package_and_total_bundle_preserve_bytes(self):
        quick = self.quick('408', 'CS408-embedded')
        combined = self.root / 'combined.zip'
        lesson.export_lesson(self.repo, self.private, output=combined, snapshot_id=fixtures.COMMIT,
                             run_id='RUN-1', cutoff='2026-09-08T10:00:00+00:00', quick_pack_paths=[quick])
        run = self.prepare([combined])
        self.assertEqual(run['input_packages'][0]['zip_sha256'], archive.file_hash(quick))
        self.assertEqual(len(run['input_lessons']), 1)
        with self.bridge.input_materials(run) as materials:
            self.assertIn('quick-packs/CS408-embedded/original/conversation.json', materials)
        total = self.root / 'total.zip'
        archive.write_zip({'408/lesson.zip': combined}, total)
        with archive.collect_inputs([total]) as found:
            typed = {item['manifest']['kind']: item for item in found}
            self.assertEqual(typed['lesson_review_bundle']['zip_path'].read_bytes(), combined.read_bytes())
            self.assertEqual(typed['quick_pack']['zip_path'].read_bytes(), quick.read_bytes())

    def test_standalone_uses_evidence_coverage_without_invented_event(self):
        record = dict(schema='cs408-light-learning-evidence-v1', evidence_id='LE-1', study_date='2026-09-08',
                      lesson_id='ordinary', formal_node_id=None, source_id='source',
                      question={'stem': 'Q', 'options': {}, 'assets': []},
                      turns=[dict(role='user', kind='answer', text='Actual answer', observed_at=None)],
                      result={'choice_result': 'correct', 'reasoning_result': 'sound'},
                      recorded_at='2026-09-08T09:00:00+00:00')
        raw = lesson.encoded(record)
        source = self.private / '.learning-evidence-v1/records' / (lesson.sha(raw) + '.json')
        source.parent.mkdir(parents=True); source.write_bytes(raw)
        self.ledger.write_bytes(b'')
        standalone = self.root / 'standalone.zip'
        lesson.export_lesson(self.repo, self.private, output=standalone, snapshot_id=fixtures.COMMIT,
                             date='2026-09-08', cutoff='2026-09-08T10:00:00+00:00')
        run = self.prepare([standalone])
        row = run['input_review_updates'][0]
        self.assertEqual(row['event_ids'], [])
        self.assertEqual(row['coverage_ids'], ['LE-1'])
        self.assertIsNone(row['event_id'])
        self.observed(run)
        self.assertEqual(run['web_read_status'], 'verified')


if __name__ == '__main__':
    unittest.main()
