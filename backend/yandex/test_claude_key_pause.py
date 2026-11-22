"""A key out of credit stays paused until the provider's own resume time, or an escalating backoff; fakes only, no network."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer
import claude_primary as c

HOUR = 3600


def epoch(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def usage_limit(text='You have reached your specified API usage limits. You will regain access on 2026-12-01 at 00:00 UTC.'):
    return {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': text}}


NOW = epoch('2026-11-22T12:00:00')


def failure(status, headers=None, body=None, now=NOW):
    error = c.key_failure(status, headers or {}, {} if body is None else body, now)
    assert isinstance(error, c.Failover), (status, error)
    return error


class ResumeTimeTests(unittest.TestCase):
    def test_402_without_any_date_escalates_from_the_first_step(self):
        error = failure(402)
        self.assertEqual((str(error), error.cooldown_seconds, error.resume_at, error.escalate), ('billing_unavailable', HOUR, None, True))

    def test_the_provider_date_in_a_400_message_is_the_resume_time(self):
        error = failure(400, body=usage_limit())
        self.assertEqual((str(error), error.resume_at, error.escalate), ('billing_unavailable', epoch('2026-12-01T00:00:00'), False))

    def test_date_formats_in_the_message(self):
        for text, expected in (('You have reached your specified API usage limits, regain access on 2026-12-01.', '2026-12-01T00:00:00'),
                               ('This organization has been disabled until 2026-12-01T06:30:00Z', '2026-12-01T06:30:00'),
                               ('You have reached your specified workspace API usage limits. You will regain access on 2026-12-01 at 09:15 UTC', '2026-12-01T09:15:00')):
            with self.subTest(text=text):
                self.assertEqual(failure(400, body=usage_limit(text)).resume_at, epoch(expected))

    def test_402_takes_the_date_from_its_body_too(self):
        self.assertEqual(failure(402, body=usage_limit()).resume_at, epoch('2026-12-01T00:00:00'))

    def test_429_with_a_usage_limit_text_pauses_until_its_date(self):
        error = failure(429, body=usage_limit())
        self.assertEqual((str(error), error.resume_at), ('rate_limit', epoch('2026-12-01T00:00:00')))

    def test_a_date_in_an_unrelated_rejection_is_not_a_resume_time(self):
        error = failure(400, body=usage_limit('messages.0.content: text before 2026-12-01 is invalid'))
        self.assertEqual((str(error), error.resume_at, error.escalate), ('request_rejected', None, False))

    def test_a_past_or_absurdly_far_date_is_ignored_and_the_backoff_applies(self):
        for text in ('regain access on 2026-11-01', 'regain access on 2099-01-01'):
            with self.subTest(text=text):
                error = failure(400, body=usage_limit('You have reached your specified API usage limits. ' + text))
                self.assertEqual((error.resume_at, error.escalate), (None, True))

    def test_retry_after_seconds_are_the_pause_not_shortened_to_an_hour_default(self):
        error = failure(402, {'Retry-After': '7200'})
        self.assertEqual((error.cooldown_seconds, error.resume_at, error.escalate), (7200, None, True))
        self.assertEqual(failure(402, {'retry-after': '5'}).cooldown_seconds, 60)

    def test_retry_after_http_date(self):
        error = failure(402, {'Retry-After': 'Mon, 23 Nov 2026 12:00:00 GMT'})
        self.assertEqual((error.cooldown_seconds, error.escalate), (24 * HOUR, True))

    def test_ratelimit_reset_header_is_used_when_there_is_no_retry_after(self):
        headers = {'anthropic-ratelimit-requests-reset': '2026-11-22T14:00:00Z', 'anthropic-ratelimit-tokens-reset': '2026-11-22T15:30:00Z'}
        error = failure(402, headers)
        self.assertEqual((error.cooldown_seconds, error.escalate), (3.5 * HOUR, True))

    def test_a_reset_header_without_an_offset_is_not_believed(self):
        error = failure(402, {'anthropic-ratelimit-requests-reset': '2026-11-22T14:00:00'})
        self.assertEqual((error.cooldown_seconds, error.escalate), (HOUR, True))

    def test_a_short_header_still_climbs_the_ladder(self):
        self.assertTrue(failure(429, {'retry-after': '20'}, usage_limit('Your credit balance is too low')).escalate)

    def test_a_billing_message_on_a_403_is_a_billing_pause(self):
        error = failure(403, body=usage_limit('This organization has been disabled.'))
        self.assertEqual((str(error), error.escalate), ('billing_unavailable', True))

    def test_a_huge_error_message_is_cut_before_the_masks_run(self):
        self.assertLessEqual(len(c.error_excerpt({'error': {'message': 'a' * 1_000_000}})), c.EXCERPT_LIMIT)

    def test_the_latest_explicit_time_wins_so_a_key_is_never_retried_early(self):
        error = failure(400, {'retry-after': '120'}, usage_limit())
        self.assertEqual(error.resume_at, epoch('2026-12-01T00:00:00'))

    def test_malformed_headers_fall_back_to_the_backoff(self):
        error = failure(402, {'retry-after': 'soon', 'anthropic-ratelimit-tokens-reset': 'tomorrow'})
        self.assertEqual((error.resume_at, error.escalate, error.cooldown_seconds), (None, True, HOUR))

    def test_non_billing_failures_keep_their_short_pauses(self):
        self.assertEqual([(failure(s).cooldown_seconds, failure(s).escalate) for s in (401, 429, 503, 404)],
                         [(300, False), (60, False), (15, False), (300, False)])


class ExcerptTests(unittest.TestCase):
    def test_excerpt_is_the_provider_message_without_secrets_ids_or_emails(self):
        message = ('Your credit balance is too low. key sk-ant-api03-' + 'A' * 40 + ' for ops@example.com in org '
                   '3f2a9c1e-5b7d-4e8a-9c0d-1a2b3c4d5e6f / org_01HXYZabcdefghijklmnop')
        excerpt = c.error_excerpt({'error': {'message': message}})
        for leaked in ('sk-ant', 'AAAAAAAA', 'ops@example.com', '3f2a9c1e', 'org_01HXYZ'):
            self.assertNotIn(leaked, excerpt)
        self.assertTrue(excerpt.startswith('Your credit balance is too low.'))

    def test_excerpt_is_one_short_line(self):
        excerpt = c.error_excerpt({'error': {'message': 'line one\n\n\tline two ' + 'word ' * 100}})
        self.assertLessEqual(len(excerpt), 200)
        self.assertNotRegex(excerpt, r'[\n\t]| {2}')

    def test_no_message_means_no_excerpt(self):
        for body in (None, [], {}, {'error': 'text'}, {'error': {'message': 5}}, {'error': {'message': '  '}}):
            with self.subTest(body=body):
                self.assertIsNone(c.error_excerpt(body))

    def test_every_failure_carries_its_excerpt_but_a_success_does_not_need_one(self):
        self.assertEqual(failure(400, body=usage_limit('Model not allowed for this key')).excerpt, 'Model not allowed for this key')
        self.assertIsNone(failure(402).excerpt)


def key(n):
    return c.Credential('slot' + str(n), 'sk-ant-api03-fixture-' + str(n) * 30)


class PauseRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.sqlite'
        self.state = c.State(self.path)
        self.addCleanup(lambda: self.state.close())
        self.now = NOW
        self.calls, self.effects, self.turns = [], {}, 0
        self.settings = c.Settings(allowed=frozenset({'a'}))

        async def send(credential, body, timeout):
            self.calls.append(credential.label)
            result = self.effects.get(credential.label, 'Ответ')
            if isinstance(result, BaseException):
                raise result
            return result

        async def fallback(payload):
            return {'text': 'Резерв', 'conversation_id': 'https://chatgpt.com/c/owned'}

        self.send, self.fallback = send, fallback
        self.router = self.make_router()

    def make_router(self, credentials=None):
        return c.Router(self.settings, credentials or [key(1), key(2), key(3)], self.state,
                        send=self.send, fallback=self.fallback, clock=lambda: self.now)

    def reopen(self):
        self.state.close()
        self.state = c.State(self.path)
        self.router = self.make_router()

    async def turn(self):
        self.turns += 1
        self.calls.clear()
        return await self.router.submit(dict(turn_id=f'turn-{self.turns}', station_id='a', text='Привет', conversation_id=None))

    def billing(self, *, resume_at=None):
        error = failure(400, body=usage_limit()) if resume_at is None else c.Failover('billing_unavailable', HOUR, resume_at=resume_at)
        return error

    async def test_a_dated_pause_skips_the_key_without_probing_until_the_date_then_probes_once(self):
        self.effects = {'slot1': self.billing()}
        await self.turn()
        self.assertEqual(self.calls, ['slot1', 'slot2'])
        self.effects = {}
        for days in (1, 3, 8):
            self.now = NOW + days * 86400
            await self.turn()
            self.assertEqual(self.calls, ['slot2'], f'probed a paused key {days} days in')
        self.now = epoch('2026-12-01T00:00:00') - 1
        await self.turn()
        self.assertEqual(self.calls, ['slot2'])
        self.now = epoch('2026-12-01T00:00:00')
        await self.turn()
        self.assertEqual(self.calls, ['slot1'], 're-probe at the resume date')

    async def test_the_resume_time_survives_a_restart(self):
        self.effects = {'slot1': self.billing()}
        await self.turn()
        self.reopen()
        self.now = epoch('2026-11-30T23:00:00')
        self.effects = {}
        await self.turn()
        self.assertEqual(self.calls, ['slot2'])

    async def test_without_a_date_the_pause_escalates_1_3_12_24_then_stays_at_24_hours(self):
        self.effects = {'slot1': failure(402)}
        steps = []
        for _ in range(6):
            await self.turn()
            self.assertEqual(self.calls[0], 'slot1')
            steps.append(self.state.paused_until(key(1).fingerprint, self.now) - self.now)
            self.now = self.state.paused_until(key(1).fingerprint, self.now) - 1
            await self.turn()
            self.assertNotIn('slot1', self.calls, 'probed before the pause ended')
            self.now += 1
        self.assertEqual(steps, [HOUR, 3 * HOUR, 12 * HOUR, 24 * HOUR, 24 * HOUR, 24 * HOUR])

    async def test_escalation_survives_a_restart_and_a_success_resets_it(self):
        self.effects = {'slot1': failure(402)}
        await self.turn()
        self.now += HOUR
        self.reopen()
        await self.turn()
        self.assertEqual(self.state.paused_until(key(1).fingerprint, self.now) - self.now, 3 * HOUR)
        self.now += 3 * HOUR
        self.effects = {}
        await self.turn()
        self.assertEqual(self.calls, ['slot1'])
        self.assertEqual(self.state.strikes(key(1).fingerprint), 0)
        self.effects = {'slot1': failure(402)}
        await self.turn()
        self.assertEqual(self.state.paused_until(key(1).fingerprint, self.now) - self.now, HOUR)

    async def test_a_provider_given_time_is_used_even_when_shorter_than_the_backoff_step(self):
        self.effects = {'slot1': failure(402, {'retry-after': '7200'})}
        await self.turn()
        self.assertEqual(self.state.paused_until(key(1).fingerprint, self.now) - self.now, 7200)

    async def test_a_later_short_failure_never_shortens_a_dated_pause(self):
        self.effects = {'slot1': self.billing()}
        await self.turn()
        until = self.state.paused_until(key(1).fingerprint, self.now)
        self.state.cooldown(key(1).fingerprint, self.now + 30)
        self.assertEqual(self.state.paused_until(key(1).fingerprint, self.now), until)

    async def test_only_the_billing_key_is_paused_among_several_and_the_result_lists_every_key(self):
        self.effects = {'slot1': self.billing()}
        result = await self.turn()
        self.assertEqual(result['credential_slot'], 'slot2')
        self.assertEqual(result['keys'], [{'slot': 'slot1', 'ok': False, 'until': epoch('2026-12-01T00:00:00')},
                                          {'slot': 'slot2', 'ok': True, 'until': None},
                                          {'slot': 'slot3', 'ok': True, 'until': None}])

    async def test_a_skipped_key_is_listed_with_its_resume_time_on_the_next_turn(self):
        self.effects = {'slot1': self.billing()}
        await self.turn()
        self.effects = {}
        result = await self.turn()
        self.assertEqual([k['ok'] for k in result['keys']], [False, True, True])
        self.assertEqual(result['attempts'][0]['result'], 'cooldown')

    async def test_all_keys_paused_goes_to_the_reserve_and_lists_them_all(self):
        self.effects = {f'slot{n}': failure(402) for n in (1, 2, 3)}
        result = await self.turn()
        self.assertEqual(result['provider'], 'chatgpt_web')
        self.assertEqual([k['ok'] for k in result['keys']], [False, False, False])
        self.assertTrue(all(k['until'] == NOW + HOUR for k in result['keys']))
        self.assertEqual(await self.turn_calls(), [])

    async def turn_calls(self):
        await self.turn()
        return self.calls

    async def test_the_pause_is_reported_on_stderr_with_the_sanitized_excerpt_only(self):
        self.effects = {'slot1': failure(400, body=usage_limit('Your credit balance is too low. sk-ant-api03-' + 'B' * 40))}
        with patch.object(c, 'diagnostic') as report:
            await self.turn()
        (event,) = [call.args[0] for call in report.call_args_list if call.args[0].get('event') == 'claude_key_paused']
        self.assertEqual((event['slot'], event['code'], event['until']), ('slot1', 'billing_unavailable', NOW + HOUR))
        self.assertNotIn('sk-ant', str(event))
        self.assertIn('credit balance is too low', event['excerpt'])


class OldStateMigrationTests(unittest.TestCase):
    def test_a_cooldowns_table_from_the_previous_release_gains_the_new_column_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'router.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('CREATE TABLE cooldowns (keyhash TEXT PRIMARY KEY, until_time REAL NOT NULL);'
                             "INSERT INTO cooldowns VALUES('abc', 5000.0);")
            db.commit()
            db.close()
            state = c.State(path)
            self.addCleanup(state.close)
            self.assertEqual(state.paused_until('abc', 1000.0), 5000.0)
            self.assertEqual(state.strikes('abc'), 0)
            state.cooldown('abc', 6000.0, strikes=2)
            self.assertEqual(state.strikes('abc'), 2)


class LedgerExcerptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'usage.sqlite3'
        env = patch.dict(os.environ, {'AISIS_CLAUDE_USAGE_DB': str(self.db)})
        env.start()
        self.addCleanup(env.stop)
        self.resume = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()   # relative: the provider date must stay in the future
        self.reply = (400, usage_limit(f'You have reached your specified API usage limits. You will regain access on {self.resume} at 00:00 UTC.'))

        async def endpoint(request):
            await request.json()
            status, body = self.reply
            return web.json_response(body, status=status, headers={'request-id': 'req_fixture'})

        app = web.Application()
        app.router.add_post('/v1/messages', endpoint)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.addAsyncCleanup(c.flush_usage)
        url = patch.object(c, 'API_URL', str(self.server.make_url('/v1/messages')))
        url.start()
        self.addCleanup(url.stop)
        self.credential = key(1)
        self.body = {'model': c.MODEL, 'messages': [{'role': 'user', 'content': 'q'}], 'max_tokens': 32}

    async def rows(self):
        await c.flush_usage()
        with sqlite3.connect(self.db) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM provider_requests ORDER BY started_at')]

    async def test_a_billing_rejection_stores_the_sanitized_excerpt_next_to_status_and_outcome(self):
        with self.assertRaises(c.Failover) as caught:
            await c.anthropic_send(self.credential, self.body, 2)
        self.assertEqual(caught.exception.resume_at, epoch(self.resume + 'T00:00:00'))
        (row,) = await self.rows()
        self.assertEqual((row['http_status'], row['outcome']), (400, 'billing_unavailable'))
        self.assertIn('regain access on ' + self.resume, row['error_excerpt'])
        self.assertLessEqual(len(row['error_excerpt']), 200)

    async def test_a_success_and_a_bodyless_failure_leave_the_excerpt_null(self):
        self.reply = (402, {})
        with self.assertRaises(c.Failover):
            await c.anthropic_send(self.credential, self.body, 2)
        self.reply = (200, {'id': 'msg_1', 'model': c.MODEL, 'content': [{'type': 'text', 'text': 'Ответ'}], 'stop_reason': 'end_turn'})
        await c.anthropic_send(self.credential, self.body, 2)
        self.assertEqual([row['error_excerpt'] for row in await self.rows()], [None, None])

    async def test_an_existing_ledger_without_the_column_is_upgraded_in_place(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(self.db)
        old.execute('''CREATE TABLE provider_requests (attempt_id TEXT PRIMARY KEY, started_at REAL NOT NULL, duration_ms INTEGER NOT NULL,
            purpose TEXT NOT NULL, turn_id TEXT, credential_slot TEXT, credential_fingerprint TEXT, requested_model TEXT,
            response_model TEXT, organization_id TEXT, request_id TEXT, response_id TEXT, stop_reason TEXT, http_status INTEGER,
            outcome TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, cache_read_input_tokens INTEGER,
            cache_creation_input_tokens INTEGER, cache_creation_5m_input_tokens INTEGER, cache_creation_1h_input_tokens INTEGER)''')
        old.execute("INSERT INTO provider_requests (attempt_id, started_at, duration_ms, purpose, outcome) VALUES ('old', 1, 1, 'brain', 'complete')")
        old.commit()
        old.close()
        with self.assertRaises(c.Failover):
            await c.anthropic_send(self.credential, self.body, 2)
        rows = await self.rows()
        self.assertEqual([(r['attempt_id'], r['error_excerpt'] is None) for r in rows], [('old', True), (rows[1]['attempt_id'], False)])


if __name__ == '__main__':
    unittest.main()
