"""Private merchant order connections. Chat messages are never order authority."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import quote, urlencode, urlsplit
import uuid

import httpx

from core.ai_settings import normalize_base_url, AISettingsError, _contains_secret
from core.fulfillment_store import FulfillmentError, text_field

PROTOCOL = "uecb-fulfillment-v1"
PLATFORMS = [
    {"id": "dujiao", "name": "独角商城"}, {"id": "xianyu", "name": "闲鱼"},
    {"id": "taobao", "name": "淘宝"}, {"id": "jingdong", "name": "京东"},
    {"id": "pinduoduo", "name": "拼多多"}, {"id": "douyin", "name": "抖店"},
    {"id": "kuaishou", "name": "快手小店"}, {"id": "xiaohongshu", "name": "小红书千帆"},
]


def _source(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != {"id", "name", "platform", "shop_id", "base_url", "secret", "enabled", "auto_process"}:
        raise FulfillmentError("invalid_source", "订单连接配置格式无效")
    result = {key: text_field(data.get(key), maximum=2048 if key == "base_url" else 200)
              for key in ("id", "name", "platform", "shop_id", "base_url")}
    if not re.fullmatch(r"[a-f0-9]{32}", result["id"]) or result["platform"] not in {p["id"] for p in PLATFORMS}:
        raise FulfillmentError("invalid_source", "订单连接的平台或标识无效")
    try:
        result["base_url"] = normalize_base_url(result["base_url"])
    except AISettingsError:
        raise FulfillmentError("invalid_source_url", "请填写订单接口基础地址：远程须用 HTTPS，本机可用 HTTP，不含账号、参数或片段") from None
    # A stable ASCII route avoids differences between client and server URI signing.
    path = urlsplit(result["base_url"]).path
    if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", path):
        raise FulfillmentError("invalid_source_url", "订单接口路径只能包含英文字母、数字、斜杠、短横线和下划线")
    secret = text_field(data.get("secret"), maximum=4096)
    if len(secret.encode()) < 32:
        raise FulfillmentError("invalid_secret", "订单接口密钥至少需要 32 字节，请使用服务端单独生成的密钥")
    if any(_contains_secret(result[key], secret) for key in result):
        raise FulfillmentError("invalid_secret", "请勿把订单接口密钥填到名称、地址或店铺标识中")
    result["secret"] = secret
    for key in ("enabled", "auto_process"):
        if not isinstance(data.get(key), bool):
            raise FulfillmentError("invalid_source", "启用和自动处理状态须为 true 或 false")
        result[key] = data[key]
    return result


def public_source(source: dict) -> dict:
    return {**{key: value for key, value in source.items() if key != "secret"},
            "has_secret": bool(source["secret"]), "status": "configured" if source["enabled"] else "disabled"}


class FulfillmentSources:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def load(self) -> list[dict]:
        with self.lock:
            if not self.path.exists():
                return []
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 262144:
                    raise ValueError()
                raw = json.loads(self.path.read_text("utf-8"))
                if not isinstance(raw, list) or len(raw) > 50:
                    raise ValueError()
                sources = [_source(item) for item in raw]
                if len({s["id"] for s in sources}) != len(sources):
                    raise ValueError()
                return sources
            except (OSError, ValueError, UnicodeError, TypeError):
                raise FulfillmentError("invalid_source_file", "本地订单连接配置无法读取，请检查私有配置文件") from None

    def get(self, source_id: str) -> dict:
        for source in self.load():
            if source["id"] == source_id:
                return source
        raise FulfillmentError("source_missing", "请先在工作台保存订单连接")

    def public_state(self) -> list[dict]:
        return [public_source(source) for source in self.load()]

    def save(self, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) - {"id", "name", "platform", "shop_id", "base_url", "secret", "enabled", "auto_process"}:
            raise FulfillmentError("invalid_source", "订单连接配置包含不支持的字段")
        with self.lock:
            sources = self.load()
            old = next((s for s in sources if s["id"] == data.get("id")), None)
            if data.get("id") and not old:
                raise FulfillmentError("source_missing", "未找到要修改的订单连接")
            draft = {"id": uuid.uuid4().hex, "enabled": False, "auto_process": False, **(old or {}), **data}
            if old:
                if any(draft.get(key) != old[key] for key in ("platform", "shop_id")):
                    raise FulfillmentError("source_identity_changed", "更换平台或店铺请新建订单连接，原订单继续使用原连接")
                if draft.get("base_url") != old["base_url"] and not data.get("secret"):
                    raise FulfillmentError("fresh_secret_required", "更换订单接口地址时，请重新填写对应密钥")
                if data.get("secret") == "":
                    draft["secret"] = old["secret"]
            source = _source(draft)
            if old and any(_contains_secret(source[key], old["secret"]) for key in ("name", "platform", "shop_id", "base_url")):
                raise FulfillmentError("invalid_secret", "请勿把原订单接口密钥填到公开字段中")
            sources = [s for s in sources if s["id"] != source["id"]] + [source]
            if len(sources) > 50:
                raise FulfillmentError("too_many_sources", "最多保存 50 个订单连接")
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.is_symlink():
                    raise OSError()
                fd, temporary = tempfile.mkstemp(prefix=".fulfillment-", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(sources, output, ensure_ascii=False, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            except OSError:
                raise FulfillmentError("source_save_failed", "订单连接保存失败，请检查本地文件权限") from None
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            return public_source(source)


def signed_headers(secret: str, method: str, path: str, body: bytes, *, timestamp=None, nonce=None) -> dict:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or uuid.uuid4().hex
    message = "\n".join([method, path, stamp, nonce, hashlib.sha256(body).hexdigest()])
    return {"X-Fulfillment-Timestamp": stamp, "X-Fulfillment-Nonce": nonce,
            "X-Fulfillment-Signature": hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest(),
            "Content-Type": "application/json", "Accept": "application/json"}


class BridgeAdapter:
    """Calls only a locally configured merchant bridge, never an order-supplied URL."""
    def __init__(self, source: dict, *, transport=None):
        self.source = deepcopy(source)
        self.transport = transport
        self.capabilities: set[str] | None = None

    async def _request(self, method: str, suffix: str, data=None) -> dict:
        raw = b"" if data is None else json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 262144:
            raise FulfillmentError("delivery_too_large", "单次交付内容超过 256 KB，请人工处理")
        url = httpx.URL(self.source["base_url"] + suffix)
        headers = signed_headers(self.source["secret"], method, url.raw_path.decode("ascii"), raw)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                async with asyncio.timeout(25):
                    async with client.stream(method, url, content=raw, headers=headers) as response:
                        # Even an error response may contain secrets. Never forward its body.
                        if response.status_code != 200:
                            raise FulfillmentError("bridge_rejected", "订单接口未接受请求，请检查授权和服务状态")
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > 262144:
                                raise FulfillmentError("bridge_invalid", "订单接口返回内容过大，已停止处理")
                        result = json.loads(chunks)
                        if not isinstance(result, dict):
                            raise ValueError()
                        return result
        except FulfillmentError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError):
            raise FulfillmentError("bridge_unavailable", "订单接口暂时无法连接或返回格式不正确，请稍后再试") from None

    async def health(self) -> dict:
        result = await self._request("GET", "/health")
        capabilities = result.get("capabilities")
        if (result.get("ok") is not True or result.get("protocol") != PROTOCOL
                or any(result.get(key) != self.source[key] for key in ("platform", "shop_id"))
                or not isinstance(capabilities, list) or not capabilities
                or any(not isinstance(cap, str) or cap not in {"card", "link", "physical"} for cap in capabilities)):
            raise FulfillmentError("bridge_identity_mismatch", "订单接口的平台、店铺或发货能力与配置不一致")
        self.capabilities = set(capabilities)
        return {"ok": True, "message": "订单接口连接成功；这还不是实际发货测试", "capabilities": sorted(self.capabilities)}

    def reference(self, order_id: str) -> dict:
        return {"source_id": self.source["id"], "platform": self.source["platform"],
                "shop_id": self.source["shop_id"], "order_id": text_field(order_id)}

    async def list_orders(self, cursor=None) -> dict:
        query = {"limit": "50"}
        if cursor is not None:
            query["cursor"] = text_field(cursor, maximum=200)
        data = await self._request("GET", "/orders?" + urlencode(query))
        rows = data.get("orders")
        if not isinstance(rows, list) or len(rows) > 50:
            raise FulfillmentError("bridge_invalid", "订单列表格式无效")
        ids = []
        for row in rows:
            if not isinstance(row, dict):
                raise FulfillmentError("bridge_invalid", "订单列表格式无效")
            ids.append(text_field(row.get("order_id")))
        next_cursor = data.get("next_cursor")
        if next_cursor is not None:
            next_cursor = text_field(next_cursor)
        return {"orders": ids, "next_cursor": next_cursor}

    async def verify_order(self, reference: dict) -> dict:
        if reference != self.reference(reference.get("order_id")):
            raise FulfillmentError("identity_mismatch", "订单来源与配置不匹配")
        if self.capabilities is None:
            await self.health()
        return await self._request("GET", "/orders/" + quote(reference["order_id"], safe=""))

    async def deliver(self, job: dict, payload: dict, idempotency_key: str) -> dict:
        if self.capabilities is None:
            await self.health()
        if (job["type"] not in self.capabilities
                or any(job[key] != self.source[key] for key in ("platform", "shop_id"))):
            return {"status": "failed"}
        data = {key: job[key] for key in ("order_id", "line_id", "buyer_id", "item_id", "sku_id", "quantity", "amount", "currency")}
        data.update(idempotency_key=idempotency_key, payload=payload)
        # HTTP errors/timeout may follow an accepted transaction. Engine marks unknown.
        result = await self._request("POST", "/deliveries", data)
        if result.get("status") == "sent" and isinstance(result.get("receipt"), str) and 0 < len(result["receipt"]) <= 200:
            return {"status": "sent", "receipt": result["receipt"]}
        if result.get("status") == "failed":
            return {"status": "failed"}
        return {"status": "unknown"}
