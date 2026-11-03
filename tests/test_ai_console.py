"""Merchant AI settings: public HTTP contract, private storage and real SDK probe."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from web.server import create_app

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-Requested-With": "merchant-console"}
SECRET = "test-only-credential-never-return"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for name in ("API_KEY", "MODEL_BASE_URL", "MODEL_NAME", "LLM_ENABLED", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config/platforms.json", tmp_path / "config/platforms.json")
    return tmp_path


@pytest.fixture
def client(workspace):
    with TestClient(create_app(workspace)) as client:
        yield client


def draft(**changes):
    return {"enabled": True, "base_url": "https://provider.example/v1", "model": "test-model",
            "timeout_seconds": 3, "api_key": SECRET, **changes}


def test_saved_settings_are_private_persistent_and_can_be_cleared(client, workspace, monkeypatch):
    assert client.get('/api/ai-settings').json()['has_api_key'] is False
    saved = client.put('/api/ai-settings', headers=HEADERS, json=draft())
    assert saved.status_code == 200
    assert saved.json()['saved'] is True
    assert saved.json()['source'] == 'local'
    assert SECRET not in saved.text
    path = workspace / 'config/ai.local.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())['api_key'] == SECRET
    retained = client.put('/api/ai-settings', headers=HEADERS, json={'api_key': '', 'model': 'new-model'})
    assert retained.json()['has_api_key'] is True
    with TestClient(create_app(workspace)) as reopened:
        public = reopened.get('/api/ai-settings')
        assert public.json()['model'] == 'new-model'
        assert SECRET not in public.text
        assert reopened.get('/api/state').json()['ai_configured'] is True
    monkeypatch.setenv('API_KEY', 'legacy-key-must-not-reappear')
    assert client.put('/api/ai-settings', headers=HEADERS, json={'clear_api_key': True}).json()['has_api_key'] is False
    assert client.get('/api/state').json()['ai_configured'] is False
    assert json.loads(path.read_text())['api_key'] == ''


def test_new_origin_requires_new_key_before_save_or_probe(client, workspace):
    client.put('/api/ai-settings', headers=HEADERS, json=draft())
    before = (workspace / 'config/ai.local.json').read_bytes()
    changed = {'base_url': 'https://different.example/v1', 'api_key': ''}
    for method, path in ((client.put, '/api/ai-settings'), (client.post, '/api/ai-settings/test')):
        response = method(path, headers=HEADERS, json=changed)
        assert response.status_code == 400
        assert '重新填写' in response.json()['detail']
        assert SECRET not in response.text
    assert (workspace / 'config/ai.local.json').read_bytes() == before


@pytest.mark.parametrize('path,method', [('/api/ai-settings', 'put'), ('/api/ai-settings/test', 'post')])
def test_settings_routes_require_local_workbench_and_sanitize_invalid_bodies(client, path, method):
    send = getattr(client, method)
    assert send(path, json=draft()).status_code == 403
    assert send(path, headers={**HEADERS, 'Origin': 'https://other.example'}, json=draft()).status_code == 403
    for body in ([SECRET], SECRET, {'api_key': {'secret': SECRET}}, {'model': SECRET + '\n'}):
        response = send(path, headers=HEADERS, json=body)
        assert response.status_code == 400
        assert SECRET not in response.text
        assert response.headers['Cache-Control'] == 'no-store'
    malformed = send(path, headers={**HEADERS, 'Content-Type': 'application/json'},
                     content='{"api_key":"' + SECRET + '",')
    assert malformed.status_code == 400
    assert SECRET not in malformed.text
    assert client.get('/api/ai-settings', headers={'Host': 'other.example'}).status_code == 403


def test_corrupt_private_settings_do_not_break_workbench_and_can_be_replaced(client, workspace):
    path = workspace / 'config/ai.local.json'
    path.write_text('{"api_key":"' + SECRET)
    state = client.get('/api/state')
    assert state.status_code == 200
    assert state.json()['ai_configured'] is False
    assert SECRET not in client.get('/api/ai-settings').text
    assert client.put('/api/ai-settings', headers=HEADERS, json=draft()).status_code == 200


@pytest.fixture
def provider():
    requests = []
    behavior = {'status': 200, 'content': '您好，想了解哪件商品呢？'}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': data})
            self.send_response(behavior['status'])
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            body = {'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': data['model'],
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': behavior['content']},
                                 'finish_reason': 'stop'}]}
            if behavior['status'] != 200:
                body = {'error': {'message': SECRET + ' private upstream error'}}
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests, behavior
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_actual_sdk_probe_uses_draft_without_saving_or_enabling_then_save_drives_preview(client, workspace, provider):
    url, requests, behavior = provider
    candidate = draft(base_url=url, enabled=False)
    response = client.post('/api/ai-settings/test', headers=HEADERS, json=candidate)
    assert response.status_code == 200
    assert response.json()['ok'] is True
    assert response.json()['latency_ms'] >= 0
    assert behavior['content'] not in response.text
    assert SECRET not in response.text
    assert not (workspace / 'config/ai.local.json').exists()
    assert client.get('/api/state').json()['ai_configured'] is False
    assert client.get('/api/events').json() == {'events': [], 'handoffs': []}
    assert len(requests) == 1
    assert requests[0]['path'] == '/v1/chat/completions'
    assert requests[0]['authorization'] == 'Bearer ' + SECRET
    assert requests[0]['body']['messages'] == [{'role': 'user', 'content': 'Reply with OK.'}]
    assert requests[0]['body']['model'] == 'test-model'
    candidate['enabled'] = True
    assert client.put('/api/ai-settings', headers=HEADERS, json=candidate).status_code == 200
    original = (workspace / 'config/ai.local.json').read_bytes()
    preview = client.post('/api/preview', headers=HEADERS, json={'text': '你好'}).json()
    assert preview['reply_source'] == 'ai'
    assert preview['reply'] == behavior['content']
    behavior['status'] = 401
    failed = client.post('/api/ai-settings/test', headers=HEADERS, json={'api_key': ''})
    assert failed.json()['ok'] is False
    assert failed.json()['error_code'] == 'auth_failed'
    assert SECRET not in failed.text
    assert 'private upstream error' not in failed.text
    assert (workspace / 'config/ai.local.json').read_bytes() == original
    assert client.get('/api/state').json()['running'] is False


def test_one_probe_at_a_time_and_saving_during_probe_uses_isolated_snapshot(workspace, monkeypatch):
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        snapshots = []
        async def probe(settings):
            snapshots.append(settings)
            entered.set()
            await finish.wait()
            return {'ok': True, 'code': 'ok', 'message': '连接成功'}
        monkeypatch.setattr('web.server.test_connection', probe)
        app = create_app(workspace)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
                task = asyncio.create_task(c.post('/api/ai-settings/test', headers=HEADERS, json=draft()))
                await asyncio.wait_for(entered.wait(), 3)
                try:
                    duplicate = await c.post('/api/ai-settings/test', headers=HEADERS, json=draft())
                    assert duplicate.status_code == 429
                    saved = await c.put('/api/ai-settings', headers=HEADERS, json=draft(model='updated-model'))
                    assert saved.status_code == 200
                    assert snapshots[0].model == 'test-model'
                finally:
                    finish.set()
                assert (await task).json()['ok'] is True
    asyncio.run(scenario())
