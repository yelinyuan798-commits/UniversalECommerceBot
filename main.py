#!/usr/bin/env python
"""Universal E-Commerce Bot - 多平台 AI 自动客服系统"""
import os, sys, json, asyncio, importlib
from dotenv import load_dotenv
from loguru import logger
from core.agent import XianyuReplyBot
from core.context import ChatContextManager

load_dotenv("config/.env")
log_level = os.getenv("LOG_LEVEL", "INFO")
logger.remove()
logger.add(sys.stderr, level=log_level)
logger.add("data/bot.log", rotation="10 MB", level="INFO")

bot = XianyuReplyBot()
context = ChatContextManager(db_path="data/chat.db")

async def platform_message_handler(user_id, item_id, message, platform_name):
    """统一消息处理器：所有平台的消息都经过这里"""
    item_info = context.get_item_info(item_id)
    item_desc = f"【{platform_name}】商品 {item_id}"
    if item_info:
        item_data = json.loads(item_info) if isinstance(item_info, str) else item_info
        item_desc = f"当前商品信息: {json.dumps(item_data, ensure_ascii=False)}"
    chat_ctx = context.get_context_by_chat(f"{platform_name}:{user_id}") or []
    reply = bot.generate_reply(message, item_desc, context=chat_ctx)
    if reply == "-":
        return None
    context.add_message_by_chat(f"{platform_name}:{user_id}", user_id, item_id, "user", message)
    if reply:
        context.add_message_by_chat(f"{platform_name}:{user_id}", "assistant", item_id, "assistant", reply)
    return reply

async def main():
    logger.info("=" * 50)
    logger.info("  通用电商 AI 客服系统 启动中...")
    logger.info("=" * 50)
    config_path = "config/platforms.json"
    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在: {config_path}")
        return
    with open(config_path, encoding="utf-8") as f:
        platforms_config = json.load(f)
    tasks = []
    for name, cfg in platforms_config.get("platforms", {}).items():
        if not cfg.get("enabled"):
            continue
        try:
            module_path, class_name = cfg["class"].rsplit(".", 1)
            module = importlib.import_module(module_path)
            platform_class = getattr(module, class_name)
            platform = platform_class(cfg)
            tasks.append(asyncio.create_task(platform.start(
                lambda uid, iid, msg, pf=cfg["name"]: platform_message_handler(uid, iid, msg, pf)
            )))
            logger.info(f"  ✅ [{cfg['name']}] 已启动")
        except Exception as e:
            logger.error(f"  ❌ [{cfg.get('name', name)}] 启动失败: {e}")
    if not tasks:
        logger.warning("没有启用任何平台")
        return
    logger.info(f"\n共启动 {len(tasks)} 个平台，开始自动值守...\n")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n正在关闭所有平台...")
    except Exception as e:
        logger.error(f"系统异常: {e}")
