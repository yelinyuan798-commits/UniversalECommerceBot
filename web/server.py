"""Loopback-only merchant setup and conversation rehearsal UI."""
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.profiles import ProfileStore, build_profile
from core.runtime import RuntimeManager, read_platforms, validate_platforms
from core.service import ServiceEngine
from core.store import StateStore
from core.platform_catalog import platform_metadata

ROOT = Path(__file__).resolve().parents[1]


def create_app(root=ROOT):
    root = Path(root)
    runtime = RuntimeManager(root)
    profile_store = ProfileStore(root / "config/merchant.json")
    preview_store = StateStore(root / "data/preview.db")

    def profile():
        if (root / "config/merchant.json").exists():
            return profile_store.load()
        return ProfileStore(root / "config/merchant.example.json").load()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await runtime.stop()
        preview_store.close()

    app = FastAPI(title="商家客服工作台", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runtime = runtime

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "仅允许本机访问"}, 403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "不允许跨站操作"}, 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("x-requested-with") != "merchant-console":
                return JSONResponse({"detail": "请从本地工作台提交"}, 403)
            try:
                content_length = int(request.headers.get("content-length", 0))
            except ValueError:
                return JSONResponse({"detail": "无效的请求长度"}, 400)
            if content_length > 262144 or len(await request.body()) > 262144:
                return JSONResponse({"detail": "配置过大，请限制在 256 KB 内"}, 413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.get("/")
    async def home():
        return FileResponse(ROOT / "web/static/index.html")

    @app.get("/api/state")
    async def state():
        return {**runtime.state(), "profile_saved": (root / "config/merchant.json").exists(),
                "ai_configured": bool(os.getenv("API_KEY")), "profile": profile(),
                "connections": read_platforms(root), "catalog": platform_metadata()}

    @app.post("/api/profile/generate")
    async def generate(data: dict):
        return build_profile(data)

    @app.put("/api/profile")
    async def save_profile(data: dict):
        if runtime.running:
            raise ValueError("请先停止机器人再修改商品规则")
        profile_store.save(data)
        return {"saved": True, "profile": profile_store.load()}

    @app.put("/api/connections")
    async def connections(data: dict):
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
        engine = ServiceEngine(profile(), preview_store)
        result = await engine.reply({"platform": str(data.get("platform", "xianyu")),
            "shop_id": "local-preview", "user_id": "test-buyer", "conversation_id": session,
            "message_id": str(uuid.uuid4()), "item_id": str(data.get("item_id", "")), "text": text})
        if result.get("reply"):
            engine.mark_sent(result["event_key"])
        result["simulated"] = True
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

    app.mount("/static", StaticFiles(directory=ROOT / "web/static"), name="static")
    return app
