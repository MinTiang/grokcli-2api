"""Origin SSO recovery: batch import/recover accounts from data/origin_sso/*.json.

Flow per account:
  1) Prefer sso / sso_cookie from the JSON → sso_to_token → import
  2) On failure, if password present → Turnstile + CreateSession password
     reauth → new SSO → sso_to_token → import

Uses an independent in-memory batch/session map (mirrors registration shape so
admin UI can poll the same way).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("GROK2API_DATA_DIR") or (ROOT / "data")).resolve()
ORIGIN_SSO_DIR = DATA_DIR / "origin_sso"

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}
_batches: dict[str, dict[str, Any]] = {}
_active_runners: dict[str, bool] = {}


def _now() -> float:
    return time.time()


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
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def _append_log(sess: dict[str, Any], status: str, message: str, *, max_lines: int = 40) -> None:
    lines = list(sess.get("log_lines") or [])
    ts = time.strftime("%H:%M:%S", time.localtime())
    lines.append(f"[{ts}] [{status}] {message}")
    sess["log_lines"] = lines[-max_lines:]


def _compact_session(sess: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(sess, dict):
        return {}
    out = _jsonable(sess) or {}
    # never leak secrets in list/poll
    for k in ("password", "register_password", "sso", "sso_cookie", "yescaptcha_key"):
        out.pop(k, None)
    return out


def _read_origin_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    email = str(data.get("email") or "").strip()
    if not email:
        return None
    return data


def scan_origin_sso_files() -> dict[str, Any]:
    """Scan data/origin_sso/*.json and return a summary list."""
    ORIGIN_SSO_DIR.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        paths = sorted(ORIGIN_SSO_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(e),
            "files": [],
            "count": 0,
            "origin_sso_dir": str(ORIGIN_SSO_DIR),
        }

    for path in paths[:5000]:
        data = _read_origin_json(path)
        if data is None:
            errors.append(f"{path.name}: invalid JSON or missing email")
            continue
        email = str(data.get("email") or "").strip()
        sso = str(data.get("sso") or data.get("sso_cookie") or data.get("sso_token") or "").strip()
        password = str(data.get("password") or data.get("register_password") or "").strip()
        files.append(
            {
                "path": str(path),
                "name": path.name,
                "email": email,
                "has_sso": bool(sso),
                "has_password": bool(password),
                "mtime": path.stat().st_mtime,
            }
        )
    return {
        "ok": True,
        "files": files,
        "count": len(files),
        "origin_sso_dir": str(ORIGIN_SSO_DIR),
        "errors": errors[:30],
    }


def _load_existing_by_email() -> dict[str, dict[str, Any]]:
    """email(lower) → durable account summary (id/expired/expires_at)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from grok2api.pool.accounts import list_accounts

        for a in list_accounts() or []:
            if not isinstance(a, dict):
                continue
            email = str(a.get("email") or "").strip().lower()
            if not email:
                continue
            # Prefer first hit; later ones with same email are rare after dedupe.
            out.setdefault(email, a)
    except Exception:
        pass
    return out


def _group_files_by_email(files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group scanned files by lowercased email, preserving scan order.

    Scan is mtime-desc, so the newest json is the group's first candidate
    ("优先用第一个"). Files without email are dropped upstream by scan.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        email_l = str(f.get("email") or "").strip().lower()
        if not email_l:
            continue
        groups.setdefault(email_l, []).append(f)
    return groups


def preview_origin_sso(
    *,
    filter_mode: str = "expired_and_new",
    selected_emails: list[str] | None = None,
    selected_files: list[str] | None = None,  # backward-compat (paths)
) -> dict[str, Any]:
    """Dry-run: scan + classify by email group without starting workers.

    Each candidate is one email (real account) with an ordered ``files`` list
    (its alternative credential jsons). Selection is by email.
    """
    scan = scan_origin_sso_files()
    if not scan.get("ok"):
        return scan

    files = list(scan.get("files") or [])

    # Optional path-based prefilter (legacy) narrows the file set first.
    if selected_files:
        sel_paths = {str(x).strip() for x in selected_files if str(x).strip()}
        files = [
            f for f in files if f.get("path") in sel_paths or f.get("name") in sel_paths
        ]

    groups = _group_files_by_email(files)

    sel_emails: set[str] | None = None
    if selected_emails:
        sel_emails = {str(x).strip().lower() for x in selected_emails if str(x).strip()}

    existing = _load_existing_by_email()
    expired: list[dict[str, Any]] = []
    new_items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for email_l, group_files in groups.items():
        if sel_emails is not None and email_l not in sel_emails:
            continue
        # Display email from first file (original case).
        email = str(group_files[0].get("email") or email_l).strip()
        matched = existing.get(email_l)
        ordered = [
            {
                "path": gf.get("path"),
                "name": gf.get("name"),
                "has_sso": bool(gf.get("has_sso")),
                "has_password": bool(gf.get("has_password")),
            }
            for gf in group_files
        ]
        row = {
            "email": email,
            "files": ordered,
            "file_count": len(ordered),
            "has_sso": any(x["has_sso"] for x in ordered),
            "has_password": any(x["has_password"] for x in ordered),
            "existing_id": (matched or {}).get("id"),
            "existing_expired": bool((matched or {}).get("expired")),
        }
        if matched:
            if matched.get("expired"):
                row["reason"] = "expired"
                expired.append(row)
            else:
                row["reason"] = "not_expired"
                skipped.append(row)
        else:
            row["reason"] = "new"
            new_items.append(row)

    mode = (filter_mode or "expired_and_new").strip().lower()
    if mode not in {"expired_and_new", "expired_only", "new_only", "all"}:
        mode = "expired_and_new"

    candidates: list[dict[str, Any]] = []
    if mode in {"expired_and_new", "all"}:
        candidates.extend(expired)
        candidates.extend(new_items)
    elif mode == "expired_only":
        candidates.extend(expired)
    elif mode == "new_only":
        candidates.extend(new_items)

    return {
        "ok": True,
        "origin_sso_dir": scan.get("origin_sso_dir"),
        "filter_mode": mode,
        "total_files": len(files),
        "total_emails": len(groups),
        "expired_count": len(expired),
        "new_count": len(new_items),
        "skipped_count": len(skipped),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "skipped": skipped[:200],
        "scan_errors": scan.get("errors") or [],
    }


def start_origin_sso_recovery(
    *,
    filter_mode: str = "expired_and_new",
    concurrency: int = 3,
    selected_emails: list[str] | None = None,
    selected_files: list[str] | None = None,
    captcha_provider: str | None = None,
    yescaptcha_key: str | None = None,
    local_solver_url: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Start a recovery batch. Returns batch_id for polling."""
    preview = preview_origin_sso(
        filter_mode=filter_mode,
        selected_emails=selected_emails,
        selected_files=selected_files,
    )
    if not preview.get("ok"):
        return preview
    candidates = list(preview.get("candidates") or [])
    if not candidates:
        return {
            "ok": False,
            "error": "没有可处理的账号（过滤后为空）。未勾选文件时会处理全部过期+新账号；活跃账号会跳过。",
            "filter_mode": preview.get("filter_mode"),
            "expired_count": preview.get("expired_count", 0),
            "new_count": preview.get("new_count", 0),
            "skipped_count": preview.get("skipped_count", 0),
            "origin_sso_dir": preview.get("origin_sso_dir"),
        }

    try:
        conc = max(1, min(10, int(concurrency or 3)))
    except (TypeError, ValueError):
        conc = 3

    captcha = (
        captcha_provider
        or os.environ.get("GROK2API_CAPTCHA_PROVIDER")
        or os.environ.get("CAPTCHA_PROVIDER")
        or "local"
    ).strip().lower()
    if captcha not in {"local", "yescaptcha"}:
        captcha = "local"

    batch_id = f"origin_sso_{uuid.uuid4().hex[:16]}"
    batch = {
        "id": batch_id,
        "type": "origin_sso_recovery",
        "status": "queued",
        "phase": "queued",
        "message": f"已排队，共 {len(candidates)} 个账号",
        "created_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
        "total": len(candidates),
        "done": 0,
        "success": 0,
        "fail": 0,
        "skipped": 0,
        "percent": 0,
        "filter_mode": preview.get("filter_mode"),
        "concurrency": conc,
        "session_ids": [],
        "candidates": candidates,
        "captcha_provider": captcha,
        "yescaptcha_key": (yescaptcha_key or "").strip() or None,
        "local_solver_url": (local_solver_url or "").strip() or None,
        "proxy": (proxy or "").strip() or None,
        "cancel_requested": False,
        "ok": None,
        "error": None,
    }
    with _lock:
        _batches[batch_id] = batch

    t = threading.Thread(
        target=_run_batch,
        args=(batch_id,),
        daemon=True,
        name=f"origin-sso-{batch_id[-8:]}",
    )
    t.start()

    return {
        "ok": True,
        "async": True,
        "batch_id": batch_id,
        "batch_type": "origin_sso_recovery",
        "status": "queued",
        "total": len(candidates),
        "expired_count": preview.get("expired_count", 0),
        "new_count": preview.get("new_count", 0),
        "skipped_count": preview.get("skipped_count", 0),
        "concurrency": conc,
        "message": f"Origin SSO 恢复已启动：{len(candidates)} 个账号（并发 {conc}）",
        "poll_url": f"/admin/api/accounts/origin-sso/batches/{batch_id}",
    }


def _run_batch(batch_id: str) -> None:
    with _lock:
        if _active_runners.get(batch_id):
            return
        _active_runners[batch_id] = True
        batch = _batches.get(batch_id)

    if not isinstance(batch, dict):
        with _lock:
            _active_runners.pop(batch_id, None)
        return

    candidates = list(batch.get("candidates") or [])
    conc = int(batch.get("concurrency") or 3)

    def _patch_batch(**kwargs: Any) -> None:
        with _lock:
            b = _batches.get(batch_id)
            if not b:
                return
            b.update(kwargs)
            b["updated_at"] = _now()
            total = max(1, int(b.get("total") or 1))
            done = int(b.get("done") or 0)
            b["percent"] = min(100, int(done * 100 / total))
            _batches[batch_id] = b

    _patch_batch(status="running", phase="running", message=f"处理中 0/{len(candidates)}")

    def _process(cand: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            b = _batches.get(batch_id) or {}
            if b.get("cancel_requested"):
                return {"ok": False, "cancelled": True}

        sid = f"osr_{uuid.uuid4().hex[:14]}"
        email = str(cand.get("email") or "").strip()
        cand_files = cand.get("files") if isinstance(cand.get("files"), list) else []
        sess = {
            "id": sid,
            "batch_id": batch_id,
            "status": "started",
            "phase": "started",
            "created_at": _now(),
            "updated_at": _now(),
            "email": email,
            "file_count": len(cand_files),
            "origin_file": (cand_files[0].get("name") if cand_files else None),
            "existing_id": cand.get("existing_id"),
            "reason": cand.get("reason"),
            "message": f"started; email={email} ({len(cand_files)} json)",
            "log_lines": [],
            "error": None,
            "ok": None,
            "method": None,
            "deleted_files": [],
        }
        with _lock:
            _sessions[sid] = sess
            b = _batches.get(batch_id)
            if b is not None:
                b.setdefault("session_ids", []).append(sid)
                b["updated_at"] = _now()
                _batches[batch_id] = b

        result = _recover_one(sid, cand, batch)
        with _lock:
            b = _batches.get(batch_id)
            if b is not None:
                b["done"] = int(b.get("done") or 0) + 1
                if result.get("cancelled"):
                    b["skipped"] = int(b.get("skipped") or 0) + 1
                elif result.get("ok"):
                    b["success"] = int(b.get("success") or 0) + 1
                else:
                    b["fail"] = int(b.get("fail") or 0) + 1
                total = max(1, int(b.get("total") or 1))
                done = int(b.get("done") or 0)
                b["percent"] = min(100, int(done * 100 / total))
                b["message"] = (
                    f"处理中 {done}/{total} · 成功 {b.get('success', 0)} · 失败 {b.get('fail', 0)}"
                )
                b["updated_at"] = _now()
                _batches[batch_id] = b
        return result

    try:
        with ThreadPoolExecutor(max_workers=max(1, conc), thread_name_prefix="osr-") as ex:
            futs = [ex.submit(_process, c) for c in candidates]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    pass
    finally:
        with _lock:
            b = _batches.get(batch_id)
            if b is not None:
                cancelled = bool(b.get("cancel_requested"))
                fail = int(b.get("fail") or 0)
                success = int(b.get("success") or 0)
                if cancelled:
                    b["status"] = "cancelled"
                    b["phase"] = "cancelled"
                    b["ok"] = False
                    b["message"] = f"已停止 · 成功 {success} · 失败 {fail}"
                elif fail == 0:
                    b["status"] = "done"
                    b["phase"] = "done"
                    b["ok"] = True
                    b["message"] = f"完成 · 成功 {success}"
                elif success == 0:
                    b["status"] = "error"
                    b["phase"] = "error"
                    b["ok"] = False
                    b["message"] = f"全部失败 · 失败 {fail}"
                else:
                    b["status"] = "partial"
                    b["phase"] = "partial"
                    b["ok"] = True
                    b["message"] = f"部分完成 · 成功 {success} · 失败 {fail}"
                b["finished_at"] = _now()
                b["updated_at"] = _now()
                b["percent"] = 100
                _batches[batch_id] = b
            _active_runners.pop(batch_id, None)


def _update_session(sid: str, status: str, message: str, **kwargs: Any) -> None:
    with _lock:
        sess = _sessions.get(sid)
        if not sess:
            return
        sess["status"] = status
        sess["phase"] = status
        sess["message"] = message
        sess["updated_at"] = _now()
        sess.update(kwargs)
        try:
            _append_log(sess, status, message)
        except Exception:
            pass
        _sessions[sid] = sess


def _delete_origin_file(path: Path, sess_id: str, email: str) -> None:
    """Delete a wrong-password origin json (best-effort)."""
    try:
        path.unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            return
    except Exception:
        return
    _update_session(sess_id, "deleted_bad_json", f"密码错误，已删除 {path.name}")


def _write_back_sso(path: Path, sso_cookie: str, sess_id: str, email: str) -> bool:
    """Write a freshly obtained SSO back into its origin json + updated_at.

    Called right after password reauth yields a new SSO, BEFORE the (rate-limit
    prone) device-flow token conversion. Guarantees the new SSO is never lost:
    a later rerun picks it up directly from the json and skips password+turnstile.
    """
    try:
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        data["sso"] = sso_cookie
        data["sso_cookie"] = sso_cookie
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        _update_session(sess_id, "sso_written_back", f"新 SSO 已写回 {path.name}")
        return True
    except Exception as e:  # noqa: BLE001
        _update_session(sess_id, "sso_writeback_failed", f"写回 SSO 失败: {str(e)[:120]}")
        return False


def _import_sso_entry(
    *, sess_id: str, email: str, sso_cookie: str, password: str, source: str, origin_name: str
) -> dict[str, Any]:
    """SSO cookie -> token -> import into account pool.

    Returns import result plus ``sso_reason`` classifying any sso_to_token
    failure (sso_invalid / device_flow_failed / network_error) so the caller
    can decide whether to fall back to password reauth.
    """
    import scripts.sso_to_auth_json as sso_conv
    from grok2api.pool import accounts

    detail: dict[str, Any] = {}
    token = sso_conv.sso_to_token(sso_cookie, quiet=True, detail=detail)
    if not isinstance(token, dict) or not (token.get("access_token") or token.get("key")):
        return {
            "ok": False,
            "error": "sso_conversion_failed",
            "sso_reason": str(detail.get("reason") or "device_flow_failed"),
            "sso_valid": bool(detail.get("sso_valid")),
        }
    _key, entry = sso_conv.token_to_auth_entry(token, email=email)
    entry = dict(entry or {})
    entry["sso"] = sso_cookie
    entry["sso_cookie"] = sso_cookie
    entry["sso_token"] = sso_cookie
    if password:
        entry["password"] = password
        entry["register_password"] = password
    entry["source"] = source
    entry["origin_sso_file"] = origin_name
    result = accounts.import_auth_payload(entry, merge=True)
    return result if isinstance(result, dict) else {"ok": False, "error": "import_failed"}


def _try_one_file(
    sid: str, file_info: dict[str, Any], batch: dict[str, Any]
) -> dict[str, Any]:
    """Attempt recovery from a single origin json.

    Returns dict with keys:
      ok (bool), method ("sso"|"password"|None), error (str|None),
      wrong_password (bool) - True only when CreateSession said invalid-credentials,
      transient (bool) - non-fatal failure that should NOT delete the json.
    """
    path_s = str(file_info.get("path") or "").strip()
    if not path_s:
        return {"ok": False, "error": "missing path", "wrong_password": False, "transient": True}
    path = Path(path_s)
    data = _read_origin_json(path)
    if not data:
        return {"ok": False, "error": "bad json", "wrong_password": False, "transient": True}

    email = str(data.get("email") or "").strip()
    password = str(data.get("password") or data.get("register_password") or "").strip()
    sso_cookie = str(
        data.get("sso") or data.get("sso_cookie") or data.get("sso_token") or ""
    ).strip()

    # -- Step 1: try existing SSO cookie ----------------------------------
    if sso_cookie:
        _update_session(sid, "trying_sso", f"尝试现有 SSO · {email} · {path.name}")
        try:
            result = _import_sso_entry(
                sess_id=sid,
                email=email,
                sso_cookie=sso_cookie,
                password=password,
                source="origin_sso_recovery",
                origin_name=path.name,
            )
            if result.get("ok"):
                _update_session(
                    sid, "imported", f"导入成功 (SSO) · {email}",
                    ok=True, method="sso", imported=result.get("imported"),
                )
                return {"ok": True, "method": "sso", "error": None,
                        "wrong_password": False, "transient": False}
            reason = str(result.get("sso_reason") or "")
            if result.get("error") == "sso_conversion_failed":
                if reason == "sso_invalid":
                    # SSO truly dead (redirected to sign-in) -> fall back to password.
                    _update_session(sid, "sso_invalid", "SSO 已失效，转账密回退")
                else:
                    # SSO likely still good; device-flow/rate-limit/network failed.
                    # Do NOT burn password+turnstile - keep json, retry later.
                    _update_session(
                        sid, "sso_device_flow_failed",
                        f"SSO 有效但换 token 失败({reason or 'device_flow'})；保留文件稍后重试",
                    )
                    return {"ok": False, "error": f"sso_{reason or 'device_flow_failed'}",
                            "wrong_password": False, "transient": True}
            else:
                _update_session(
                    sid, "sso_import_failed",
                    f"SSO 有效但入库失败: {str(result.get('error'))[:160]}",
                )
                return {"ok": False, "error": str(result.get("error") or "import_failed"),
                        "wrong_password": False, "transient": True}
        except Exception as e:  # noqa: BLE001
            _update_session(sid, "sso_failed", f"SSO 转换异常: {str(e)[:160]}")
            return {"ok": False, "error": str(e)[:160],
                    "wrong_password": False, "transient": True}
    else:
        _update_session(sid, "no_sso", f"{path.name} 无 SSO，走账密")

    # -- Step 2: password -> new SSO --------------------------------------
    if not password:
        return {"ok": False, "error": "no password for fallback",
                "wrong_password": False, "transient": True}

    detail: dict[str, Any] = {}
    try:
        new_sso = _password_to_sso(
            sid=sid, email=email, password=password, batch=batch, detail=detail,
        )
    except Exception as e:  # noqa: BLE001
        _update_session(sid, "password_error", f"账密换 SSO 异常: {str(e)[:200]}")
        return {"ok": False, "error": str(e)[:200],
                "wrong_password": False, "transient": True}

    if not new_sso:
        reason = str(detail.get("reason") or "")
        if reason == "wrong_password":
            _update_session(
                sid, "wrong_password",
                f"密码错误 · {email} · {path.name}（将删除该文件）",
            )
            return {"ok": False, "error": "wrong_password",
                    "wrong_password": True, "transient": False}
        _update_session(
            sid, "password_failed",
            f"账密换 SSO 失败(非密码错误): grpc={detail.get('grpc_status')} "
            f"msg={str(detail.get('grpc_msg') or '')[:120]}",
        )
        return {"ok": False, "error": f"reauth_failed:{detail.get('grpc_msg') or 'empty'}",
                "wrong_password": False, "transient": True}

    # New SSO obtained -> write it back to the json IMMEDIATELY (before the
    # rate-limit-prone token conversion) so it is never lost on failure.
    _write_back_sso(path, new_sso, sid, email)

    _update_session(sid, "converting", "新 SSO 已拿到并写回，转换 token 并入库")
    try:
        result = _import_sso_entry(
            sess_id=sid,
            email=email,
            sso_cookie=new_sso,
            password=password,
            source="origin_sso_password_reauth",
            origin_name=path.name,
        )
        if result.get("ok"):
            _update_session(
                sid, "imported", f"导入成功 (账密换 SSO) · {email}",
                ok=True, method="password", imported=result.get("imported"),
            )
            return {"ok": True, "method": "password", "error": None,
                    "wrong_password": False, "transient": False}
        err = str(result.get("error") or "import failed")
        _update_session(
            sid, "import_failed",
            f"新 SSO 已写回但换 token/入库失败: {err[:140]}（可重跑直接用新 SSO）",
        )
        return {"ok": False, "error": err, "wrong_password": False, "transient": True}
    except Exception as e:  # noqa: BLE001
        _update_session(sid, "import_error", f"转换/入库异常: {str(e)[:200]}")
        return {"ok": False, "error": str(e)[:200],
                "wrong_password": False, "transient": True}


def _recover_one(sid: str, cand: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    """Recover one email account by trying its ordered json files in turn.

    Ordering: newest-first (scan is mtime-desc). Only a confirmed wrong-password
    rejection deletes that json AND advances to the next file. Any success stops
    immediately. Transient failures do not delete and do not advance.
    """
    email = str(cand.get("email") or "").strip()
    files = list(cand.get("files") or [])
    if not files:
        _update_session(sid, "error", "该邮箱无候选文件", ok=False, error="no files")
        return {"ok": False, "error": "no files"}

    last_error = "no attempt"
    for idx, file_info in enumerate(files, 1):
        # Honour batch stop between files.
        with _lock:
            b = _batches.get(str(batch.get("id") or "")) or {}
            if b.get("cancel_requested"):
                _update_session(sid, "cancelled", "已取消", ok=False)
                return {"ok": False, "cancelled": True}

        name = str(file_info.get("name") or "?")
        _update_session(
            sid, "trying_file",
            f"尝试文件 {idx}/{len(files)} · {name} · {email}",
            origin_file=name, origin_path=file_info.get("path"),
        )
        res = _try_one_file(sid, file_info, batch)
        if res.get("ok"):
            return {"ok": True, "method": res.get("method"), "email": email,
                    "file": name}
        last_error = str(res.get("error") or "failed")

        if res.get("wrong_password"):
            # Delete this json and advance to the next candidate for the same email.
            _delete_origin_file(Path(str(file_info.get("path") or "")), sid, email)
            continue
        # Non-wrong-password failure: stop here (do not burn other files/turnstiles
        # on a transient issue). The email can be retried in a later batch.
        _update_session(
            sid, "error",
            f"恢复失败(非密码错误，保留文件): {last_error[:160]}",
            ok=False, error=last_error,
        )
        return {"ok": False, "error": last_error, "email": email}

    # All files exhausted (every one was a wrong password → all deleted).
    _update_session(
        sid, "error",
        f"该邮箱所有 {len(files)} 个文件密码均错误，已全部删除",
        ok=False, error="all_files_wrong_password",
    )
    return {"ok": False, "error": "all_files_wrong_password", "email": email}


def _password_to_sso(
    *,
    sid: str,
    email: str,
    password: str,
    batch: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> str | None:
    """Solve Turnstile then CreateSession password login → SSO JWT.

    ``detail`` out-dict is populated by obtain_session_via_password so the caller
    can distinguish wrong-password from transient failures.
    """
    from xconsole_client import XConsoleAuthClient, YesCaptchaSolver
    from xconsole_client import config as C

    captcha = str(batch.get("captcha_provider") or "local").strip().lower()
    proxy = str(batch.get("proxy") or "").strip()
    website_url = "https://accounts.x.ai/sign-in?redirect=grok-com"
    sitekey = getattr(C, "TURNSTILE_SITEKEY", None) or "0x4AAAAAAAhr9JGVDZbrZOo0"

    if captcha == "yescaptcha":
        key = (
            str(batch.get("yescaptcha_key") or "").strip()
            or os.environ.get("GROK2API_YESCAPTCHA_KEY")
            or os.environ.get("YESCAPTCHA_API_KEY")
            or ""
        ).strip()
        if not key:
            raise RuntimeError("YesCaptcha 模式需要 yescaptcha_key")
        _update_session(sid, "solving_turnstile", "YesCaptcha 解 Turnstile")
        solver = YesCaptchaSolver(key, timeout=120, poll_interval=2.0, debug=False)
        turnstile = solver.solve_turnstile(
            website_url=website_url, website_key=sitekey,
            premium=True, fallback_non_premium=True,
        )
    else:
        from grok2api.upstream import grok_build_adapter as gba

        endpoint = (
            str(batch.get("local_solver_url") or "").strip()
            or os.environ.get("GROK2API_LOCAL_SOLVER_URL")
            or "http://127.0.0.1:5072"
        ).rstrip("/")
        _update_session(sid, "waiting_solver", f"等待本地过盾 {endpoint}")
        wait = gba.wait_for_local_solver(
            endpoint, timeout_sec=60.0,
            progress=lambda m: _update_session(sid, "waiting_solver", str(m)),
        )
        if not wait.get("ready"):
            raise RuntimeError(wait.get("error") or f"本地过盾未就绪: {endpoint}")
        _update_session(sid, "solving_turnstile", "本地过盾解 Turnstile")
        solver = YesCaptchaSolver(
            "local", endpoint=endpoint, timeout=120, poll_interval=2.0,
            debug=False, auto_fallback_endpoint=False,
        )
        turnstile = solver.solve_turnstile(
            website_url=website_url, website_key=sitekey,
            premium=False, fallback_non_premium=True,
        )

    if not turnstile:
        raise RuntimeError("Turnstile 解析返回空")

    _update_session(sid, "password_login", f"CreateSession 密码登录 · {email}")
    client = XConsoleAuthClient(debug=False, proxy=proxy or "")
    try:
        sso = client.obtain_session_via_password(
            email=email,
            password=password,
            turnstile_token=turnstile,
            referer=website_url,
            retries=3,
            detail=detail,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
    return str(sso).strip() if sso else None


def get_batch(batch_id: str) -> dict[str, Any] | None:
    with _lock:
        batch = _batches.get(str(batch_id or "").strip())
        if not batch:
            return None
        out = _jsonable(batch) or {}
    # attach compact sessions for UI
    sids = list(out.get("session_ids") or [])
    sessions = []
    with _lock:
        for sid in sids:
            s = _sessions.get(sid)
            if s:
                sessions.append(_compact_session(s))
    out["sessions"] = sessions
    out.pop("yescaptcha_key", None)
    # candidates can be large; keep count only in poll if huge
    cands = out.get("candidates")
    if isinstance(cands, list) and len(cands) > 200:
        out["candidates_truncated"] = True
        out["candidates"] = cands[:200]
    return out


def get_session(session_id: str) -> dict[str, Any] | None:
    with _lock:
        sess = _sessions.get(str(session_id or "").strip())
        if not sess:
            return None
        return _compact_session(sess)


def list_sessions(*, batch_id: str | None = None) -> dict[str, Any]:
    with _lock:
        items = list(_sessions.values())
    if batch_id:
        bid = str(batch_id).strip()
        items = [s for s in items if str(s.get("batch_id") or "") == bid]
    items.sort(key=lambda s: float(s.get("updated_at") or 0), reverse=True)
    return {
        "ok": True,
        "sessions": [_compact_session(s) for s in items[:500]],
        "count": len(items),
    }


def stop_batch(batch_id: str) -> dict[str, Any]:
    bid = str(batch_id or "").strip()
    with _lock:
        batch = _batches.get(bid)
        if not batch:
            return {"ok": False, "error": "batch not found"}
        batch["cancel_requested"] = True
        if str(batch.get("status") or "") not in {"done", "partial", "error", "cancelled"}:
            batch["status"] = "stopping"
            batch["phase"] = "stopping"
            batch["message"] = "停止中…"
            batch["updated_at"] = _now()
        _batches[bid] = batch
        return {"ok": True, "batch_id": bid, "status": batch.get("status")}
