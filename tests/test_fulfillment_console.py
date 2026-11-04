"""End-to-end console/HTTP bridge tests; no real orders, accounts or stock."""
import asyncio
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from core.fulfillment_sources import BridgeAdapter, FulfillmentSources, signed_headers
from core.fulfillment_runtime import FulfillmentRuntime
from core.fulfillment_store import FulfillmentError
from web.server import create_app

HEADERS = {"X-Requested-With": "merchant-console"}
SECRET = "synthetic-merchant-secret-for-tests-only"


def source(**changes):
    return {"name": "测试商城", "platform": "dujiao", "shop_id": "test-shop",
            "base_url": "https://merchant.example/api/v1/merchant-fulfillment", "secret": SECRET,
            "enabled": True, "auto_process": False, **changes}


def order(order_id="card-order", item_id="card-item", **changes):
    return {"platform": "dujiao", "shop_id": "test-shop", "order_id": order_id,
            "buyer_id": "user:synthetic", "status": "paid", "amount": "20.00", "currency": "CNY",
            "shipping_amount": "0.00", "items": [{"line_id": "line-1", "item_id": item_id,
                "sku_id": "", "quantity": 2, "amount": "20.00"}], **changes}


class Merchant:
    def __init__(self):
        self.orders = {"card-order": order(), "link-order": order("link-order", "link-item"),
                       "physical-order": order("physical-order", "physical-item")}
        self.calls = []
        self.deliveries = {}
        self.mode = "normal"

    def handle(self, request):
        self.calls.append(request)
        expected = signed_headers(SECRET, request.method, request.url.raw_path.decode(), request.content,
                                  timestamp=request.headers["x-fulfillment-timestamp"], nonce=request.headers["x-fulfillment-nonce"])
        assert hmac.compare_digest(expected["X-Fulfillment-Signature"], request.headers["x-fulfillment-signature"])
        assert abs(int(request.headers["x-fulfillment-timestamp"]) - time.time()) < 5
        path = request.url.path
        if path.endswith("/health"):
            return httpx.Response(200, json={"ok": True, "protocol": "uecb-fulfillment-v1", "platform": "dujiao",
                                  "shop_id": "test-shop", "capabilities": ["card", "link", "physical"]})
        if path.endswith("/orders"):
            return httpx.Response(200, json={"orders": [{"order_id": oid} for oid in self.orders], "next_cursor": None})
        if "/orders/" in path:
            return httpx.Response(200, json=self.orders[path.rsplit("/", 1)[-1]])
        assert path.endswith("/deliveries") and request.method == "POST"
        body = json.loads(request.content)
        if self.mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://untrusted.example/collect"})
        if self.mode == "timeout":
            raise httpx.ReadTimeout("synthetic-card-1 " + SECRET)
        key = body["idempotency_key"]
        if key in self.deliveries:
            assert self.deliveries[key] == body
        self.deliveries[key] = body
        return httpx.Response(200, json={"status": "sent", "receipt": "receipt-" + hashlib.sha256(key.encode()).hexdigest()})


@pytest.fixture
def console(tmp_path):
    app = create_app(tmp_path)
    merchant = Merchant()
    app.state.fulfillment.adapter_factory = lambda config: BridgeAdapter(config, transport=httpx.MockTransport(merchant.handle))
    with TestClient(app) as client:
        yield client, app.state.fulfillment, merchant


def configure(client):
    response = client.put("/api/fulfillment/sources", headers=HEADERS, json=source())
    assert response.status_code == 200
    return response.json()["source"]["id"]


def add_rule(client, kind, content=None):
    body = {"platform": "dujiao", "shop_id": "test-shop", "item_id": kind + "-item", "sku_id": "",
            "type": kind, "enabled": True, "min_unit_amount": "10.00", "currency": "CNY"}
    if content:
        body["delivery_content"] = content
    response = client.put("/api/fulfillment/rules", headers=HEADERS, json=body)
    assert response.status_code == 200
    rule = response.json()["rule"]
    if kind == "card":
        assert client.post("/api/fulfillment/cards", headers=HEADERS, json={"rule_id": rule["id"],
            "cards": ["synthetic-card-1", "synthetic-card-2", "synthetic-card-1"]}).json() == {"added": 2, "duplicates": 1, "available": 2}
    return rule


def test_console_card_link_and_physical_full_flow(console):
    client, runtime, merchant = console
    source_id = configure(client)
    for kind in ("card", "link", "physical"):
        add_rule(client, kind, "https://files.example/synthetic-access-code" if kind == "link" else None)
    test = client.post(f"/api/fulfillment/sources/{source_id}/test", headers=HEADERS)
    assert test.json()["ok"] is True
    preview = client.post("/api/fulfillment/sync", headers=HEADERS, json={"source_id": source_id}).json()
    assert preview["dry_run"] is True and preview["count"] == 3
    assert not merchant.deliveries and not client.get("/api/fulfillment").json()["jobs"]
    result = client.post("/api/fulfillment/sync", headers=HEADERS, json={"source_id": source_id, "dry_run": False})
    assert result.status_code == 200 and len(merchant.deliveries) == 2
    state = client.get("/api/fulfillment").json()
    jobs = {j["type"]: j for j in state["jobs"]}
    assert jobs["card"]["status"] == jobs["link"]["status"] == "sent"
    assert jobs["physical"]["status"] == "awaiting_shipment"
    # Repeated notifications allocate/send nothing new.
    client.post("/api/fulfillment/sync", headers=HEADERS, json={"source_id": source_id, "dry_run": False})
    assert len([r for r in merchant.calls if r.method == "POST"]) == 2
    job_id = jobs["physical"]["id"]
    saved = client.post(f"/api/fulfillment/jobs/{job_id}/tracking", headers=HEADERS,
                        json={"carrier": "测试快递", "tracking_number": "SYNTHETIC-123"}).json()
    assert saved["job"]["status"] == "awaiting_shipment" and len(merchant.deliveries) == 2
    sent = client.post(f"/api/fulfillment/jobs/{job_id}/retry", headers=HEADERS, json={"dry_run": False}).json()
    assert sent["job"]["status"] == "sent"
    physical = next(d for d in merchant.deliveries.values() if d["payload"]["type"] == "physical")
    assert physical["payload"] == {"type": "physical", "carrier": "测试快递", "tracking_number": "SYNTHETIC-123"}
    state_text = client.get("/api/fulfillment").text
    for private in (SECRET, "synthetic-card-1", "synthetic-access-code", "receipt-"):
        assert private not in state_text
    assert os.stat(runtime.sources.path).st_mode & 0o777 == 0o600
    assert os.stat(runtime.store.path).st_mode & 0o777 == 0o600
    listing = client.get("/api/fulfillment/jobs?status=sent&limit=2").json()
    assert listing["total"] == listing["counts"]["all"] == 3
    assert len(listing["jobs"]) == 2 and listing["has_more"]
    second = client.get("/api/fulfillment/jobs?status=sent&offset=2&limit=2").json()
    assert len(second["jobs"]) == 1 and not second["has_more"]
    assert not ({j["id"] for j in second["jobs"]} & {j["id"] for j in listing["jobs"]})
    assert client.get("/api/fulfillment/jobs?status=invalid").status_code == 400
    assert client.get("/api/fulfillment/jobs?offset=-1").status_code == 400
    assert client.get("/api/fulfillment/jobs?limit=500").status_code == 400


def test_console_rejects_chat_payment_and_untrusted_fields(console):
    client, runtime, merchant = console
    source_id = configure(client)
    add_rule(client, "card")
    base = {"source_id": source_id, "order_id": "card-order", "dry_run": False}
    for untrusted in ({"paid": True}, {"buyer_id": "chat-user"}, {"delivery_target": "https://evil.example"}):
        assert client.post("/api/fulfillment/process", headers=HEADERS, json={**base, **untrusted}).status_code == 400
    merchant.orders["card-order"]["status"] = "unpaid"
    response = client.post("/api/fulfillment/process", headers=HEADERS, json=base).json()
    assert response["error_code"] == "order_unpaid" and not merchant.deliveries
    assert runtime.store.list_rules()[0]["inventory_available"] == 2


@pytest.mark.parametrize("mode", ["timeout", "redirect"])
def test_uncertain_delivery_stays_unknown_without_blind_retry(console, mode):
    client, runtime, merchant = console
    source_id = configure(client)
    add_rule(client, "card")
    merchant.mode = mode
    result = client.post("/api/fulfillment/process", headers=HEADERS,
                         json={"source_id": source_id, "order_id": "card-order", "dry_run": False})
    assert "synthetic-card-1" not in result.text and SECRET not in result.text
    job = result.json()["jobs"][0]
    assert job["status"] == "unknown"
    assert runtime.store.list_rules()[0]["inventory_available"] == 0
    merchant.mode = "normal"
    retry = client.post(f"/api/fulfillment/jobs/{job['id']}/retry", headers=HEADERS, json={"dry_run": False}).json()
    assert retry["status"] == "needs_review"
    assert len([r for r in merchant.calls if r.method == "POST"]) == 1
    assert all(r.url.host == "merchant.example" for r in merchant.calls)


def test_sources_private_atomic_and_bound_to_shop(tmp_path):
    store = FulfillmentSources(tmp_path / "private.json")
    saved = store.save(source())
    assert SECRET not in json.dumps(saved) and saved["has_secret"]
    assert store.save({"id": saved["id"], "secret": "", "name": "更名"})["name"] == "更名"
    for changes in ({"platform": "taobao"}, {"shop_id": "other"}, {"base_url": "https://changed.example", "secret": ""}):
        with pytest.raises(FulfillmentError):
            store.save({"id": saved["id"], **changes})
    for endpoint in ("http://remote.example", "https://u:password@example.com", "https://example.com/?token=oops", "https://example.com/#secret"):
        with pytest.raises(FulfillmentError):
            store.save(source(base_url=endpoint))
    assert len(store.public_state()) == 1
    with pytest.raises(FulfillmentError):
        store.save({"id": saved["id"], "secret": "a-new-synthetic-secret-with-over-32-bytes", "name": SECRET})


def test_invalid_health_capabilities_return_safe_error(tmp_path):
    async def scenario():
        saved = FulfillmentSources(tmp_path / "source.json").save(source())
        config = {**source(), "id": saved["id"]}
        adapter = BridgeAdapter(config, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "ok": True, "protocol": "uecb-fulfillment-v1", "platform": "dujiao", "shop_id": "test-shop", "capabilities": [{}]})))
        with pytest.raises(FulfillmentError, match="平台、店铺"):
            await adapter.health()
    asyncio.run(scenario())


def test_disabled_source_dry_run_allowed_actual_send_blocked(console):
    client, runtime, merchant = console
    saved = client.put("/api/fulfillment/sources", headers=HEADERS, json=source(enabled=False)).json()["source"]
    add_rule(client, "card")
    body = {"source_id": saved["id"], "order_id": "card-order"}
    assert client.post("/api/fulfillment/process", headers=HEADERS, json=body).json()["dry_run"]
    assert client.post("/api/fulfillment/process", headers=HEADERS, json={**body, "dry_run": False}).status_code == 400
    assert not merchant.deliveries
    assert client.post("/api/fulfillment/start", headers=HEADERS).status_code == 400


def test_console_guards_and_validation_never_echo_secrets(console):
    client, runtime, merchant = console
    for body in ([SECRET], SECRET, ["synthetic-card-1"]):
        response = client.put("/api/fulfillment/sources", headers=HEADERS, json=body)
        assert response.status_code == 400 and SECRET not in response.text
    assert client.put("/api/fulfillment/sources", json=source()).status_code == 403
    assert client.put("/api/fulfillment/sources", headers={**HEADERS, "Origin": "https://evil.example"}, json=source()).status_code == 403
    assert client.get("/api/fulfillment", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/fulfillment").headers["Cache-Control"] == "no-store"


def test_background_polling_catches_existing_orders_and_restart_deduplicates(tmp_path):
    async def scenario():
        merchant = Merchant()
        merchant.orders = {"card-order": order()}
        factory = lambda config: BridgeAdapter(config, transport=httpx.MockTransport(merchant.handle))
        runtime = FulfillmentRuntime(tmp_path, adapter_factory=factory, interval=0.01)
        saved = runtime.save_source(source(auto_process=True))
        rule = runtime.store.save_rule({"platform": "dujiao", "shop_id": "test-shop", "item_id": "card-item",
            "type": "card", "enabled": True, "min_unit_amount": "10", "currency": "CNY"})
        runtime.store.import_cards(rule["id"], ["fake-one", "fake-two"])
        await runtime.start()
        for _ in range(100):
            if runtime.store.list_jobs() and runtime.store.list_jobs()[0]["status"] == "sent":
                break
            await asyncio.sleep(0.01)
        assert runtime.store.list_jobs()[0]["status"] == "sent"
        with pytest.raises(FulfillmentError):
            runtime.save_source({"id": saved["id"], "enabled": False})
        await runtime.close()
        restarted = FulfillmentRuntime(tmp_path, adapter_factory=factory, interval=0.01)
        assert not restarted.running  # restarting the app never silently enables live fulfillment
        await restarted.sync(saved["id"], dry_run=False)
        assert len([r for r in merchant.calls if r.method == "POST"]) == 1
        await restarted.close()
    asyncio.run(scenario())


def test_source_health_and_order_identity_mismatch_blocks_before_inventory(console):
    client, runtime, merchant = console
    source_id = configure(client)
    add_rule(client, "card")
    merchant.orders["card-order"]["shop_id"] = "someone-else"
    response = client.post("/api/fulfillment/process", headers=HEADERS,
                           json={"source_id": source_id, "order_id": "card-order", "dry_run": False}).json()
    assert response["status"] == "blocked" and not merchant.deliveries
    assert runtime.store.list_rules()[0]["inventory_available"] == 2
