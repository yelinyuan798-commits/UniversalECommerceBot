"""Private local AI settings and immutable, per-request model configuration."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
DEFAULT_TIMEOUT = 12.0


class AISettingsError(ValueError):
    """A static user-facing error. Never interpolate untrusted settings values."""


@dataclass(frozen=True)
class AISettings:
    enabled: bool = True
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT
    api_key: str = field(default="", repr=False)

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.api_key)


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def normalize_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or _has_control(value):
        raise AISettingsError("请填写有效的模型服务地址，不要包含控制字符")
    value = value.strip()
    if any(char.isspace() for char in value) or "\\" in value or _has_control(unquote(value)):
        raise AISettingsError("模型服务地址不能包含空白、反斜杠或控制字符")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except (ValueError, UnicodeError):
        raise AISettingsError("模型服务地址或端口无效") from None
    if parts.scheme.lower() not in {"https", "http"} or not host or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise AISettingsError("模型服务地址须使用 HTTPS，且不能包含用户名或密码")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise AISettingsError("模型服务地址不能包含查询参数或片段；请只填写接口基础地址")
    if port is not None and not 1 <= port <= 65535:
        raise AISettingsError("模型服务端口无效")
    host = host.lower()
    if "%" in host:
        raise AISettingsError("模型服务主机名不能包含编码字符或作用域标识")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise AISettingsError("模型服务主机名无效") from None
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.rstrip(".").split(".")):
            raise AISettingsError("模型服务主机名无效")
        # Keep the displayed host canonical to make origin comparison dependable.
        host = host.rstrip(".")
    if parts.scheme.lower() == "http" and not (host == "localhost" or (ip is not None and ip.is_loopback)):
        raise AISettingsError("远程模型服务必须使用 HTTPS；HTTP 仅允许本机回环地址")
    if ip is not None:
        host = ip.compressed
    netloc = f"[{host}]" if ":" in host else host
    default_port = 443 if parts.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


def _origin(base_url: str) -> tuple:
    parsed = urlsplit(base_url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _contains_secret(value: str, secret: str) -> bool:
    if not secret:
        return False
    # Public fields must not expose a known key, including percent-encoded
    # paths. Each changed decoding shortens the string, so this terminates.
    while True:
        if secret in value:
            return True
        decoded = unquote(value)
        if decoded == value:
            return False
        value = decoded


def _validated(data: dict) -> AISettings:
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AISettingsError("enabled 必须为 true 或 false")
    base_url = normalize_base_url(data.get("base_url", DEFAULT_BASE_URL))
    model = data.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or _has_control(model):
        raise AISettingsError("请填写有效的模型名称，不要包含控制字符")
    model = model.strip()
    timeout = data.get("timeout_seconds", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool):
        raise AISettingsError("连接超时必须是 1 至 60 秒之间的数字")
    try:
        timeout = float(timeout)
    except (ValueError, TypeError, OverflowError):
        raise AISettingsError("连接超时必须是 1 至 60 秒之间的数字") from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise AISettingsError("连接超时必须是 1 至 60 秒之间的数字")
    key = data.get("api_key", "")
    if not isinstance(key, str) or len(key) > 8192 or _has_control(key):
        raise AISettingsError("API 密钥格式无效，不能包含换行或控制字符")
    key = key.strip()
    if _contains_secret(base_url, key) or _contains_secret(model, key):
        raise AISettingsError("请勿把 API 密钥填写在服务地址或模型名称中")
    return AISettings(enabled, base_url, model, timeout, key)


def environment_settings() -> AISettings:
    """Compatibility with config/.env already loaded by the application's entrypoint."""
    try:
        timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT)))
        if not math.isfinite(timeout):
            raise ValueError()
        timeout = max(1.0, min(timeout, 60.0))
        return _validated({
            "enabled": os.getenv("LLM_ENABLED", "true").strip().lower() not in {"false", "0", "no"},
            "base_url": os.getenv("MODEL_BASE_URL", DEFAULT_BASE_URL),
            "model": os.getenv("MODEL_NAME", DEFAULT_MODEL),
            "timeout_seconds": timeout,
            "api_key": os.getenv("API_KEY", ""),
        })
    except (ValueError, TypeError, OverflowError):
        raise AISettingsError("环境中的 AI 配置无效，请在工作台重新填写服务地址、模型和密钥") from None


def public_settings(settings: AISettings, *, source: str | None = None) -> dict:
    settings = _validated(_private_dict(settings))
    data = {
        "enabled": settings.enabled, "base_url": settings.base_url, "model": settings.model,
        "timeout_seconds": settings.timeout_seconds, "has_api_key": bool(settings.api_key),
        "status": "disabled" if not settings.enabled else "enabled" if settings.api_key else "unconfigured",
    }
    if source:
        data["source"] = source
    return data


def _private_dict(settings: AISettings) -> dict:
    return {"enabled": settings.enabled, "base_url": settings.base_url, "model": settings.model,
            "timeout_seconds": settings.timeout_seconds, "api_key": settings.api_key}


class AiSettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> AISettings:
        with self._lock:
            if not self.path.exists():
                return environment_settings()
            try:
                if self.path.stat().st_size > 65536:
                    raise ValueError()
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError()
                return _validated(data)
            except (OSError, ValueError, UnicodeError, TypeError):
                raise AISettingsError("本地 AI 配置无法读取，请在工作台重新填写并保存") from None

    def public_state(self) -> dict:
        with self._lock:
            settings = self.load()
            return public_settings(settings, source="local" if self.path.exists() else "environment")

    def prepare(self, data: dict) -> AISettings:
        """Validate a draft without saving it or modifying process environment."""
        if not isinstance(data, dict):
            raise AISettingsError("AI 配置必须是对象")
        allowed = {"enabled", "base_url", "model", "timeout_seconds", "api_key", "clear_api_key"}
        if set(data) - allowed:
            raise AISettingsError("AI 配置包含不支持的字段")
        with self._lock:
            try:
                current = self.load()
            except AISettingsError:
                # Permit recovery from corrupt local/environment settings only
                # when the user supplies a complete fresh draft. Never retain an
                # unreadable key or silently combine it with a different origin.
                if not all(name in data for name in ("enabled", "base_url", "model", "timeout_seconds")) or not (data.get("api_key") or data.get("clear_api_key") is True):
                    raise
                current = AISettings()
            clear = data.get("clear_api_key", False)
            if not isinstance(clear, bool):
                raise AISettingsError("clear_api_key 必须为 true 或 false")
            provided_key = data.get("api_key", "")
            if not isinstance(provided_key, str):
                raise AISettingsError("API 密钥必须是文字")
            if _has_control(provided_key) or len(provided_key) > 8192:
                raise AISettingsError("API 密钥格式无效，不能包含换行或控制字符")
            provided_key = provided_key.strip()
            if clear and provided_key:
                raise AISettingsError("不能同时清除密钥和填写新密钥")
            values = _private_dict(current)
            values.update({key: value for key, value in data.items() if key in {"enabled", "base_url", "model", "timeout_seconds"}})
            values["api_key"] = "" if clear else provided_key or current.api_key
            prepared = _validated(values)
            if _contains_secret(prepared.base_url, current.api_key) or _contains_secret(prepared.model, current.api_key):
                raise AISettingsError("请勿把 API 密钥填写在服务地址或模型名称中")
            if current.api_key and not clear and not provided_key and _origin(current.base_url) != _origin(prepared.base_url):
                raise AISettingsError("服务地址已更换站点，请重新填写该服务的 API 密钥，或先清除原密钥")
            return prepared

    def save(self, data: dict) -> dict:
        with self._lock:
            prepared = self.prepare(data)
            temporary = None
            descriptor = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(prefix=".ai-settings-", suffix=".json", dir=self.path.parent)
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                else:
                    os.chmod(temporary, 0o600)
                handle = os.fdopen(descriptor, "w", encoding="utf-8")
                descriptor = None  # The file object now owns the descriptor.
                with handle:
                    json.dump(_private_dict(prepared), handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except OSError:
                raise AISettingsError("AI 配置保存失败，请检查本地文件夹权限") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
            return public_settings(prepared, source="local")


@asynccontextmanager
async def model_client(settings: AISettings):
    """All model calls are explicit, redirect-free, and independent of env proxies."""
    settings = _validated(_private_dict(settings))
    import httpx
    from openai import AsyncOpenAI
    async with httpx.AsyncClient(timeout=settings.timeout_seconds, follow_redirects=False, trust_env=False) as transport:
        async with AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url,
                               timeout=settings.timeout_seconds, max_retries=0, http_client=transport) as client:
            yield client


def _connection_error(exc: Exception) -> dict:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or name in {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}:
        code, message = "timeout", "连接超时，请检查服务地址、网络或适当增加超时时间"
    elif isinstance(status, int) and 300 <= status < 400:
        code, message = "redirect_blocked", "服务要求跳转，已停止连接；请填写最终的模型接口基础地址"
    elif status in {401, 403}:
        code, message = "auth_failed", "密钥无效或没有访问权限，请检查密钥和账户权限"
    elif status == 404:
        code, message = "not_found", "未找到接口或模型，请检查服务地址和模型名称"
    elif status == 429:
        code, message = "rate_limited", "服务请求受限，请检查额度或稍后再试"
    elif isinstance(status, int) and status >= 500:
        code, message = "service_unavailable", "模型服务暂时不可用，请稍后再试"
    elif isinstance(status, int) and status >= 400:
        code, message = "request_rejected", "服务未接受测试请求，请检查接口兼容性和模型名称"
    elif name in {"APIConnectionError", "ConnectError", "ConnectionError", "RemoteProtocolError"}:
        code, message = "connection_failed", "无法连接模型服务，请检查地址和网络"
    else:
        code, message = "invalid_response", "未收到有效的模型回复，请检查服务的接口兼容性"
    return {"ok": False, "code": code, "message": message}


async def test_connection(settings: AISettings) -> dict:
    """Probe a draft even when automatic AI use is disabled; no draft is saved."""
    try:
        # Callers can construct AISettings directly, so validate at this boundary.
        settings = _validated(_private_dict(settings))
    except (AISettingsError, AttributeError, TypeError):
        return {"ok": False, "code": "invalid_settings", "message": "AI 配置无效，请检查服务地址、模型名称和超时时间"}
    if not settings.api_key:
        return {"ok": False, "code": "missing_api_key", "message": "请先填写 API 密钥，再测试连接"}
    try:
        async with model_client(settings) as client:
            response = await asyncio.wait_for(client.chat.completions.create(
                model=settings.model,
                messages=[{"role": "user", "content": "Reply with OK."}],
                max_tokens=16,
            ), timeout=settings.timeout_seconds + 1)
            content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            return {"ok": False, "code": "invalid_response", "message": "服务已响应，但没有返回有效文本，请检查模型名称和接口兼容性"}
        return {"ok": True, "code": "ok", "message": "连接成功，已收到模型回复"}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _connection_error(exc)


# A production helper named test_connection should not become a pytest test when
# imported directly into a test module.
test_connection.__test__ = False
