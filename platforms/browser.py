"""Configurable seller-web inbox adapter. Standard CSS selectors must be calibrated per account.

No claim is made that a site's private DOM or native mobile app is supported by
these selectors: real-account calibration and acceptance remain necessary.
"""
import asyncio
from datetime import datetime
import math
import time
from pathlib import Path
from urllib.parse import urlsplit

from core.base_platform import BasePlatform
from platforms.journal import DeliveryJournal


class BrowserPlatform(BasePlatform):
    platform_name = 'browser'

    def __init__(self, config):
        super().__init__(config)
        self.selectors = self.options.get('selectors', {})
        self.attributes = dict(conversation_id='data-conversation-id', user_id='data-user-id',
                               item_id='data-item-id', message_id='data-message-id', direction='data-direction', delivery_status='data-status', message_timestamp='data-timestamp')
        self.attributes.update(self.options.get('attributes', {}))
        self.journal = DeliveryJournal(self.options.get('journal_path', f'data/{self.platform_name}-{self.shop_id}-delivery.db'))
        self.page = self.context = self.playwright = None
        self._message_handler = None
        self._initialized = set()
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.last_preview = None
        self._calibration_error = ''
        self.started_at = time.time()

    def check_config(self):
        errors = []
        url = self.options.get('url', '')
        if urlsplit(url).scheme not in ('https', 'http', 'file'):
            errors.append('config.url 需填写商家消息页面的完整地址')
        for key in ('ready', 'conversation', 'messages', 'message_text', 'composer', 'send'):
            if not isinstance(self.selectors.get(key), str) or not self.selectors[key].strip():
                errors.append(f'config.selectors.{key} 尚未配置（需要在实际商家页面校准）')
        for key in ('conversation_id', 'user_id', 'item_id', 'message_id', 'direction', 'delivery_status', 'message_timestamp'):
            if not isinstance(self.attributes.get(key), str) or not self.attributes[key].strip():
                errors.append(f'config.attributes.{key} 不能为空')
        if not isinstance(self.dry_run, bool):
            errors.append('dry_run 必须为 true 或 false')
        try:
            if float(self.options.get('poll_interval', 2)) < 0.1:
                errors.append('poll_interval 至少为 0.1 秒')
        except (ValueError, TypeError):
            errors.append('poll_interval 必须为数字')
        return errors

    async def connect(self):
        self._calibration_error = ''
        self.started_at = time.time()
        errors = self.check_config()
        if errors:
            raise ValueError('; '.join(errors))
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        profile = Path(self.options.get('profile_dir', f'data/browser/{self.platform_name}-{self.shop_id}')).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), headless=self.options.get('headless', False))
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.page.set_default_timeout(3000)
            await self.page.goto(self.options['url'], wait_until='domcontentloaded')
            self.status = 'waiting_login'
            return True
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self):
        self._running = False
        self._stop.set()
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.context = self.playwright = self.page = None
        self.status = 'stopped'

    async def start(self, message_handler):
        self._message_handler = message_handler
        self._stop.clear()
        self._running = True
        try:
            await self.connect()
            while self._running:
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.status = 'needs_attention'
                    self.last_error = '商家页面读取失败，请检查登录状态和页面选择器'
                try:
                    await asyncio.wait_for(self._stop.wait(), float(self.options.get('poll_interval', 2)))
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.disconnect()

    async def _ready(self):
        if not self.page or self.page.is_closed():
            return False
        expected = urlsplit(self.options['url'])
        actual = urlsplit(self.page.url)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            return False
        ready = self.page.locator(self.selectors['ready'])
        return await ready.count() == 1 and await ready.is_visible()

    async def _identity(self):
        panels = self.page.locator(self.selectors['conversation'])
        if await panels.count() != 1 or not await panels.is_visible():
            return None
        values = {key: await panels.get_attribute(self.attributes[key]) for key in ('conversation_id', 'user_id', 'item_id')}
        if not values['conversation_id'] or not values['user_id']:
            return None
        values['item_id'] = values['item_id'] or ''
        return values

    async def poll_once(self):
        if not await self._ready():
            self.status = 'waiting_login'
            return 0
        if self._calibration_error:
            self.status, self.last_error = 'needs_attention', self._calibration_error
        else:
            self.status = 'preview' if self.dry_run else 'listening'
            self.last_error = ''
        count = 0
        thread_selector = self.selectors.get('conversations')
        if thread_selector:
            ids = await self.page.locator(thread_selector).evaluate_all(
                '(nodes, attr) => nodes.map(n => n.getAttribute(attr)).filter(Boolean)', self.attributes['conversation_id'])
            for cid in dict.fromkeys(ids):
                if not self._running and self._stop.is_set():
                    break
                # Find by exact attribute equality rather than interpolating untrusted IDs into CSS.
                rows = self.page.locator(thread_selector)
                found = None
                for i in range(await rows.count()):
                    if await rows.nth(i).get_attribute(self.attributes['conversation_id']) == cid:
                        if found is not None:
                            found = None
                            break
                        found = rows.nth(i)
                if found is None:
                    continue
                current_panel = self.page.locator(self.selectors['conversation'])
                if await current_panel.count() == 1:
                    draft = current_panel.locator(self.selectors['composer'])
                    if await draft.count() == 1 and (await draft.evaluate("e => ('value' in e ? e.value : e.innerText)")).strip():
                        self.last_error = '当前会话有人工草稿，已暂停会话切换'
                        self.status = 'needs_attention'
                        break
                await found.click()
                try:
                    await self.page.wait_for_function('''({selector, attr, cid}) => {
                        const panels = document.querySelectorAll(selector);
                        return panels.length === 1 && panels[0].getAttribute(attr) === cid;
                    }''', arg=dict(selector=self.selectors['conversation'], attr=self.attributes['conversation_id'], cid=cid), timeout=3000)
                except Exception:
                    continue
                identity = await self._identity()
                if not identity or identity['conversation_id'] != cid:
                    continue
                count += await self._poll_current(identity)
        else:
            identity = await self._identity()
            if identity:
                count += await self._poll_current(identity)
            else:
                self.status = 'needs_attention'
                self.last_error = '无法唯一识别当前会话及收件人'
        return count

    async def _poll_current(self, identity):
        panel = self.page.locator(self.selectors['conversation'])
        rows = panel.locator(self.selectors['messages'])
        initial = identity['conversation_id'] not in self._initialized
        count = 0
        for i in range(await rows.count()):
            if await self._identity() != identity:
                break
            row = rows.nth(i)
            mid = await row.get_attribute(self.attributes['message_id'])
            direction = await row.get_attribute(self.attributes['direction'])
            # Never infer inbound direction from absence of an outbound CSS class.
            if not mid or direction != self.options.get('inbound_value', 'inbound'):
                continue
            text_node = row.locator(self.selectors['message_text'])
            if await text_node.count() != 1:
                continue
            text = (await text_node.inner_text()).strip()
            if not text:
                continue
            event = dict(platform=self.platform_name, shop_id=self.shop_id, message_id=mid,
                         text=text, direction='inbound', kind='text', **identity)
            key, fresh = self.journal.reserve(event)
            if not fresh:
                continue
            raw_timestamp = await row.get_attribute(self.attributes['message_timestamp'])
            timestamp = self._timestamp(raw_timestamp)
            if not self.options.get('process_existing', False):
                if timestamp is not None and timestamp < self.started_at:
                    self.journal.set(key, 'history')
                    continue
                if timestamp is None:
                    if not self.dry_run:
                        self.status = 'needs_attention'
                        self._calibration_error = '消息缺少可靠时间，请校准 message_timestamp 后重新启动'
                        self.last_error = self._calibration_error
                        self.journal.set(key, 'invalid_timestamp')
                        continue
                    if initial:
                        self.journal.set(key, 'history')
                        continue
                elif timestamp > time.time() + 300:
                    self.journal.set(key, 'invalid_timestamp')
                    self.status = 'needs_attention'
                    self._calibration_error = '消息时间异常，已跳过'
                    self.last_error = self._calibration_error
                    continue
                if timestamp is not None and self._calibration_error:
                    # Only a fresh, valid inbound message clears the calibration
                    # failure. Re-reading deduplicated history must not hide it.
                    self._calibration_error = ''
                    self.status = 'preview' if self.dry_run else 'listening'
                    self.last_error = ''
            if self._message_handler is None:
                self.journal.set(key, 'ignored')
                continue
            result = None
            try:
                result = await self._message_handler(event)
                if not result or result.get('duplicate') or not result.get('reply'):
                    self.journal.set(key, 'ignored')
                    continue
                if self.dry_run:
                    self.last_preview = dict(event=event, reply=result['reply'])
                    status = 'preview'
                else:
                    self.journal.set(key, 'sending')
                    status = await self._send_reply(identity, result['reply'])
                self.journal.set(key, status)
                await self.report_delivery(result, status)
                count += 1
            except asyncio.CancelledError:
                self.journal.set(key, 'unknown')
                if result:
                    await self.report_delivery(result, 'unknown', '发送过程被中断，需要人工核实')
                raise
            except Exception:
                self.journal.set(key, 'failed')
                if result:
                    await self.report_delivery(result, 'failed', '回复处理失败，需要人工核实')
                self.last_error = '回复处理失败，需要人工核实'
        self._initialized.add(identity['conversation_id'])
        return count

    @staticmethod
    def _timestamp(value):
        if not value or not isinstance(value, str):
            return None
        try:
            numeric = float(value)
            if numeric > 10**12:
                numeric /= 1000
            return numeric if math.isfinite(numeric) and numeric > 0 else None
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return parsed.timestamp() if parsed.tzinfo is not None else None
            except (ValueError, OverflowError):
                return None

    async def _outgoing_count(self, identity, message):
        if await self._identity() != identity:
            return -1
        rows = self.page.locator(self.selectors['conversation']).locator(self.selectors['messages'])
        count = 0
        for i in range(await rows.count()):
            row = rows.nth(i)
            if await row.get_attribute(self.attributes['direction']) == self.options.get('outbound_value', 'outbound'):
                texts = row.locator(self.selectors['message_text'])
                status = await row.get_attribute(self.attributes['delivery_status'])
                if status in self.options.get('sent_values', ['sent', 'delivered']) and await texts.count() == 1 and (await texts.inner_text()).strip() == message.strip():
                    count += 1
        return count

    async def _send_reply(self, identity, message):
        async with self._send_lock:
            if not await self._ready() or await self._identity() != identity:
                return 'failed'
            panel = self.page.locator(self.selectors['conversation'])
            composer, send = panel.locator(self.selectors['composer']), panel.locator(self.selectors['send'])
            if await composer.count() != 1 or await send.count() != 1:
                return 'failed'
            current = await composer.evaluate("e => ('value' in e ? e.value : e.innerText)")
            if current.strip():
                self.last_error = '输入框已有人工草稿，已停止自动发送'
                return 'failed'
            before = await self._outgoing_count(identity, message)
            await composer.fill(message)
            if await self._identity() != identity or not await self._ready():
                return 'failed'
            # Last guard happens in the same browser task as the click, preventing
            # a page navigation between a separate identity read and send action.
            clicked = await self.page.evaluate('''({selectors, attrs, identity, message}) => {
                const panels = document.querySelectorAll(selectors.conversation);
                if (panels.length !== 1) return false;
                const panel = panels[0];
                for (const key of ['conversation_id','user_id','item_id']) {
                    if ((panel.getAttribute(attrs[key]) || '') !== identity[key]) return false;
                }
                const inputs = panel.querySelectorAll(selectors.composer), buttons = panel.querySelectorAll(selectors.send);
                if (inputs.length !== 1 || buttons.length !== 1 || buttons[0].disabled) return false;
                const text = 'value' in inputs[0] ? inputs[0].value : inputs[0].innerText;
                if (text !== message) return false;
                buttons[0].click(); return true;
            }''', dict(selectors=self.selectors, attrs=self.attributes, identity=identity, message=message))
            if not clicked:
                return 'failed'
            for _ in range(20):
                await asyncio.sleep(0.1)
                if await self._outgoing_count(identity, message) > before:
                    return 'sent'
            # The click happened but the merchant UI did not confirm it. Never auto-resend.
            return 'unknown'

    async def send_message(self, user_id, message, conversation_id=None, item_id=''):
        if self.dry_run:
            return False
        identity = dict(user_id=user_id, conversation_id=conversation_id, item_id=item_id)
        return await self._send_reply(identity, message) == 'sent'
