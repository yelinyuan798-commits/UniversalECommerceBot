import importlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from core.platform_catalog import (
    PLATFORM_ALIASES, PLATFORM_CATALOG, PLATFORM_LABELS, PLATFORMS, platform_metadata,
)
from platforms.browser import BrowserPlatform

NEW_PLATFORMS = {'pinduoduo', 'douyin', 'kuaishou', 'xiaohongshu'}
EXISTING_PLATFORMS = {'xianyu', 'taobao', 'jingdong'}
ROOT = Path(__file__).resolve().parents[1]


class PlatformCatalogTests(unittest.TestCase):
    def test_catalog_imports_all_seven_real_adapters(self):
        self.assertEqual(set(PLATFORMS), EXISTING_PLATFORMS | NEW_PLATFORMS)
        self.assertEqual(set(PLATFORM_LABELS), set(PLATFORMS))
        for key, path in PLATFORMS.items():
            with self.subTest(platform=key):
                module, name = path.rsplit('.', 1)
                cls = getattr(importlib.import_module(module), name)
                self.assertEqual(cls.platform_name, key)
                if key != 'xianyu':
                    self.assertTrue(issubclass(cls, BrowserPlatform))

    def test_aliases_preserve_existing_inputs_and_new_chinese_names(self):
        cases = {'咸鱼': 'xianyu', '天猫': 'taobao', '淘宝/天猫': 'taobao', '千牛': 'taobao',
                 'jd': 'jingdong', '拼多多': 'pinduoduo', 'pdd': 'pinduoduo', '抖店': 'douyin',
                 '快手小店': 'kuaishou', '小红书': 'xiaohongshu', '千帆': 'xiaohongshu'}
        for alias, key in cases.items():
            self.assertEqual(PLATFORM_ALIASES[alias], key)
        self.assertTrue(all(target in PLATFORMS for target in PLATFORM_ALIASES.values()))
        for key in PLATFORMS:
            self.assertEqual(PLATFORM_ALIASES[key], key)

    def test_metadata_distinguishes_user_report_from_local_tests(self):
        metadata = {row['id']: row for row in platform_metadata()}
        self.assertEqual(set(metadata), set(PLATFORMS))
        for key in EXISTING_PLATFORMS:
            self.assertEqual(metadata[key]['verification'], 'user_verified')
            self.assertIn('用户报告', metadata[key]['verification_label'])
        for key in NEW_PLATFORMS:
            self.assertEqual(metadata[key]['verification'], 'local_tested')
            self.assertIn('待实店验证', metadata[key]['verification_label'])
        for row in metadata.values():
            self.assertEqual(urlsplit(row['url']).scheme, 'https')
            self.assertNotIn('class', row)

    def test_metadata_returns_a_copy(self):
        first = platform_metadata()
        first[0]['name'] = 'changed'
        self.assertEqual(platform_metadata()[0]['name'], PLATFORM_CATALOG[first[0]['id']]['name'])

    def test_default_templates_are_disabled_and_require_actual_calibration(self):
        configs = json.loads((ROOT / 'config/platforms.json').read_text())['platforms']
        self.assertEqual(set(configs), set(PLATFORMS))
        with tempfile.TemporaryDirectory() as directory:
            for key in NEW_PLATFORMS:
                with self.subTest(platform=key):
                    cfg = configs[key]
                    self.assertFalse(cfg['enabled'])
                    self.assertTrue(cfg['dry_run'])
                    self.assertEqual(cfg['class'], PLATFORMS[key])
                    self.assertEqual(cfg['config']['url'], PLATFORM_CATALOG[key]['url'])
                    self.assertEqual(cfg['config']['profile_dir'], f'data/browser/{key}')
                    self.assertTrue(all(value == '' for value in cfg['config']['selectors'].values()))
                    cfg['config']['journal_path'] = str(Path(directory) / f'{key}.db')
                    module, name = PLATFORMS[key].rsplit('.', 1)
                    bot = getattr(importlib.import_module(module), name)(cfg)
                    self.assertTrue(any('selectors' in error for error in bot.check_config()))
