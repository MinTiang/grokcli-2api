"""Push registered accounts to a remote grokcli-2api instance.

Uses POST /admin/api/accounts/import with Bearer admin token.
Failures are recorded but never raised into the registration pipeline.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config_store import load_config

log = logging.getLogger("standalone.push")


def _normalize_account(acc: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Map local import_payload / auth entry into remote import shape."""
    out: dict[str, Any] = {}
    for k in (
        "key",
        "access_token",
        "token",
        "refresh_token",
        "email",
        "expires_at",
        "auth_mode",
        "oidc_issuer",
        "oidc_client_id",
        "sso",
        "sso_cookie",
        "password",
        "register_password",
        "user_id",
        "principal_id",
        "team_id",
        "first_name",
        "last_name",
        "registration_session_id",
        "registration_batch_id",
    ):
        if k in acc and acc[k] not in (None, ""):
            out[k] = acc[k]
    # Prefer key / access_token alias
    if "key" not in out:
        tok = out.get("access_token") or out.get("token")
        if tok:
            out["key"] = tok
    if "access_token" not in out and out.get("key"):
        out["access_token"] = out["key"]
    out["source"] = source or "standalone-registration"
    return out


def push_accounts(
    accounts: list[dict[str, Any]],
    *,
    cfg: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Synchronously push account dicts to remote grokcli-2api.

    Returns a result dict always (never raises).
    """
    c = cfg or load_config()
    if not c.get("grokcli2api_auto_push"):
        return {"ok": True, "skipped": True, "reason": "auto_push disabled", "total": len(accounts or [])}

    base = str(c.get("grokcli2api_admin_url") or "").strip().rstrip("/")
    token = str(c.get("grokcli2api_admin_token") or "").strip()
    source = str(c.get("grokcli2api_push_source") or "standalone-registration")
    if not base or not token:
        return {
            "ok": False,
            "skipped": True,
            "reason": "grokcli2api URL or token not configured",
            "total": len(accounts or []),
        }
    if not accounts:
        return {"ok": True, "skipped": True, "reason": "no accounts", "total": 0}

    payload_accounts = [
        _normalize_account(a, source=source) for a in accounts if isinstance(a, dict)
    ]
    # Filter empties (must have some token)
    payload_accounts = [
        a
        for a in payload_accounts
        if a.get("key") or a.get("access_token") or a.get("refresh_token") or a.get("sso")
    ]
    if not payload_accounts:
        return {"ok": False, "skipped": False, "error": "no pushable fields", "total": 0}

    # Remote API accepts bare auth object / list / {payload: ...} / {accounts: ...}
    body: dict[str, Any] = {
        "merge": True,
        "payload": payload_accounts if len(payload_accounts) > 1 else payload_accounts[0],
    }
    # Also send top-level list under "accounts" for docs compatibility
    body["accounts"] = payload_accounts

    url = f"{base}/admin/api/accounts/import"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "standalone-registration/1.0",
    }
    started = time.time()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.post(url, json=body, headers=headers)
            elapsed = round(time.time() - started, 2)
            text = (resp.text or "")[:800]
            try:
                data = resp.json()
            except Exception:
                data = {"raw": text}
            if resp.status_code >= 400:
                log.warning(
                    "push failed status=%s url=%s body=%s",
                    resp.status_code,
                    url,
                    text,
                )
                return {
                    "ok": False,
                    "skipped": False,
                    "status": resp.status_code,
                    "error": data.get("detail") or data.get("error") or text,
                    "total": len(payload_accounts),
                    "elapsed_sec": elapsed,
                    "url": url,
                }
            log.info(
                "push ok count=%s status=%s elapsed=%ss",
                len(payload_accounts),
                resp.status_code,
                elapsed,
            )
            return {
                "ok": True,
                "skipped": False,
                "status": resp.status_code,
                "result": data,
                "total": len(payload_accounts),
                "elapsed_sec": elapsed,
                "url": url,
            }
    except Exception as exc:  # noqa: BLE001
        log.exception("push exception")
        return {
            "ok": False,
            "skipped": False,
            "error": str(exc),
            "total": len(payload_accounts),
            "url": url,
        }


def test_push_connection(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Lightweight connectivity check against remote admin API."""
    c = cfg or load_config()
    base = str(c.get("grokcli2api_admin_url") or "").strip().rstrip("/")
    token = str(c.get("grokcli2api_admin_token") or "").strip()
    if not base or not token:
        return {"ok": False, "error": "URL 或 Admin Token 未配置"}
    headers = {"Authorization": f"Bearer {token}"}
    # Prefer a cheap authenticated endpoint
    candidates = [
        f"{base}/admin/api/status",
        f"{base}/admin/api/accounts",
        f"{base}/admin/api/settings",
        f"{base}/health",
    ]
    errors: list[str] = []
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            for url in candidates:
                try:
                    r = client.get(url, headers=headers)
                    if r.status_code < 500:
                        return {
                            "ok": r.status_code < 400,
                            "status": r.status_code,
                            "url": url,
                            "message": "连通正常" if r.status_code < 400 else f"HTTP {r.status_code}",
                            "body_preview": (r.text or "")[:200],
                        }
                    errors.append(f"{url} -> {r.status_code}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{url} -> {exc}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "; ".join(errors) or "unreachable"}
