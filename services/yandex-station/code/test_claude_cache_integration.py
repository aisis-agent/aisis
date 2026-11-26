"""Cache prefix and shared transport integration; no real account or provider calls."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import adapter as a
import asr_correct


class AdapterCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = a.Adapter({
            'AISIS_ALLOWED_STATIONS': 'speaker', 'AISIS_MEMORY': '0',
            'AISIS_STATE_DB': str(self.root / 'adapter.sqlite3'),
            'AISIS_DIALOGS_DB': str(self.root / 'dialogs.sqlite3'),
            'AISIS_CHATGPT_COMMAND': '["python3", "/fixture/claude_primary.py"]', 'AISIS_HA_ACTIONS': 'off',
            'AISIS_USER_PROMPT': '0', 'AISIS_JEV': 'off'})
        self.addAsyncCleanup(self.adapter.close, None)
        self.adapter._persona_block = Mock(return_value='Stable owner persona and capabilities')
        self.adapter._dialog_blocks = AsyncMock(return_value='Dynamic recent conversation\n')
        self.native = SimpleNamespace(SUPPORTS_CACHE_TELEMETRY=True)
        self.adapter._claude_module = Mock(return_value=self.native)
        self.run = AsyncMock(return_value={'text': 'A spoken answer', 'conversation_id': None})
        self.patcher = patch('adapter.run_command', self.run)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_native_persona_is_separate_on_every_turn_and_context_is_preserved(self):
        await self.adapter.turn('speaker', 'First question')
        await self.adapter.turn('speaker', 'Second question')
        sent = [call.args[1] for call in self.run.await_args_list]
        self.assertEqual([p['persona'] for p in sent], ['Stable owner persona and capabilities'] * 2)
        for payload in sent:
            self.assertNotIn('Stable owner persona', payload['text'])
            self.assertIn('Dynamic recent conversation', payload['text'])
        self.assertIn('First question', sent[0]['text'])
        self.assertIn('Second question', sent[1]['text'])

    async def test_colocated_browser_backend_keeps_its_existing_payload_contract(self):
        self.adapter.command = ['python3', '/fixture/chatgpt_pm.py']
        await self.adapter.turn('speaker', 'Question')
        payload = self.run.await_args.args[1]
        self.assertNotIn('persona', payload)
        self.assertIn('Stable owner persona', payload['text'])

    async def test_old_router_keeps_existing_persona_injection_contract(self):
        self.adapter._claude_module.return_value = SimpleNamespace()
        await self.adapter.turn('speaker', 'Question')
        payload = self.run.await_args.args[1]
        self.assertNotIn('persona', payload)
        self.assertIn('Stable owner persona', payload['text'])
        self.assertIn('Dynamic recent conversation', payload['text'])

    async def test_large_dynamic_context_keeps_current_question_and_native_persona(self):
        self.adapter._dialog_blocks.return_value = 'x' * 17000
        await self.adapter.turn('speaker', 'Keep this question')
        payload = self.run.await_args.args[1]
        self.assertEqual(payload['text'], 'Keep this question')
        self.assertEqual(payload['persona'], 'Stable owner persona and capabilities')

    def test_trace_retains_allowlisted_nullable_counters_and_no_raw_metadata(self):
        trace = self.adapter._brain_trace({
            'provider': 'claude', 'model': 'claude-sonnet-5-5', 'request_id': 'req_fixture',
            'usage': {'input_tokens': 8, 'output_tokens': None, 'cache_read_input_tokens': 0,
                      'cache_creation_input_tokens': True, 'cache_creation_5m_input_tokens': 42,
                       'extra': 'private provider body'},
            'raw_response': 'private provider body'}, True, 2, 100)
        self.assertEqual(trace['usage']['input_tokens'], 8)
        self.assertEqual(trace['usage']['cache_read_input_tokens'], 0)
        self.assertIsNone(trace['usage']['output_tokens'])
        self.assertIsNone(trace['usage']['cache_creation_input_tokens'])
        self.assertEqual(trace['usage']['cache_creation_5m_input_tokens'], 42)
        self.assertIsNone(trace['usage']['cache_creation_1h_input_tokens'])
        self.assertEqual(trace['request_id'], 'req_fixture')
        self.assertNotIn('private provider body', json.dumps(trace))

    async def test_fact_extraction_uses_same_audited_transport_and_stable_instruction(self):
        self.native.Settings = SimpleNamespace(from_env=lambda _: SimpleNamespace(model='claude-sonnet-5-5'))
        self.native.load_credentials = lambda _: ['fixture-credential']
        self.native.stable_system = lambda text: [{'type': 'text', 'text': text,
                                                   'cache_control': {'type': 'ephemeral', 'ttl': '5m'}}]
        self.native.anthropic_send = AsyncMock(return_value='{"facts":[]}')
        await self.adapter._extract_facts('Private owner input', 'Private answer')
        call = self.native.anthropic_send.await_args
        self.assertEqual(call.kwargs, {'purpose': 'facts'})
        self.assertEqual(call.args[1]['system'][0]['text'], a.EXTRACT_PROMPT)
        self.assertNotIn(a.EXTRACT_PROMPT, call.args[1]['messages'][0]['content'])
        self.assertIn('Private owner input', call.args[1]['messages'][0]['content'])


class AsrTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_asr_delegates_once_to_native_transport_with_the_existing_session(self):
        session = object()
        native = SimpleNamespace(SUPPORTS_CACHE_TELEMETRY=True,
                                 anthropic_send=AsyncMock(return_value='0|U|'))
        post = asr_correct.make_post(session, native)
        body = {'model': 'claude-haiku-4-5'}
        self.assertEqual(await post('fixture-credential', body, .7), '0|U|')
        native.anthropic_send.assert_awaited_once_with('fixture-credential', body, .7,
            session=session, purpose='asr', propagate_timeout=True, deadline=None)

    async def test_asr_forwards_the_actual_outer_deadline_for_timeout_attribution(self):
        native = SimpleNamespace(SUPPORTS_CACHE_TELEMETRY=True,
                                 anthropic_send=AsyncMock(return_value='0|U|'))
        credential = SimpleNamespace(fingerprint='fixture')
        corrector = asr_correct.Corrector(lambda: [credential], asr_correct.make_post(object(), native),
                                         lambda: None)
        before = asyncio.get_running_loop().time()
        await corrector._ask(dict(asr_correct.DEFAULTS), 'Real instructions', 'Current phrase', .8)
        deadline = native.anthropic_send.await_args.kwargs['deadline']
        self.assertGreaterEqual(deadline, before)
        self.assertLessEqual(deadline, asyncio.get_running_loop().time() + .8)
        self.assertIsNone(asr_correct._REQUEST_DEADLINE.get())

    async def test_asr_cache_prefix_excludes_recent_dialog_and_never_pads(self):
        post = AsyncMock(return_value='0|U|')
        credential = SimpleNamespace(fingerprint='fixture')
        corrector = asr_correct.Corrector(lambda: [credential], post, lambda: None)
        await corrector._ask(dict(asr_correct.DEFAULTS), 'Actual instructions and catalog',
                             'Dynamic user message and recent dialog', 1)
        body = post.await_args.args[1]
        self.assertEqual(body['system'], [{'type': 'text', 'text': 'Actual instructions and catalog',
                                          'cache_control': {'type': 'ephemeral', 'ttl': '5m'}}])
        self.assertEqual(body['messages'][0]['content'], 'Dynamic user message and recent dialog')
        self.assertEqual(body['model'], 'claude-haiku-4-5')

    async def test_a_failing_side_model_call_never_cools_the_keys_the_live_corrector_uses_and_the_reverse(self):
        keys = [SimpleNamespace(fingerprint='one'), SimpleNamespace(fingerprint='two')]
        failing = Mock(side_effect=RuntimeError('boom'))

        async def post(credential, body, timeout):
            if body['model'] == 'claude-haiku-5-5':
                failing()
                return ''
            return '0|U|'

        corrector = asr_correct.Corrector(lambda: keys, post, lambda: None)
        self.assertEqual(await corrector.ask_model('claude-haiku-5-5', 'sys', 'msg', 1), ('', 'no_key'))
        self.assertEqual(failing.call_count, 2)  # both keys tried for the side model, then both are cooled in ITS map
        self.assertEqual(await corrector.ask_model('claude-haiku-5-5', 'sys', 'msg', 1), ('', 'no_key'))
        self.assertEqual(failing.call_count, 2)  # cooled: not asked again
        self.assertEqual(corrector._cooldown, {})
        self.assertEqual(await corrector._ask(dict(asr_correct.DEFAULTS), 'sys', 'msg', 1), ('0|U|', 'ok'))

    async def test_a_cooled_live_corrector_key_does_not_cool_the_side_model_calls(self):
        keys = [SimpleNamespace(fingerprint='one')]
        corrector = asr_correct.Corrector(lambda: keys, AsyncMock(return_value='x'), lambda: None)
        corrector._cooldown['one'] = corrector._now() + 600
        self.assertEqual(await corrector._ask(dict(asr_correct.DEFAULTS), 'sys', 'msg', 1), ('', 'no_key'))
        self.assertEqual(await corrector.ask_model('claude-haiku-5-5', 'sys', 'msg', 1), ('x', 'ok'))


if __name__ == '__main__':
    unittest.main()
