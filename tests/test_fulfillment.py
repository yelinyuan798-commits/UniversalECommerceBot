import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from core.fulfillment import FulfillmentEngine, canonical_order, order_reference
from core.fulfillment_store import FulfillmentError, FulfillmentStore


def reference(order_id='order-1', *, platform='xianyu', shop_id='shop-1'):
    return dict(source_id='trusted-source', platform=platform, shop_id=shop_id, order_id=order_id)


def order(ref=None, *, quantity=1, items=None, status='paid', buyer_id='buyer-from-order'):
    ref = ref or reference()
    items = items or [dict(line_id='line-1', item_id='product-1', sku_id='', quantity=quantity, amount=str(10 * quantity))]
    return {**{key: ref[key] for key in ('platform', 'shop_id', 'order_id')}, 'buyer_id': buyer_id,
            'status': status, 'amount': str(sum(float(item['amount']) for item in items)), 'currency': 'CNY',
            'items': deepcopy(items), 'delivery_target': {'buyer_open_id': buyer_id}}


class Adapter:
    def __init__(self, orders=None):
        self.orders = orders or {('xianyu', 'shop-1', 'order-1'): order()}
        self.verified = []
        self.delivered = []
        self.verify_hook = None
        self.delivery_hook = None

    async def verify_order(self, ref):
        self.verified.append(deepcopy(ref))
        if self.verify_hook:
            outcome = self.verify_hook(ref, len(self.verified))
            if outcome is not None:
                return outcome
        return deepcopy(self.orders[tuple(ref[key] for key in ('platform', 'shop_id', 'order_id'))])

    async def deliver(self, job, payload, idempotency_key):
        self.delivered.append((deepcopy(job), deepcopy(payload), idempotency_key))
        if self.delivery_hook:
            return await self.delivery_hook(job, payload)
        return {'status': 'sent', 'receipt': 'private-receipt-must-not-be-public'}


class FulfillmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'private' / 'fulfillment.db'
        self.store = FulfillmentStore(self.path)
        self.adapter = Adapter()
        self.engine = FulfillmentEngine(self.store, self.adapter)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def rule(self, **kwargs):
        data = dict(platform='xianyu', shop_id='shop-1', item_id='product-1', sku_id='', type='card', enabled=True, min_unit_amount='10', currency='CNY')
        data.update(kwargs)
        return self.store.save_rule(data)

    def cards(self, values=('private-card-A', 'private-card-B'), **kwargs):
        rule = self.rule(**kwargs)
        self.store.import_cards(rule['id'], list(values))
        return rule

    def job(self, result):
        return result['jobs'][0]

    async def test_default_dry_run_does_not_write_allocate_or_deliver(self):
        self.cards()
        result = await self.engine.process(reference())
        self.assertTrue(result['dry_run'])
        self.assertEqual(self.job(result)['status'], 'ready')
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 2)
        self.assertEqual(self.adapter.delivered, [])
        self.assertNotIn('private-card', repr(result))

    async def test_duplicate_notifications_and_two_live_stores_send_only_once(self):
        self.cards()
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold(job, payload):
            entered.set()
            await release.wait()
            return {'status': 'sent'}
        self.adapter.delivery_hook = hold
        first = asyncio.create_task(self.engine.process(reference(), dry_run=False))
        await entered.wait()
        second_store = FulfillmentStore(self.path)
        try:
            second = await FulfillmentEngine(second_store, self.adapter).process(reference(), dry_run=False)
            self.assertEqual(self.job(second)['status'], 'sending')
            release.set()
            result = await first
            self.assertEqual(self.job(result)['status'], 'sent')
            repeat = await self.engine.process(reference(), dry_run=False)
            self.assertEqual(self.job(repeat)['attempts'], 1)
            preview = await self.engine.process(reference())
            self.assertEqual(self.job(preview)['status'], 'sent')
            self.assertTrue(self.job(preview)['existing'])
            self.assertEqual(len(self.adapter.delivered), 1)
            self.assertEqual(self.store.list_rules()[0]['inventory_available'], 1)
        finally:
            release.set()
            second_store.close()

    async def test_inventory_shortage_is_atomic_and_only_explicit_retry_allocates(self):
        rule = self.cards(['only-card'])
        self.adapter.orders[('xianyu', 'shop-1', 'order-1')] = order(quantity=2)
        result = await self.engine.process(reference(), dry_run=False)
        job = self.job(result)
        self.assertEqual((job['status'], job['error_code'], job['assigned_count']), ('blocked', 'insufficient_inventory', 0))
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 1)
        self.store.import_cards(rule['id'], ['second-card'])
        repeat = await self.engine.process(reference(), dry_run=False)
        self.assertEqual(self.job(repeat)['assigned_count'], 0)
        preview = await self.engine.retry(job['id'])
        self.assertTrue(preview['dry_run'])
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 2)
        done = await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual((done['job']['status'], done['job']['assigned_count']), ('sent', 2))
        self.assertEqual(self.adapter.delivered[0][1]['cards'], ['only-card', 'second-card'])
        self.assertTrue(all(ref['source_id'] == 'trusted-source' for ref in self.adapter.verified))

    async def test_two_orders_cannot_overdraw_stock_with_concurrent_reservations(self):
        self.cards(['one', 'two', 'three'])
        one, two = order(quantity=2), order(reference('order-2'), quantity=2)
        self.adapter.orders = {('xianyu', 'shop-1', value['order_id']): value for value in (one, two)}
        result = await asyncio.gather(self.engine.process(reference(), dry_run=False), self.engine.process(reference('order-2'), dry_run=False))
        self.assertEqual(sorted(self.job(item)['status'] for item in result), ['blocked', 'sent'])
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 1)
        self.assertEqual(len(self.adapter.delivered), 1)

    async def test_minimum_paid_amount_and_currency_are_checked(self):
        self.cards()
        for amount, currency, expected in [('11', 'CNY', 'sent'), ('9.99', 'CNY', 'blocked'), ('10', 'USD', 'blocked')]:
            with self.subTest(amount=amount, currency=currency):
                ref = reference('order-' + amount + currency)
                current = order(ref)
                current.update(amount=amount, currency=currency)
                current['items'][0]['amount'] = amount
                self.adapter.orders[('xianyu', 'shop-1', ref['order_id'])] = current
                result = await self.engine.process(ref, dry_run=False)
                self.assertEqual(self.job(result)['status'], expected)
                if expected == 'blocked':
                    self.assertEqual(self.job(result)['error_code'], 'price_mismatch')
                    self.assertEqual(self.job(result)['assigned_count'], 0)

    async def test_multiple_products_partial_delivery_and_private_link_snapshot(self):
        self.cards(['card-for-first-line'])
        link = self.rule(item_id='product-2', type='link', delivery_content='https://files.example.test/private-token')
        items = [dict(line_id='line-1', item_id='product-1', quantity=1, amount='10'), dict(line_id='line-2', item_id='product-2', quantity=1, amount='10')]
        self.adapter.orders[('xianyu', 'shop-1', 'order-1')] = order(items=items)
        async def deliver(job, payload):
            if payload['type'] == 'card':
                self.store.save_rule({key: value for key, value in {**link, 'delivery_content': 'https://files.example.test/edited-token'}.items() if key not in {'has_delivery_content', 'inventory_available', 'inventory_total'}})
                return {'status': 'unknown', 'delivered_count': 1}
            return {'status': 'sent'}
        self.adapter.delivery_hook = deliver
        result = await self.engine.process(reference(), dry_run=False)
        self.assertEqual([item['status'] for item in result['jobs']], ['unknown', 'sent'])
        self.assertEqual(self.adapter.delivered[1][1]['content'], 'https://files.example.test/private-token')
        self.assertNotIn('token', repr(result))
        self.assertNotIn('token', repr(self.store.list_rules()))
        self.assertNotIn('card-for-first-line', repr(self.store.list_jobs()))
        await self.engine.process(reference(), dry_run=False)
        self.assertEqual(len(self.adapter.delivered), 2)

    async def test_unknown_restart_cannot_retry_or_allocate_new_cards(self):
        self.cards()
        async def uncertain(job, payload):
            raise OSError('private-card-A https://key-in-exception.invalid')
        self.adapter.delivery_hook = uncertain
        first = self.job(await self.engine.process(reference(), dry_run=False))
        self.assertEqual(first['status'], 'unknown')
        self.assertNotIn('private-card', repr(first))
        original_payload = self.adapter.delivered[0][1]
        original_key = self.adapter.delivered[0][2]
        self.store.close()
        self.store = FulfillmentStore(self.path)
        self.engine = FulfillmentEngine(self.store, self.adapter)
        await self.engine.process(reference(), dry_run=False)
        self.assertEqual(len(self.adapter.delivered), 1)
        self.adapter.delivery_hook = None
        retried = await self.engine.retry(first['id'], dry_run=False)
        self.assertEqual(retried['status'], 'needs_review')
        self.assertEqual(retried['job']['status'], 'unknown')
        self.assertFalse(retried['can_retry'])
        self.assertFalse((await self.engine.retry(first['id']))['can_retry'])
        self.assertFalse(self.store.prepare_retry(first['id']))
        self.assertEqual(len(self.adapter.delivered), 1)
        self.assertEqual(self.adapter.delivered[0][1], original_payload)
        self.assertEqual(self.adapter.delivered[0][2], original_key)
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 1)

    async def test_confirmed_failure_retry_reuses_same_cards_and_idempotency(self):
        self.cards()
        async def rejected(job, payload):
            return {'status': 'failed', 'receipt': 'provider-confirmed-not-sent'}
        self.adapter.delivery_hook = rejected
        job = self.job(await self.engine.process(reference(), dry_run=False))
        self.assertEqual(job['status'], 'blocked')
        self.assertEqual(job['error_code'], 'delivery_failed')
        self.adapter.delivery_hook = None
        result = await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual(result['job']['status'], 'sent')
        self.assertEqual(self.adapter.delivered[1][1:], self.adapter.delivered[0][1:])
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 1)

    async def test_current_rule_pause_and_higher_floor_block_snapshotted_job(self):
        self.cards()
        for changes, expected in [({'enabled': False}, 'rule_disabled'), ({'min_unit_amount': '11'}, 'price_mismatch')]:
            with self.subTest(changes=changes):
                self.rule()
                ref = reference('order-' + expected)
                self.adapter.orders[('xianyu', 'shop-1', ref['order_id'])] = order(ref)
                def pause_before_second_verify(ref, count):
                    self.rule(**changes)
                self.adapter.verify_hook = pause_before_second_verify
                # Reserve directly so the initial rule snapshot predates the edit.
                job = self.store.reserve_order(ref, canonical_order(order(ref), ref))[0]
                await self.engine._send(job['id'])
                self.assertEqual(self.store.get_job(job['id'])['error_code'], expected)
                self.assertEqual(self.adapter.delivered, [])
                self.assertEqual(self.store.get_job(job['id'])['assigned_count'], 1)
                self.adapter.verify_hook = None
                await self.engine.retry(job['id'], dry_run=False)
                self.assertEqual(self.adapter.delivered, [])

    async def test_explicit_retry_binds_previously_missing_rule_without_duplicate_job(self):
        job = self.job(await self.engine.process(reference(), dry_run=False))
        self.assertEqual(job['error_code'], 'rule_missing')
        self.cards(['newly-imported'])
        await self.engine.process(reference(), dry_run=False)
        self.assertEqual(self.adapter.delivered, [])
        result = await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual(result['job']['id'], job['id'])
        self.assertEqual(result['job']['status'], 'sent')
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertEqual(self.adapter.delivered[0][1]['cards'], ['newly-imported'])

    async def test_real_thread_concurrent_reservation_allocates_exactly_once(self):
        self.cards(['one', 'two', 'three'])
        another = FulfillmentStore(self.path)
        barrier = threading.Barrier(2)
        def reserve(store, ref):
            barrier.wait(timeout=3)
            return store.reserve_order(ref, canonical_order(order(ref, quantity=2), ref))[0]
        try:
            one, two = await asyncio.gather(asyncio.to_thread(reserve, self.store, reference()), asyncio.to_thread(reserve, another, reference('order-2')))
            self.assertEqual(sorted(job['status'] for job in (one, two)), ['blocked', 'reserved'])
            self.assertEqual(self.store.list_rules()[0]['inventory_available'], 1)
        finally:
            another.close()

    async def test_process_exit_during_sending_recovers_as_unknown_without_redelivery(self):
        self.cards()
        normalized = canonical_order(order(), reference())
        job = self.store.reserve_order(reference(), normalized)[0]
        self.store.close()
        script = 'import sys; from core.fulfillment_store import FulfillmentStore; s=FulfillmentStore(sys.argv[1]); assert s.claim_sending(sys.argv[2]); s.close()'
        subprocess.run([sys.executable, '-c', script, str(self.path), job['id']], check=True, capture_output=True, timeout=10)
        self.store = FulfillmentStore(self.path)
        self.engine = FulfillmentEngine(self.store, self.adapter)
        self.assertEqual(self.store.get_job(job['id'])['status'], 'unknown')
        await self.engine.process(reference(), dry_run=False)
        self.assertEqual(self.adapter.delivered, [])
        self.assertEqual(self.store.get_job(job['id'])['assigned_count'], 1)

    async def test_refund_before_submission_blocks_and_does_not_return_reserved_secret(self):
        self.cards(['reserved-card'])
        self.adapter.verify_hook = lambda ref, count: order(status='refunded') if count >= 2 else None
        job = self.job(await self.engine.process(reference(), dry_run=False))
        self.assertEqual((job['status'], job['error_code']), ('blocked', 'order_refunded'))
        self.assertEqual(job['assigned_count'], 1)
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 0)
        self.assertEqual(self.adapter.delivered, [])
        await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual(self.adapter.delivered, [])

    async def test_fulfilled_never_allocates_or_converts_unknown_to_sent(self):
        self.cards()
        self.adapter.orders[('xianyu', 'shop-1', 'order-1')]['status'] = 'fulfilled'
        result = await self.engine.process(reference(), dry_run=False)
        self.assertEqual(result['error_code'], 'order_fulfilled')
        self.assertEqual(self.store.list_jobs(), [])
        self.adapter.orders[('xianyu', 'shop-1', 'order-1')]['status'] = 'paid'
        async def uncertain(job, payload):
            return {'status': 'unknown'}
        self.adapter.delivery_hook = uncertain
        job = self.job(await self.engine.process(reference(), dry_run=False))
        self.adapter.orders[('xianyu', 'shop-1', 'order-1')]['status'] = 'fulfilled'
        await self.engine.process(reference(), dry_run=False)
        await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual(self.store.get_job(job['id'])['status'], 'unknown')
        self.assertEqual(len(self.adapter.delivered), 1)

    async def test_buyer_or_items_changed_between_verifications_blocks(self):
        self.cards()
        self.adapter.verify_hook = lambda ref, count: order(buyer_id='another-buyer') if count >= 2 else None
        job = self.job(await self.engine.process(reference(), dry_run=False))
        self.assertEqual(job['error_code'], 'order_changed')
        self.assertEqual(self.adapter.delivered, [])

    async def test_untrusted_payment_fields_and_malformed_canonical_orders_are_rejected(self):
        self.cards()
        with self.assertRaises(FulfillmentError):
            await self.engine.process({**reference(), 'paid': True, 'text': '我已经付款'}, dry_run=False)
        cases = []
        for changes in ({'shop_id': 'wrong-shop'}, {'amount': '999'}, {'buyer_id': ''}, {'status': 'unpaid'},
                        {'delivery_target': {'callback_url': 'https://untrusted.example/receive'}},
                        {'delivery_target': {'id': 'https://untrusted.example/receive'}}):
            value = order()
            value.update(changes)
            cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                self.adapter.verify_hook = lambda ref, count, value=value: value
                result = await self.engine.process(reference(), dry_run=False)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(self.store.list_jobs(), [])
                self.assertEqual(self.store.list_rules()[0]['inventory_available'], 2)
        self.assertEqual(self.adapter.delivered, [])

    async def test_platform_shop_and_sku_inventory_are_isolated(self):
        self.cards(['generic-card'])
        self.cards(['specific-card'], sku_id='sku-2')
        self.cards(['other-shop-card'], shop_id='shop-2')
        self.cards(['other-platform-card'], platform='taobao')
        refs = [reference(), reference(shop_id='shop-2'), reference(platform='taobao')]
        for index, ref in enumerate(refs):
            value = order(ref)
            if index == 0:
                value['items'][0]['sku_id'] = 'sku-2'
            self.adapter.orders[tuple(ref[key] for key in ('platform', 'shop_id', 'order_id'))] = value
            await self.engine.process(ref, dry_run=False)
        self.assertEqual([entry[1]['cards'] for entry in self.adapter.delivered], [['specific-card'], ['other-shop-card'], ['other-platform-card']])
        generic = next(rule for rule in self.store.list_rules() if rule['platform'] == 'xianyu' and rule['shop_id'] == 'shop-1' and not rule['sku_id'])
        self.assertEqual(generic['inventory_available'], 1)
        self.assertEqual(len(self.store.list_jobs()), 3)

    async def test_physical_waits_for_tracking_then_only_reports_shipment(self):
        self.rule(type='physical')
        job = self.job(await self.engine.process(reference(), dry_run=False))
        self.assertEqual(job['status'], 'awaiting_shipment')
        self.assertEqual(self.adapter.delivered, [])
        await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual(self.adapter.delivered, [])
        self.store.record_tracking(job['id'], '测试承运商', 'TRACK-123')
        self.assertEqual(self.adapter.delivered, [])
        result = await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual(result['job']['status'], 'sent')
        self.assertEqual(self.adapter.delivered[0][1], {'type': 'physical', 'carrier': '测试承运商', 'tracking_number': 'TRACK-123'})
        with self.assertRaises(FulfillmentError):
            self.store.record_tracking(job['id'], 'other', 'changed')

    async def test_cancelled_delivery_task_remains_unknown_and_keeps_inventory(self):
        self.cards(['reserved-card'])
        entered = asyncio.Event()
        async def pending(job, payload):
            entered.set()
            await asyncio.Event().wait()
        self.adapter.delivery_hook = pending
        task = asyncio.create_task(self.engine.process(reference(), dry_run=False))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        job = self.store.list_jobs()[0]
        self.assertEqual(job['status'], 'unknown')
        self.adapter.orders[('xianyu', 'shop-1', 'order-1')]['status'] = 'cancelled'
        await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 0)
        self.assertEqual(len(self.adapter.delivered), 1)

    async def test_card_import_is_deduplicated_globally_and_private_file_permissions(self):
        rule = self.rule()
        self.assertEqual(self.store.import_cards(rule['id'], [' secret-a ', 'secret-a', 'secret-b']), {'added': 2, 'duplicates': 1, 'available': 2})
        other = self.rule(item_id='other-product')
        self.assertEqual(self.store.import_cards(other['id'], ['secret-a'])['added'], 0)
        self.assertNotIn('secret-a', repr(self.store.list_rules()))
        self.assertNotIn('delivery_content', self.store.list_rules()[0])
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_adapter_exception_and_timeout_never_publish_private_details(self):
        self.cards()
        async def broken(ref):
            raise FulfillmentError('secret-from-provider', 'private-card-A')
        self.adapter.verify_order = broken
        result = await self.engine.process(reference(), dry_run=False)
        self.assertEqual(result['error_code'], 'verification_failed')
        self.assertNotIn('secret', repr(result))
        self.assertNotIn('private-card-A', repr(result))
        self.adapter.verify_order = Adapter.verify_order.__get__(self.adapter)
        async def delayed(job, payload):
            await asyncio.Event().wait()
        self.adapter.delivery_hook = delayed
        self.engine.timeout_seconds = 0.01
        result = await self.engine.process(reference(), dry_run=False)
        self.assertEqual(self.job(result)['status'], 'unknown')
        self.assertEqual(self.job(result)['error_code'], 'delivery_uncertain')
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 1)

    async def test_wrong_json_types_fail_as_safe_validation_errors(self):
        for value in ([], {}, True, None, 1):
            with self.subTest(value=value):
                with self.assertRaises(FulfillmentError):
                    self.rule(type=value)
                current = order()
                current['status'] = value
                self.adapter.verify_hook = lambda ref, count, current=current: current
                result = await self.engine.process(reference(), dry_run=False)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['error_code'], 'verification_failed')
        with self.assertRaises(FulfillmentError):
            self.store.import_cards({}, ['private-card'])

    async def test_cross_type_edit_blocks_without_replacing_reserved_card(self):
        self.cards(['original-card', 'unused-card'])
        job = self.store.reserve_order(reference(), canonical_order(order(), reference()))[0]
        self.rule(type='link', delivery_content='https://example.test/new-private-link')
        await self.engine._send(job['id'])
        self.assertEqual(self.store.get_job(job['id'])['error_code'], 'rule_changed')
        self.assertEqual(self.adapter.delivered, [])
        self.rule(type='card')
        result = await self.engine.retry(job['id'], dry_run=False)
        self.assertEqual(result['job']['status'], 'sent')
        self.assertEqual(self.adapter.delivered[0][1], {'type': 'card', 'cards': ['original-card']})
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 1)

    async def test_amount_quantity_and_refund_flags_fail_closed(self):
        self.cards()
        cases = []
        for amount in ('NaN', 'Infinity', '-1', '0.001', True, {}, '1000000000000'):
            current = order()
            current['amount'] = amount
            cases.append(current)
        for quantity in (False, 0, -1, '1', 1.1, 10001, []):
            current = order()
            current['items'][0]['quantity'] = quantity
            cases.append(current)
        for flag in ('refunded', 'refund_pending', 'cancelled'):
            for invalid in ('true', 1, None, {}):
                current = order()
                current[flag] = invalid
                cases.append(current)
        for current in cases:
            with self.subTest(current=current):
                self.adapter.verify_hook = lambda ref, count, current=current: current
                result = await self.engine.process(reference(), dry_run=False)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.adapter.delivered, [])
        self.assertEqual(self.store.list_rules()[0]['inventory_available'], 2)

    async def test_job_pagination_keeps_old_pending_jobs_accessible_and_counts_all_rows(self):
        self.rule(type='physical')
        first = self.store.reserve_order(reference(), canonical_order(order(), reference()))[0]
        self.rule(type='link', delivery_content='https://example.test/private-resource')
        statuses = ['reserved', 'sending', 'unknown', 'blocked'] + ['sent'] * 103
        active_claims = []
        for index, status in enumerate(statuses):
            ref = reference('page-order-' + str(index))
            job = self.store.reserve_order(ref, canonical_order(order(ref), ref))[0]
            if status == 'blocked':
                self.store.block(job['id'], 'verification_failed')
            elif status in {'sent', 'unknown', 'sending'}:
                token = self.store.claim_sending(job['id'])
                if status == 'sending':
                    active_claims.append((job['id'], token))
                else:
                    self.store.finish(job['id'], token, status)
        try:
            self.assertEqual(self.store.job_counts(), {'all': 108, 'pending': 3, 'unknown': 1, 'sent': 103, 'blocked': 1})
            self.assertEqual(len(self.store.list_jobs()), 100)
            self.assertNotIn(first['id'], [job['id'] for job in self.store.list_jobs()])
            pending = self.store.list_jobs(status='pending')
            self.assertEqual([job['status'] for job in pending], ['sending', 'reserved', 'awaiting_shipment'])
            self.assertEqual(pending[-1]['id'], first['id'])
            self.assertEqual(self.store.list_jobs(limit=1, offset=2, status='pending')[0]['id'], first['id'])
            pages = self.store.list_jobs(limit=100, status='sent') + self.store.list_jobs(limit=100, offset=100, status='sent')
            self.assertEqual(len(pages), 103)
            self.assertEqual(len({job['id'] for job in pages}), 103)
            self.assertEqual(self.store.list_jobs(offset=9223372036854775807), [])
            self.assertEqual(self.store.list_jobs(status='unknown')[0]['status'], 'unknown')
            self.assertEqual(self.store.list_jobs(status='blocked')[0]['status'], 'blocked')
            self.assertNotIn('private-resource', repr(pending + pages))
            self.assertNotIn('rule_json', repr(pages))
        finally:
            for job_id, token in active_claims:
                self.store.finish(job_id, token, 'unknown')

    async def test_job_pagination_rejects_unsafe_parameters_without_sql_errors(self):
        self.assertEqual(self.store.job_counts(), {'all': 0, 'pending': 0, 'unknown': 0, 'sent': 0, 'blocked': 0})
        invalid = [{'limit': value} for value in (True, False, None, 0, -1, 101, 1.1, '10', {}, [])]
        invalid += [{'offset': value} for value in (True, None, -1, 1.1, '0', {}, [], 9223372036854775808)]
        invalid += [{'status': value} for value in (None, True, {}, [], 'awaiting_shipment', 'ALL', "sent' OR 1=1--")]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(FulfillmentError) as error:
                self.store.list_jobs(**kwargs)
            self.assertEqual(error.exception.code, 'invalid_pagination')

    async def test_multiline_resource_instructions_are_delivered_without_reformatting(self):
        content = 'https://example.test/private-download\r\n提取码：测试私密内容\n使用说明：\t下载后解压。\n'
        rule = self.rule(type='link', delivery_content=content)
        self.assertTrue(rule['has_delivery_content'])
        self.assertNotIn(content, repr(rule))
        self.store.close()
        self.store = FulfillmentStore(self.path)
        self.engine = FulfillmentEngine(self.store, self.adapter)
        result = await self.engine.process(reference(), dry_run=False)
        self.assertEqual(self.job(result)['status'], 'sent')
        self.assertEqual(self.adapter.delivered[0][1], {'type': 'link', 'content': content})
        self.assertNotIn('测试私密内容', repr(result))

    async def test_resource_text_control_exception_does_not_weaken_ids_or_cards(self):
        for value in ('text\x00hidden', 'text\x1bhidden', 'text\u200bhidden', 'x' * 10001, ' \n\r\t '):
            with self.subTest(value=value), self.assertRaises(FulfillmentError):
                self.rule(type='link', delivery_content=value)
        self.rule(type='link', delivery_content='x' * 10000)
        card_rule = self.rule(type='card')
        for char in ('\n', '\r', '\t'):
            with self.subTest(char=repr(char)):
                with self.assertRaises(FulfillmentError):
                    self.store.import_cards(card_rule['id'], ['secret' + char + 'other'])
                with self.assertRaises(FulfillmentError):
                    await self.engine.process(reference('order' + char + 'id'))


if __name__ == '__main__':
    unittest.main()
