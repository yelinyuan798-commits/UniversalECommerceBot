"""Xianyu web-protocol adapter: preview first, no unverified automatic fulfillment."""
import asyncio
import base64
import hashlib
import json
import os
import ssl
import time
from urllib.parse import parse_qs, urlsplit

import certifi
import websockets

from core.base_platform import BasePlatform
from platforms.journal import DeliveryJournal
from platforms.xianyu.api import AuthenticationRequired, XianyuApis
from utils.helpers import decrypt, generate_device_id, generate_mid, generate_uuid, trans_cookies


class XianyuBot(BasePlatform):
    platform_name = 'xianyu'

    def __init__(self, config):
        super().__init__(config)
        self.api = XianyuApis(self.options.get('env_path'), self.options.get('cookies_env', 'COOKIES_STR'))
        self.cookies_str = self.myid = self.device_id = self.current_token = None
        self.ws = None
        self._message_handler = None
        self._stop = asyncio.Event()
        self._pending = {}
        self._queue = asyncio.Queue(maxsize=1000)
        self.started_at = time.time()
        self.journal = DeliveryJournal(self.options.get('journal_path', f'data/xianyu-{self.shop_id}-delivery.db'))
        self.last_preview = None

    def check_config(self):
        errors = []
        cookies = trans_cookies(os.getenv(self.options.get('cookies_env', 'COOKIES_STR'), ''))
        if not cookies.get('unb') or not cookies.get('_m_h5_tk'):
            errors.append('闲鱼登录信息未配置或不完整（请仅在本机 config/.env 配置）')
        if not isinstance(self.dry_run, bool):
            errors.append('dry_run 必须为 true 或 false')
        return errors

    def load_platform_config(self):
        errors = self.check_config()
        if errors:
            self.status, self.last_error = 'waiting_login', '; '.join(errors)
            return False
        self.cookies_str = os.getenv(self.options.get('cookies_env', 'COOKIES_STR'), '')
        cookies = trans_cookies(self.cookies_str)
        self.api.session.cookies.update(cookies)
        self.myid = cookies['unb']
        self.device_id = generate_device_id(self.myid)
        return True

    async def connect(self):
        self.status = 'connecting'
        token_result = await asyncio.to_thread(self.api.get_token, self.device_id)
        self.current_token = token_result.get('data', {}).get('accessToken')
        if not self.current_token:
            raise AuthenticationRequired('闲鱼未返回有效登录凭据，请在本机重新登录')
        self.cookies_str = self.api.cookie_string()
        return True

    async def disconnect(self):
        self._running = False
        self._stop.set()
        if self.ws:
            await self.ws.close()
        self.ws = None
        self._cancel_pending()
        self.status = 'stopped'

    def _cancel_pending(self):
        for future in self._pending.values():
            if not future.done():
                future.set_result(None)
        self._pending.clear()

    async def send_message(self, user_id, message, conversation_id=None, **kwargs):
        # Conversation ID and receiver ID are different protocol fields.
        if self.dry_run or not self.ws or not self.myid or not conversation_id or user_id == self.myid:
            return False
        mid = generate_mid()
        future = asyncio.get_running_loop().create_future()
        self._pending[mid] = future
        content = base64.b64encode(json.dumps({'contentType': 1, 'text': {'text': message}}, ensure_ascii=False).encode()).decode()
        payload = {'lwp': '/r/MessageSend/sendByReceiverScope', 'headers': {'mid': mid}, 'body': [{
            'uuid': generate_uuid(), 'cid': f'{conversation_id}@goofish', 'conversationType': 1,
            'content': {'contentType': 101, 'custom': {'type': 1, 'data': content}},
            'redPointPolicy': 0, 'extension': {'extJson': '{}'},
            'ctx': {'appVersion': '1.0', 'platform': 'web'}, 'mtags': {}, 'msgReadStatusSetting': 1,
        }, {'actualReceivers': [f'{user_id}@goofish', f'{self.myid}@goofish']}]}
        try:
            await self.ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, float(self.options.get('ack_timeout', 10)))
        except asyncio.TimeoutError:
            return None  # submitted but no platform acknowledgment; never retry blindly
        finally:
            self._pending.pop(mid, None)

    async def get_item_info(self, item_id):
        result = await asyncio.to_thread(self.api.get_item_info, item_id)
        return result.get('data', {}).get('itemDO', {})

    def is_chat_message(self, message):
        return isinstance(message, dict) and isinstance(message.get('1'), dict) and isinstance(message['1'].get('10'), dict) and isinstance(message['1']['10'].get('reminderContent'), str)

    def extract_info(self, message):
        try:
            body, meta = message['1'], message['1']['10']
            timestamp = float(body['5'])
            if timestamp > 10**12:
                timestamp /= 1000
            user = str(meta['senderUserId']).split('@')[0]
            conversation = str(body['2']).split('@')[0]
            item = parse_qs(urlsplit(meta.get('reminderUrl', '')).query).get('itemId', [''])[0]
            return timestamp, user, meta['reminderContent'], item, conversation
        except (KeyError, TypeError, ValueError):
            return None, None, None, None, None

    def _normalize(self, message):
        if not self.is_chat_message(message):
            return None
        timestamp, user, text, item, conversation = self.extract_info(message)
        if not user or not conversation or not text or user == self.myid:
            return None
        if timestamp is None or timestamp > time.time() + 300 or (not self.options.get('process_existing', False) and timestamp < self.started_at):
            return None
        body, meta = message['1'], message['1']['10']
        # IDs are native where supplied; a deterministic timestamp-scoped digest
        # covers older packet formats without turning repeated identical questions
        # at different times into the same message.
        message_id = body.get('1') or meta.get('messageId') or message.get('messageId')
        if not isinstance(message_id, (str, int)) or not str(message_id):
            message_id = hashlib.sha256(json.dumps([timestamp, user, conversation, item, text], ensure_ascii=False).encode()).hexdigest()
        return dict(platform='xianyu', shop_id=self.shop_id, user_id=user,
                    conversation_id=conversation, item_id=item or '', message_id=str(message_id),
                    text=text, direction='inbound', kind='text')

    async def _process_sync(self, data, ws=None):
        package = data.get('body', {}).get('syncPushPackage', {})
        entries = package.get('data', []) if isinstance(package, dict) else []
        if not isinstance(entries, list):
            return
        for entry in entries:
            try:
                raw = entry.get('data') if isinstance(entry, dict) else None
                if not isinstance(raw, str):
                    continue
                padded = raw + '=' * (-len(raw) % 4)
                try:
                    message = json.loads(base64.b64decode(padded).decode('utf-8'))
                except (ValueError, UnicodeError):
                    message = json.loads(decrypt(raw))
                event = self._normalize(message)
                if event:
                    await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = '收到无法处理的消息，已跳过；原始内容未写入日志'

    async def _handle_event(self, event):
        key, fresh = self.journal.reserve(event)
        if not fresh:
            return
        result = None
        try:
            if not self._message_handler:
                self.journal.set(key, 'ignored')
                return
            result = await self._message_handler(event)
            if not result or result.get('duplicate') or not result.get('reply'):
                self.journal.set(key, 'ignored')
                return
            if self.dry_run:
                self.last_preview = dict(event=event, reply=result['reply'])
                status = 'preview'
            else:
                self.journal.set(key, 'sending')
                confirmed = await self.send_message(event['user_id'], result['reply'], conversation_id=event['conversation_id'])
                status = 'sent' if confirmed is True else 'failed' if confirmed is False else 'unknown'
            self.journal.set(key, status)
            await self.report_delivery(result, status)
        except asyncio.CancelledError:
            self.journal.set(key, 'unknown')
            if result:
                await self.report_delivery(result, 'unknown', '发送过程被中断，需要人工核实')
            raise
        except Exception:
            self.journal.set(key, 'unknown')
            if result:
                await self.report_delivery(result, 'unknown', '连接中断或回复失败，需要人工核实')
            self.last_error = '回复处理失败，需要人工核实'

    async def _receive(self, ws):
        async for raw in ws:
            try:
                packet = json.loads(raw)
                if not isinstance(packet, dict):
                    continue
                mid = packet.get('headers', {}).get('mid')
                if mid in self._pending and 'code' in packet:
                    future = self._pending[mid]
                    if not future.done():
                        future.set_result(packet['code'] == 200)
                if isinstance(packet.get('body'), dict) and 'syncPushPackage' in packet['body']:
                    # Acknowledge the transport envelope independently of reply generation.
                    headers = packet.get('headers', {})
                    if headers.get('mid'):
                        ack_headers = {key: headers[key] for key in ('mid', 'sid') if key in headers}
                        await ws.send(json.dumps({'code': 200, 'headers': ack_headers}))
                    await self._queue.put(packet)
            except (ValueError, TypeError, AttributeError):
                continue

    async def _consume(self, ws):
        while self._running:
            packet = await self._queue.get()
            try:
                await self._process_sync(packet, ws)
            finally:
                self._queue.task_done()

    async def _heartbeat(self, ws):
        interval = max(5, float(self.options.get('heartbeat_interval', 15)))
        while self._running:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({'lwp': '/!', 'headers': {'mid': generate_mid()}}))

    async def _register(self, ws):
        mid = generate_mid()
        future = asyncio.get_running_loop().create_future()
        self._pending[mid] = future
        try:
            await ws.send(json.dumps({'lwp': '/reg', 'headers': {
                'cache-header': 'app-key token ua wv', 'app-key': '444e9908a51d1cb236a27862abc769c9',
                'token': self.current_token, 'ua': 'DingWeb/2.1.5', 'dt': 'j', 'wv': 'im:3,au:3,sy:6',
                'did': self.device_id, 'mid': mid}}))
            if not await asyncio.wait_for(future, float(self.options.get('ack_timeout', 10))):
                raise AuthenticationRequired('闲鱼消息服务拒绝登录，请在本机重新登录')
            await ws.send(json.dumps({'lwp': '/r/SyncStatus/ackDiff', 'headers': {'mid': generate_mid()},
                'body': [{'pipeline': 'sync', 'tooLong2Tag': 'PNM,1', 'channel': 'sync', 'topic': 'sync',
                          'highPts': 0, 'pts': int(time.time() * 1000) * 1000, 'seq': 0,
                          'timestamp': int(time.time() * 1000)}]}))
        finally:
            self._pending.pop(mid, None)

    async def start(self, message_handler):
        self._message_handler = message_handler
        if not self.load_platform_config():
            return
        self._running = True
        self._stop.clear()
        self.started_at = time.time()
        backoff = 2
        try:
            while self._running:
                tasks = []
                try:
                    await self.connect()
                    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
                    async with websockets.connect('wss://wss-goofish.dingtalk.com/',
                        extra_headers={'Cookie': self.cookies_str, 'User-Agent': self.api.session.headers['user-agent']},
                        ssl=ssl_ctx, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                        self.ws = ws
                        receiver = asyncio.create_task(self._receive(ws))
                        tasks.append(receiver)
                        await self._register(ws)
                        tasks += [asyncio.create_task(self._consume(ws)), asyncio.create_task(self._heartbeat(ws))]
                        self.status = 'preview' if self.dry_run else 'listening'
                        self.last_error = ''
                        backoff = 2
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                except AuthenticationRequired as exc:
                    self.status, self.last_error = 'waiting_login', str(exc)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.status, self.last_error = 'reconnecting', '闲鱼连接暂时中断，正在重试'
                finally:
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    self.ws = None
                    self._cancel_pending()
                if self._running:
                    try:
                        await asyncio.wait_for(self._stop.wait(), backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 60)
        finally:
            previous_status, previous_error = self.status, self.last_error
            await self.disconnect()
            if previous_status == 'waiting_login':
                self.status, self.last_error = previous_status, previous_error
