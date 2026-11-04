"""Runtime shutdown and cursor recovery with synthetic, in-memory adapters."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from core.fulfillment_runtime import FulfillmentRuntime
from core.fulfillment_store import FulfillmentError, FulfillmentStore


def source_data():
    return {'name': '合成测试订单源', 'platform': 'dujiao', 'shop_id': 'synthetic-shop',
            'base_url': 'https://synthetic.invalid/bridge',
            'secret': 'synthetic-test-only-secret-with-more-than-32-bytes',
            'enabled': True, 'auto_process': True}


class Adapter:
    def __init__(self, source, scenario):
        self.source = source
        self.scenario = scenario

    def reference(self, order_id):
        return {'source_id': self.source['id'], 'platform': self.source['platform'],
                'shop_id': self.source['shop_id'], 'order_id': order_id}

    async def health(self):
        return {'ok': True, 'capabilities': ['card']}

    async def verify_order(self, reference):
        return {'platform': reference['platform'], 'shop_id': reference['shop_id'],
                'order_id': reference['order_id'], 'buyer_id': 'synthetic-buyer',
                'status': 'paid', 'amount': '10.00', 'currency': 'CNY',
                'items': [{'line_id': 'line-1', 'item_id': 'synthetic-item', 'sku_id': '',
                           'quantity': 1, 'amount': '10.00'}]}

    async def deliver(self, job, payload, idempotency_key):
        return await self.scenario.deliver(job, payload, idempotency_key)

    async def list_orders(self, cursor=None):
        return await self.scenario.list_orders(cursor)


def configure(runtime):
    source = runtime.save_source(source_data())
    rule = runtime.store.save_rule({'platform': 'dujiao', 'shop_id': 'synthetic-shop',
                                   'item_id': 'synthetic-item', 'type': 'card',
                                   'min_unit_amount': '10.00', 'currency': 'CNY'})
    runtime.store.import_cards(rule['id'], ['synthetic-card-1', 'synthetic-card-2'])
    return source['id']


class ShutdownScenario:
    def __init__(self, outcome):
        self.outcome = outcome
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.deliveries = 0

    async def deliver(self, job, payload, key):
        self.deliveries += 1
        self.entered.set()
        await self.release.wait()
        if self.outcome == 'unknown':
            raise OSError('synthetic connection lost after submission')
        return {'status': 'sent', 'receipt': 'synthetic-confirmed-receipt'}


class CursorScenario:
    def __init__(self):
        self.cursors = []
        self.expired_once = False
        self.delivered = asyncio.Event()
        self.deliveries = 0

    async def list_orders(self, cursor):
        self.cursors.append(cursor)
        if cursor == 'expired-next-page':
            self.expired_once = True
            raise FulfillmentError('expired_cursor', '合成测试分页已过期')
        if cursor is None and self.expired_once:
            return {'orders': ['recovered-order'], 'next_cursor': None}
        if cursor is None:
            return {'orders': [], 'next_cursor': 'page-1'}
        number = int(cursor.split('-')[1])
        return {'orders': [], 'next_cursor': 'expired-next-page' if number == 9 else f'page-{number + 1}'}

    async def deliver(self, job, payload, key):
        self.deliveries += 1
        self.delivered.set()
        return {'status': 'sent', 'receipt': 'synthetic-recovered-receipt'}


class FulfillmentRuntimeRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_waits_for_manual_delivery_and_persists_result_before_sqlite_closes(self):
        for outcome in ('sent', 'unknown'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                scenario = ShutdownScenario(outcome)
                runtime = FulfillmentRuntime(root, adapter_factory=lambda source: Adapter(source, scenario))
                source_id = configure(runtime)
                process = asyncio.create_task(runtime.process(source_id, 'manual-order', dry_run=False))
                closing = None
                try:
                    await asyncio.wait_for(scenario.entered.wait(), 2)
                    self.assertTrue(runtime.lock.locked())
                    closing = asyncio.create_task(runtime.close())
                    await asyncio.sleep(0)  # Give close() a turn to wait on the lock.
                    self.assertFalse(closing.done())
                    self.assertEqual(runtime.store.list_jobs()[0]['status'], 'sending')
                    scenario.release.set()
                    result = await asyncio.wait_for(process, 2)
                    self.assertEqual(result['jobs'][0]['status'], outcome)
                    await asyncio.wait_for(closing, 2)
                    reopened = FulfillmentStore(root / 'data/fulfillment.db')
                    try:
                        persisted = reopened.list_jobs()[0]
                        self.assertEqual(persisted['status'], outcome)
                        self.assertEqual(persisted['assigned_count'], 1)
                        self.assertEqual(persisted['attempts'], 1)
                        self.assertEqual(reopened.list_rules()[0]['inventory_available'], 1)
                    finally:
                        reopened.close()
                    self.assertEqual(scenario.deliveries, 1)
                finally:
                    scenario.release.set()
                    await asyncio.gather(process, *([closing] if closing else []), return_exceptions=True)
                    await runtime.close()

    async def test_automatic_expired_cursor_restarts_scan_and_finds_unprocessed_order(self):
        with tempfile.TemporaryDirectory() as temp:
            scenario = CursorScenario()
            runtime = FulfillmentRuntime(Path(temp), adapter_factory=lambda source: Adapter(source, scenario), interval=0.001)
            source_id = configure(runtime)
            runtime.cursors[source_id] = 'obsolete-before-save'
            runtime.save_source({'id': source_id, 'name': '更新后的测试订单源'})
            self.assertNotIn(source_id, runtime.cursors)
            runtime.cursors[source_id] = 'obsolete-before-start'
            try:
                await runtime.start()
                self.assertNotIn(source_id, runtime.cursors)
                await asyncio.wait_for(scenario.delivered.wait(), 2)
                # Completion is recorded after the adapter returns its receipt.
                async with runtime.lock:
                    self.assertEqual(runtime.store.list_jobs()[0]['status'], 'sent')
                self.assertEqual(scenario.cursors[:12], [None] + [f'page-{number}' for number in range(1, 10)] + ['expired-next-page', None])
                self.assertEqual(scenario.deliveries, 1)
                self.assertEqual(runtime.store.list_jobs()[0]['order_id'], 'recovered-order')
                self.assertIsNone(runtime.cursors.get(source_id))
            finally:
                await runtime.close()


if __name__ == '__main__':
    unittest.main()
