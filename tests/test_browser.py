"""Real Chromium integration against a local synthetic inbox, not platform certification."""
import tempfile
import re
import unittest
from pathlib import Path

from core.service import ServiceEngine, conversation_key
from core.store import StateStore
from platforms.taobao.bot import TaobaoBot
from platforms.jd.bot import JingdongBot

FIXTURE = Path(__file__).parent / 'fixtures' / 'merchant_inbox.html'


def config(root, **overrides):
    result = {'shop_id': 'test-shop', 'dry_run': False, 'config': {
        'url': FIXTURE.resolve().as_uri(), 'profile_dir': str(root / 'browser'),
        'journal_path': str(root / 'journal.db'), 'headless': True,
        'selectors': {'ready': '#ready', 'conversation': '#conversation', 'conversations': '.thread',
                      'messages': '.message', 'message_text': '.text', 'composer': '#composer', 'send': '#send'},
    }}
    result.update(overrides)
    return result


class BrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bots = []
        self.events, self.receipts = [], []

    async def asyncTearDown(self):
        for bot in self.bots:
            await bot.disconnect()
        self.temp.cleanup()

    async def make_bot(self, cls=TaobaoBot, **overrides):
        bot = cls(config(self.root, **overrides))
        async def handler(event):
            self.events.append(event)
            return {'reply': '商品价格 99 元', 'event_key': event['message_id']}
        async def delivered(key, status, error=''):
            self.receipts.append((key, status))
        bot._message_handler = handler
        bot.on_delivery = delivered
        self.bots.append(bot)
        await bot.connect()
        return bot

    async def test_taobao_full_flow_skips_history_outbound_and_duplicate(self):
        bot = await self.make_bot()
        self.assertEqual(await bot.poll_once(), 0)
        self.assertEqual(self.events, [])
        await bot.page.evaluate("addMessage('chat-a','new-a','多少钱？')")
        await bot.page.evaluate("addMessage('chat-b','new-b','多少钱？')")
        await bot.page.evaluate("addMessage('chat-b','own-b','人工回复','outbound')")
        self.assertEqual(await bot.poll_once(), 2)
        self.assertEqual(await bot.poll_once(), 0)
        self.assertEqual([e['platform'] for e in self.events], ['taobao', 'taobao'])
        sent = await bot.page.evaluate('window.sent')
        self.assertEqual([(m['conversation'], m['user']) for m in sent], [('chat-a', 'buyer-a'), ('chat-b', 'buyer-b')])
        self.assertEqual(self.receipts, [('new-a', 'sent'), ('new-b', 'sent')])
        # Journal survives a new adapter instance, even when backlog processing is opted into.
        await bot.disconnect()
        again = TaobaoBot(config(self.root))
        self.assertEqual(again.journal.status(self.events[0]), 'sent')

    async def test_jd_preview_never_clicks_send(self):
        bot = await self.make_bot(JingdongBot, dry_run=True)
        await bot.poll_once()
        await bot.page.evaluate("addMessage('chat-a','preview-a','多少钱？')")
        self.assertEqual(await bot.poll_once(), 1)
        self.assertEqual(await bot.page.evaluate('window.sent'), [])
        self.assertEqual(self.events[0]['platform'], 'jingdong')
        self.assertEqual(self.receipts, [('preview-a', 'preview')])
        self.assertEqual(bot.last_preview['reply'], '商品价格 99 元')

    async def test_jd_full_send_works_on_same_contract(self):
        bot = await self.make_bot(JingdongBot)
        await bot.poll_once()
        await bot.page.evaluate("addMessage('chat-b','jd-new','多少钱？')")
        self.assertEqual(await bot.poll_once(), 1)
        self.assertEqual((await bot.page.evaluate('window.sent'))[0]['user'], 'buyer-b')
        self.assertEqual(self.receipts, [('jd-new', 'sent')])

    async def test_conversation_switch_during_generation_prevents_misdelivery(self):
        bot = await self.make_bot()
        await bot.poll_once()
        async def handler(event):
            await bot.page.evaluate("selectConversation('chat-b')")
            return {'reply': '不可串发', 'event_key': event['message_id']}
        bot._message_handler = handler
        await bot.page.evaluate("addMessage('chat-a','switch-a','多少钱？')")
        await bot.poll_once()
        self.assertEqual(await bot.page.evaluate('window.sent'), [])
        self.assertEqual(self.receipts, [('switch-a', 'failed')])

    async def test_operator_draft_is_preserved(self):
        bot = await self.make_bot()
        # Single-active-conversation mode avoids navigating away from a human draft.
        bot.selectors.pop('conversations')
        await bot.poll_once()
        await bot.page.evaluate("addMessage('chat-a','draft-a','多少钱？')")
        await bot.page.locator('#composer').fill('人工正在编辑')
        await bot.poll_once()
        self.assertEqual(await bot.page.locator('#composer').input_value(), '人工正在编辑')
        self.assertEqual(await bot.page.evaluate('window.sent'), [])
        self.assertEqual(self.receipts, [('draft-a', 'failed')])

    async def test_multi_conversation_navigation_preserves_operator_draft(self):
        bot = await self.make_bot()
        await bot.poll_once()
        await bot.page.locator('#composer').fill('人工草稿不应随切换丢失')
        await bot.poll_once()
        self.assertEqual(await bot.page.locator('#composer').input_value(), '人工草稿不应随切换丢失')
        self.assertEqual(bot.status, 'needs_attention')
        self.assertEqual(await bot.page.evaluate('window.sent'), [])

    async def test_text_echo_without_delivery_status_is_unknown(self):
        bot = await self.make_bot()
        bot.selectors.pop('conversations')
        await bot.poll_once()
        await bot.page.evaluate("addMessage('chat-a','pending-a','多少钱？')")
        # Simulate a seller UI that echoes a pending local message, not server acceptance.
        await bot.page.evaluate("""() => { document.querySelector('#send').onclick=()=>{
          const text=document.querySelector('#composer').value;
          addMessage('chat-a','pending-out',text,'outbound');
        }; }""")
        await bot.poll_once()
        self.assertEqual(self.receipts, [('pending-a', 'unknown')])
        self.assertEqual(await bot.poll_once(), 0)

    async def test_new_buyer_first_message_after_start_is_answered(self):
        bot = await self.make_bot()
        await bot.poll_once()
        await bot.page.evaluate("""() => {
          threads['chat-new']={user:'buyer-new',item:'product-a',messages:[]};
          addMessage('chat-new','first-new','多少钱？');render();
        }""")
        self.assertEqual(await bot.poll_once(), 1)
        self.assertEqual(self.events[0]['user_id'], 'buyer-new')
        self.assertEqual((await bot.page.evaluate('window.sent'))[0]['conversation'], 'chat-new')

    async def test_live_missing_or_future_timestamp_never_sends_even_on_repeat(self):
        bot = await self.make_bot()
        await bot.poll_once()
        await bot.page.evaluate("""() => {
          threads['chat-a'].messages.push({id:'bad-time',direction:'inbound',text:'多少钱？',timestamp:'invalid'});
          threads['chat-a'].messages.push({id:'future-time',direction:'inbound',text:'多少钱？',timestamp:Date.now()+9999999});
          render();
        }""")
        await bot.poll_once()
        await bot.poll_once()
        self.assertEqual(self.events, [])
        self.assertEqual(await bot.page.evaluate('window.sent'), [])

    async def test_timestamp_calibration_failure_stays_visible_until_valid_new_message(self):
        bot = await self.make_bot()
        await bot.poll_once()
        await bot.page.evaluate("""() => {
          threads['chat-a'].messages.push({id:'missing-time',direction:'inbound',text:'多少钱？'});
          render();
        }""")
        await bot.poll_once()
        self.assertEqual(bot.status, 'needs_attention')
        self.assertIn('message_timestamp', bot.last_error)
        await bot.poll_once()
        self.assertEqual(bot.status, 'needs_attention')
        self.assertIn('message_timestamp', bot.last_error)
        self.assertEqual(self.events, [])
        await bot.page.evaluate("addMessage('chat-a','valid-time','多少钱？')")
        await bot.poll_once()
        self.assertEqual(bot.status, 'listening')
        self.assertEqual(bot.last_error, '')
        self.assertEqual([event['message_id'] for event in self.events], ['valid-time'])

    async def test_missing_login_ready_never_reads_or_sends(self):
        bot = await self.make_bot()
        await bot.poll_once()
        await bot.page.evaluate("addMessage('chat-a','login-a','多少钱？');document.querySelector('#ready').id='logged-out'")
        self.assertEqual(await bot.poll_once(), 0)
        self.assertEqual(bot.status, 'waiting_login')
        self.assertEqual(self.events, [])

    async def _service_end_to_end(self, cls):
        profile = {'merchant_name': '本地测试商店', 'products': [
            {'id': 'product-a', 'name': '样品甲', 'price': 99, 'min_price': 90, 'stock': 10},
            {'id': 'product-b', 'name': '样品乙', 'price': 199, 'min_price': 180, 'stock': 10},
        ]}
        state_path = self.root / 'service.db'
        state = StateStore(state_path)
        engine = ServiceEngine(profile, state)
        bot = await self.make_bot(cls)
        bot._message_handler = engine.reply
        async def confirmed(key, status, error=''):
            if status == 'sent':
                engine.mark_sent(key)
            elif status in {'failed', 'unknown'}:
                engine.mark_failed(key, 'delivery_uncertain')
        bot.on_delivery = confirmed
        try:
            await bot.poll_once()
            for i in range(4):
                await bot.page.evaluate("([id,text])=>addMessage('chat-a',id,text)", [f'bargain-{i}', '再便宜点，1 元卖吗？'])
                await bot.poll_once()
            sent = await bot.page.evaluate('window.sent')
            amounts = [float(re.search(r'(\d+(?:\.\d+)?) 元', row['text'])[1]) for row in sent]
            self.assertEqual(amounts, [96.0, 93.0, 90.0, 90.0])
            self.assertTrue(all(row['status'] == 'sent' for row in state.list_events()))
            # Same native message ID cannot trigger a fifth reply.
            await bot.page.evaluate("addMessage('chat-a','bargain-3','重复推送')")
            await bot.poll_once()
            self.assertEqual(len(await bot.page.evaluate('window.sent')), 4)
            await bot.page.evaluate("addMessage('chat-a','need-human','我要退款，转人工')")
            await bot.poll_once()
            self.assertEqual(len(await bot.page.evaluate('window.sent')), 5)
            key = conversation_key(cls.platform_name, 'test-shop', 'chat-a')
            self.assertTrue(state.is_paused(key))
            # A new service instance sees the durable handoff and stops responding.
            another_state = StateStore(state_path)
            try:
                another_engine = ServiceEngine(profile, another_state)
                bot._message_handler = another_engine.reply
                await bot.page.evaluate("addMessage('chat-a','after-human','多少钱？')")
                await bot.poll_once()
                self.assertTrue(another_state.is_paused(key))
                self.assertEqual(len(await bot.page.evaluate('window.sent')), 5)
                self.assertEqual(another_state.list_events()[0]['intent'], 'human_takeover')
            finally:
                another_state.close()
        finally:
            state.close()

    async def test_taobao_engine_to_browser_delivery_and_handoff(self):
        await self._service_end_to_end(TaobaoBot)

    async def test_jd_engine_to_browser_delivery_and_handoff(self):
        await self._service_end_to_end(JingdongBot)

    async def test_switch_preview_to_live_never_replays_observed_message(self):
        bot = await self.make_bot(dry_run=True)
        await bot.poll_once()
        await bot.page.evaluate("addMessage('chat-a','observed','多少钱？')")
        await bot.poll_once()
        bot.dry_run = False
        await bot.poll_once()
        self.assertEqual(await bot.page.evaluate('window.sent'), [])
        await bot.page.evaluate("addMessage('chat-a','new-live','多少钱？')")
        await bot.poll_once()
        self.assertEqual(len(await bot.page.evaluate('window.sent')), 1)
        self.assertEqual(self.receipts, [('observed', 'preview'), ('new-live', 'sent')])

    def test_missing_selectors_require_calibration(self):
        bot = TaobaoBot({'config': {'journal_path': str(self.root / 'bad.db')}})
        self.assertGreaterEqual(len(bot.check_config()), 7)
