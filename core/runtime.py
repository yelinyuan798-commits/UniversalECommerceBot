"""Manage the lifecycle of configured merchant connections."""
import asyncio
import importlib
import json
from pathlib import Path

from core.profiles import ProfileStore
from core.service import ServiceEngine
from core.store import StateStore
from core.platform_catalog import PLATFORMS

def read_platforms(root: Path) -> dict:
    defaults = json.loads((root / "config/platforms.json").read_text(encoding="utf-8"))
    local = root / "config/platforms.local.json"
    if local.exists():
        overrides = json.loads(local.read_text(encoding="utf-8"))
        validate_platforms(overrides)
        defaults["platforms"].update(overrides["platforms"])
    return defaults


def validate_platforms(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
        raise ValueError("需要 platforms 配置对象")
    for key, cfg in data["platforms"].items():
        if key not in PLATFORMS or not isinstance(cfg, dict):
            raise ValueError(f"不支持的平台：{key}")
        if cfg.get("class", PLATFORMS[key]) != PLATFORMS[key]:
            raise ValueError("平台适配器必须使用内置实现")
        cfg["class"] = PLATFORMS[key]
        if not isinstance(cfg.get("config", {}), dict):
            raise ValueError("config 必须是对象")
        for flag in ("enabled", "dry_run"):
            if flag in cfg and not isinstance(cfg[flag], bool):
                raise ValueError(f"{flag} 必须为布尔值")
        if not isinstance(cfg.get("shop_id"), str) or not cfg["shop_id"].strip():
            raise ValueError("每个平台必须设置 shop_id")
    return data


class RuntimeManager:
    def __init__(self, root):
        self.root = Path(root)
        self.tasks = {}
        self.adapters = {}
        self.errors = {}
        self.engine = None
        self.mode = "stopped"
        self.lock = asyncio.Lock()

    @property
    def running(self):
        return any(not task.done() for task in self.tasks.values())

    def state(self):
        return {"running": self.running, "mode": self.mode, "platforms": {
            key: {"status": getattr(adapter, "status", "running" if self.running else "stopped"),
                  "error": self.errors.get(key) or getattr(adapter, "last_error", ""),
                  "active": key in self.tasks and not self.tasks[key].done()}
            for key, adapter in self.adapters.items()}}

    async def start(self, live=False):
        async with self.lock:
            if self.running:
                raise ValueError("机器人正在运行，请先停止")
            profile_path = self.root / "config/merchant.json"
            if not profile_path.exists():
                raise ValueError("请先保存自己的商品配置，再连接店铺")
            profile = ProfileStore(profile_path).load()
            configs = validate_platforms(read_platforms(self.root))["platforms"]
            enabled = {k: dict(v) for k, v in configs.items() if v.get("enabled")}
            if not enabled:
                raise ValueError("尚未启用任何平台，请在接入配置中启用")
            if live and all(cfg.get("dry_run", True) for cfg in enabled.values()):
                raise ValueError("所有平台仍处于观察保护；请在目标平台配置中设置 dry_run 为 false")
            adapters = {}
            for key, cfg in enabled.items():
                cfg["dry_run"] = not live or cfg.get("dry_run", True)
                module, cls = PLATFORMS[key].rsplit(".", 1)
                adapter = getattr(importlib.import_module(module), cls)(cfg)
                if hasattr(adapter, "check_config"):
                    errors = adapter.check_config()
                    if errors:
                        raise ValueError(f"{cfg.get('name', key)}：{'；'.join(errors)}")
                adapters[key] = adapter
            # Preview and live event ledgers are deliberately separate.
            if self.engine:
                self.engine.store.close()
            self.engine = ServiceEngine(profile, StateStore(self.root / "data" / ("live.db" if live else "observe.db")))
            self.adapters, self.errors, self.tasks = adapters, {}, {}
            self.mode = "live" if live else "observe"
            for key, adapter in adapters.items():
                adapter.on_delivery = self._delivery
                self.tasks[key] = asyncio.create_task(self._run(key, adapter), name=f"merchant-{key}")
            return self.state()

    async def _delivery(self, event_key, status, error=""):
        if status == "sent":
            self.engine.mark_sent(event_key)
        elif status in ("failed", "unknown"):
            self.engine.mark_failed(event_key, error or status)

    async def _run(self, key, adapter):
        try:
            await adapter.start(self.engine.reply)
            if getattr(adapter, "last_error", ""):
                self.errors[key] = adapter.last_error
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Exception strings from network libraries can contain auth URLs.
            self.errors[key] = f"连接中止（{type(exc).__name__}），请检查本机登录及接入配置"
        finally:
            final_status = getattr(adapter, "status", "stopped")
            await adapter.disconnect()
            if key in self.errors:
                adapter.status = final_status if final_status != "stopped" else "needs_attention"

    async def stop(self):
        async with self.lock:
            for task in self.tasks.values():
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            self.mode = "stopped"
            return self.state()
