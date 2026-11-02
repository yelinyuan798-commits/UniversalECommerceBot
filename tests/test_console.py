import json
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
    profile = state['profile']
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
    profile = client.get('/api/state').json()['profile']
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
