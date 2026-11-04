"""Explicitly started order polling, separate from chat and optional AI."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from core.fulfillment import FulfillmentEngine
from core.fulfillment_store import FulfillmentStore, FulfillmentError
from core.fulfillment_sources import BridgeAdapter, FulfillmentSources, PLATFORMS


class FulfillmentRuntime:
    def __init__(self, root: Path, *, adapter_factory=BridgeAdapter, interval=30):
        self.sources = FulfillmentSources(root / "config/fulfillment.local.json")
        self.store = FulfillmentStore(root / "data/fulfillment.db")
        self.adapter_factory = adapter_factory
        self.interval = interval
        self.lock = asyncio.Lock()
        self.task = None
        self.last_error = ""
        self.last_sync = None
        self.cursors = {}

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def runtime_state(self):
        return {"running": self.running, "busy": self.lock.locked(), "last_error": self.last_error,
                "last_sync": self.last_sync, "interval_seconds": self.interval}

    def state(self):
        return {"rules": self.store.list_rules(), "jobs": self.store.list_jobs(),
                "sources": self.sources.public_state(), "runtime": self.runtime_state(), "platforms": PLATFORMS}

    def _adapter(self, source_id, *, dry_run):
        if not isinstance(dry_run, bool):
            raise FulfillmentError("invalid_dry_run", "请明确选择试运行或正式处理")
        source = self.sources.get(source_id)
        if not dry_run and not source["enabled"]:
            raise FulfillmentError("source_disabled", "请先启用该订单连接")
        return self.adapter_factory(source)

    def save_source(self, data):
        if self.running or self.lock.locked():
            raise FulfillmentError("fulfillment_busy", "请先停止自动发货并等待当前操作完成，再修改订单连接")
        saved = self.sources.save(data)
        self.cursors.pop(saved["id"], None)
        return saved

    async def test(self, source_id):
        adapter = self._adapter(source_id, dry_run=True)
        return await adapter.health()

    async def process(self, source_id, order_id, *, dry_run=True):
        async with self.lock:
            adapter = self._adapter(source_id, dry_run=dry_run)
            return await FulfillmentEngine(self.store, adapter).process(adapter.reference(order_id), dry_run=dry_run)

    async def retry(self, job_id, *, dry_run=True):
        async with self.lock:
            job = self.store.private_job(job_id)
            if not job:
                raise FulfillmentError("job_missing", "未找到该发货任务")
            adapter = self._adapter(job["reference"].get("source_id"), dry_run=dry_run)
            return await FulfillmentEngine(self.store, adapter).retry(job_id, dry_run=dry_run)

    async def sync(self, source_id, *, dry_run=True, automatic=False):
        async with self.lock:
            adapter = self._adapter(source_id, dry_run=dry_run)
            await adapter.health()
            engine = FulfillmentEngine(self.store, adapter)
            # Every completed scan starts from the beginning: a paid order missed
            # during an outage is found later, and the durable ledger deduplicates.
            cursor = self.cursors.get(source_id) if automatic else None
            visited, results = set(), []
            for _ in range(10):
                if cursor in visited:
                    raise FulfillmentError("invalid_cursor", "订单分页重复，已停止同步，请检查订单服务")
                visited.add(cursor)
                page = await adapter.list_orders(cursor)
                for order_id in page["orders"]:
                    results.append(await engine.process(adapter.reference(order_id), dry_run=dry_run))
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            if automatic:
                self.cursors[source_id] = cursor
            self.last_sync = datetime.now(timezone.utc).isoformat()
            blocked = sum(result.get("status") == "blocked" or any(job["status"] in {"blocked", "unknown"} for job in result["jobs"]) for result in results)
            self.last_error = "部分订单未通过核验或交付待核实，请查看本次结果和发货记录" if blocked else ""
            return {"dry_run": dry_run, "results": results, "count": len(results),
                    "blocked_count": blocked, "has_more": cursor is not None,
                    "message": "本批次已处理；仍有后续订单，自动轮询会继续" if cursor else "本轮订单扫描完成"}

    async def start(self):
        if self.running:
            return self.runtime_state()
        if self.lock.locked():
            raise FulfillmentError("fulfillment_busy", "正在处理订单，请完成后再开启自动发货")
        if not any(s["enabled"] and s["auto_process"] for s in self.sources.load()):
            raise FulfillmentError("no_auto_source", "请先启用订单连接，并勾选该连接的自动处理")
        self.cursors.clear()
        self.task = asyncio.create_task(self._run())
        return self.runtime_state()

    async def _run(self):
        try:
            while True:
                errors = []
                for source in self.sources.load():
                    if source["enabled"] and source["auto_process"]:
                        try:
                            result = await self.sync(source["id"], dry_run=False, automatic=True)
                            if result["blocked_count"]:
                                errors.append("部分订单被阻止或交付待核实，请查看发货记录")
                        except FulfillmentError as exc:
                            self.cursors.pop(source["id"], None)
                            errors.append(str(exc))
                        except Exception:
                            self.cursors.pop(source["id"], None)
                            errors.append("自动发货遇到异常，订单已保留，请核对后继续")
                self.last_error = "；".join(dict.fromkeys(errors))
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_error = "订单连接配置无法读取，自动发货已停止"

    async def stop(self):
        task, self.task = self.task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return self.runtime_state()

    async def close(self):
        await self.stop()
        # A manual request may still be waiting for a delivery receipt. Let it
        # persist the result before closing the underlying database.
        async with self.lock:
            self.store.close()
