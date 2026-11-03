#!/usr/bin/env python3
"""Start the local console, run checks, or connect configured merchants."""
import argparse
import asyncio
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def doctor():
    from core.runtime import PLATFORMS, read_platforms, read_profile, validate_platforms
    import importlib
    checks = []
    profile = read_profile(ROOT)
    checks.append({"check": "客服模式", "ok": True, "reply_mode": profile["reply_mode"],
                   "using_builtin_default": not (ROOT / "config/merchant.json").exists()})
    data = validate_platforms(read_platforms(ROOT))
    for key, cfg in data["platforms"].items():
        module, name = PLATFORMS[key].rsplit(".", 1)
        cls = getattr(importlib.import_module(module), name)
        checks.append({"check": f"{key} 适配器可导入", "ok": True})
        if cfg.get("enabled"):
            adapter = cls(cfg)
            errors = adapter.check_config() if hasattr(adapter, "check_config") else []
            checks.append({"check": f"{key} 接入配置", "ok": not errors, "details": errors})
    print(json.dumps({"checks": checks, "note": "检查配置不代表真实平台已联调成功"}, ensure_ascii=False, indent=2))
    return 0 if all(c["ok"] for c in checks) else 1


async def run(live):
    from core.runtime import RuntimeManager
    manager = RuntimeManager(ROOT)
    try:
        print(json.dumps(await manager.start(live=live), ensure_ascii=False))
        await asyncio.gather(*manager.tasks.values())
        if manager.errors:
            raise RuntimeError("平台连接中止，请检查本机登录及接入配置")
    finally:
        await manager.stop()


def main():
    os.chdir(ROOT)
    from dotenv import load_dotenv
    load_dotenv(ROOT / "config/.env")
    parser = argparse.ArgumentParser(description="通用电商客服：商品配置、试聊与店铺接入")
    sub = parser.add_subparsers(dest="command")
    console = sub.add_parser("console", help="打开本地工作台（默认）")
    console.add_argument("--port", type=int, default=8765)
    sub.add_parser("doctor", help="检查配置和依赖，不连接真实店铺")
    runner = sub.add_parser("run", help="连接已启用的平台，默认只观察")
    runner.add_argument("--live", action="store_true", help="允许向真实买家自动回复")
    args = parser.parse_args()
    if args.command == "doctor":
        return doctor()
    if args.command == "run":
        asyncio.run(run(args.live))
        return 0
    import uvicorn
    from web.server import create_app
    port = getattr(args, "port", 8765)
    print(f"商家工作台：http://127.0.0.1:{port}（Ctrl+C 停止）")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, access_log=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
