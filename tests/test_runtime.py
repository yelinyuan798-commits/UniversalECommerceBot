"""Lifecycle and per-platform send-mode checks without merchant network access."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.profiles import ProfileStore
from core.runtime import RuntimeManager, read_platforms
from core.platform_catalog import PLATFORMS
from platforms.taobao.bot import TaobaoBot


class StubAdapter:
    def __init__(self, config):
        self.config = config
        self.dry_run = config['dry_run']
        self.options = config.get('config', {})
        self.status = 'stopped'
        self.last_error = ''
        self.on_delivery = None
        self.started = asyncio.Event()
        self.cancelled = False
        self.disconnected = False

    def check_config(self):
        return self.options.get('errors', [])

    async def start(self, handler):
        self.status = 'preview' if self.dry_run else 'listening'
        self.started.set()
        if self.options.get('failure') == 'auth':
            self.status = 'waiting_login'
            self.last_error = '登录已失效，请在本机重新登录'
            return
        if self.options.get('failure') == 'exception':
            raise ConnectionError('private transport diagnostics must not reach the console')
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def disconnect(self):
        self.status = 'stopped'
        self.disconnected = True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        ProfileStore(self.root / 'config/merchant.json').save({
            'merchant_name': '测试商家',
            'products': [{'id': 'p1', 'name': '样品', 'price': 99}],
        })
        self.manager = RuntimeManager(self.root)
        module = SimpleNamespace(XianyuBot=StubAdapter, TaobaoBot=StubAdapter, JingdongBot=StubAdapter)
        self.import_patch = patch('core.runtime.importlib.import_module', return_value=module)
        self.mock_import = self.import_patch.start()

    async def asyncTearDown(self):
        await self.manager.stop()
        if self.manager.engine:
            self.manager.engine.store.close()
        self.import_patch.stop()
        self.temp.cleanup()

    def write_config(self, **platforms):
        normalized = {}
        for name, options in platforms.items():
            normalized[name] = {'name': name, 'shop_id': 'test-' + name, 'enabled': True,
                                'dry_run': True, 'config': {}, **options}
        (self.root / 'config/platforms.json').write_text(
            json.dumps({'platforms': normalized}), encoding='utf-8')

    async def wait_started(self):
        await asyncio.wait_for(asyncio.gather(*(a.started.wait() for a in self.manager.adapters.values())), 2)

    async def test_live_preserves_each_platform_observe_protection(self):
        self.write_config(xianyu={'dry_run': False}, taobao={'dry_run': True})
        await self.manager.start(live=True)
        await self.wait_started()
        self.assertFalse(self.manager.adapters['xianyu'].dry_run)
        self.assertTrue(self.manager.adapters['taobao'].dry_run)
        self.assertEqual(self.manager.adapters['xianyu'].status, 'listening')
        self.assertEqual(self.manager.adapters['taobao'].status, 'preview')
        saved = json.loads((self.root / 'config/platforms.json').read_text())
        self.assertTrue(saved['platforms']['taobao']['dry_run'])

    async def test_observe_forces_no_send_even_if_platform_was_live_enabled(self):
        self.write_config(xianyu={'dry_run': False}, jingdong={'dry_run': False})
        await self.manager.start(live=False)
        await self.wait_started()
        self.assertTrue(all(a.dry_run for a in self.manager.adapters.values()))
        self.assertTrue(all(a.status == 'preview' for a in self.manager.adapters.values()))

    async def test_live_cannot_start_when_every_platform_is_protected(self):
        self.write_config(xianyu={'dry_run': True})
        with self.assertRaisesRegex(ValueError, '观察保护'):
            await self.manager.start(live=True)
        self.assertFalse(self.manager.running)
        self.assertEqual(self.manager.tasks, {})

    async def test_missing_merchant_profile_blocks_connection(self):
        self.write_config(xianyu={})
        (self.root / 'config/merchant.json').unlink()
        with self.assertRaisesRegex(ValueError, '保存自己的商品配置'):
            await self.manager.start()
        self.assertEqual(self.manager.tasks, {})

    async def test_invalid_profile_blocks_connection(self):
        self.write_config(xianyu={})
        (self.root / 'config/merchant.json').write_text(json.dumps({
            'products': [{'id': 'p1', 'name': '样品', 'price': 10, 'min_price': 99}]}))
        with self.assertRaisesRegex(ValueError, '最低报价'):
            await self.manager.start()
        self.assertEqual(self.manager.tasks, {})

    async def test_real_browser_selector_validation_runs_before_any_start(self):
        self.write_config(xianyu={}, taobao={'config': {
            'url': 'https://example.invalid/inbox',
            'journal_path': str(self.root / 'journal.db'),
        }})
        module = SimpleNamespace(XianyuBot=StubAdapter, TaobaoBot=TaobaoBot)
        self.mock_import.return_value = module
        with self.assertRaisesRegex(ValueError, 'selectors.ready'):
            await self.manager.start()
        self.assertEqual(self.manager.tasks, {})
        self.assertFalse(self.manager.running)

    async def test_auth_failure_return_is_recorded_and_survives_disconnect(self):
        self.write_config(xianyu={'config': {'failure': 'auth'}})
        await self.manager.start()
        await asyncio.wait_for(self.manager.tasks['xianyu'], 2)
        adapter = self.manager.adapters['xianyu']
        self.assertTrue(adapter.disconnected)
        self.assertEqual(adapter.status, 'waiting_login')
        self.assertIn('登录已失效', self.manager.errors['xianyu'])
        state = self.manager.state()
        self.assertFalse(state['running'])
        self.assertFalse(state['platforms']['xianyu']['active'])
        self.assertEqual(state['platforms']['xianyu']['status'], 'waiting_login')

    async def test_one_failed_connection_does_not_cancel_other_platforms(self):
        self.write_config(xianyu={'config': {'failure': 'exception'}}, taobao={})
        await self.manager.start()
        await self.wait_started()
        await asyncio.wait_for(self.manager.tasks['xianyu'], 2)
        self.assertTrue(self.manager.running)
        self.assertFalse(self.manager.tasks['taobao'].done())
        self.assertFalse(self.manager.adapters['taobao'].cancelled)
        self.assertIn('ConnectionError', self.manager.errors['xianyu'])
        self.assertNotIn('private transport', self.manager.errors['xianyu'])
        self.assertFalse(self.manager.state()['platforms']['xianyu']['active'])
        self.assertEqual(self.manager.state()['platforms']['taobao']['status'], 'preview')

    async def test_stop_cancels_and_disconnects_all_active_connections(self):
        self.write_config(xianyu={}, taobao={})
        await self.manager.start()
        await self.wait_started()
        state = await asyncio.wait_for(self.manager.stop(), 2)
        self.assertFalse(state['running'])
        self.assertEqual(state['mode'], 'stopped')
        self.assertTrue(all(t.done() for t in self.manager.tasks.values()))
        self.assertTrue(all(a.cancelled and a.disconnected for a in self.manager.adapters.values()))
        self.assertTrue(all(a.status == 'stopped' for a in self.manager.adapters.values()))
        await self.manager.stop()


class PlatformConfigurationMigrationTests(unittest.TestCase):
    """An upgrade must expose new integrations without activating or resetting stores."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        repository = Path(__file__).resolve().parents[1]
        self.default_path = self.root / 'config/platforms.json'
        self.default_path.write_bytes((repository / 'config/platforms.json').read_bytes())
        self.local_path = self.root / 'config/platforms.local.json'
        self.defaults = json.loads(self.default_path.read_text(encoding='utf-8'))
        self.existing = {
            'xianyu': {'name': '已有闲鱼店铺', 'shop_id': 'existing-xianyu',
                       'enabled': True, 'dry_run': False, 'class': PLATFORMS['xianyu'],
                       'config': {'cookies_env': 'EXISTING_XIANYU_COOKIE'}},
            'taobao': {'name': '已有淘宝店铺', 'shop_id': 'existing-taobao',
                       'enabled': False, 'dry_run': True, 'class': PLATFORMS['taobao'],
                       'config': {'url': 'https://example.invalid/old-inbox',
                                  'profile_dir': 'data/browser/existing-taobao',
                                  'selectors': {'ready': '#old-ready', 'composer': '#old-composer'}}},
            'jingdong': {'name': '已有京东店铺', 'shop_id': 'existing-jingdong',
                        'enabled': True, 'dry_run': False, 'class': PLATFORMS['jingdong'],
                        'config': {'profile_dir': 'data/browser/existing-jd',
                                   'process_existing': True, 'poll_interval': 9}},
        }

    def write_old_local(self, platforms=None):
        self.local_path.write_text(json.dumps({'platforms': self.existing if platforms is None else platforms},
                                             ensure_ascii=False, indent=2), encoding='utf-8')

    def test_old_three_platform_settings_survive_and_four_new_platforms_stay_disabled(self):
        self.write_old_local()
        defaults_before, local_before = self.default_path.read_bytes(), self.local_path.read_bytes()
        merged = read_platforms(self.root)['platforms']
        self.assertEqual(set(merged), {'xianyu', 'taobao', 'jingdong', 'pinduoduo',
                                       'douyin', 'kuaishou', 'xiaohongshu'})
        for name, expected in self.existing.items():
            with self.subTest(existing=name):
                self.assertEqual(merged[name], expected)
        for name in ('pinduoduo', 'douyin', 'kuaishou', 'xiaohongshu'):
            with self.subTest(new=name):
                self.assertEqual(merged[name], self.defaults['platforms'][name])
                self.assertIs(merged[name]['enabled'], False)
                self.assertIs(merged[name]['dry_run'], True)
        self.assertEqual(self.default_path.read_bytes(), defaults_before)
        self.assertEqual(self.local_path.read_bytes(), local_before)

    def test_reading_and_editing_returned_config_does_not_mutate_saved_configuration(self):
        self.write_old_local()
        first = read_platforms(self.root)
        first['platforms']['taobao']['config']['selectors']['ready'] = '#temporary-edit'
        first['platforms']['douyin']['enabled'] = True
        reloaded = read_platforms(self.root)['platforms']
        self.assertEqual(reloaded['taobao']['config']['selectors']['ready'], '#old-ready')
        self.assertIs(reloaded['douyin']['enabled'], False)

    def test_missing_local_file_uses_all_seven_safe_defaults(self):
        merged = read_platforms(self.root)
        self.assertEqual(merged, self.defaults)
        self.assertEqual(len(merged['platforms']), 7)
        self.assertTrue(all(cfg['enabled'] is False and cfg['dry_run'] is True
                            for cfg in merged['platforms'].values()))

    def test_older_local_entry_without_class_is_normalized_without_overwriting_file(self):
        entry = dict(self.existing['taobao'])
        entry.pop('class')
        self.write_old_local({'taobao': entry})
        before = self.local_path.read_bytes()
        merged = read_platforms(self.root)['platforms']
        self.assertEqual(merged['taobao']['class'], PLATFORMS['taobao'])
        self.assertEqual(merged['taobao']['config'], entry['config'])
        self.assertEqual(self.local_path.read_bytes(), before)

    def test_invalid_local_send_setting_is_not_silently_accepted_during_merge(self):
        entry = {**self.existing['taobao'], 'enabled': 'false'}
        self.write_old_local({'taobao': entry})
        with self.assertRaisesRegex(ValueError, 'enabled 必须为布尔值'):
            read_platforms(self.root)

    def test_local_override_cannot_replace_builtin_adapter_class(self):
        entry = {**self.existing['taobao'], 'class': 'os.system'}
        self.write_old_local({'taobao': entry})
        with self.assertRaisesRegex(ValueError, '内置实现'):
            read_platforms(self.root)
