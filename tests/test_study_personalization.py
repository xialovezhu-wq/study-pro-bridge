import asyncio
import json
import unittest
from unittest.mock import patch

from bridge_archives import BridgeError
from test_study_mcp import Fixture
from study_mcp import LibraryStore, PAGE_BYTES, build_server
from study_personalization import compare_identity


class PersonalizationTests(unittest.TestCase):
    def test_old_or_missing_versions_cannot_claim_current(self):
        old = {'version': 'p1', 'formal_version': 'f1'}
        self.assertEqual(compare_identity(old, {'status': 'ready', **old}, personalized=True)['status'], 'current')
        self.assertEqual(compare_identity(old, {'status': 'ready', 'version': 'p2', 'formal_version': 'f1'}, personalized=True)['status'], 'stale')
        self.assertEqual(compare_identity(old, {'status': 'ready', 'version': 'p1', 'formal_version': 'f2'}, personalized=True)['status'], 'stale')
        self.assertEqual(compare_identity(old, {'status': 'stale', **old}, personalized=True)['status'], 'pending')
        self.assertEqual(compare_identity({}, {'status': 'ready', **old}, personalized=True)['status'], 'unknown')

    def test_new_personal_tool_reads_pinned_materialized_bytes(self):
        fixture = Fixture(personalized=True)
        store = LibraryStore(fixture.base)
        try:
            seen = []
            def native(subject, operation, **kwargs):
                from pathlib import Path
                path = Path(kwargs['manifest_path'])
                self.assertTrue(path.is_relative_to(store.cache))
                self.assertEqual(json.loads(path.read_text())['version'], 'personal-v1')
                self.assertEqual(kwargs['view'], 'protected')
                seen.append(path)
                return {'status': 'available', 'version': 'personal-v1', 'records': []}
            with patch.object(store, 'freshness', return_value={'status': 'current'}), patch('study_personalization.native_call', side_effect=native):
                result = store.personal_context('math', ['可微定义'], source_commit=fixture.commit)
            self.assertEqual(result['source_commit'], fixture.commit)
            self.assertEqual(result['personal_context']['version'], 'personal-v1')
            self.assertEqual(len(seen), 1)
            seen[0].chmod(0o600)
            seen[0].write_text('{}')
            with patch.object(store, 'freshness', return_value={'status': 'current'}), patch('study_personalization.native_call') as call:
                with self.assertRaisesRegex(Exception, 'CACHED_SOURCE_HASH_MISMATCH'):
                    store.personal_context('math', ['可微定义'], source_commit=fixture.commit)
                call.assert_not_called()
        finally:
            store.close()
            fixture.close()

    def test_stale_default_refuses_and_explicit_historical_preserves_label(self):
        fixture = Fixture(personalized=True)
        store = LibraryStore(fixture.base)
        try:
            with patch.object(store, 'freshness', return_value={'status': 'stale'}), patch('study_personalization.native_call', return_value={'records': []}) as call:
                result = store.personal_context('math', ['泰勒公式'], source_commit=fixture.commit)
                self.assertFalse(result['ok'])
                call.assert_not_called()
                historical = store.personal_context('math', ['泰勒公式'], source_commit=fixture.commit, allow_historical=True)
                self.assertEqual(historical['freshness']['status'], 'stale')
                self.assertTrue(historical['historical_read_requested'])
                self.assertEqual(call.call_count, 1)
        finally:
            store.close()
            fixture.close()

    def test_existing_query_schema_can_reach_the_same_personal_reader(self):
        fixture = Fixture(personalized=True)
        store = LibraryStore(fixture.base)
        try:
            with patch.object(store, 'freshness', return_value={'status': 'current'}), patch('study_personalization.native_call', return_value={'records': []}) as call:
                result = store.query('math', 'personal_context', fixture.commit, ['可微定义'], '{"view":"after_attempt"}')
                self.assertEqual(result['answer_view'], 'after_attempt')
                self.assertEqual(call.call_args.kwargs['knowledge_points'], ['可微定义'])
                self.assertEqual(call.call_args.kwargs['max_bytes'], 10000)
                with self.assertRaisesRegex(Exception, 'INVALID_PERSONAL_QUERY_OPTIONS'):
                    store.query('math', 'personal_context', fixture.commit, ['可微定义'], '{"allow_historical":"false"}')
        finally:
            store.close()
            fixture.close()

    def test_personal_query_distinguishes_omitted_and_explicit_budgets(self):
        fixture = Fixture(personalized=True, subject='408')
        store = LibraryStore(fixture.base)
        try:
            with patch.object(store, 'freshness', return_value={'status': 'current'}), \
                    patch('study_personalization.native_call', return_value={'records': []}) as call:
                common = ('408', 'personal_context', fixture.commit, ['OS03-01'])
                store.query(*common)
                store.query(*common, max_bytes=65536)
                store.query(*common, max_bytes=32000)
                self.assertEqual(
                    [row.kwargs['max_bytes'] for row in call.call_args_list],
                    [8192, 65536, 32000])
                with self.assertRaisesRegex(BridgeError, 'INVALID_PERSONAL_BUDGET'):
                    store.query(*common, max_bytes=65537)
                self.assertEqual(call.call_count, 3)
        finally:
            store.close()
            fixture.close()

    def test_generic_query_keeps_its_65536_default(self):
        fixture = Fixture()
        store = LibraryStore(fixture.base)
        try:
            with patch.object(LibraryStore, 'page', wraps=LibraryStore.page) as page:
                store.query('math', 'formal_cards', source_commit=fixture.commit)
            self.assertEqual(page.call_args.args[-1], PAGE_BYTES)
        finally:
            store.close()
            fixture.close()

    def test_server_tools_preserve_omitted_and_explicit_budgets(self):
        fixture = Fixture()
        server, store = build_server(fixture.base)
        try:
            with patch.object(store, 'query', return_value={'ok': True}) as query_call:
                asyncio.run(server._tool_manager.call_tool(
                    'query', {'subject': '408', 'domain': 'personal_context',
                              'ids': ['OS03-01']}))
                asyncio.run(server._tool_manager.call_tool(
                    'query', {'subject': '408', 'domain': 'personal_context',
                              'ids': ['OS03-01'], 'max_bytes': 65536}))
                self.assertIsNone(query_call.call_args_list[0].kwargs['max_bytes'])
                self.assertEqual(query_call.call_args_list[1].kwargs['max_bytes'], 65536)
            with patch.object(store, 'personal_context', return_value={'ok': True}) as personal_call:
                asyncio.run(server._tool_manager.call_tool(
                    'personal_context', {'subject': '408',
                                         'knowledge_points': ['OS03-01']}))
                asyncio.run(server._tool_manager.call_tool(
                    'personal_context', {'subject': '408',
                                         'knowledge_points': ['OS03-01'],
                                         'max_bytes': 32000}))
                self.assertIsNone(personal_call.call_args_list[0].kwargs['max_bytes'])
                self.assertEqual(personal_call.call_args_list[1].kwargs['max_bytes'], 32000)
        finally:
            store.close()
            fixture.close()


if __name__ == '__main__':
    unittest.main()
