"""New merchant adapters on synthetic pages in real Chromium, not real-store proof."""
import importlib
from pathlib import Path
import tempfile
import unittest

from core.platform_catalog import PLATFORMS
from core.service import ServiceEngine, conversation_key
from core.store import StateStore

NEW_PLATFORMS = ('pinduoduo', 'douyin', 'kuaishou', 'xiaohongshu')
FIXTURE = Path(__file__).parent / 'fixtures' / 'merchant_inbox.html'


class NewPlatformIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bots = []
        self.stores = []

    async def asyncTearDown(self):
        for bot in self.bots:
            await bot.disconnect()
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    async def make_bot(self, key, engine, journal_path=None):
        module, name = PLATFORMS[key].rsplit('.', 1)
        cls = getattr(importlib.import_module(module), name)
        bot = cls({'shop_id': 'same-shop-id', 'dry_run': False, 'config': {
            'url': FIXTURE.resolve().as_uri(), 'profile_dir': str(self.root / key / 'browser'),
            'journal_path': str(journal_path or self.root / key / 'delivery.db'), 'headless': True,
            'selectors': {'ready': '#ready', 'conversation': '#conversation', 'conversations': '.thread',
                          'messages': '.message', 'message_text': '.text', 'composer': '#composer', 'send': '#send'},
        }})
        bot._message_handler = engine.reply
        async def delivery(event_key, status, error=''):
            if status == 'sent':
                self.assertTrue(engine.mark_sent(event_key))
            elif status in {'failed', 'unknown'}:
                engine.mark_failed(event_key, 'delivery_uncertain')
        bot.on_delivery = delivery
        self.bots.append(bot)
        await bot.connect()
        self.assertEqual(await bot.poll_once(), 0)
        return bot

    async def run_full_flow(self, key):
        profile = {'merchant_name': '新增平台测试商店', 'products': [
            {'id': 'product-a', 'platform': key, 'name': '测试商品甲', 'price': 99, 'min_price': 90, 'stock': 8},
            {'id': 'product-b', 'platform': key, 'name': '测试商品乙', 'price': 199, 'stock': 6},
        ]}
        state_path = self.root / 'state.db'
        store = StateStore(state_path)
        self.stores.append(store)
        engine = ServiceEngine(profile, store)
        bot = await self.make_bot(key, engine)
        await bot.page.evaluate("addMessage('chat-a','quote','多少钱？')")
        self.assertEqual(await bot.poll_once(), 1)
        sent = await bot.page.evaluate('window.sent')
        self.assertEqual(len(sent), 1)
        self.assertIn('99 元', sent[0]['text'])
        self.assertEqual(sent[0]['conversation'], 'chat-a')
        self.assertEqual(sent[0]['user'], 'buyer-a')
        self.assertEqual(store.list_events()[0]['platform'], key)
        self.assertEqual(store.list_events()[0]['status'], 'sent')
        # Re-delivery of the same native ID does not repeat a response.
        await bot.page.evaluate("addMessage('chat-a','quote','多少钱？')")
        self.assertEqual(await bot.poll_once(), 0)
        self.assertEqual(len(await bot.page.evaluate('window.sent')), 1)
        await bot.page.evaluate("addMessage('chat-b','different-buyer','多少钱？')")
        self.assertEqual(await bot.poll_once(), 1)
        sent = await bot.page.evaluate('window.sent')
        self.assertEqual(sent[-1]['user'], 'buyer-b')
        self.assertIn('199 元', sent[-1]['text'])
        await bot.page.evaluate("addMessage('chat-a','handoff','退款，转人工')")
        self.assertEqual(await bot.poll_once(), 1)
        self.assertEqual(len(await bot.page.evaluate('window.sent')), 3)
        ckey = conversation_key(key, 'same-shop-id', 'chat-a')
        self.assertTrue(store.is_paused(ckey))
        # Handoff remains effective across a separate engine/database connection.
        reopened = StateStore(state_path)
        self.stores.append(reopened)
        bot._message_handler = ServiceEngine(profile, reopened).reply
        await bot.page.evaluate("addMessage('chat-a','after-handoff','多少钱？')")
        self.assertEqual(await bot.poll_once(), 0)
        self.assertEqual(len(await bot.page.evaluate('window.sent')), 3)
        self.assertEqual(reopened.list_events()[0]['intent'], 'human_takeover')
        self.assertTrue(reopened.is_paused(ckey))

    async def test_pinduoduo_engine_delivery_dedupe_handoff(self):
        await self.run_full_flow('pinduoduo')

    async def test_douyin_engine_delivery_dedupe_handoff(self):
        await self.run_full_flow('douyin')

    async def test_kuaishou_engine_delivery_dedupe_handoff(self):
        await self.run_full_flow('kuaishou')

    async def test_xiaohongshu_engine_delivery_dedupe_handoff(self):
        await self.run_full_flow('xiaohongshu')

    async def test_same_native_ids_are_isolated_across_all_four_platforms(self):
        profile = {'merchant_name': '隔离测试商店', 'products': [
            {'id': 'product-a', 'platform': key, 'name': f'{key}商品', 'price': 100 + index * 10}
            for index, key in enumerate(NEW_PLATFORMS)
        ]}
        store = StateStore(self.root / 'shared-state.db')
        self.stores.append(store)
        engine = ServiceEngine(profile, store)
        # Deliberately share the delivery journal as well as service state.
        # Correct isolation must come from platform identity, not just filenames.
        shared_journal = self.root / 'shared-delivery.db'
        for index, key in enumerate(NEW_PLATFORMS):
            with self.subTest(platform=key):
                bot = await self.make_bot(key, engine, shared_journal)
                await bot.page.evaluate("addMessage('chat-a','same-native-id','多少钱？')")
                self.assertEqual(await bot.poll_once(), 1)
                sent = await bot.page.evaluate('window.sent')
                self.assertEqual(len(sent), 1)
                self.assertIn(f'{100 + index * 10} 元', sent[0]['text'])
                self.assertEqual(await bot.poll_once(), 0)
                await bot.disconnect()
        events = store.list_events()
        self.assertEqual(len(events), 4)
        self.assertEqual({event['platform'] for event in events}, set(NEW_PLATFORMS))
        self.assertEqual(len({event['event_key'] for event in events}), 4)
        self.assertTrue(all(event['status'] == 'sent' for event in events))
