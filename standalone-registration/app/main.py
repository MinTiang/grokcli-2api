"""Standalone registration Web + API service.

Public surface:
  GET  /                  Web UI
  GET  /health            Liveness
  POST /api/login         Session cookie
  GET  /api/config        Read durable config (secrets masked)
  PUT  /api/config        Update durable config
  POST /api/push/test     Test remote grokcli-2api connectivity
  POST /api/register      Start registration batch/session
  GET  /api/sessions      List sessions
  GET  /api/sessions/{id} Session detail
  POST /api/sessions/{id}/stop
  GET  /api/batches/{id}  Batch detail
  POST /api/batches/{id}/stop
  POST /api/batches/{id}/resume
  POST /api/stop          Stop all
  GET  /api/availability  Registration readiness
  GET  /api/accounts      Local cache of registered accounts (file store)
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Make parent repo packages importable when running from this directory ──
ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent  # grokcli-2api root when nested; or set STANDALONE_VENDOR_ROOT
VENDOR = Path(os.environ.get("STANDALONE_VENDOR_ROOT") or REPO_ROOT).resolve()
for p in (str(VENDOR), str(VENDOR / "grok-build-auth"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Data dir + file-mode store BEFORE any grok2api import (config reads env at import time)
os.environ["GROK2API_DATA_DIR"] = str(
    Path(os.environ.get("GROK2API_DATA_DIR") or (ROOT / "data")).resolve()
)
os.environ["GROK2API_STORE_BACKEND"] = "file"
os.environ["GROK2API_REG_SKIP_PROBE"] = os.environ.get("GROK2API_REG_SKIP_PROBE") or "1"
# Prevent hybrid defaults from trying localhost PostgreSQL/Redis
os.environ.setdefault("GROK2API_DATABASE_URL", "")
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("GROK2API_REQUIRE_SHARED_STORES", "0")
os.environ.setdefault("PYTHONPATH", f"{VENDOR}:{VENDOR / 'grok-build-auth'}")

from app.config_store import (  # noqa: E402
    active_mail_api_key,
    active_mail_base_url,
    active_mail_domain,
    apply_to_environ,
    load_config,
    mask_secrets,
    save_config,
)
from app.hooks import install_hooks  # noqa: E402
from app.push import test_push_connection  # noqa: E402

# Apply env from durable config early
apply_to_environ(load_config())
install_hooks()

app = FastAPI(title="Standalone Grok Registration", version="1.0.0")

STATIC_DIR = ROOT / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# In-memory session tokens for Web UI (password login)
_sessions: dict[str, float] = {}
_SESSION_TTL = 7 * 24 * 3600


def _jsonable(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str) or k.startswith("_") or callable(v):
                continue
            out[k] = _jsonable(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth=depth + 1) for v in value]
    try:
        import json

        json.dumps(value)
        return value
    except Exception:
        return str(value)


def _adapter():
    try:
        from grok2api.upstream import grok_build_adapter as reg
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail=f"registration adapter unavailable: {exc}"
        ) from exc
    return reg


def _cfg() -> dict[str, Any]:
    return load_config()


def _check_auth(request: Request) -> None:
    """Accept session cookie, Authorization Bearer (api_token), or X-API-Token."""
    cfg = _cfg()
    expected = str(cfg.get("api_token") or "").strip()
    password = str(cfg.get("ui_password") or "").strip()

    # Bearer / X-API-Token
    auth = (request.headers.get("authorization") or "").strip()
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = (request.headers.get("x-api-token") or "").strip()
    if token and expected and secrets.compare_digest(token, expected):
        return
    # Also allow ui password as bearer for simple scripts
    if token and password and secrets.compare_digest(token, password):
        return

    # Session cookie
    cookie = (request.cookies.get("reg_session") or "").strip()
    if cookie and cookie in _sessions:
        if time.time() - _sessions[cookie] < _SESSION_TTL:
            _sessions[cookie] = time.time()
            return
        _sessions.pop(cookie, None)

    raise HTTPException(status_code=401, detail="unauthorized")


def require_auth(fn: Callable):
    async def wrapper(request: Request, *args: Any, **kwargs: Any):
        _check_auth(request)
        return await fn(request, *args, **kwargs) if _is_async(fn) else fn(request, *args, **kwargs)

    wrapper.__name__ = getattr(fn, "__name__", "wrapped")
    return wrapper


def _is_async(fn: Callable) -> bool:
    import asyncio

    return asyncio.iscoroutinefunction(fn)


# ── Public ──────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, Any]:
    ok_adapter = True
    adapter_err = None
    avail = None
    try:
        reg = _adapter()
        avail = reg.registration_available()
    except Exception as exc:  # noqa: BLE001
        ok_adapter = False
        adapter_err = str(exc)[:300]
    cfg = _cfg()
    return {
        "ok": True,
        "service": "standalone-registration",
        "adapter_ok": ok_adapter,
        "adapter_error": adapter_err,
        "availability": avail,
        "auto_push": bool(cfg.get("grokcli2api_auto_push")),
        "push_url_configured": bool(cfg.get("grokcli2api_admin_url")),
        "mail_provider": cfg.get("mail_provider"),
        "captcha_provider": cfg.get("captcha_provider"),
    }


@app.get("/")
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(str(index_path))


@app.post("/api/login")
async def login(request: Request, response: Response) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = str((body or {}).get("password") or "").strip()
    cfg = _cfg()
    expected = str(cfg.get("ui_password") or "").strip()
    if not expected or not secrets.compare_digest(password, expected):
        raise HTTPException(status_code=401, detail="密码错误")
    sid = secrets.token_urlsafe(24)
    _sessions[sid] = time.time()
    response.set_cookie(
        "reg_session",
        sid,
        httponly=True,
        samesite="lax",
        max_age=_SESSION_TTL,
        path="/",
    )
    return {"ok": True, "message": "登录成功"}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    cookie = (request.cookies.get("reg_session") or "").strip()
    if cookie:
        _sessions.pop(cookie, None)
    response.delete_cookie("reg_session", path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    try:
        _check_auth(request)
        return {"ok": True, "authenticated": True}
    except HTTPException:
        return {"ok": True, "authenticated": False}


# ── Config ──────────────────────────────────────────────────────────────────


@app.get("/api/config")
def get_config(request: Request) -> dict[str, Any]:
    _check_auth(request)
    return {"ok": True, "config": mask_secrets(_cfg())}


@app.put("/api/config")
async def put_config(request: Request) -> dict[str, Any]:
    _check_auth(request)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be object")
    # Support nested {config: {...}}
    patch = body.get("config") if isinstance(body.get("config"), dict) else body
    try:
        saved = save_config(patch)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "config": mask_secrets(saved), "message": "配置已保存"}


@app.post("/api/push/test")
def push_test(request: Request) -> dict[str, Any]:
    _check_auth(request)
    return test_push_connection(_cfg())


# ── Registration ────────────────────────────────────────────────────────────


def _build_start_kwargs(body: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge request body with durable config for start_registration()."""
    mail = str(
        body.get("mail_provider") or cfg.get("mail_provider") or "yyds"
    ).strip().lower()

    # Resolve mail key / domain / base
    api_key = (
        body.get("moemail_api_key")
        or body.get("api_key")
        or body.get("yyds_api_key")
        or body.get("gptmail_api_key")
        or body.get("cfmail_api_key")
        or active_mail_api_key(cfg)
    )
    domain = body.get("domain") or active_mail_domain(cfg) or None
    base_url = body.get("moemail_base_url") or body.get("base_url") or active_mail_base_url(cfg) or None

    captcha = str(
        body.get("captcha_provider") or cfg.get("captcha_provider") or "local"
    ).strip().lower()
    yescaptcha_key = body.get("yescaptcha_key") or cfg.get("yescaptcha_key") or None
    if captcha == "local":
        yescaptcha_key = None

    def _int(name: str, default: int) -> int:
        raw = body.get(name, cfg.get(name, default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    kwargs: dict[str, Any] = {
        "captcha_provider": captcha,
        "local_solver_url": str(cfg.get("local_solver_url") or "http://127.0.0.1:5072"),
        "yescaptcha_key": yescaptcha_key or None,
        "proxy": body.get("proxy") if "proxy" in body else cfg.get("proxy") or None,
        "proxy_username": body.get("proxy_username")
        if "proxy_username" in body
        else cfg.get("proxy_username") or None,
        "proxy_password": body.get("proxy_password")
        if "proxy_password" in body
        else cfg.get("proxy_password") or None,
        "proxy_strategy": body.get("proxy_strategy")
        if "proxy_strategy" in body
        else cfg.get("proxy_strategy") or "round_robin",
        "moemail_api_key": api_key or None,
        "moemail_base_url": base_url or None,
        "domain": domain or None,
        "expiry_ms": _int("expiry_ms", int(cfg.get("expiry_ms") or 3600000)),
        "mail_provider": mail,
        "count": _int("count", int(cfg.get("count") or 1)),
        "concurrency": _int("concurrency", int(cfg.get("concurrency") or 3)),
        "stagger_ms": _int("stagger_ms", int(cfg.get("stagger_ms") or 300)),
        "probe_delay_sec": _int("probe_delay_sec", int(cfg.get("probe_delay_sec") or 0)),
    }
    # Drop Nones so adapter defaults still apply where intended
    return {k: v for k, v in kwargs.items() if v is not None and v != ""}


@app.get("/api/availability")
def availability(request: Request) -> dict[str, Any]:
    _check_auth(request)
    adapter = _adapter()
    return _jsonable(adapter.registration_available())


@app.post("/api/register")
async def start_register(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _check_auth(request)
    _ = idempotency_key
    adapter = _adapter()
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # Persist job-ish fields if client asks (default: yes for UI)
    if body.get("save_config", True):
        persist_keys = {
            "mail_provider",
            "captcha_provider",
            "proxy",
            "proxy_username",
            "proxy_password",
            "proxy_strategy",
            "count",
            "concurrency",
            "stagger_ms",
            "probe_delay_sec",
            "expiry_ms",
            "yescaptcha_key",
            "moemail_api_key",
            "yyds_api_key",
            "gptmail_api_key",
            "cfmail_api_key",
            "moemail_base_url",
            "moemail_domain",
            "yyds_domain",
            "gptmail_domain",
            "cfmail_domain",
            "cfmail_base_url",
            "domain",
            "base_url",
        }
        patch = {k: body[k] for k in persist_keys if k in body}
        # Map active fields into per-provider slots
        mail = str(body.get("mail_provider") or _cfg().get("mail_provider") or "yyds").lower()
        if body.get("api_key"):
            key_map = {
                "moemail": "moemail_api_key",
                "yyds": "yyds_api_key",
                "gptmail": "gptmail_api_key",
                "cfmail": "cfmail_api_key",
            }
            patch[key_map.get(mail, "moemail_api_key")] = body["api_key"]
        if body.get("domain") is not None:
            dmap = {
                "moemail": "moemail_domain",
                "yyds": "yyds_domain",
                "gptmail": "gptmail_domain",
                "cfmail": "cfmail_domain",
            }
            patch[dmap.get(mail, "moemail_domain")] = body["domain"]
        if body.get("base_url") is not None and mail in {"moemail", "cfmail"}:
            bmap = {"moemail": "moemail_base_url", "cfmail": "cfmail_base_url"}
            patch[bmap[mail]] = body["base_url"]
        if patch:
            try:
                save_config(patch)
            except Exception as exc:  # noqa: BLE001
                print(f"[register] WARN: save_config failed: {exc}")

    cfg = _cfg()
    apply_to_environ(cfg)
    kwargs = _build_start_kwargs(body, cfg)
    result = adapter.start_registration(**kwargs)
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="invalid registration response")
    if result.get("ok") is False:
        raise HTTPException(
            status_code=400, detail=str(result.get("error") or "registration failed")
        )
    return _jsonable(result)


@app.get("/api/sessions")
def list_sessions(request: Request) -> dict[str, Any]:
    _check_auth(request)
    adapter = _adapter()
    return _jsonable(adapter.list_registration_sessions())


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    _check_auth(request)
    adapter = _adapter()
    include_auth = (request.query_params.get("include_auth_json") or "").strip() in {
        "1",
        "true",
        "yes",
    }
    sess = adapter.get_registration_session(session_id, include_auth_json=include_auth)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    return _jsonable(sess)


@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: str, request: Request) -> dict[str, Any]:
    _check_auth(request)
    adapter = _adapter()
    return _jsonable(adapter.stop_registration_session(session_id))


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str, request: Request) -> dict[str, Any]:
    _check_auth(request)
    adapter = _adapter()
    batch = adapter.get_registration_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="batch not found")
    return _jsonable(batch)


@app.post("/api/batches/{batch_id}/stop")
def stop_batch(batch_id: str, request: Request) -> dict[str, Any]:
    _check_auth(request)
    adapter = _adapter()
    return _jsonable(adapter.stop_registration_batch(batch_id))


@app.post("/api/batches/{batch_id}/resume")
async def resume_batch(batch_id: str, request: Request) -> dict[str, Any]:
    _check_auth(request)
    adapter = _adapter()
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            force = bool(body.get("force"))
    except Exception:
        force = False
    return _jsonable(adapter.resume_registration_batch(batch_id, force=force))


@app.post("/api/stop")
def stop_all(request: Request) -> dict[str, Any]:
    _check_auth(request)
    adapter = _adapter()
    return _jsonable(adapter.stop_all_active_registrations())


@app.get("/api/accounts")
def list_local_accounts(request: Request) -> dict[str, Any]:
    """Accounts written to local file store during registration (cache)."""
    _check_auth(request)
    try:
        from grok2api.pool.accounts import list_accounts

        items = list_accounts() or []
        return {"ok": True, "accounts": _jsonable(items), "count": len(items)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "accounts": [], "count": 0}


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def main() -> None:
    import uvicorn

    host = os.environ.get("REG_UI_HOST") or os.environ.get("GROK2API_REGISTRATION_HOST") or "0.0.0.0"
    port = int(os.environ.get("REG_UI_PORT") or os.environ.get("GROK2API_REGISTRATION_PORT") or 8080)
    # Log bootstrap token once so operators can copy it
    cfg = load_config()
    print(f"[standalone-registration] UI http://{host}:{port}")
    print(f"[standalone-registration] api_token={cfg.get('api_token')}")
    print(
        f"[standalone-registration] push="
        f"{'on' if cfg.get('grokcli2api_auto_push') else 'off'} "
        f"url={cfg.get('grokcli2api_admin_url') or '(not set)'}"
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
