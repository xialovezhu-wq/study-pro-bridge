import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from bridge_archives import BridgeError
import lesson_export_408 as lesson


class LessonExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo, self.private = self.root / 'repo', self.root / 'private'
        self.repo.mkdir(); self.private.mkdir()
        self.ledger = self.repo / lesson.LEDGER
        self.ledger.parent.mkdir(parents=True)
        self.output = self.root / 'lesson.zip'

    def event(self, eid='RE-1', kind='outcome', item='MCQ-1', **extra):
        e = dict(event_id=eid, event_kind=kind, session_id='RUN-1', item_id=item,
                 formal_node_id='DS_2024_002', event_time='2026-09-08T09:00:00+00:00',
                 observed_date='2026-09-08', session_review_date='2026-09-08',
                 choice_result='correct', reasoning_result='sound', first_result='independent_correct')
        if kind == 'outcome': e['campaign_item_commitment_sha256'] = 'c' * 64
        e.update(extra)
        return e

    def ledger_write(self, events):
        self.ledger.write_bytes(b''.join(lesson.encoded(e) for e in events))

    def export(self, **kw):
        args = dict(output=self.output, snapshot_id='snapshot-1', run_id='RUN-1',
                    cutoff='2026-09-08T10:00:00+00:00')
        args.update(kw)
        return lesson.export_lesson(self.repo, self.private, **args)

    def unpack(self):
        dest = self.root / 'unpacked'
        with zipfile.ZipFile(self.output) as z: z.extractall(dest)
        return lesson.verify_lesson_directory(dest)

    def request(self, turns=None):
        display = dict(run_id='RUN-1', item_id='MCQ-1', formal_node_id='DS_2024_002',
                       campaign_item_commitment_sha256='c'*64,
                       learner_surface={'stem':'question','options':{'A':'x'}}, question_assets=[])
        raw = lesson.encoded(display); digest = lesson.sha(raw)
        dp = self.private / 'turns' / (digest + '.json'); dp.parent.mkdir(); dp.write_bytes(raw)
        request = dict(run_id='RUN-1', item_id='MCQ-1', command='answer-choice',
                       input={'display_receipt_path':str(dp),'display_receipt_sha256':digest})
        if turns is not None: request['learning_turns'] = turns
        rp = self.private / '.turn-runner-v1/requests/request.json'; rp.parent.mkdir(parents=True)
        rp.write_bytes(lesson.encoded(request))
        saved = dict(request_file=str(rp),request_sha256=lesson.file_hash(rp),
                     outcome={'run_id':'RUN-1','item_id':'MCQ-1','status':'reason_audited'})
        raw = lesson.encoded(saved)
        result = self.private / '.turn-runner-v1/results' / (lesson.sha(raw)+'.json')
        result.parent.mkdir(); result.write_bytes(raw)
        return rp, result, dp

    def test_correct_without_capture_and_repeat_identical(self):
        self.ledger_write([self.event()])
        self.request([{'role':'user','kind':'answer','text':'My actual reasoning','observed_at':None}])
        one = self.export(); two = self.export()
        self.assertEqual(one['zip_sha256'],two['zip_sha256'])
        row = self.unpack()['results'][0]
        self.assertIsNone(row['package_id'])
        self.assertEqual(row['coverage_ids'],['RE-1'])
        self.assertEqual(row['raw_turns'][0]['turns'][0]['text'],'My actual reasoning')
        self.assertEqual(row['evidence_gaps'],[])

    def test_hint_repair_is_same_item_preserves_first_wrong(self):
        first = self.event(choice_result='incorrect', first_result='wrong', reasoning_result='diverged')
        repair = self.event('RE-2','repair',event_time='2026-09-08T09:05:00+00:00',
                            hint_used=True,prompt_level='minimal',correction_result='partial')
        self.ledger_write([first,repair])
        result=self.export()
        self.assertEqual((result['review_count'],result['event_count']),(1,2))
        row=self.unpack()['results'][0]
        self.assertEqual(row['first_result']['first_result'],'wrong')
        self.assertTrue(row['final_result']['hint_used'])

    def test_duplicates_and_missing_expected_rejected(self):
        e=self.event();self.ledger_write([e,e])
        with self.assertRaisesRegex(BridgeError,'DUPLICATE_EVENT_ID'): self.export()
        self.ledger_write([e])
        with self.assertRaisesRegex(BridgeError,'LESSON_COVERAGE_MISMATCH'):
            self.export(expected_event_ids=['RE-1','RE-2'])
        with self.assertRaisesRegex(BridgeError,'DUPLICATE_COVERAGE_ID'):
            lesson.validate_coverage(['RE-1'],['RE-1','RE-1'])

    def test_missing_words_explicit_not_synthesized(self):
        self.ledger_write([self.event()]);self.request()
        self.export();row=self.unpack()['results'][0]
        self.assertIn('raw_learning_dialogue_missing',row['evidence_gaps'][0])
        self.assertEqual(row['raw_turns'][0]['turns'],[])

    def test_supplement_binding_and_provenance(self):
        e=self.event();self.ledger_write([e]);self.request()
        supplement = dict(schema='cs408-lesson-evidence-supplement-v1',event_id='RE-1',run_id='RUN-1',
                          item_id='MCQ-1',formal_node_id='DS_2024_002',event_sha256=lesson.sha(lesson.encoded(e)),
                          provenance='recovered_from_current_conversation',
                          turns=[dict(role='user',kind='answer',text='Recovered exact words',observed_at=None)])
        p=self.root/'supplement.json';p.write_bytes(lesson.encoded(supplement))
        self.export(supplement_paths=[p]);row=self.unpack()['results'][0]
        self.assertEqual(row['raw_turns'][-1]['source'],'recovered_from_current_conversation')
        self.output.unlink();supplement['event_sha256']='f'*64;p.write_bytes(lesson.encoded(supplement))
        with self.assertRaisesRegex(BridgeError,'SUPPLEMENT_EVENT_HASH_MISMATCH'):self.export(supplement_paths=[p])

    def auto_supplement(self, event, **extra):
        value = dict(schema='cs408-lesson-evidence-supplement-v1', event_id=event['event_id'],
                     run_id=lesson.run_of(event), item_id=event['item_id'], formal_node_id=event['formal_node_id'],
                     event_sha256=lesson.sha(lesson.encoded(event)), provenance='recovered_from_current_conversation',
                     turns=[dict(role='user',kind='answer',text='Saved original words',observed_at=None)])
        value.update(extra)
        raw=lesson.encoded(value)
        p=self.private/'.learning-evidence-v1/supplements'/(lesson.sha(raw)+'.json')
        p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
        return p

    def test_auto_supplement_scoped_and_deduplicated_with_explicit(self):
        event=self.event();self.ledger_write([event]);self.request()
        p=self.auto_supplement(event)
        self.auto_supplement(self.event('RE-OTHER',session_id='RUN-OTHER'))
        self.export(supplement_paths=[p])
        row=self.unpack()['results'][0]
        supplements=[r for r in row['raw_turns'] if r['source']=='recovered_from_current_conversation']
        self.assertEqual(len(supplements),1)
        self.assertEqual(supplements[0]['event_id'],'RE-1')
        self.assertEqual(row['evidence_gaps'],[])
        with zipfile.ZipFile(self.output) as z:
            self.assertEqual(len([n for n in z.namelist() if n.startswith('raw/supplements/')]),1)

    def test_auto_supplement_filename_hash_rejected(self):
        event=self.event();self.ledger_write([event]);p=self.auto_supplement(event)
        p.rename(p.with_name('f'*64+'.json'))
        with self.assertRaisesRegex(BridgeError,'SUPPLEMENT_FILE_HASH_MISMATCH'):self.export()

    def test_auto_supplement_identity_rejected(self):
        event=self.event();self.ledger_write([event]);self.auto_supplement(event,item_id='MCQ-OTHER')
        with self.assertRaisesRegex(BridgeError,'SUPPLEMENT_IDENTITY_MISMATCH'):self.export()


    def test_request_and_display_hash_safety(self):
        self.ledger_write([self.event()]);rp,_,_=self.request()
        rp.write_bytes(rp.read_bytes()+b' ')
        with self.assertRaisesRegex(BridgeError,'REQUEST_HASH_MISMATCH'):self.export()

    def test_symlink_and_internal_output_rejected(self):
        self.ledger_write([self.event()])
        with self.assertRaisesRegex(BridgeError,'OUTPUT_MUST_BE_EXTERNAL'):self.export(output=self.repo/'bad.zip')
        real=self.root/'real.json';real.write_text('{}')
        link=self.root/'link.json';link.symlink_to(real)
        with self.assertRaisesRegex(BridgeError,'UNSAFE_SOURCE_PATH'):self.export(supplement_paths=[link])

    def test_standalone_by_date_has_real_evidence_id(self):
        self.ledger_write([])
        record=dict(schema='cs408-light-learning-evidence-v1',evidence_id='LE-1',study_date='2026-09-08',
                    lesson_id='ordinary',formal_node_id=None,source_id='source',
                    question={'stem':'Q','options':{},'assets':[]},
                    turns=[dict(role='user',kind='answer',text='Because ...',observed_at=None)],
                    result={'choice_result':'correct','reasoning_result':'sound'},recorded_at='2026-09-08T09:00:00+00:00')
        raw=lesson.encoded(record);p=self.private/'.learning-evidence-v1/records'/(lesson.sha(raw)+'.json')
        p.parent.mkdir(parents=True);p.write_bytes(raw)
        self.export(run_id=None,date='2026-09-08')
        row=self.unpack()['results'][0]
        self.assertIsNone(row['event_id']);self.assertEqual(row['event_ids'],[])
        self.assertEqual(row['coverage_ids'],['LE-1']);self.assertIsNone(row['package_id'])

    def test_verify_detects_removed_row_even_with_reinventoried_files(self):
        self.ledger_write([self.event(),self.event('RE-2',item='MCQ-2')]);self.export()
        data=self.unpack();root=self.root/'unpacked'
        results=root/'lesson_results.jsonl';results.write_bytes(lesson.encoded(data['results'][0]))
        outer=json.loads((root/'manifest.json').read_text())
        for row in outer['files']:
            if row['path']=='lesson_results.jsonl':row.update(sha256=lesson.file_hash(results),size=results.stat().st_size)
        (root/'manifest.json').write_bytes(lesson.encoded(outer))
        with self.assertRaisesRegex(BridgeError,'LESSON_COVERAGE_MISMATCH'):lesson.verify_lesson_directory(root)

    def test_quick_pack_zip_preserves_bytes(self):
        self.ledger_write([self.event()])
        qroot=self.root/'quick';(qroot/'original').mkdir(parents=True)
        native=dict(subject='cs408',package_id='CS408-EXISTING',study_date='2026-09-08',canonical_sha256='a'*64)
        for name,value in {'manifest.json':native,'source.json':{},'receipt.json':{},'conversation.json':{'turns':[]}}.items():
            (qroot/'original'/name).write_bytes(lesson.encoded(value))
        files={p.relative_to(qroot).as_posix():p for p in (qroot/'original').iterdir()}
        manifest=dict(schema_version=1,kind='quick_pack',subject='408',package_id='CS408-EXISTING',
                      study_date='2026-09-08',version='a'*64,native_manifest_sha256=lesson.file_hash(qroot/'original/manifest.json'),
                      questions=[{'question_id':'DS_2024_002','conversation_path':'original/conversation.json'}],
                      files=lesson.inventory(files))
        (qroot/'manifest.json').write_bytes(lesson.encoded(manifest));files['manifest.json']=qroot/'manifest.json'
        qzip=self.root/'quick.zip';lesson.write_zip(files,qzip)
        self.export(quick_pack_paths=[qzip])
        with zipfile.ZipFile(self.output) as z:
            self.assertEqual(z.read('quick_packs/CS408-EXISTING.zip'),qzip.read_bytes())

    def test_ledger_changes_during_export_rejected(self):
        self.ledger_write([self.event()])
        real=lesson.inventory
        def concurrent(files):
            result=real(files)
            self.ledger.write_bytes(self.ledger.read_bytes()+lesson.encoded(self.event('RE-2',item='MCQ-2')))
            return result
        with patch.object(lesson,'inventory',side_effect=concurrent):
            with self.assertRaisesRegex(BridgeError,'EVENT_LEDGER_CHANGED_DURING_EXPORT'):self.export()
        self.assertFalse(self.output.exists())


if __name__ == '__main__': unittest.main()
