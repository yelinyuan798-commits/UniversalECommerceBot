"""Private AI configuration and actual SDK requests against temporary local servers."""
import asyncio
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.ai_settings import (AISettings, AISettingsError, AiSettingsStore,
                              normalize_base_url, public_settings, test_connection)
from core.default_skill import reply_default
from core.profiles import default_profile
from core.runtime import RuntimeManager
from core.service import ServiceEngine
from core.store import StateStore


PRICE_REPLY = '价格可以先看商品页或下单页，我这边还没有具体报价。'
PRIVATE_ERROR = 'private-provider-error-and-key-should-not-be-shown'
LEGACY_ENV = {'API_KEY': 'legacy-env-key', 'MODEL_BASE_URL': 'https://legacy.invalid/v1',
              'MODEL_NAME': 'legacy-model', 'LLM_TIMEOUT_SECONDS': '9', 'LLM_ENABLED': ' false '}


def event(identifier, text='多少钱', **extra):
    return {'platform': 'xianyu', 'shop_id': 'local-test-shop', 'user_id': 'buyer',
            'conversation_id': identifier, 'message_id': identifier, 'item_id': '', 'text': text, **extra}


@contextmanager
def local_model(*, status=200, content=PRICE_REPLY, redirect=None, delay=0, before_response=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': body})
            if before_response:
                before_response(body)
            if delay:
                time.sleep(delay)
            response = {'id': 'test-completion', 'object': 'chat.completion', 'created': 1,
                        'model': body.get('model'), 'choices': [{'index': 0, 'finish_reason': 'stop',
                        'message': {'role': 'assistant', 'content': content}}]}
            if status != 200:
                response = {'error': {'message': PRIVATE_ERROR, 'type': 'test_error'}}
            data = json.dumps(response, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            if redirect:
                self.send_header('Location', redirect)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


class AISettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AiSettingsStore(self.root / 'config/ai.local.json')
        self.env = patch.dict(os.environ, LEGACY_ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_missing_local_file_uses_loaded_env_and_save_survives_restart_without_env_mutation(self):
        before = dict(os.environ)
        self.assertEqual(self.store.load().api_key, 'legacy-env-key')
        self.assertEqual(self.store.public_state()['source'], 'environment')
        self.assertEqual(self.store.public_state()['status'], 'disabled')
        public = self.store.save({'enabled': True, 'model': 'saved-model'})
        self.assertEqual(public['source'], 'local')
        self.assertEqual(public['status'], 'enabled')
        self.assertNotIn('api_key', public)
        self.assertNotIn('legacy-env-key', repr(public))
        self.assertNotIn('legacy-env-key', repr(self.store.load()))
        restarted = AiSettingsStore(self.store.path)
        self.assertEqual(restarted.load().model, 'saved-model')
        self.assertEqual(restarted.load().api_key, 'legacy-env-key')
        self.assertEqual(dict(os.environ), before)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        with self.assertRaises(FrozenInstanceError):
            restarted.load().model = 'changed'
        self.assertFalse(list(self.store.path.parent.glob('.ai-settings-*')))

    def test_existing_dotenv_values_are_compatible(self):
        from dotenv import load_dotenv
        dotenv_file = self.root / '.env'
        dotenv_file.write_text('API_KEY=from-test-dotenv\nMODEL_NAME=dotenv-model\nMODEL_BASE_URL=https://dotenv.invalid/v1\nLLM_ENABLED=true\nLLM_TIMEOUT_SECONDS=17\n')
        with patch.dict(os.environ, {}, clear=True):
            load_dotenv(dotenv_file)
            settings = self.store.load()
            self.assertEqual(settings.api_key, 'from-test-dotenv')
            self.assertEqual(settings.model, 'dotenv-model')
            self.assertEqual(settings.timeout_seconds, 17)
            self.assertTrue(settings.usable)

    def test_blank_or_missing_key_preserves_but_explicit_clear_does_not_reload_env_key(self):
        self.store.save({'enabled': True})
        self.store.save({'api_key': '   ', 'model': 'second'})
        self.assertEqual(self.store.load().api_key, 'legacy-env-key')
        self.store.save({'timeout_seconds': 6})
        self.assertEqual(self.store.load().api_key, 'legacy-env-key')
        public = self.store.save({'clear_api_key': True})
        self.assertFalse(public['has_api_key'])
        self.assertEqual(public['status'], 'unconfigured')
        self.assertEqual(AiSettingsStore(self.store.path).load().api_key, '')
        self.assertEqual(os.environ['API_KEY'], 'legacy-env-key')
        with self.assertRaises(AISettingsError):
            self.store.prepare({'clear_api_key': True, 'api_key': 'new-key'})

    def test_origin_change_requires_reentered_key_but_path_change_is_allowed(self):
        for data in ({'base_url': 'https://other.invalid/v1'},
                     {'base_url': 'https://legacy.invalid:444/v1', 'api_key': ''}):
            with self.subTest(data=data), self.assertRaisesRegex(AISettingsError, '重新填写'):
                self.store.prepare(data)
        self.assertFalse(self.store.path.exists())
        allowed = self.store.prepare({'base_url': 'https://LEGACY.invalid:443/v2/'})
        self.assertEqual(allowed.base_url, 'https://legacy.invalid/v2')
        self.assertEqual(allowed.api_key, 'legacy-env-key')
        self.assertEqual(self.store.prepare({'base_url': 'https://other.invalid/v1', 'api_key': 'other-key'}).api_key, 'other-key')
        self.assertEqual(self.store.prepare({'base_url': 'https://other.invalid/v1', 'clear_api_key': True}).api_key, '')

    def test_unsafe_urls_and_controls_are_rejected_without_echoing_values(self):
        urls = ['http://remote.invalid/v1', 'ftp://remote.invalid/v1',
                'https://user:secret@remote.invalid/v1', 'https://remote.invalid/v1?api_key=secret',
                'https://remote.invalid/v1#secret', 'https://remote.invalid/v1?',
                'https://remote.invalid/\nsecret', 'https://remote.invalid/%0asecret',
                'http://0.0.0.0/v1', 'http://localhost.evil.invalid/v1',
                'https://remote.invalid:99999/v1', 'https://bad..invalid/v1',
                'https://remote.invalid/\\secret', 'http://[::1%lo0]/v1']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(AISettingsError) as error:
                normalize_base_url(url)
            self.assertNotIn('secret', str(error.exception))
        for url in ('http://127.0.0.1:8080/v1', 'http://localhost/v1', 'http://[::1]:8080/v1', 'https://model.invalid/v1'):
            self.assertTrue(normalize_base_url(url))
        for data in ({'api_key': 'secret\nkey'}, {'model': 'secret\nmodel'}, {'enabled': 'false'},
                     {'timeout_seconds': True}, {'timeout_seconds': 0}, {'timeout_seconds': 'nan'}, {'timeout_seconds': 61}):
            with self.subTest(data=data), self.assertRaises(AISettingsError) as error:
                self.store.prepare(data)
            self.assertNotIn('secret', str(error.exception))

    def test_key_cannot_appear_in_public_model_or_url_fields(self):
        for data in ({'model': 'legacy-env-key'}, {'base_url': 'https://legacy.invalid/legacy-env-key'},
                     {'base_url': 'https://legacy.invalid/%6cegacy-env-key'},
                     {'base_url': 'https://legacy.invalid/%256cegacy-env-key'},
                     {'model': '%6cegacy-env-key', 'api_key': 'replacement-key'},
                     {'model': 'legacy-env-key', 'clear_api_key': True}):
            with self.subTest(data=data), self.assertRaises(AISettingsError):
                self.store.prepare(data)
        with self.assertRaises(AISettingsError):
            public_settings(AISettings(model='secret', api_key='secret'))
        with self.assertRaises(AISettingsError):
            public_settings(AISettings(base_url='https://api.example.test/v1/%73ecret-review-key', api_key='secret-review-key'))

    def test_invalid_saved_json_and_env_have_static_safe_errors_and_can_be_repaired(self):
        self.store.path.parent.mkdir()
        self.store.path.write_text('{"api_key":"secret-unclosed')
        with self.assertRaises(AISettingsError) as error:
            self.store.load()
        self.assertNotIn('secret', str(error.exception))
        repaired = self.store.save({'enabled': True, 'base_url': 'https://fresh.invalid/v1',
                                    'model': 'fresh', 'timeout_seconds': 12, 'api_key': 'fresh-key'})
        self.assertTrue(repaired['has_api_key'])
        with patch.dict(os.environ, {'MODEL_BASE_URL': 'https://secret@invalid'}):
            empty_store = AiSettingsStore(self.root / 'other/ai.local.json')
            with self.assertRaises(AISettingsError) as error:
                empty_store.load()
            self.assertNotIn('secret', str(error.exception))

    def test_failed_atomic_replace_keeps_old_settings_and_removes_temporary_secret_file(self):
        self.store.save({'enabled': True})
        previous = self.store.path.read_bytes()
        with patch('core.ai_settings.os.replace', side_effect=OSError('private-exception')):
            with self.assertRaises(AISettingsError) as error:
                self.store.save({'api_key': 'replacement-key'})
        self.assertNotIn('private-exception', str(error.exception))
        self.assertEqual(self.store.path.read_bytes(), previous)
        self.assertFalse(list(self.store.path.parent.glob('.ai-settings-*')))
        with patch('core.ai_settings.tempfile.mkstemp', side_effect=OSError('private-folder')):
            with self.assertRaises(AISettingsError) as error:
                self.store.save({'model': 'another'})
        self.assertNotIn('private-folder', str(error.exception))


class AISettingsHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = AiSettingsStore(self.root / 'config/ai.local.json')
        self.env = patch.dict(os.environ, LEGACY_ENV, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    async def test_disabled_draft_connection_test_uses_fixed_prompt_and_never_saves_or_echoes(self):
        with local_model(content='provider-body-with-secret-must-not-be-returned') as (url, requests):
            draft = self.store.prepare({'enabled': False, 'base_url': url, 'api_key': 'local-test-key', 'model': 'probe-model'})
            result = await test_connection(draft)
        self.assertEqual(result, {'ok': True, 'code': 'ok', 'message': '连接成功，已收到模型回复'})
        self.assertFalse(self.store.path.exists())
        self.assertEqual(requests[0]['body']['messages'], [{'role': 'user', 'content': 'Reply with OK.'}])
        self.assertEqual(requests[0]['body']['model'], 'probe-model')
        self.assertEqual(requests[0]['authorization'], 'Bearer local-test-key')
        self.assertEqual(len(requests), 1)
        self.assertNotIn('local-test-key', repr(result))
        self.assertNotIn('provider-body', repr(result))
        self.assertEqual(os.environ['API_KEY'], 'legacy-env-key')

    async def test_http_failures_have_safe_codes_and_no_raw_provider_details(self):
        cases = [(401, 'auth_failed'), (403, 'auth_failed'), (404, 'not_found'),
                 (429, 'rate_limited'), (500, 'service_unavailable'), (400, 'request_rejected')]
        for status, code in cases:
            with self.subTest(status=status), local_model(status=status) as (url, requests):
                result = await test_connection(AISettings(base_url=url, model='test', api_key='private-local-key'))
                self.assertEqual(result['code'], code)
                self.assertFalse(result['ok'])
                self.assertNotIn(PRIVATE_ERROR, repr(result))
                self.assertNotIn('private-local-key', repr(result))
                self.assertEqual(len(requests), 1)

    async def test_redirect_is_not_followed_and_no_key_or_query_leaks_to_target(self):
        with local_model() as (target, target_requests):
            with local_model(status=307, redirect=target + '/collect?key=private-redirect-value') as (url, requests):
                result = await test_connection(AISettings(base_url=url, model='test', api_key='private-local-key'))
        self.assertEqual(result['code'], 'redirect_blocked')
        self.assertEqual(len(requests), 1)
        self.assertEqual(target_requests, [])
        self.assertNotIn('private-', repr(result))
        self.assertNotIn(target, repr(result))

    async def test_empty_response_timeout_and_missing_key_fail_safely(self):
        with local_model(content=' ') as (url, _):
            result = await test_connection(AISettings(base_url=url, model='test', api_key='local-key'))
            self.assertEqual(result['code'], 'invalid_response')
        with local_model(delay=1.2) as (url, _):
            result = await test_connection(AISettings(base_url=url, model='test', api_key='local-key', timeout_seconds=1))
            self.assertEqual(result['code'], 'timeout')
        self.assertEqual((await test_connection(AISettings()))['code'], 'missing_api_key')

    async def test_actual_default_and_custom_calls_use_the_saved_root_settings(self):
        state = StateStore(':memory:')
        try:
            with local_model() as (url, requests):
                self.store.save({'enabled': True, 'base_url': url, 'api_key': 'saved-key', 'model': 'default-model'})
                result = await ServiceEngine(default_profile(), state, settings_provider=self.store.load).reply(event('default'))
            self.assertEqual(result['reply_source'], 'ai')
            self.assertEqual(result['reply'], PRICE_REPLY)
            self.assertEqual(requests[0]['body']['model'], 'default-model')
            self.assertEqual(requests[0]['authorization'], 'Bearer saved-key')
            custom = {'products': [{'id': 'p1', 'name': '杯子', 'faq': [{'question': '能用洗碗机吗', 'answer': '可使用轻柔模式。'}]}]}
            with local_model(content='{"intent":"faq","faq_index":0}') as (url, requests):
                self.store.save({'base_url': url, 'api_key': 'custom-key', 'model': 'custom-model'})
                result = await ServiceEngine(custom, state, settings_provider=self.store.load).reply(event('custom', '机器洗会坏吗', item_id='p1'))
            self.assertEqual(result['reply'], '可使用轻柔模式。')
            self.assertEqual(result['intent'], 'faq')
            self.assertEqual(requests[0]['body']['model'], 'custom-model')
            self.assertEqual(requests[0]['authorization'], 'Bearer custom-key')
            self.assertEqual(os.environ['MODEL_NAME'], 'legacy-model')
        finally:
            state.close()

    async def test_live_updates_keep_inflight_snapshot_and_roots_do_not_share_keys(self):
        started, release = threading.Event(), threading.Event()

        def block_old(body):
            if body['model'] == 'old-model':
                started.set()
                release.wait(timeout=3)

        state = StateStore(':memory:')
        try:
            with local_model(before_response=block_old) as (url, requests):
                self.store.save({'enabled': True, 'base_url': url, 'api_key': 'old-key', 'model': 'old-model'})
                engine = ServiceEngine(default_profile(), state, settings_provider=self.store.load)
                old = asyncio.create_task(engine.reply(event('old')))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.store.save({'api_key': 'new-key', 'model': 'new-model'})
                new = await engine.reply(event('new'))
                other_store = AiSettingsStore(self.root / 'other/config/ai.local.json')
                other_store.save({'enabled': True, 'base_url': url, 'api_key': 'other-key', 'model': 'other-model'})
                other = await ServiceEngine(default_profile(), state, settings_provider=other_store.load).reply(event('other'))
                release.set()
                original = await old
            self.assertTrue(all(result['reply_source'] == 'ai' for result in (original, new, other)))
            by_model = {request['body']['model']: request['authorization'] for request in requests}
            self.assertEqual(by_model, {'old-model': 'Bearer old-key', 'new-model': 'Bearer new-key', 'other-model': 'Bearer other-key'})
            self.assertEqual(os.environ['API_KEY'], 'legacy-env-key')
        finally:
            release.set()
            state.close()

    async def test_runtime_reads_same_root_settings_without_restart_and_broken_file_falls_back(self):
        class Adapter:
            def __init__(self, config):
                self.status = 'stopped'
                self.last_error = ''
                self.ready = asyncio.Event()
            async def start(self, handler):
                self.handler = handler
                self.ready.set()
                await asyncio.Event().wait()
            async def disconnect(self):
                self.status = 'stopped'

        with local_model() as (url, requests):
            self.store.save({'enabled': True, 'base_url': url, 'api_key': 'runtime-key', 'model': 'runtime-model'})
            (self.root / 'config/platforms.json').write_text(json.dumps({'platforms': {'xianyu': {'shop_id': 'test', 'enabled': True, 'dry_run': True}}}))
            runtime = RuntimeManager(self.root)
            try:
                with patch('core.runtime.importlib.import_module', return_value=SimpleNamespace(XianyuBot=Adapter)):
                    await runtime.start()
                await runtime.adapters['xianyu'].ready.wait()
                result = await runtime.adapters['xianyu'].handler(event('live-first'))
                self.assertEqual(result['reply_source'], 'ai')
                self.store.save({'enabled': False})
                disabled = await runtime.adapters['xianyu'].handler(event('live-disabled'))
                self.assertEqual(disabled['reply_source'], 'builtin')
                self.store.path.write_text('{"api_key":"private-broken-json')
                broken = await runtime.adapters['xianyu'].handler(event('live-broken'))
                self.assertEqual(broken['reply_source'], 'builtin')
                self.assertNotIn('private-broken', repr(broken))
                self.assertEqual(len(requests), 1)
                self.assertTrue(runtime.running)
            finally:
                await runtime.stop()
                if runtime.engine:
                    runtime.engine.store.close()

    async def test_automatic_replies_also_refuse_redirects(self):
        state = StateStore(':memory:')
        try:
            with local_model() as (target, target_requests):
                with local_model(status=302, redirect=target) as (url, requests):
                    settings = AISettings(base_url=url, api_key='private-auto-key', model='test')
                    result = await ServiceEngine(default_profile(), state, ai_settings=settings).reply(event('redirect'))
            self.assertEqual(result['reply_source'], 'builtin')
            self.assertEqual(result['reply'], reply_default('多少钱').reply)
            self.assertEqual(len(requests), 1)
            self.assertEqual(target_requests, [])
        finally:
            state.close()


if __name__ == '__main__':
    unittest.main()
