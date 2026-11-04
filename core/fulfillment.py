"""Paid-order fulfillment with no LLM or buyer-chat payment authority.

The injected adapter is a trusted server integration. It must query authorized
order data and bind the delivery destination to that order's buyer. A client
notification is only an order reference, never evidence of payment.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
import json
import re
from typing import Protocol

from core.fulfillment_store import FulfillmentError, FulfillmentStore, money, text_field


class FulfillmentAdapter(Protocol):
    async def verify_order(self, reference: dict) -> dict: ...
    async def deliver(self, job: dict, payload: dict, idempotency_key: str) -> dict: ...


def order_reference(data: dict) -> dict:
    if not isinstance(data, dict) or not {"platform", "shop_id", "order_id"} <= set(data) or set(data) - {"source_id", "platform", "shop_id", "order_id"}:
        raise FulfillmentError("invalid_reference", "请提供平台、店铺和订单编号；付款状态须由订单接口核验")
    return {key: text_field(data[key]) for key in ("source_id", "platform", "shop_id", "order_id") if key in data}


def canonical_order(data: dict, reference: dict) -> dict:
    if not isinstance(data, dict):
        raise FulfillmentError("verification_failed", "订单接口未返回有效订单，请人工核对")
    order = {key: text_field(data.get(key)) for key in ("platform", "shop_id", "order_id", "buyer_id")}
    if any(order[key] != reference[key] for key in ("platform", "shop_id", "order_id")):
        raise FulfillmentError("identity_mismatch", "订单平台、店铺或编号不匹配，已停止发货")
    status = data.get("status")
    if not isinstance(status, str) or status not in {"paid", "unpaid", "cancelled", "refunded", "fulfilled"}:
        raise FulfillmentError("verification_failed", "订单付款状态无法确认，请人工核对")
    # Conservative handling for integrations that expose both payment and
    # cancellation/refund flags. A paid flag never overrides a refund.
    if any(key in data and not isinstance(data[key], bool) for key in ("refunded", "refund_pending", "cancelled")):
        raise FulfillmentError("verification_failed", "订单退款或取消标记无法确认，请人工核对")
    if data.get("refunded") is True or data.get("refund_pending") is True:
        status = "refunded"
    elif data.get("cancelled") is True:
        status = "cancelled"
    order["status"] = status
    order["amount"] = money(data.get("amount"))
    order["shipping_amount"] = money(data.get("shipping_amount", "0"))
    currency = text_field(data.get("currency"), maximum=3).upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise FulfillmentError("invalid_currency", "订单币种无效，请人工核对")
    order["currency"] = currency
    items = data.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise FulfillmentError("invalid_items", "订单商品明细无效，请人工核对")
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise FulfillmentError("invalid_items", "订单商品明细无效，请人工核对")
        line = {key: text_field(item.get(key), optional=key == "sku_id") for key in ("line_id", "item_id", "sku_id")}
        quantity = item.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 10000 or line["line_id"] in seen:
            raise FulfillmentError("invalid_items", "订单数量或子单编号无效，请人工核对")
        seen.add(line["line_id"])
        line.update(quantity=quantity, amount=money(item.get("amount")))
        normalized.append(line)
    if sum(Decimal(item["amount"]) for item in normalized) + Decimal(order["shipping_amount"]) != Decimal(order["amount"]):
        raise FulfillmentError("amount_mismatch", "订单总额与商品实付金额不一致，已停止发货")
    order["items"] = sorted(normalized, key=lambda item: item["line_id"])
    target = data.get("delivery_target", {})
    try:
        if not isinstance(target, dict) or _contains_endpoint(target):
            raise ValueError()
        serialized = json.dumps(target, ensure_ascii=False, allow_nan=False)
        if len(serialized) > 8192:
            raise ValueError()
        order["delivery_target"] = json.loads(serialized)
    except (ValueError, TypeError):
        raise FulfillmentError("invalid_destination", "订单交付目标无法确认，请人工核对") from None
    return order


def _contains_endpoint(value) -> bool:
    if isinstance(value, dict):
        return any(any(word in str(key).lower() for word in ("url", "webhook", "callback", "endpoint")) or _contains_endpoint(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_endpoint(item) for item in value)
    return isinstance(value, str) and bool(re.search(r"[a-z][a-z0-9+.-]*://|^\s*//", value, flags=re.I))


def _same_order(original: dict, current: dict) -> bool:
    return {key: value for key, value in original.items() if key != "status"} == {key: value for key, value in current.items() if key != "status"}


class FulfillmentEngine:
    def __init__(self, store: FulfillmentStore, adapter: FulfillmentAdapter, *, timeout_seconds=30):
        self.store = store
        self.adapter = adapter
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 120:
            raise FulfillmentError("invalid_timeout", "发货接口超时须为 0 至 120 秒之间的数字")
        self.timeout_seconds = timeout_seconds

    async def _verify(self, reference: dict) -> dict:
        try:
            raw = await asyncio.wait_for(self.adapter.verify_order(deepcopy(reference)), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise FulfillmentError("verification_failed", "订单核验失败，已停止发货，请稍后重试") from None
        return canonical_order(raw, reference)

    def _block_reference(self, reference: dict, code: str):
        for job in self.store.jobs_for_order(reference):
            self.store.block(job["id"], code)

    async def process(self, reference: dict, *, dry_run=True) -> dict:
        reference = order_reference(reference)
        if not isinstance(dry_run, bool):
            raise FulfillmentError("invalid_dry_run", "dry_run 必须为 true 或 false")
        try:
            order = await self._verify(reference)
        except FulfillmentError as exc:
            if not dry_run:
                self._block_reference(reference, "verification_failed")
            return {"dry_run": dry_run, "reference": reference, "status": "blocked", "error_code": exc.code, "jobs": []}
        if order["status"] != "paid":
            code = "order_" + order["status"]
            if not dry_run:
                self._block_reference(reference, code)
            return {"dry_run": dry_run, "reference": reference, "status": "blocked", "error_code": code, "jobs": self.store.jobs_for_order(reference)}
        if dry_run:
            return {"dry_run": True, "reference": reference, "status": "preview", "jobs": self.store.preview(order)}
        jobs = self.store.reserve_order(reference, order)
        for job in jobs:
            if job["status"] == "reserved":
                await self._send(job["id"])
        return {"dry_run": False, "reference": reference, "status": "processed", "jobs": [self.store.get_job(job["id"]) for job in jobs]}

    async def _revalidate_job(self, job: dict) -> bool:
        try:
            current = await self._verify(job["reference"])
        except FulfillmentError:
            self.store.block(job["id"], "verification_failed")
            return False
        if current["status"] != "paid":
            self.store.block(job["id"], "order_" + current["status"])
            return False
        if not _same_order(job["order"], current):
            self.store.block(job["id"], "order_changed")
            return False
        error = self.store.current_rule_error(job)
        if error:
            self.store.block(job["id"], error)
            return False
        return True

    async def _send(self, job_id: str):
        job = self.store.private_job(job_id)
        if not job or job["status"] != "reserved" or not await self._revalidate_job(job):
            return
        token = self.store.claim_sending(job_id)
        if token is None:
            return
        # Read after the transactional claim. Configuration changes cannot alter
        # this job's snapshotted rules, assignments, or buyer destination.
        job = self.store.private_job(job_id)
        if job["type"] == "card":
            payload = {"type": "card", "cards": job["cards"]}
        elif job["type"] == "link":
            payload = {"type": "link", "content": job["rule"]["delivery_content"]}
        else:
            payload = {"type": "physical", "carrier": job["carrier"], "tracking_number": job["tracking_number"]}
        public_job = self.store.get_job(job_id)
        public_job["delivery_target"] = deepcopy(job["order"]["delivery_target"])
        try:
            result = await asyncio.wait_for(self.adapter.deliver(public_job, payload, "fulfillment:" + job_id), timeout=self.timeout_seconds)
            status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
            receipt = result.get("receipt") if isinstance(result, dict) else None
            self.store.finish(job_id, token, status, receipt=receipt)
        except asyncio.CancelledError:
            self.store.finish(job_id, token, "unknown")
            raise
        except Exception:
            # Timeout or connection failure may occur after the provider accepted
            # the delivery. Keep the same secrets and require explicit review.
            self.store.finish(job_id, token, "unknown")

    async def retry(self, job_id: str, *, dry_run=True) -> dict:
        if not isinstance(dry_run, bool):
            raise FulfillmentError("invalid_dry_run", "dry_run 必须为 true 或 false")
        job = self.store.private_job(text_field(job_id))
        if not job:
            raise FulfillmentError("job_missing", "未找到该发货任务")
        if dry_run:
            # Preview does not allocate cards, clear uncertainty, or write state.
            current = await self._verify(job["reference"])
            valid = current["status"] == "paid" and _same_order(job["order"], current)
            can_retry = valid and job["status"] in {"blocked", "awaiting_shipment"}
            if job["type"] == "physical" and not (job["carrier"] and job["tracking_number"]):
                can_retry = False
            return {"dry_run": True, "status": "preview" if valid else "blocked", "job": self.store.get_job(job_id), "can_retry": can_retry}
        if job["status"] in {"unknown", "sending"}:
            return {"dry_run": False, "status": "needs_review", "can_retry": False, "job": self.store.get_job(job_id)}
        if job["status"] not in {"blocked", "awaiting_shipment"}:
            return {"dry_run": False, "status": "unchanged", "job": self.store.get_job(job_id)}
        self.store.bind_missing_rule(job_id)
        job = self.store.private_job(job_id)
        # Verify before retry allocation AND immediately before submission.
        if not await self._revalidate_job(job):
            return {"dry_run": False, "status": "blocked", "job": self.store.get_job(job_id)}
        if self.store.prepare_retry(job_id):
            await self._send(job_id)
        return {"dry_run": False, "status": "processed", "job": self.store.get_job(job_id)}
