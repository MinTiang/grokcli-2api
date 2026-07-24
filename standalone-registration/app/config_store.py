"""JSON-backed durable config for the standalone registration service.

Priority (highest first):
  1. Runtime overrides from Web UI (data/config.json)
  2. Environment variables (.env / docker compose)
  3. Built-in defaults

Secrets (API keys, admin tokens) are masked when returned to the UI unless
``include_secrets=True``. Empty string on save means "keep previous secret".
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("GROK2API_DATA_DIR") or (ROOT / "data")).resolve()
CONFIG_PATH = DATA_DIR / "config.json"

_lock = threading.RLock()
_cache: dict[str, Any] | None = None

# Keys that must never be echoed back in full to the browser.
_SECRET_KEYS = {
    "ui_password",
    "api_token",
    "yescaptcha_key",
    "moemail_api_key",
    "yyds_api_key",
    "gptmail_api_key",
    "cfmail_api_key",
    "proxy_password",
    "grokcli2api_admin_token",
}


def _truthy(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _default_config() -> dict[str, Any]:
    """Seed config from environment (used on first boot / missing file)."""
    captcha = (
        os.environ.get("GROK2API_CAPTCHA_PROVIDER")
        or os.environ.get("CAPTCHA_PROVIDER")
        or "local"
    ).strip().lower()
    if captcha not in {"local", "yescaptcha"}:
        captcha = "local"

    mail = (
        os.environ.get("GROK2API_MAIL_PROVIDER")
        or os.environ.get("GROK2API_TEMPMAIL_PROVIDER")
        or os.environ.get("MAIL_PROVIDER")
        or "yyds"
    ).strip().lower() or "yyds"

    api_token = (os.environ.get("REG_API_TOKEN") or "").strip()
    if not api_token:
        api_token = secrets.token_urlsafe(24)

    return {
        # UI / API auth
        "ui_password": (os.environ.get("REG_UI_PASSWORD") or "change-me").strip(),
        "api_token": api_token,
        # Mail
        "mail_provider": mail,
        "moemail_base_url": (
            os.environ.get("GROK2API_MOEMAIL_BASE_URL") or "https://moemail.example.com"
        ).strip(),
        "moemail_api_key": (os.environ.get("GROK2API_MOEMAIL_API_KEY") or "").strip(),
        "moemail_domain": (os.environ.get("GROK2API_MOEMAIL_DOMAIN") or "").strip(),
        "yyds_api_key": (
            os.environ.get("GROK2API_YYDS_KEY")
            or os.environ.get("GROK2API_YYDS_API_KEY")
            or ""
        ).strip(),
        "yyds_domain": (os.environ.get("GROK2API_YYDS_DOMAIN") or "").strip(),
        "gptmail_api_key": (os.environ.get("GROK2API_GPTMAIL_API_KEY") or "").strip(),
        "gptmail_domain": (os.environ.get("GROK2API_GPTMAIL_DOMAIN") or "").strip(),
        "cfmail_api_key": (os.environ.get("GROK2API_CFMAIL_API_KEY") or "").strip(),
        "cfmail_base_url": (os.environ.get("GROK2API_CFMAIL_BASE_URL") or "").strip(),
        "cfmail_domain": (os.environ.get("GROK2API_CFMAIL_DOMAIN") or "").strip(),
        "expiry_ms": int(os.environ.get("GROK2API_MOEMAIL_EXPIRY_MS") or 3600000),
        # Captcha
        "captcha_provider": captcha,
        "local_solver_url": (
            os.environ.get("GROK2API_LOCAL_SOLVER_URL")
            or os.environ.get("LOCAL_SOLVER_URL")
            or "http://127.0.0.1:5072"
        ).strip().rstrip("/"),
        "yescaptcha_key": (
            os.environ.get("GROK2API_YESCAPTCHA_KEY")
            or os.environ.get("YESCAPTCHA_API_KEY")
            or ""
        ).strip(),
        # Proxy
        "proxy": (
            os.environ.get("GROK2API_XAI_PROXY_POOL")
            or os.environ.get("GROK2API_XAI_PROXY")
            or os.environ.get("GROK2API_PROXY")
            or ""
        ).strip(),
        "proxy_username": (
            os.environ.get("GROK2API_XAI_PROXY_USERNAME")
            or os.environ.get("GROK2API_PROXY_USERNAME")
            or ""
        ).strip(),
        "proxy_password": (
            os.environ.get("GROK2API_XAI_PROXY_PASSWORD")
            or os.environ.get("GROK2API_PROXY_PASSWORD")
            or ""
        ).strip(),
        "proxy_strategy": (
            os.environ.get("GROK2API_XAI_PROXY_STRATEGY")
            or os.environ.get("GROK2API_PROXY_STRATEGY")
            or "round_robin"
        ).strip().lower()
        or "round_robin",
        # Job defaults
        "count": 1,
        "concurrency": int(os.environ.get("GROK2API_REG_CONCURRENCY") or 3),
        "stagger_ms": 300,
        "probe_delay_sec": 0,
        # Push target
        "grokcli2api_admin_url": (
            os.environ.get("GROKCLI2API_ADMIN_URL") or ""
        ).strip().rstrip("/"),
        "grokcli2api_admin_token": (
            os.environ.get("GROKCLI2API_ADMIN_TOKEN") or ""
        ).strip(),
        "grokcli2api_auto_push": _truthy(
            os.environ.get("GROKCLI2API_AUTO_PUSH"), True
        ),
        "grokcli2api_push_source": (
            os.environ.get("GROKCLI2API_PUSH_SOURCE") or "standalone-registration"
        ).strip()
        or "standalone-registration",
    }


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_file() -> dict[str, Any] | None:
    if not CONFIG_PATH.is_file():
        return None
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_file(cfg: dict[str, Any]) -> None:
    _ensure_data_dir()
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    payload = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def load_config(*, force: bool = False) -> dict[str, Any]:
    """Return full config (secrets included). Thread-safe, cached."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return deepcopy(_cache)
        base = _default_config()
        file_cfg = _read_file()
        if file_cfg:
            for k, v in file_cfg.items():
                if k in _SECRET_KEYS and (v is None or v == ""):
                    # never wipe a secret with empty file value
                    continue
                base[k] = v
        # Env still wins for bootstrap secrets if file never set them
        if not base.get("api_token"):
            base["api_token"] = secrets.token_urlsafe(24)
        _cache = deepcopy(base)
        if not CONFIG_PATH.is_file():
            try:
                _write_file(base)
            except Exception as exc:  # noqa: BLE001
                print(f"[config] WARN: initial write failed: {exc}")
        return deepcopy(_cache)


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge *patch* into durable config. Empty secret strings keep previous."""
    global _cache
    if not isinstance(patch, dict):
        raise ValueError("patch must be object")
    with _lock:
        current = load_config(force=True)
        for k, v in patch.items():
            if k not in current and k not in _SECRET_KEYS and k not in _default_config():
                # allow unknown keys for forward-compat
                pass
            if k in _SECRET_KEYS:
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue  # keep previous
                current[k] = str(v).strip() if isinstance(v, str) else v
            else:
                current[k] = v
        # normalize a few fields
        if "captcha_provider" in current:
            p = str(current["captcha_provider"] or "local").strip().lower()
            current["captcha_provider"] = p if p in {"local", "yescaptcha"} else "local"
        if "mail_provider" in current:
            current["mail_provider"] = str(current["mail_provider"] or "yyds").strip().lower()
        if "grokcli2api_admin_url" in current:
            current["grokcli2api_admin_url"] = str(
                current.get("grokcli2api_admin_url") or ""
            ).strip().rstrip("/")
        if "grokcli2api_auto_push" in current:
            current["grokcli2api_auto_push"] = _truthy(current["grokcli2api_auto_push"], True)
        for int_key in ("count", "concurrency", "stagger_ms", "expiry_ms", "probe_delay_sec"):
            if int_key in current and current[int_key] is not None:
                try:
                    current[int_key] = int(current[int_key])
                except (TypeError, ValueError):
                    pass
        _write_file(current)
        _cache = deepcopy(current)
        apply_to_environ(current)
        return deepcopy(current)


def mask_secrets(cfg: dict[str, Any]) -> dict[str, Any]:
    """Public view for Web UI — secrets replaced with has_* flags + redacted."""
    out = deepcopy(cfg)
    for k in _SECRET_KEYS:
        val = out.get(k)
        has = bool(isinstance(val, str) and val.strip())
        out[f"has_{k}"] = has
        if has:
            out[k] = "********"
        else:
            out[k] = ""
    return out


def apply_to_environ(cfg: dict[str, Any] | None = None) -> None:
    """Mirror durable config into process env so grok_build_adapter picks it up."""
    c = cfg or load_config()
    mapping = {
        "GROK2API_CAPTCHA_PROVIDER": str(c.get("captcha_provider") or "local"),
        "CAPTCHA_PROVIDER": str(c.get("captcha_provider") or "local"),
        "GROK2API_LOCAL_SOLVER_URL": str(c.get("local_solver_url") or "http://127.0.0.1:5072"),
        "LOCAL_SOLVER_URL": str(c.get("local_solver_url") or "http://127.0.0.1:5072"),
        "GROK2API_YESCAPTCHA_KEY": str(c.get("yescaptcha_key") or ""),
        "YESCAPTCHA_API_KEY": str(c.get("yescaptcha_key") or ""),
        "GROK2API_MAIL_PROVIDER": str(c.get("mail_provider") or "yyds"),
        "MAIL_PROVIDER": str(c.get("mail_provider") or "yyds"),
        "GROK2API_MOEMAIL_BASE_URL": str(c.get("moemail_base_url") or ""),
        "GROK2API_MOEMAIL_API_KEY": str(c.get("moemail_api_key") or ""),
        "GROK2API_MOEMAIL_DOMAIN": str(c.get("moemail_domain") or ""),
        "GROK2API_YYDS_KEY": str(c.get("yyds_api_key") or ""),
        "GROK2API_YYDS_API_KEY": str(c.get("yyds_api_key") or ""),
        "GROK2API_YYDS_DOMAIN": str(c.get("yyds_domain") or ""),
        "GROK2API_GPTMAIL_API_KEY": str(c.get("gptmail_api_key") or ""),
        "GROK2API_GPTMAIL_DOMAIN": str(c.get("gptmail_domain") or ""),
        "GROK2API_CFMAIL_API_KEY": str(c.get("cfmail_api_key") or ""),
        "GROK2API_CFMAIL_BASE_URL": str(c.get("cfmail_base_url") or ""),
        "GROK2API_CFMAIL_DOMAIN": str(c.get("cfmail_domain") or ""),
        "GROK2API_XAI_PROXY_POOL": str(c.get("proxy") or ""),
        "GROK2API_XAI_PROXY": str(c.get("proxy") or "").splitlines()[0].strip()
        if str(c.get("proxy") or "").strip()
        else "",
        "GROK2API_XAI_PROXY_USERNAME": str(c.get("proxy_username") or ""),
        "GROK2API_XAI_PROXY_PASSWORD": str(c.get("proxy_password") or ""),
        "GROK2API_XAI_PROXY_STRATEGY": str(c.get("proxy_strategy") or "round_robin"),
        "GROK2API_REG_CONCURRENCY": str(c.get("concurrency") or 3),
        "GROK2API_REG_PROBE_DELAY_SEC": str(c.get("probe_delay_sec") or 0),
        "GROK2API_STORE_BACKEND": "file",
        "GROKCLI2API_ADMIN_URL": str(c.get("grokcli2api_admin_url") or ""),
        "GROKCLI2API_ADMIN_TOKEN": str(c.get("grokcli2api_admin_token") or ""),
        "GROKCLI2API_AUTO_PUSH": "true" if c.get("grokcli2api_auto_push") else "false",
        "GROKCLI2API_PUSH_SOURCE": str(
            c.get("grokcli2api_push_source") or "standalone-registration"
        ),
        "GROK2API_REG_SKIP_PROBE": os.environ.get("GROK2API_REG_SKIP_PROBE") or "1",
    }
    # Active mail key alias: adapter often reads MOEMAIL_API_KEY for any provider
    # when the request doesn't pass api_key explicitly. Prefer selected provider.
    mail = str(c.get("mail_provider") or "yyds").lower()
    if mail == "yyds" and c.get("yyds_api_key"):
        mapping["GROK2API_MOEMAIL_API_KEY"] = str(c.get("yyds_api_key"))
        mapping["MOEMAIL_API_KEY"] = str(c.get("yyds_api_key"))
    elif mail == "gptmail" and c.get("gptmail_api_key"):
        mapping["GROK2API_MOEMAIL_API_KEY"] = str(c.get("gptmail_api_key"))
        mapping["MOEMAIL_API_KEY"] = str(c.get("gptmail_api_key"))
    elif mail == "cfmail" and c.get("cfmail_api_key"):
        mapping["GROK2API_MOEMAIL_API_KEY"] = str(c.get("cfmail_api_key"))
        mapping["MOEMAIL_API_KEY"] = str(c.get("cfmail_api_key"))
    elif c.get("moemail_api_key"):
        mapping["MOEMAIL_API_KEY"] = str(c.get("moemail_api_key"))

    for k, v in mapping.items():
        if v is None:
            continue
        os.environ[k] = str(v)


def active_mail_api_key(cfg: dict[str, Any] | None = None) -> str:
    c = cfg or load_config()
    mail = str(c.get("mail_provider") or "yyds").lower()
    if mail == "yyds":
        return str(c.get("yyds_api_key") or "")
    if mail == "gptmail":
        return str(c.get("gptmail_api_key") or "")
    if mail == "cfmail":
        return str(c.get("cfmail_api_key") or "")
    return str(c.get("moemail_api_key") or "")


def active_mail_domain(cfg: dict[str, Any] | None = None) -> str:
    c = cfg or load_config()
    mail = str(c.get("mail_provider") or "yyds").lower()
    if mail == "yyds":
        return str(c.get("yyds_domain") or c.get("domain") or "")
    if mail == "gptmail":
        return str(c.get("gptmail_domain") or c.get("domain") or "")
    if mail == "cfmail":
        return str(c.get("cfmail_domain") or c.get("domain") or "")
    return str(c.get("moemail_domain") or c.get("domain") or "")


def active_mail_base_url(cfg: dict[str, Any] | None = None) -> str:
    c = cfg or load_config()
    mail = str(c.get("mail_provider") or "yyds").lower()
    if mail == "moemail":
        return str(c.get("moemail_base_url") or c.get("base_url") or "")
    if mail == "cfmail":
        return str(c.get("cfmail_base_url") or c.get("base_url") or "")
    return str(c.get("base_url") or "")
