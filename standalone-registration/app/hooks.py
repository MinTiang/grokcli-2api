"""Runtime patches so registration works without full grokcli-2api stack.

Goals:
  1. After local import succeeds → push account to remote grokcli-2api
  2. Skip pool health probe (no chat upstream in this container)
  3. Keep local auth.json as a durable cache of registered accounts
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

log = logging.getLogger("standalone.hooks")
_installed = False


def install_hooks() -> None:
    global _installed
    if _installed:
        return

    # Force file-mode account store (no PostgreSQL in this container)
    os.environ.setdefault("GROK2API_STORE_BACKEND", "file")
    os.environ.setdefault("GROK2API_REG_SKIP_PROBE", "1")

    try:
        from grok2api.pool import accounts as accounts_mod
    except Exception as exc:  # noqa: BLE001
        log.error("cannot import accounts module for hooks: %s", exc)
        return

    original_import: Callable = accounts_mod.import_auth_payload

    def import_auth_payload_and_push(
        raw: Any, *, merge: bool = True
    ) -> dict[str, Any]:
        result = original_import(raw, merge=merge)
        if not isinstance(result, dict) or not result.get("ok"):
            return result

        # Build push payloads from the raw entry (has tokens) rather than
        # the summary `imported` list (id/email only).
        push_items: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            push_items.append(dict(raw))
        elif isinstance(raw, list):
            push_items.extend(dict(x) for x in raw if isinstance(x, dict))

        # Enrich with ids returned by local import when present
        imported = result.get("imported") or []
        if imported and push_items:
            for i, row in enumerate(imported):
                if i < len(push_items) and isinstance(row, dict):
                    if row.get("id"):
                        push_items[i].setdefault("account_id", row.get("id"))
                    if row.get("email") and not push_items[i].get("email"):
                        push_items[i]["email"] = row.get("email")

        if not push_items:
            return result

        try:
            from app.push import push_accounts

            push_result = push_accounts(push_items)
            result["grokcli2api_push"] = push_result
            if push_result.get("ok"):
                log.info(
                    "auto-push ok total=%s skipped=%s",
                    push_result.get("total"),
                    push_result.get("skipped"),
                )
            else:
                log.warning(
                    "auto-push failed: %s",
                    push_result.get("error") or push_result.get("reason"),
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("auto-push exception")
            result["grokcli2api_push"] = {"ok": False, "error": str(exc)}
        return result

    accounts_mod.import_auth_payload = import_auth_payload_and_push  # type: ignore[assignment]
    log.info("hooked accounts.import_auth_payload → local import + remote push")

    # Skip probe when env set
    if (os.environ.get("GROK2API_REG_SKIP_PROBE") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        try:
            import grok2api.pool.model_health as model_health

            def _skip_probe(account_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return {
                    "ok": True,
                    "skipped": True,
                    "account_id": account_id,
                    "reason": "standalone registration skips pool probe",
                }

            model_health.probe_single_account = _skip_probe  # type: ignore[assignment]
            log.info("hooked model_health.probe_single_account → skip")
        except Exception as exc:  # noqa: BLE001
            log.warning("probe skip hook not applied: %s", exc)

    # Silence optional auto-pushes that need main-app settings
    for mod_name, attr in (
        ("grok2api.upstream.sub2api_client", "maybe_auto_push_registered_accounts"),
        ("grok2api.upstream.cliproxyapi_client", "maybe_auto_push_registered_accounts"),
    ):
        try:
            import importlib

            mod = importlib.import_module(mod_name)

            def _skip_external(*_a: Any, **_k: Any) -> dict[str, Any]:
                return {"ok": True, "skipped": True, "reason": "standalone mode"}

            setattr(mod, attr, _skip_external)
            log.info("hooked %s.%s → skip", mod_name, attr)
        except Exception:
            pass

    _installed = True
