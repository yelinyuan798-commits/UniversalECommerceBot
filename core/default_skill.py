"""Built-in, deterministic reception for merchants with no product configuration.

This is application logic, not an external Codex skill. It never reads a product
page, invokes a model, quotes prices, or treats a buyer's text as order evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class SkillReply:
    reply: str
    action: str
    intent: str


_HANDOFF_REPLY = "这个问题需要店主确认，已加入人工待处理列表。请稍等，具体结果以店主回复为准。"

# Payment and order actions take precedence over words such as "价格" or "发货".
# These triggers only open a local handoff; they never authorize a transaction.
_HANDOFF_WORDS = (
    "售后", "退款", "退货", "退钱", "换货", "退换", "维修", "保修", "质保",
    "投诉", "人工", "维权", "付款", "支付", "付钱", "付了", "付过", "付完", "付好",
    "已付", "打款", "转账", "转帐", "扣款", "扣钱", "收款", "到账", "交钱", "交了钱",
    "交过钱", "钱付", "钱转", "钱已", "下单了", "已下单", "拍了", "拍好了", "已拍",
    "改价", "调价", "改价格", "改下价", "改金额", "改运费", "改订单", "修改订单", "取消", "撤销", "关单",
    "关闭订单", "改地址", "修改地址", "更换地址", "收货地址", "改收件人", "改手机",
    "改规格", "改款", "改颜色", "改尺码", "改数量", "催单", "催发货", "催一下",
    "还不发货", "还没发货", "未发货", "没发货", "还没有发货", "怎么没发", "怎么还没",
    "怎么还没有", "一直不发", "迟迟不发", "到底什么时候发", "催", "发货了吗", "发货了没",
    "发货没", "发了吗", "发了没", "我的订单", "这笔订单", "这单", "我的物流",
    "查单", "查订单", "查物流", "到哪了", "到哪里了",
    "订单状态", "订单号", "物流状态", "签收", "补发", "漏发", "错发", "破损", "坏了",
    "没收到", "未收到", "少发", "补差", "差价",
    "refund", "complaint", "human", "cancel", "paid", "payment",
)
_ORDER_EDIT = re.compile(
    r"(?:修改|改|调整|变更).{0,8}(?:价格|价|金额|运费|地址|订单|数量|颜色|规格|尺码|收件人|手机)"
    r"|(?:价格|金额|运费|订单|地址|收件人|手机号|数量|颜色|规格|尺码).{0,8}(?:修改|改|调整|变更|取消)"
)


def skill_metadata() -> dict:
    """Fresh, credential-free metadata for the merchant console."""
    return {
        "id": "builtin-default-customer-service-v1",
        "name": "默认客服",
        "description": "不用先填商品资料，可接待问候、询价、发货和库存咨询；具体商品问题、订单和售后交给店主确认。",
        "capabilities": [
            "回复问候和感谢",
            "价格、库存、发货和运费引导查看商品或订单页面",
            "没有店主授权时不自行报价或承诺折扣",
            "具体商品问题、付款、改订单和售后加入人工待办，并暂停该会话自动回复",
        ],
        "limitations": [
            "不会读取商品页面的实时价格或库存",
            "不会承诺发货时间、自动发货、核验付款或修改订单",
            "不需要 AI 密钥；不使用已保存商品的价格和资料",
        ],
    }


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


def _contains(text: str, words: Iterable[str]) -> bool:
    return any(_normalize(word) in text for word in words if _normalize(word))


def reply_default(text: str, *, handoff_keywords: Iterable[str] = ()) -> SkillReply:
    """Return a generic answer without accepting product or transaction facts.

    Concrete product questions ask for a link and immediately enter the human
    queue. A paused conversation is handled by ServiceEngine, so this request
    cannot turn into an endless loop of asking the buyer for the same details.
    """
    normalized = _normalize(text)
    if _contains(normalized, (*_HANDOFF_WORDS, *handoff_keywords)) or _ORDER_EDIT.search(normalized):
        return SkillReply(_HANDOFF_REPLY, "handoff", "default_human_requested")
    if _contains(normalized, ("便宜", "优惠", "砍价", "少点", "最低", "折扣", "议价", "打折", "降价")):
        return SkillReply("我这里没有店主授权的优惠价格，暂时不能承诺折扣。请先以商品页面的价格为准。",
                          "reply", "default_bargain")
    if _contains(normalized, ("多少钱", "价格", "售价", "报价", "什么价", "价钱", "标价", "单价", "多钱", "多少米", "多少元")):
        return SkillReply("价格请以商品页面和下单页面显示的金额为准，我这里暂时没有这件商品的报价信息。",
                          "reply", "default_price")
    if _contains(normalized, ("发货", "什么时候发", "啥时候发", "几天发", "包邮", "邮费", "快递", "多久到", "几天到", "配送", "运费", "物流")):
        return SkillReply("发货时间和运费请先查看商品页面的说明；已下单的话，请查看订单页面。具体时效和费用需要店主确认。",
                          "reply", "default_shipping")
    if _contains(normalized, ("库存", "有货", "还有吗", "还在吗", "现货", "售罄", "缺货", "卖完")):
        return SkillReply("库存请以商品页面实时显示为准，我这里无法确认是否有货。", "reply", "default_stock")
    if normalized in {"你好", "您好", "在吗", "有人吗", "有人在吗", "老板在吗", "老板你好", "你好呀", "您好呀", "在不", "hello", "hi"}:
        return SkillReply("您好，在的。您想了解商品的哪方面？", "reply", "default_greeting")
    if normalized in {"谢谢", "感谢", "好的", "好", "谢谢老板", "谢谢啦", "谢谢你", "ok", "thanks", "thankyou"}:
        return SkillReply("不客气，有其他问题可以继续留言。", "reply", "default_thanks")
    if _contains(normalized, ("介绍", "商品信息", "商品描述", "参数", "规格", "尺寸", "型号", "功能", "材质", "支持", "适合", "兼容", "颜色", "尺码")):
        return SkillReply("请发一下商品链接和想了解的具体规格或问题，已加入人工待处理列表，商品细节由店主确认。",
                          "handoff", "default_product_details")
    return SkillReply("这个问题需要店主确认。请补充商品链接和具体问题，已加入人工待处理列表。",
                      "handoff", "default_knowledge_missing")
