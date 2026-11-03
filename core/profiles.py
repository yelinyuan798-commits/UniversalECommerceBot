"""Validated merchant knowledge and a deterministic local configuration wizard."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from core.platform_catalog import PLATFORMS, PLATFORM_ALIASES, PLATFORM_LABELS


class ProfileError(ValueError):
    """A user-facing configuration validation error."""


def _text(value: Any, label: str, limit: int = 5000, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProfileError(f"{label}必须是文字")
    value = value.strip()
    if required and not value:
        raise ProfileError(f"请填写{label}")
    if len(value) > limit:
        raise ProfileError(f"{label}不能超过 {limit} 个字符")
    return value


def _money(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ProfileError(f"{label}必须是有效金额")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ProfileError(f"{label}必须是有效金额") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("999999999.99"):
        raise ProfileError(f"{label}必须是 0 至 999999999.99 之间的金额")
    if amount != amount.quantize(Decimal("0.01")):
        raise ProfileError(f"{label}最多保留两位小数")
    return float(amount)


def _string_list(value: Any, label: str, limit: int = 50) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,，、\n]", value)
    if not isinstance(value, list) or len(value) > limit:
        raise ProfileError(f"{label}必须是列表，最多 {limit} 项")
    return list(dict.fromkeys(_text(item, label, 200, required=True) for item in value if item != ""))


@dataclass
class FAQ:
    question: str
    answer: str


@dataclass
class Product:
    id: str
    name: str
    platform: str = ""
    description: str = ""
    price: float | None = None
    min_price: float | None = None
    bargain_enabled: bool = True
    stock: int | None = None
    shipping: str = ""
    returns: str = ""
    keywords: list[str] = field(default_factory=list)
    faq: list[FAQ] = field(default_factory=list)


@dataclass
class MerchantProfile:
    merchant_name: str
    tone: str = "友好、简洁"
    products: list[Product] = field(default_factory=list)
    handoff_keywords: list[str] = field(default_factory=lambda: ["人工", "投诉", "退款", "退货", "维权"])
    custom_instructions: str = ""
    schema_version: int = 1
    reply_mode: str = "custom"


def _platform(value: Any) -> str:
    value = _text(value, "商品平台", 50).lower()
    if value in {"", "通用", "全部", "所有平台", "global"}:
        return ""
    canonical = PLATFORM_ALIASES.get(value, value)
    if canonical not in PLATFORMS:
        supported = "、".join(PLATFORM_LABELS[name] for name in PLATFORMS)
        raise ProfileError(f"商品平台请选择通用、{supported}")
    return canonical


def validate_profile(data: dict) -> dict:
    """Normalize and validate JSON before it becomes active configuration."""
    if not isinstance(data, dict):
        raise ProfileError("店铺配置必须是对象")
    if data.get("schema_version", 1) != 1:
        raise ProfileError("不支持的配置版本")
    reply_mode = data.get("reply_mode", "custom")
    if not isinstance(reply_mode, str) or reply_mode not in {"default", "custom"}:
        raise ProfileError("reply_mode 必须为 default（默认客服）或 custom（商品定制）")
    products = data.get("products", [])
    if not isinstance(products, list) or len(products) > 500:
        raise ProfileError("商品必须是列表，最多 500 个")
    parsed: list[Product] = []
    seen: set[tuple[str, str]] = set()
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            raise ProfileError(f"第 {index + 1} 个商品必须是对象")
        item_id = _text(product.get("id"), "商品 ID", 200, required=True)
        platform = _platform(product.get("platform", ""))
        identity = (platform, item_id)
        if identity in seen:
            raise ProfileError(f"同一平台内商品 ID 重复：{platform or '通用'} / {item_id}")
        seen.add(identity)
        price = _money(product.get("price"), "售价")
        minimum = _money(product.get("min_price"), "最低报价")
        if minimum is not None and (price is None or minimum > price):
            raise ProfileError("最低报价必须小于等于售价，并且需要先填写售价")
        bargain_enabled = product.get("bargain_enabled", True)
        if not isinstance(bargain_enabled, bool):
            raise ProfileError("是否允许议价必须是 true 或 false")
        stock = product.get("stock")
        if stock == "":
            stock = None
        if isinstance(stock, str) and re.fullmatch(r"\d+", stock):
            stock = int(stock)
        if stock is not None and (isinstance(stock, bool) or not isinstance(stock, int) or not 0 <= stock <= 1000000000):
            raise ProfileError("库存必须是非负整数，未知请留空")
        faqs = product.get("faq", [])
        if not isinstance(faqs, list) or len(faqs) > 100:
            raise ProfileError("常见问题必须是列表，最多 100 项")
        parsed_faqs = []
        for entry in faqs:
            if not isinstance(entry, dict):
                raise ProfileError("每条常见问题必须包含 question 和 answer")
            parsed_faqs.append(FAQ(_text(entry.get("question"), "常见问题", 500, required=True),
                                   _text(entry.get("answer"), "常见问题答案", 2000, required=True)))
        parsed.append(Product(
            id=item_id, name=_text(product.get("name"), "商品名称", 200, required=True), platform=platform,
            description=_text(product.get("description", ""), "商品描述"),
            price=price, min_price=minimum, bargain_enabled=bargain_enabled, stock=stock,
            shipping=_text(product.get("shipping", ""), "发货说明", 2000),
            returns=_text(product.get("returns", ""), "售后说明", 2000),
            keywords=_string_list(product.get("keywords", []), "商品关键词"), faq=parsed_faqs))
    profile = MerchantProfile(
        merchant_name=_text(data.get("merchant_name", "我的店铺"), "店铺名称", 200, required=True),
        tone=_text(data.get("tone", "友好、简洁"), "回复语气", 500), products=parsed, reply_mode=reply_mode,
        handoff_keywords=_string_list(data.get("handoff_keywords", ["人工", "投诉", "退款", "退货", "维权"]), "转人工关键词"),
        custom_instructions=_text(data.get("custom_instructions", ""), "自定义要求", 10000))
    return asdict(profile)


def default_profile() -> dict:
    """A fresh no-setup profile; never copy example or merchant product facts."""
    return validate_profile({"merchant_name": "我的店铺", "reply_mode": "default", "products": []})


_CONDITION_LABELS = {
    "店铺": "merchant_name", "店铺名称": "merchant_name", "商家": "merchant_name",
    "语气": "tone", "回复语气": "tone", "商品": "name", "商品名称": "name",
    "商品id": "id", "商品编号": "id", "售价": "price", "价格": "price",
    "平台": "platform", "商品平台": "platform",
    "最低价": "min_price", "最低报价": "min_price", "底价": "min_price", "库存": "stock",
    "发货": "shipping", "发货说明": "shipping", "配送": "shipping",
    "售后": "returns", "售后说明": "returns", "退换货": "returns",
    "描述": "description", "商品描述": "description", "规格": "description",
    "关键词": "keywords", "商品关键词": "keywords", "转人工关键词": "handoff_keywords",
    "要求": "custom_instructions", "自定义要求": "custom_instructions",
    "议价": "bargain_enabled", "允许议价": "bargain_enabled", "是否议价": "bargain_enabled",
}


def build_profile(data: dict) -> dict:
    """Build from an existing profile, product object, form fields or labeled lines.

    Structured fields take priority. Unknown lines are retained verbatim as
    merchant instructions, never interpreted as product facts.
    """
    if not isinstance(data, dict):
        raise ProfileError("配置条件必须是对象")
    result = {k: v for k, v in data.items() if k in {"merchant_name", "tone", "handoff_keywords", "custom_instructions", "schema_version", "reply_mode"}}
    conditions = _text(data.get("conditions", ""), "配置条件", 12000)
    condition_product: dict[str, Any] = {}
    leftover = []
    forbid_bargain = False
    for line in conditions.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"(?:不议价|禁止议价|不接受议价|不讲价|谢绝议价)[。！!]?", line):
            forbid_bargain = True
            continue
        match = re.match(r"^([^:：]{1,20})\s*[:：]\s*(.+)$", line)
        key = _CONDITION_LABELS.get(match[1].strip().lower()) if match else None
        if not key:
            leftover.append(line)
            continue
        value = match[2].strip()
        if key == "bargain_enabled":
            if value in {"否", "禁止", "不允许", "不接受", "false", "不议价"}:
                forbid_bargain = True
                continue
            if value in {"是", "允许", "可以", "true"}:
                value = True
        if key in {"price", "min_price"}:
            value = re.sub(r"^[¥￥]\s*|\s*元$", "", value)
        if key in {"merchant_name", "tone", "handoff_keywords", "custom_instructions"}:
            result.setdefault(key, value)
        else:
            condition_product[key] = value
    if leftover:
        result["custom_instructions"] = "\n".join(filter(None, [result.get("custom_instructions", ""), *leftover]))
    if "products" in data:
        result["products"] = data["products"]
        if condition_product:
            raise ProfileError("商品列表与文字商品条件不能同时填写；请直接修改对应商品")
    else:
        product = dict(condition_product)
        if "product" in data:
            if not isinstance(data["product"], dict):
                raise ProfileError("product 必须是商品对象")
            product.update(data["product"])
        for key in {"platform", "price", "min_price", "bargain_enabled", "stock", "shipping", "returns", "description", "keywords", "faq"}:
            if key in data:
                product[key] = data[key]
        if "product_name" in data:
            product["name"] = data["product_name"]
        if "product_id" in data:
            product["id"] = data["product_id"]
        if product:
            product.setdefault("id", "product-1")
            result["products"] = [product]
        else:
            result["products"] = []
    if forbid_bargain:
        result["products"] = [{**product, "bargain_enabled": False} for product in result.get("products", [])]
    return validate_profile(result)


class ProfileStore:
    """Atomic local profile persistence; credentials aren't part of this schema."""
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        with self.path.open(encoding="utf-8") as handle:
            return validate_profile(json.load(handle))

    def save(self, profile: dict) -> dict:
        normalized = validate_profile(profile)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".profile-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return normalized
