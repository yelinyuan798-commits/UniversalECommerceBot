import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from platforms.xianyu.api import AuthenticationRequired, XianyuApis
from platforms.xianyu.bot import XianyuBot
from utils.helpers import trans_cookies


def message(mid='m1', user='buyer', text='多少钱？', timestamp=None):
    return {'1': {'1': mid, '2': 'conversation-42@goofish', '5': int((timestamp or time.time()) * 1000),
                  '10': {'senderUserId': user, 'reminderContent': text,
                         'reminderUrl': 'https://www.goofish.com/item?itemId=product-1'}}}


def packet(*messages):
    return {'body': {'syncPushPackage': {'data': [
        {'data': base64.b64encode(json.dumps(m).encode()).decode()} for m in messages]}}}


class XianyuAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = {'dry_run': True, 'config': {'journal_path': str(Path(self.temp.name) / 'delivery.db'), 'process_existing': True}}
        self.bot = XianyuBot(self.cfg)
        self.bot.myid = 'seller'
        self.events, self.receipts = [], []
        async def handler(event):
            self.events.append(event)
            return {'reply': '您好，价格 99 元', 'event_key': event['message_id']}
        async def receipt(key, status, error=''):
            self.receipts.append((key, status))
        self.bot._message_handler = handler
        self.bot.on_delivery = receipt

    def tearDown(self):
        self.temp.cleanup()

    async def test_base64_json_all_batch_entries_and_persistent_dedupe(self):
        batch = packet(message('m1'), message('m2'))
        await self.bot._process_sync(batch)
        await self.bot._process_sync(batch)
        self.assertEqual([e['message_id'] for e in self.events], ['m1', 'm2'])
        self.assertEqual(self.receipts, [('m1', 'preview'), ('m2', 'preview')])
        restarted = XianyuBot(self.cfg)
        restarted.myid = 'seller'
        restarted._message_handler = self.bot._message_handler
        await restarted._process_sync(batch)
        self.assertEqual(len(self.events), 2)

    async def test_own_messages_and_historical_backlog_are_ignored(self):
        self.bot.options['process_existing'] = False
        await self.bot._process_sync(packet(message('own', user='seller'), message('old', timestamp=time.time() - 100)))
        self.assertEqual(self.events, [])

    async def test_buyer_payment_claim_never_triggers_delivery(self):
        await self.bot._process_sync(packet(message(text='我已付款，等待卖家发货')))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.bot.last_preview['reply'], '您好，价格 99 元')
        with self.assertRaises(NotImplementedError):
            await self.bot.send_delivery('buyer', 'product-1', {'link': 'example'})

    async def test_order_notice_is_not_a_chat_or_fulfillment_trigger(self):
        await self.bot._process_sync(packet({'1': 'conversation@goofish', '3': {'redReminder': '等待卖家发货'}}))
        self.assertEqual(self.events, [])

    async def test_conversation_and_receiver_are_distinct_and_ack_required(self):
        bot = self.bot
        bot.dry_run = False
        payloads = []
        class Socket:
            async def send(self, raw):
                data = json.loads(raw)
                payloads.append(data)
                bot._pending[data['headers']['mid']].set_result(True)
        bot.ws = Socket()
        self.assertTrue(await bot.send_message('buyer', '回复', conversation_id='conversation-42'))
        self.assertEqual(payloads[0]['body'][0]['cid'], 'conversation-42@goofish')
        self.assertEqual(payloads[0]['body'][1]['actualReceivers'], ['buyer@goofish', 'seller@goofish'])
        self.assertFalse(await bot.send_message('buyer', '缺少会话不能发送'))

    async def test_no_ack_is_unknown_not_confirmed(self):
        self.bot.dry_run = False
        self.bot.options['ack_timeout'] = 0.01
        class Socket:
            async def send(self, raw):
                pass
        self.bot.ws = Socket()
        await self.bot._process_sync(packet(message('unconfirmed')))
        self.assertEqual(self.receipts, [('unconfirmed', 'unknown')])
        await self.bot._process_sync(packet(message('unconfirmed')))
        self.assertEqual(len(self.events), 1)

    async def test_corrupt_batch_entry_does_not_drop_valid_following_entry(self):
        data = packet(message())
        data['body']['syncPushPackage']['data'].insert(0, {'data': 'not-a-message'})
        await self.bot._process_sync(data)
        self.assertEqual(len(self.events), 1)


class XianyuAPITests(unittest.TestCase):
    def test_cookie_parser_handles_no_spaces_and_equals_in_values(self):
        self.assertEqual(trans_cookies('a=one; b=two==;c=three'), {'a': 'one', 'b': 'two==', 'c': 'three'})

    def test_expired_login_raises_local_error_without_exit_or_secrets(self):
        api = XianyuApis()
        api.session.cookies.set('_m_h5_tk', 'secret_100')
        response = Mock()
        response.json.return_value = {'ret': ['FAIL_SYS_SESSION_EXPIRED::secret-value']}
        with patch.object(api.session, 'post', return_value=response) as post:
            with self.assertRaises(AuthenticationRequired) as caught:
                api.get_token('device')
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(post.call_args.kwargs['timeout'], (5, 20))

    def test_rotated_h5_cookie_resigns_once_without_relogin(self):
        with tempfile.TemporaryDirectory() as temp:
            api = XianyuApis(Path(temp) / '.env')
            api.session.cookies.set('_m_h5_tk', 'old_100')
            first, second = Mock(), Mock()
            first.json.return_value = {'ret': ['FAIL_SYS_TOKEN_EXOIRED::expired']}
            second.json.return_value = {'ret': ['SUCCESS::调用成功'], 'data': {'accessToken': 'test-token'}}
            calls = []
            def post(*args, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    api.session.cookies.set('_m_h5_tk', 'rotated_200')
                    return first
                return second
            with patch.object(api.session, 'post', side_effect=post):
                result = api.get_token('device')
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0]['params']['sign'], calls[1]['params']['sign'])
            self.assertEqual(result['data']['accessToken'], 'test-token')

    def test_expired_h5_cookie_without_rotation_stops_instead_of_retry_loop(self):
        api = XianyuApis()
        api.session.cookies.set('_m_h5_tk', 'same_100')
        response = Mock()
        response.json.return_value = {'ret': ['FAIL_SYS_TOKEN_EXPIRED::expired']}
        with patch.object(api.session, 'post', return_value=response) as post:
            with self.assertRaises(AuthenticationRequired):
                api.get_token('device')
            self.assertEqual(post.call_count, 1)

    def test_cookie_refresh_targets_config_env_and_protects_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.env'
            path.write_text('OTHER=value\nCOOKIES_STR=old\n')
            api = XianyuApis(path)
            api.session.cookies.set('unb', 'account')
            api.update_env_cookies()
            self.assertIn('OTHER=value', path.read_text())
            self.assertIn('unb=account', path.read_text())
