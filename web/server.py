"""Loopback-only merchant setup and conversation rehearsal UI."""
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.profiles import ProfileStore, build_profile
from core.runtime import RuntimeManager, read_platforms, read_profile, validate_platforms
from core.service import ServiceEngine
from core.store import StateStore
from core.platform_catalog import platform_metadata
from core.default_skill import skill_metadata
from core.ai_settings import AiSettingsStore, AISettingsError, test_connection

ROOT = Path(__file__).resolve().parents[1]


def create_app(root=ROOT):
    root = Path(root)
    runtime = RuntimeManager(root)
    ai_store = AiSettingsStore(root / "config/ai.local.json")
    ai_save_lock = asyncio.Lock()
    ai_test_lock = asyncio.Lock()
    profile_store = ProfileStore(root / "config/merchant.json")
    preview_store = StateStore(root / "data/preview.db")

    def profile():
        return read_profile(root)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await runtime.stop()
            if runtime.engine:
                runtime.engine.store.close()
            preview_store.close()

    app = FastAPI(title="商家客服工作台", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runtime = runtime

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        cache_headers = {"Cache-Control": "no-store" if request.url.path.startswith("/api/") else "no-cache"}
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "仅允许本机访问"}, 403, headers=cache_headers)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "不允许跨站操作"}, 403, headers=cache_headers)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("x-requested-with") != "merchant-console":
                return JSONResponse({"detail": "请从本地工作台提交"}, 403, headers=cache_headers)
            try:
                content_length = int(request.headers.get("content-length", 0))
            except ValueError:
                return JSONResponse({"detail": "无效的请求长度"}, 400, headers=cache_headers)
            if content_length > 262144 or len(await request.body()) > 262144:
                return JSONResponse({"detail": "配置过大，请限制在 256 KB 内"}, 413, headers=cache_headers)
        response = await call_next(request)
        response.headers["Cache-Control"] = cache_headers["Cache-Control"]
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # FastAPI normally includes the invalid input in its response. An AI
        # settings body may contain a credential, even when its shape is wrong.
        if request.url.path.startswith("/api/ai-settings"):
            return JSONResponse({"detail": "AI 配置格式无效，请检查填写内容后重试"}, 400)
        return await request_validation_exception_handler(request, exc)

    @app.get("/")
    async def home():
        return FileResponse(ROOT / "web/static/index.html")

    @app.get("/api/state")
    async def state():
        current_profile = profile()
        try:
            ai_status = ai_store.public_state()["status"]
        except AISettingsError:
            ai_status = "unconfigured"
        # Configuration state only, not a claim that an external API is healthy.
        ai_configured = ai_status == "enabled"
        return {**runtime.state(), "profile_saved": (root / "config/merchant.json").exists(),
                "ai_configured": ai_configured, "ai_status": ai_status,
                "profile": current_profile,
                "reply_mode": current_profile["reply_mode"], "default_skill": skill_metadata(),
                "connections": read_platforms(root), "catalog": platform_metadata()}

    @app.get("/api/ai-settings")
    async def get_ai_settings():
        return ai_store.public_state()

    @app.put("/api/ai-settings")
    async def save_ai_settings(data: dict):
        async with ai_save_lock:
            saved = ai_store.save(data)
        return {"saved": True, **saved}

    @app.post("/api/ai-settings/test")
    async def probe_ai_settings(data: dict):
        if ai_test_lock.locked():
            raise HTTPException(429, "正在测试连接，请稍后再试")
        async with ai_test_lock:
            candidate = ai_store.prepare(data)
            started = time.monotonic()
            result = await test_connection(candidate)
            response = {"ok": result["ok"], "message": result["message"],
                        "latency_ms": round((time.monotonic() - started) * 1000)}
            if not result["ok"]:
                response["error_code"] = result["code"]
            return response

    @app.get("/api/profile/example")
    async def example_profile():
        # Explicit rehearsal material only; reading it must not activate it.
        return ProfileStore(ROOT / "config/merchant.example.json").load()

    @app.put("/api/reply-mode")
    async def set_reply_mode(data: dict):
        mode = data.get("mode")
        if not isinstance(mode, str) or mode not in {"default", "custom"}:
            raise ValueError("客服模式请选择 default 或 custom")
        async with runtime.lock:
            if runtime.running:
                raise ValueError("请先停止机器人再切换客服模式")
            current_profile = profile()
            current_profile["reply_mode"] = mode
            saved = profile_store.save(current_profile)
        return {"saved": True, "reply_mode": mode, "profile": saved}

    @app.post("/api/profile/generate")
    async def generate(data: dict):
        return build_profile(data)

    @app.put("/api/profile")
    async def save_profile(data: dict):
        async with runtime.lock:
            if runtime.running:
                raise ValueError("请先停止机器人再修改商品规则")
            saved = profile_store.save(data)
        return {"saved": True, "profile": saved}

    @app.put("/api/connections")
    async def connections(data: dict):
        async with runtime.lock:
            if runtime.running:
                raise ValueError("请先停止机器人再修改接入配置")
            data = validate_platforms(data)
            target = root / "config/platforms.local.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(target)
        return {"saved": True}

    @app.post("/api/preview")
    async def preview(data: dict):
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("请输入 1 至 4000 字的试聊消息")
        session = str(data.get("session_id", "preview"))[:100]
        current_profile = profile()
        engine = ServiceEngine(current_profile, preview_store, settings_provider=ai_store.load)
        result = await engine.reply({"platform": str(data.get("platform", "xianyu")),
            "shop_id": "local-preview", "user_id": "test-buyer", "conversation_id": session,
            "message_id": str(uuid.uuid4()), "item_id": str(data.get("item_id", "")), "text": text})
        if result.get("reply"):
            engine.mark_sent(result["event_key"])
        result["simulated"] = True
        result["reply_mode"] = current_profile["reply_mode"]
        return result

    @app.post("/api/start")
    async def start(data: dict):
        if data.get("mode") not in {"observe", "live"}:
            raise ValueError("请选择观察或自动回复模式")
        return await runtime.start(live=data["mode"] == "live")

    @app.post("/api/stop")
    async def stop():
        return await runtime.stop()

    @app.get("/api/events")
    async def events():
        store = runtime.engine.store if runtime.engine else StateStore(root / "data/live.db")
        try:
            return {"events": store.list_events(limit=100), "handoffs": store.list_handoffs()}
        finally:
            if not runtime.engine:
                store.close()

    @app.post("/api/handoff")
    async def handoff(data: dict):
        required = ("platform", "shop_id", "conversation_id")
        if not all(isinstance(data.get(k), str) and data[k] for k in required):
            raise ValueError("需要平台、店铺和会话标识")
        if not isinstance(data.get("paused"), bool):
            raise ValueError("paused 必须为布尔值")
        engine = runtime.engine or ServiceEngine(profile(), StateStore(root / "data/live.db"))
        try:
            engine.set_handoff(*(data[k] for k in required), paused=data["paused"])
        finally:
            if not runtime.engine:
                engine.store.close()
        return {"saved": True}

    # 与项目介绍共用公开图片；离线工作台只提供这个图片目录。
    app.mount("/guide-images", StaticFiles(directory=ROOT / "docs/images"), name="guide-images")
    app.mount("/static", StaticFiles(directory=ROOT / "web/static"), name="static")
    return app
