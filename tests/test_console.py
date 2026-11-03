import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from web.server import create_app
from core.runtime import RuntimeManager, validate_platforms
from core.profiles import ProfileStore

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-Requested-With": "merchant-console"}

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    (tmp_path / "config").mkdir()
    for name in ("merchant.example.json", "platforms.json"):
        shutil.copy(ROOT / "config" / name, tmp_path / "config" / name)
    with TestClient(create_app(tmp_path)) as test:
        yield test


def test_console_profile_preview_handoff(client):
    assert client.get('/').status_code == 200
    state = client.get('/api/state').json()
    assert not state['profile_saved']
    assert state['reply_mode'] == 'default'
    profile = client.get('/api/profile/example').json()
    assert profile['reply_mode'] == 'custom'
    assert client.put('/api/profile', headers=HEADERS, json=profile).status_code == 200
    assert client.get('/api/state').json()['profile_saved']
    data = {'item_id': 'demo-notebook', 'text': '可以便宜点吗', 'session_id': 't'}
    replies = [client.post('/api/preview', headers=HEADERS, json=data).json() for _ in range(4)]
    assert all(r['simulated'] for r in replies)
    assert replies[0]['reply'] != replies[1]['reply']
    assert '25 元' in replies[-1]['reply']
    data['text'] = '转人工'
    assert client.post('/api/preview', headers=HEADERS, json=data).json()['action'] == 'handoff'
    data['text'] = '多少钱'
    assert client.post('/api/preview', headers=HEADERS, json=data).json()['reply'] is None
    assert client.get('/api/events').json()['events'] == []


def test_local_console_rejects_remote_writes(client):
    assert client.post('/api/stop', json={}).status_code == 403
    assert client.post('/api/stop', headers={**HEADERS, 'Origin': 'https://evil.example'}, json={}).status_code == 403
    assert client.get('/api/state', headers={'Host':'evil.example'}).status_code == 403
    assert client.post('/api/stop', headers=HEADERS, json={}).status_code == 200


def test_invalid_config_and_start(client):
    assert client.post('/api/start', headers=HEADERS, json={'mode':'live'}).status_code == 400
    assert client.put('/api/connections', headers=HEADERS, json={'platforms': {'custom':{}}}).status_code == 400
    profile = client.get('/api/profile/example').json()
    profile['products'][0]['min_price'] = 100
    assert client.put('/api/profile', headers=HEADERS, json=profile).status_code == 400
    cfg={'platforms':{'xianyu':{'shop_id':'abc','class':'os.system'}}}
    with pytest.raises(ValueError): validate_platforms(cfg)


def test_handoff_metadata(client):
    data={'platform':'taobao','shop_id':'shop','conversation_id':'conversation','paused':True}
    assert client.post('/api/handoff',headers=HEADERS,json=data).status_code==200
    rows=client.get('/api/events').json()['handoffs']
    assert rows[0]['conversation_id']=='conversation'
    data['paused']=False
    assert client.post('/api/handoff',headers=HEADERS,json=data).status_code==200
    assert client.get('/api/events').json()['handoffs']==[]


def test_state_catalog_exposes_seven_platforms_with_honest_verification_status(client):
    response = client.get('/api/state')
    assert response.status_code == 200
    state = response.json()
    catalog = state['catalog']
    expected = {'xianyu', 'taobao', 'jingdong', 'pinduoduo', 'douyin', 'kuaishou', 'xiaohongshu'}
    assert len(catalog) == 7
    assert {entry['id'] for entry in catalog} == expected
    assert set(state['connections']['platforms']) == expected
    by_id = {entry['id']: entry for entry in catalog}
    for name in ('xianyu', 'taobao', 'jingdong'):
        assert by_id[name]['verification'] == 'user_verified'
        assert '用户报告' in by_id[name]['verification_label']
    for name in ('pinduoduo', 'douyin', 'kuaishou', 'xiaohongshu'):
        assert by_id[name]['verification'] == 'local_tested'
        assert '待实店验证' in by_id[name]['verification_label']
        assert by_id[name]['name']
        assert by_id[name]['url'].startswith('https://')
        assert state['connections']['platforms'][name]['enabled'] is False
        assert state['connections']['platforms'][name]['dry_run'] is True


def test_state_migrates_old_local_connections_without_resetting_existing_stores(client, tmp_path):
    initial = client.get('/api/state').json()
    legacy = {name: initial['connections']['platforms'][name]
              for name in ('xianyu', 'taobao', 'jingdong')}
    for number, (name, cfg) in enumerate(legacy.items()):
        cfg['shop_id'] = 'existing-' + name
        cfg['enabled'] = number != 1
        cfg['dry_run'] = number == 1
    legacy['taobao']['config']['selectors']['ready'] = '#previously-calibrated-inbox'
    local_path = tmp_path / 'config/platforms.local.json'
    local_path.write_text(json.dumps({'platforms': legacy}, ensure_ascii=False), encoding='utf-8')
    before = local_path.read_bytes()
    state = client.get('/api/state').json()
    assert len(state['catalog']) == 7
    for name, expected in legacy.items():
        assert state['connections']['platforms'][name] == expected
    for name in ('pinduoduo', 'douyin', 'kuaishou', 'xiaohongshu'):
        assert state['connections']['platforms'][name] == initial['connections']['platforms'][name]
        assert state['connections']['platforms'][name]['enabled'] is False
    assert local_path.read_bytes() == before


def test_partial_connection_save_keeps_new_default_platforms_visible(client):
    previous = client.get('/api/state').json()['connections']['platforms']['taobao']
    previous['shop_id'] = 'saved-existing-taobao'
    previous['config']['selectors']['ready'] = '#persisted-ready'
    response = client.put('/api/connections', headers=HEADERS, json={'platforms': {'taobao': previous}})
    assert response.status_code == 200
    state = client.get('/api/state').json()
    assert len(state['connections']['platforms']) == 7
    assert state['connections']['platforms']['taobao'] == previous
    for name in ('pinduoduo', 'douyin', 'kuaishou', 'xiaohongshu'):
        assert state['connections']['platforms'][name]['enabled'] is False
        assert state['connections']['platforms'][name]['dry_run'] is True


def test_new_console_uses_default_skill_and_no_example_product(client, tmp_path):
    # Broken optional example material must not affect the new-installation path.
    (tmp_path / 'config/merchant.example.json').write_text('{broken example', encoding='utf-8')
    state = client.get('/api/state').json()
    assert state['reply_mode'] == 'default'
    assert state['profile']['reply_mode'] == 'default'
    assert state['profile']['products'] == []
    assert state['profile_saved'] is False
    assert state['default_skill']['id']
    assert state['default_skill']['name']
    assert state['default_skill']['description']
    assert not (tmp_path / 'config/merchant.json').exists()
    assert 'demo-notebook' not in json.dumps(state['profile'])
    assert '笔记本' not in json.dumps(state['profile'], ensure_ascii=False)


def test_default_preview_accepts_no_item_and_keeps_live_ledger_empty(client):
    response = client.post('/api/preview', headers=HEADERS,
                           json={'text': '你好，请问这个多少钱？', 'session_id': 'default-only'})
    assert response.status_code == 200
    result = response.json()
    assert result['simulated'] is True
    assert result['reply_mode'] == 'default'
    assert result['reply']
    for sample_fact in ('29.9', '25 元', '80 页', '48 小时', '笔记本', 'demo-notebook'):
        assert sample_fact not in result['reply']
    assert client.get('/api/events').json() == {'events': [], 'handoffs': []}


def test_example_is_only_loaded_by_explicit_request_and_does_not_activate(client, tmp_path):
    before = client.get('/api/state').json()['profile']
    response = client.get('/api/profile/example')
    assert response.status_code == 200
    example = response.json()
    assert example['reply_mode'] == 'custom'
    assert example['products'][0]['id'] == 'demo-notebook'
    assert client.get('/api/state').json()['profile'] == before
    assert not (tmp_path / 'config/merchant.json').exists()


@pytest.mark.parametrize('mode', ['invalid', '', None, [], {}, 1])
def test_reply_mode_rejects_invalid_values_without_saving(client, tmp_path, mode):
    response = client.put('/api/reply-mode', headers=HEADERS, json={'mode': mode})
    assert response.status_code == 400
    assert not (tmp_path / 'config/merchant.json').exists()
    assert client.get('/api/state').json()['reply_mode'] == 'default'


def test_reply_mode_switch_preserves_old_custom_profile_and_survives_restart(client, tmp_path):
    path = tmp_path / 'config/merchant.json'
    legacy = {'merchant_name': '原来的店铺', 'tone': '简短', 'custom_instructions': '请说明规格',
              'handoff_keywords': ['人工', '售后'], 'products': [
                  {'id': 'real-1', 'name': '定制杯', 'platform': 'taobao', 'price': 57.8,
                   'min_price': 50, 'stock': 8, 'shipping': '商家已确认的配送说明'}]}
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
    untouched = path.read_bytes()
    before = client.get('/api/state').json()
    assert before['reply_mode'] == 'custom'
    assert path.read_bytes() == untouched
    for mode in ('default', 'custom'):
        response = client.put('/api/reply-mode', headers=HEADERS, json={'mode': mode})
        assert response.status_code == 200
        saved = response.json()['profile']
        assert saved == {**before['profile'], 'reply_mode': mode}
        assert json.loads(path.read_text())['reply_mode'] == mode
    with TestClient(create_app(tmp_path)) as reopened:
        restored = reopened.get('/api/state').json()
        assert restored['reply_mode'] == 'custom'
        assert restored['profile'] == before['profile']


def test_selecting_mode_without_existing_profile_does_not_import_example(client, tmp_path):
    response = client.put('/api/reply-mode', headers=HEADERS, json={'mode': 'default'})
    assert response.status_code == 200
    assert response.json()['profile']['products'] == []
    assert client.get('/api/state').json()['profile_saved'] is True
    assert json.loads((tmp_path / 'config/merchant.json').read_text())['reply_mode'] == 'default'


def test_generated_and_saved_profile_respects_explicit_mode_and_default_ignores_old_price(client):
    payload = {'reply_mode': 'custom', 'merchant_name': '实际商店',
               'products': [{'id': 'real-custom-item', 'name': '定制样品', 'price': 876.54}]}
    generated = client.post('/api/profile/generate', headers=HEADERS, json=payload)
    assert generated.status_code == 200
    assert generated.json()['reply_mode'] == 'custom'
    assert client.put('/api/profile', headers=HEADERS, json=generated.json()).status_code == 200
    custom = client.post('/api/preview', headers=HEADERS,
                         json={'text': '多少钱', 'item_id': 'real-custom-item', 'session_id': 'custom-price'}).json()
    assert '876.54' in custom['reply']
    assert client.put('/api/reply-mode', headers=HEADERS, json={'mode': 'default'}).status_code == 200
    default = client.post('/api/preview', headers=HEADERS,
                          json={'text': '多少钱', 'item_id': 'real-custom-item', 'session_id': 'default-price'}).json()
    assert default['reply_mode'] == 'default'
    assert '876.54' not in default['reply']
    assert '定制样品' not in default['reply']
    assert client.get('/api/state').json()['profile']['products'][0]['price'] == 876.54
    payload['reply_mode'] = 'default'
    generated_default = client.post('/api/profile/generate', headers=HEADERS, json=payload).json()
    assert generated_default['reply_mode'] == 'default'
    assert client.put('/api/profile', headers=HEADERS, json=generated_default).json()['profile']['reply_mode'] == 'default'


class ConsoleIdleAdapter:
    def __init__(self, config):
        self.dry_run = config['dry_run']
        self.status = 'stopped'
        self.last_error = ''
        self.on_delivery = None

    def check_config(self):
        return []

    async def start(self, handler):
        self.status = 'preview' if self.dry_run else 'listening'
        await asyncio.Event().wait()

    async def disconnect(self):
        self.status = 'stopped'


def test_running_console_rejects_mode_and_profile_changes_but_default_can_connect(client, tmp_path):
    cfg = {'platforms': {'xianyu': {'name': '测试店', 'shop_id': 'test-shop',
                                  'enabled': True, 'dry_run': True, 'config': {}}}}
    assert client.put('/api/connections', headers=HEADERS, json=cfg).status_code == 200
    with patch('core.runtime.importlib.import_module', return_value=SimpleNamespace(XianyuBot=ConsoleIdleAdapter)):
        response = client.post('/api/start', headers=HEADERS, json={'mode': 'observe'})
        assert response.status_code == 200
        assert response.json()['running'] is True
        assert not (tmp_path / 'config/merchant.json').exists()
        profile = client.get('/api/state').json()['profile']
        assert client.put('/api/reply-mode', headers=HEADERS, json={'mode': 'custom'}).status_code == 400
        assert client.put('/api/profile', headers=HEADERS, json=profile).status_code == 400
        assert client.put('/api/connections', headers=HEADERS, json=cfg).status_code == 400
        assert not (tmp_path / 'config/merchant.json').exists()
        assert client.post('/api/stop', headers=HEADERS, json={}).status_code == 200
    assert client.put('/api/reply-mode', headers=HEADERS, json={'mode': 'custom'}).status_code == 200


@pytest.mark.parametrize('enabled,expected', [('false', False), ('FALSE', False), ('0', False),
                                             ('no', False), ('true', True)])
def test_ai_configured_respects_disabled_model_even_when_key_is_present(client, monkeypatch, enabled, expected):
    monkeypatch.setenv('API_KEY', 'unit-test-key')
    monkeypatch.setenv('LLM_ENABLED', enabled)
    state = client.get('/api/state').json()
    assert state['ai_configured'] is expected
    assert state['ai_status'] == ('enabled' if expected else 'disabled')
    assert 'unit-test-key' not in json.dumps(state)


def test_ai_configured_is_false_for_blank_key(client, monkeypatch):
    monkeypatch.setenv('API_KEY', '   ')
    monkeypatch.setenv('LLM_ENABLED', 'true')
    state = client.get('/api/state').json()
    assert state['ai_configured'] is False
    assert state['ai_status'] == 'unconfigured'



def test_console_and_static_revalidate_while_api_responses_are_not_stored(client):
    for path in ('/', '/static/app.js', '/static/style.css'):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-cache'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert "script-src 'self'" in response.headers['Content-Security-Policy']
    response = client.get('/api/state')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    rejected = client.post('/api/stop', json={})
    assert rejected.status_code == 403
    assert rejected.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('api_key,llm_enabled,expected', [
    (None, 'true', 'unconfigured'),
    ('', 'true', 'unconfigured'),
    ('unit-test-key', ' true ', 'enabled'),
    ('unit-test-key', ' false ', 'disabled'),
    (None, 'no', 'disabled'),
])
def test_ai_status_distinguishes_missing_configuration_and_explicit_disable(client, monkeypatch, api_key, llm_enabled, expected):
    if api_key is None:
        monkeypatch.delenv('API_KEY', raising=False)
    else:
        monkeypatch.setenv('API_KEY', api_key)
    monkeypatch.setenv('LLM_ENABLED', llm_enabled)
    state = client.get('/api/state').json()
    assert state['ai_status'] == expected
    assert state['ai_configured'] is (expected == 'enabled')
    assert 'unit-test-key' not in json.dumps(state)
    assert 'api_key' not in state


def test_default_console_explains_optional_ai_and_loads_updated_assets(client):
    html = client.get('/').text
    assert 'ai-settings-1' in html
    assert 'id="default-ai-status"' in html
    assert '接上 AI 后表达更自然' in html
    assert '打开 AI 设置' in html
    assert '#可选让默认客服说得更自然' in html
    assert '<input name="API_KEY"' not in html
    script = client.get('/static/app.js?v=ai-settings-1').text
    assert '未配置 AI' in script
    assert 'AI 辅助已开启' in script
    assert 'AI 已关闭' in script
    assert '调用失败时使用内置话术' in script
    assert '连接情况以实际试聊为准' in script


@pytest.mark.parametrize('source', ['ai', 'builtin', None])
def test_preview_preserves_actual_reply_source_without_inferring_from_configuration(client, monkeypatch, source):
    monkeypatch.setenv('API_KEY', 'unit-test-key')
    monkeypatch.setenv('LLM_ENABLED', 'true')
    async def reply(self, event):
        result = {'reply': '您好，想了解什么？', 'action': 'reply', 'event_key': 'source-contract'}
        if source is not None:
            result['reply_source'] = source
        return result
    monkeypatch.setattr('web.server.ServiceEngine.reply', reply)
    response = client.post('/api/preview', headers=HEADERS, json={'text': '你好'})
    assert response.status_code == 200
    result = response.json()
    assert result['reply_mode'] == 'default'
    if source is None:
        assert 'reply_source' not in result
    else:
        assert result['reply_source'] == source
