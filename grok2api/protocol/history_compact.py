"""Inbound chat history compaction for long Claude Code / agent tool loops.

Claude Code via sub2api can push multi-hundred-KB /v1/messages bodies as tool
rounds accumulate (Read results, command output, etc.). Upstream Grok then
times out, 400s, or returns stream-shape failures that surface as client
"API Error".

This module shrinks *past* tool results before the request is forwarded, while
keeping the latest tool rounds intact so the model can still act.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    import os

    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 10_000_000) -> int:
    import os

    try:
        v = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(minimum, min(maximum, v))


# Default OFF — IQ first. Compacting older tool results (even softly) can make
# the model re-Read, forget paths/errors, and feel dumber. Prefer full history.
# Enable only when a session is so huge that upstream starts failing / timing out:
#   GROK2API_HISTORY_COMPACT=1
# When enabled, soft-tier still prefers head+tail retention over pure placeholders.
HISTORY_COMPACT_ENABLED = _env_bool("GROK2API_HISTORY_COMPACT", False)
# Auto-force compact when messages JSON exceeds this many chars even if the
# master switch is off. 0 = never auto-force (IQ-first default).
# Set e.g. 1500000 only if you need a crash-prevention safety net.
HISTORY_COMPACT_AUTO_CHARS = _env_int(
    "GROK2API_HISTORY_COMPACT_AUTO_CHARS", 0, minimum=0, maximum=5_000_000
)
# When compacting, keep older rewrites deterministic so multi-turn prompt *prefixes*
# stay byte-stable across turns (helps upstream automatic prefix cache, same idea
# as superagent-ai/grok-cli replaying a stable message prefix).
HISTORY_PREFIX_STABLE = _env_bool("GROK2API_HISTORY_PREFIX_STABLE", True)
# If compact is ON: keep this many most-recent tool rounds nearly fully.
# IQ-first default is high so only deep history is touched.
HISTORY_KEEP_TOOL_ROUNDS = _env_int("GROK2API_HISTORY_KEEP_TOOL_ROUNDS", 32, minimum=1, maximum=64)
# Hard cap per single tool / tool_result content (chars) for *recent* rounds.
# Head+tail truncation preserves both file start and error/summary tails.
HISTORY_MAX_TOOL_RESULT_CHARS = _env_int(
    "GROK2API_HISTORY_MAX_TOOL_RESULT_CHARS", 48_000, minimum=512, maximum=2_000_000
)
# Mid-tier (just outside keep window): keep this many chars head+tail per tool result.
HISTORY_MID_TOOL_RESULT_CHARS = _env_int(
    "GROK2API_HISTORY_MID_TOOL_RESULT_CHARS", 16_000, minimum=512, maximum=2_000_000
)
# Far-tier (older history): keep this many chars head+tail — enough to remember
# "which file / which error" without replaying full dumps. Pure placeholder is
# only used later if still over the messages budget.
HISTORY_OLD_TOOL_RESULT_CHARS = _env_int(
    "GROK2API_HISTORY_OLD_TOOL_RESULT_CHARS", 8_000, minimum=256, maximum=2_000_000
)
# How many rounds after the keep window still get mid-tier retention (not old-tier).
HISTORY_MID_TOOL_ROUNDS = _env_int(
    "GROK2API_HISTORY_MID_TOOL_ROUNDS", 24, minimum=0, maximum=128
)
# Soft budget for the whole messages array JSON size (chars). High = more IQ.
HISTORY_MAX_MESSAGES_CHARS = _env_int(
    "GROK2API_HISTORY_MAX_MESSAGES_CHARS", 1_200_000, minimum=8_000, maximum=5_000_000
)
# Max tools per assistant turn for Claude-compatible paths
# (/v1/messages, /v1/responses via sub2api). Default 1: sub2api/Claude Code keep
# only one active content_block; multi-tool frames still race to
# "Content block not found" (especially Read) and agent frontends stop scheduling.
# 0 = unlimited.
OUTBOUND_MAX_TOOLS = _env_int("GROK2API_OUTBOUND_MAX_TOOLS", 1, minimum=0, maximum=64)
# Pure OpenAI chat/completions path default: unlimited (0). These clients can
# schedule multiple tool_calls in one turn without content_block races.
# Override with GROK2API_OUTBOUND_MAX_TOOLS_OPENAI if a secondary OpenAI relay
# still needs a hard cap.
OUTBOUND_MAX_TOOLS_OPENAI = _env_int(
    "GROK2API_OUTBOUND_MAX_TOOLS_OPENAI", 0, minimum=0, maximum=64
)
# OpenAI-native Responses clients (Codex TUI / OpenAI Python SDK talking to
# /v1/responses directly — not Claude Code via sub2api). Unlimited by default.
OUTBOUND_MAX_TOOLS_RESPONSES_NATIVE = _env_int(
    "GROK2API_OUTBOUND_MAX_TOOLS_RESPONSES_NATIVE", 0, minimum=0, maximum=64
)


# Real wall-clock gap between consecutive outbound tool SSE frames (seconds).
# SSE comment keepalives alone are not enough: sub2api often drains a TCP window
# of back-to-back tool chunks in one converter tick and still races content_blocks.
# 0 disables the delay (pure OpenAI clients).
def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 5.0) -> float:
    try:
        v = float(__import__("os").getenv(name, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(minimum, min(maximum, v))


OUTBOUND_TOOL_GAP_SEC = _env_float("GROK2API_OUTBOUND_TOOL_GAP_SEC", 0.08, minimum=0.0, maximum=2.0)
# Pure OpenAI / Codex clients do not need the sub2api content_block gap.
OUTBOUND_TOOL_GAP_SEC_NATIVE = _env_float(
    "GROK2API_OUTBOUND_TOOL_GAP_SEC_NATIVE", 0.0, minimum=0.0, maximum=2.0
)


def is_openai_native_client(user_agent: str | None = None) -> bool:
    """True for Codex / OpenAI SDK / pure Responses clients (not Claude Code).

    These clients speak OpenAI Responses natively and can schedule multiple
    tool_calls per turn. Claude Code via sub2api still needs the conservative
    single-tool + gap policy.
    """
    ua = (user_agent or "").strip().lower()
    if not ua:
        return False
    # Claude Code / Anthropic SDK always take the conservative path.
    if "claude-cli" in ua or "anthropic" in ua or "claude-code" in ua:
        return False
    markers = (
        "codex",
        "openai/python",
        "openai-python",
        "openai/",
        "chatgpt",
        "gpt-agent",
        "responses-sdk",
    )
    return any(m in ua for m in markers)


def resolve_outbound_max_tools(
    protocol: str | None = None,
    *,
    user_agent: str | None = None,
) -> int:
    """Pick the per-turn tool cap for a public protocol / client.

    - openai chat/completions: OUTBOUND_MAX_TOOLS_OPENAI (default 0 = unlimited)
    - openai_responses + Codex/OpenAI-native UA: OUTBOUND_MAX_TOOLS_RESPONSES_NATIVE
    - anthropic / openai_responses via sub2api (Claude): OUTBOUND_MAX_TOOLS (default 1)
    """
    proto = (protocol or "").strip().lower()
    if proto in ("openai", "chat", "chat_completions", "openai_chat"):
        return int(OUTBOUND_MAX_TOOLS_OPENAI)
    if proto in ("openai_responses", "responses") and is_openai_native_client(user_agent):
        return int(OUTBOUND_MAX_TOOLS_RESPONSES_NATIVE)
    # Default conservative: Claude / Responses-via-sub2api / unknown relays.
    return int(OUTBOUND_MAX_TOOLS)


def resolve_outbound_tool_gap_sec(
    protocol: str | None = None,
    *,
    user_agent: str | None = None,
) -> float:
    """Wall-clock gap between outbound tool frames (0 = none)."""
    proto = (protocol or "").strip().lower()
    if proto in ("openai", "chat", "chat_completions", "openai_chat"):
        return float(OUTBOUND_TOOL_GAP_SEC_NATIVE)
    if is_openai_native_client(user_agent):
        return float(OUTBOUND_TOOL_GAP_SEC_NATIVE)
    return float(OUTBOUND_TOOL_GAP_SEC)


def should_auto_compact(body: dict[str, Any] | None) -> bool:
    """True when body messages exceed the auto-compact char threshold."""
    if not HISTORY_COMPACT_AUTO_CHARS or HISTORY_COMPACT_AUTO_CHARS <= 0:
        return False
    if not isinstance(body, dict):
        return False
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    try:
        size = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        try:
            size = sum(len(str(m)) for m in messages)
        except Exception:
            return False
    return size >= int(HISTORY_COMPACT_AUTO_CHARS)


_PLACEHOLDER_PREFIX = "[compacted tool result"


def _stable_content_digest(text: str) -> str:
    """Short, stable fingerprint of original content (for prefix-stable placeholders)."""
    raw = (text or "").encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = (block.get("type") or "").lower()
                if btype in ("text", "input_text", "output_text"):
                    parts.append(str(block.get("text") or ""))
                elif btype == "tool_result":
                    parts.append(_content_to_text(block.get("content")))
                else:
                    try:
                        parts.append(json.dumps(block, ensure_ascii=False))
                    except (TypeError, ValueError):
                        parts.append(str(block))
            else:
                parts.append(str(block))
        return "".join(parts)
    if isinstance(content, dict):
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def _set_text_content(msg: dict[str, Any], text: str) -> None:
    """Replace message content with plain text, preserving role/tool ids."""
    msg["content"] = text


def _truncate_text(text: str, limit: int, *, label: str = "content") -> str:
    """Deterministic head+tail truncation (stable for the same text + limit).

    Head keeps provenance (paths, imports, command headers); tail keeps the
    part models usually need most (errors, final output, return values).
    Pure head-only cuts were dropping those tails and felt like 'IQ loss'.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    # Fixed trailer so the same (text, limit) always yields the same cut.
    trailer_budget = 120
    body = max(0, limit - trailer_budget)
    if body < 64:
        # Extremely tight budget: fall back to head-only.
        head = max(0, limit - 64)
        omitted = len(text) - head
        digest = _stable_content_digest(text)
        return (
            f"{text[:head]}\n"
            f"…[{label} truncated, {omitted} chars omitted, id={digest}]"
        )
    # Prefer more tail than head: errors / final answers live at the end.
    head_n = max(32, body // 3)
    tail_n = max(32, body - head_n)
    # Avoid overlap on short-ish texts.
    if head_n + tail_n >= len(text):
        return text
    omitted = len(text) - head_n - tail_n
    digest = _stable_content_digest(text)
    return (
        f"{text[:head_n]}\n"
        f"…[{label} truncated, {omitted} chars omitted, id={digest}]\n"
        f"{text[-tail_n:]}"
    )


def _soft_summary(original: str, *, max_chars: int, reason: str = "older round") -> str:
    """Head+tail retention for older tool results — keeps IQ better than a pure id.

    Pure placeholders (`[compacted…]`) erase paths/errors the model still needs.
    Soft summary keeps a deterministic slice of the original so re-Read is a
    last resort, not the default.
    """
    text = original or ""
    n = len(text)
    if n <= max_chars:
        return text
    # Reuse head+tail trunc; label carries the reason for humans/debug.
    return _truncate_text(text, max_chars, label=f"tool_result/{reason}")


def _placeholder(original: str, *, reason: str = "older round") -> str:
    """Deterministic pure placeholder — last-resort only when over hard budget."""
    text = original or ""
    n = len(text)
    digest = _stable_content_digest(text)
    return (
        f"{_PLACEHOLDER_PREFIX}: {reason}; original {n} chars; id={digest} "
        f"— re-Read if needed]"
    )


def _already_compacted(text: str) -> bool:
    if not text:
        return False
    if text.startswith(_PLACEHOLDER_PREFIX):
        return True
    # Soft summaries / truncations also count as already-rewritten so we do not
    # re-mutate them on later turns (prefix stability).
    if "…[tool_result" in text or "…[content truncated" in text:
        return True
    if " chars omitted, id=" in text:
        return True
    return False


def _messages_char_size(messages: list[Any]) -> int:
    try:
        return len(json.dumps(messages, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        total = 0
        for m in messages:
            total += len(str(m))
        return total


def _is_tool_message(msg: dict[str, Any]) -> bool:
    role = (msg.get("role") or "").lower()
    if role == "tool":
        return True
    if role == "function":
        return True
    return False


def _is_assistant_tool_call(msg: dict[str, Any]) -> bool:
    if (msg.get("role") or "").lower() != "assistant":
        return False
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list) and tcs:
        return True
    fc = msg.get("function_call")
    return isinstance(fc, dict) and bool(fc.get("name"))


def _tool_round_spans(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Return (start, end_exclusive) spans for each tool round.

    A round is: assistant(tool_calls) + following contiguous tool messages.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if not isinstance(m, dict):
            i += 1
            continue
        if _is_assistant_tool_call(m):
            j = i + 1
            while j < n and isinstance(messages[j], dict) and _is_tool_message(messages[j]):
                j += 1
            spans.append((i, j))
            i = j
            continue
        i += 1
    return spans


def _shrink_tool_message(
    msg: dict[str, Any],
    *,
    max_chars: int,
    force_placeholder: bool,
    prefix_stable: bool = True,
    soft_summary: bool = False,
    reason: str = "older round",
) -> bool:
    """Mutate one tool message. Returns True if content changed.

    Modes:
      - force_placeholder + soft_summary: head+tail soft summary (preferred for IQ)
      - force_placeholder + not soft_summary: pure id placeholder (last-resort)
      - not force: head+tail truncate to max_chars (recent / mid rounds)

    When ``prefix_stable`` is on, already-rewritten messages are left alone so
    multi-turn prompt prefixes stay byte-stable.
    """
    original = _content_to_text(msg.get("content"))
    if not original:
        return False
    if prefix_stable and _already_compacted(original):
        return False
    if force_placeholder:
        if soft_summary and max_chars > 0:
            new = _soft_summary(original, max_chars=max_chars, reason=reason)
        else:
            new = _placeholder(original, reason=reason)
        if new != original:
            _set_text_content(msg, new)
            return True
        return False
    if len(original) > max_chars:
        new = _truncate_text(original, max_chars, label="tool_result")
        if new != original:
            _set_text_content(msg, new)
            return True
    return False


def _shrink_assistant_oversized_content(msg: dict[str, Any], *, max_chars: int) -> bool:
    """Trim huge assistant text (rare) without touching tool_calls structure."""
    if _is_assistant_tool_call(msg):
        # Keep tool_calls; only shrink text content if present and huge.
        content = msg.get("content")
        if content is None or content == "":
            return False
        text = _content_to_text(content)
        if len(text) > max_chars:
            _set_text_content(msg, _truncate_text(text, max_chars, label="assistant"))
            return True
        return False
    text = _content_to_text(msg.get("content"))
    if len(text) > max_chars * 2:
        _set_text_content(msg, _truncate_text(text, max_chars * 2, label="assistant"))
        return True
    return False


def compact_openai_messages(
    messages: list[Any] | None,
    *,
    enabled: bool | None = None,
    keep_tool_rounds: int | None = None,
    max_tool_result_chars: int | None = None,
    max_messages_chars: int | None = None,
    prefix_stable: bool | None = None,
    mid_tool_rounds: int | None = None,
    mid_tool_result_chars: int | None = None,
    old_tool_result_chars: int | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Compact OpenAI-style messages in a copy (never mutates caller).

    Soft-tier strategy (protect model IQ):
      1. Recent ``keep_tool_rounds``: full content, only head+tail-truncate if a
         single result exceeds ``max_tool_result_chars``.
      2. Mid window (next ``mid_tool_rounds``): head+tail soft summary at
         ``mid_tool_result_chars`` — keeps paths/errors, drops bulk dumps.
      3. Older rounds: head+tail soft summary at ``old_tool_result_chars``.
      4. Only if still over ``max_messages_chars`` budget: further shrink mid/old
         then, as a last resort, pure ``[compacted…]`` placeholders (oldest first).

    Never strips tool_calls. Prefix-stable mode leaves already-rewritten content
    alone so multi-turn prompt prefixes stay byte-stable.
    """
    stats: dict[str, Any] = {
        "enabled": False,
        "applied": False,
        "before_chars": 0,
        "after_chars": 0,
        "tool_rounds": 0,
        "compacted_tool_msgs": 0,  # pure placeholders (last-resort)
        "truncated_tool_msgs": 0,  # head+tail / soft summary
        "soft_summary_msgs": 0,
        "prefix_stable": False,
        "keep_tool_rounds": 0,
        "mid_tool_rounds": 0,
        "policy": "soft-tier",
    }
    if not isinstance(messages, list) or not messages:
        return messages or [], stats

    use = HISTORY_COMPACT_ENABLED if enabled is None else enabled
    keep = HISTORY_KEEP_TOOL_ROUNDS if keep_tool_rounds is None else keep_tool_rounds
    max_tr = (
        HISTORY_MAX_TOOL_RESULT_CHARS
        if max_tool_result_chars is None
        else max_tool_result_chars
    )
    budget = (
        HISTORY_MAX_MESSAGES_CHARS if max_messages_chars is None else max_messages_chars
    )
    stable = HISTORY_PREFIX_STABLE if prefix_stable is None else bool(prefix_stable)
    mid_n = HISTORY_MID_TOOL_ROUNDS if mid_tool_rounds is None else mid_tool_rounds
    mid_chars = (
        HISTORY_MID_TOOL_RESULT_CHARS
        if mid_tool_result_chars is None
        else mid_tool_result_chars
    )
    old_chars = (
        HISTORY_OLD_TOOL_RESULT_CHARS
        if old_tool_result_chars is None
        else old_tool_result_chars
    )
    keep = max(1, int(keep))
    max_tr = max(512, int(max_tr))
    budget = max(8_000, int(budget))
    mid_n = max(0, int(mid_n))
    mid_chars = max(256, int(mid_chars))
    old_chars = max(128, int(old_chars))

    # Shallow-copy messages + dicts so we never mutate caller's request objects.
    out: list[Any] = []
    for m in messages:
        if isinstance(m, dict):
            out.append(dict(m))
        else:
            out.append(m)

    before = _messages_char_size(out)
    stats["before_chars"] = before
    stats["enabled"] = bool(use)
    stats["prefix_stable"] = bool(stable)
    stats["keep_tool_rounds"] = int(keep)
    stats["mid_tool_rounds"] = int(mid_n)

    if not use:
        stats["after_chars"] = before
        return out, stats

    # Tool-round spans: (assistant tool_calls … following tool msgs).
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(out)
    while i < n:
        m = out[i]
        if isinstance(m, dict) and _is_assistant_tool_call(m):
            j = i + 1
            while j < n and isinstance(out[j], dict) and _is_tool_message(out[j]):
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    stats["tool_rounds"] = len(spans)

    recent_spans = spans[-keep:] if keep else []
    rest = spans[:-keep] if keep else list(spans)
    mid_spans = rest[-mid_n:] if mid_n else []
    old_spans = rest[:-mid_n] if mid_n else rest

    recent_idx: set[int] = set()
    for start, end in recent_spans:
        recent_idx.update(range(start, end))

    # Pass 1 — tiered soft shrink (never pure placeholder here).
    for start, end in recent_spans:
        for idx in range(start, end):
            m = out[idx]
            if not isinstance(m, dict) or not _is_tool_message(m):
                continue
            if _shrink_tool_message(
                m,
                max_chars=max_tr,
                force_placeholder=False,
                prefix_stable=stable,
            ):
                stats["truncated_tool_msgs"] += 1

    for start, end in mid_spans:
        for idx in range(start, end):
            m = out[idx]
            if not isinstance(m, dict) or not _is_tool_message(m):
                continue
            if _shrink_tool_message(
                m,
                max_chars=mid_chars,
                force_placeholder=True,
                prefix_stable=stable,
                soft_summary=True,
                reason="mid round",
            ):
                stats["soft_summary_msgs"] += 1
                stats["truncated_tool_msgs"] += 1

    for start, end in old_spans:
        for idx in range(start, end):
            m = out[idx]
            if not isinstance(m, dict) or not _is_tool_message(m):
                continue
            if _shrink_tool_message(
                m,
                max_chars=old_chars,
                force_placeholder=True,
                prefix_stable=stable,
                soft_summary=True,
                reason="older round",
            ):
                stats["soft_summary_msgs"] += 1
                stats["truncated_tool_msgs"] += 1

    # Pass 2 — still over budget: tighten mid/old soft summaries further.
    after = _messages_char_size(out)
    if after > budget:
        tighter_old = max(512, old_chars // 2)
        tighter_mid = max(1024, mid_chars // 2)
        for tier_spans, cap, reason in (
            (old_spans, tighter_old, "budget/old"),
            (mid_spans, tighter_mid, "budget/mid"),
        ):
            if after <= budget:
                break
            for start, end in tier_spans:
                if after <= budget:
                    break
                for idx in range(start, end):
                    m = out[idx]
                    if not isinstance(m, dict) or not _is_tool_message(m):
                        continue
                    text = _content_to_text(m.get("content"))
                    # Allow re-tighten only when content is still large and not a pure placeholder.
                    if not text or text.startswith(_PLACEHOLDER_PREFIX):
                        continue
                    if len(text) <= cap:
                        continue
                    # Bypass prefix_stable skip for already-soft-summarized large blobs
                    # only when over budget — but keep rewrite deterministic on original
                    # is impossible once rewritten; re-truncate the current text stably.
                    new = _truncate_text(text, cap, label=f"tool_result/{reason}")
                    if new != text:
                        _set_text_content(m, new)
                        stats["truncated_tool_msgs"] += 1
                after = _messages_char_size(out)

    # Pass 3 — last resort: pure placeholders on oldest soft-summaries (oldest first).
    after = _messages_char_size(out)
    if after > budget:
        for start, end in old_spans + mid_spans:
            if after <= budget:
                break
            for idx in range(start, end):
                if after <= budget:
                    break
                m = out[idx]
                if not isinstance(m, dict) or not _is_tool_message(m):
                    continue
                text = _content_to_text(m.get("content"))
                if not text or text.startswith(_PLACEHOLDER_PREFIX):
                    continue
                # Convert soft summary / remaining text into pure placeholder.
                # Use current text for digest so rewrite is deterministic on current bytes.
                new = _placeholder(text, reason="size budget")
                if new != text:
                    _set_text_content(m, new)
                    stats["compacted_tool_msgs"] += 1
                    after = _messages_char_size(out)

    # Pass 4 — still over: hard-clamp recent tool results (never drop tool_calls).
    after = _messages_char_size(out)
    if after > budget:
        hard = max(2_000, max_tr // 3)
        for start, end in reversed(recent_spans):
            if after <= budget:
                break
            for idx in range(start, end):
                m = out[idx]
                if not isinstance(m, dict) or not _is_tool_message(m):
                    continue
                text = _content_to_text(m.get("content"))
                if not text or text.startswith(_PLACEHOLDER_PREFIX):
                    continue
                if len(text) > hard:
                    new = _truncate_text(text, hard, label="tool_result")
                    if new != text:
                        _set_text_content(m, new)
                        stats["truncated_tool_msgs"] += 1
            after = _messages_char_size(out)

    # Pass 5 — still over: trim huge user/assistant prose (never system).
    after = _messages_char_size(out)
    if after > budget:
        soft = max(2_000, max_tr // 2)
        for idx, m in enumerate(out):
            if after <= budget:
                break
            if not isinstance(m, dict):
                continue
            role = (m.get("role") or "").lower()
            if role == "system":
                continue
            if idx in recent_idx:
                continue
            if _is_tool_message(m):
                continue
            if role in ("user", "assistant"):
                if stable and idx < max(0, len(out) - keep * 4):
                    text = _content_to_text(m.get("content"))
                    if len(text) <= soft * 2:
                        continue
                if _shrink_assistant_oversized_content(m, max_chars=soft):
                    after = _messages_char_size(out)
                else:
                    text = _content_to_text(m.get("content"))
                    if len(text) > soft:
                        new = _truncate_text(text, soft, label=role)
                        if new != text:
                            _set_text_content(m, new)
                            after = _messages_char_size(out)

    after = _messages_char_size(out)
    stats["after_chars"] = after
    stats["applied"] = (
        stats["compacted_tool_msgs"] > 0
        or stats["truncated_tool_msgs"] > 0
        or stats["soft_summary_msgs"] > 0
        or after < before
    )
    return out, stats


def compact_upstream_body(
    body: dict[str, Any],
    *,
    enabled: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Apply message compaction to an OpenAI chat.completions body. Mutates body.

    ``force=True`` runs compact even when HISTORY_COMPACT is off (used by the
    auto threshold for Codex / long agent loops).
    """
    if not isinstance(body, dict):
        return {"enabled": False, "applied": False}
    messages = body.get("messages")
    use = enabled
    if use is None and force:
        use = True
    new_messages, stats = compact_openai_messages(messages, enabled=use)
    body["messages"] = new_messages
    if force and stats.get("applied"):
        stats["auto"] = True
    return stats


def cap_outbound_tools(
    tool_calls: list[Any] | None,
    *,
    max_tools: int | None = None,
    protocol: str | None = None,
    user_agent: str | None = None,
) -> list[Any] | None:
    """Optional safety valve: limit tools emitted in one assistant response.

    ``max_tools`` wins when provided; else protocol-aware default; else global
    OUTBOUND_MAX_TOOLS. 0 / negative → unlimited.
    """
    if not tool_calls:
        return tool_calls
    limit = (
        int(max_tools)
        if max_tools is not None
        else resolve_outbound_max_tools(protocol, user_agent=user_agent)
        if protocol is not None
        else int(OUTBOUND_MAX_TOOLS)
    )
    if limit <= 0:
        return tool_calls
    if len(tool_calls) <= limit:
        return tool_calls
    return tool_calls[:limit]


def remaining_outbound_tool_budget(
    already_emitted: int,
    *,
    max_tools: int | None = None,
    protocol: str | None = None,
    user_agent: str | None = None,
) -> int | None:
    """How many more tools may be shipped this turn.

    None means unlimited (max_tools <= 0). 0 means stop emitting.
    """
    limit = (
        int(max_tools)
        if max_tools is not None
        else resolve_outbound_max_tools(protocol, user_agent=user_agent)
        if protocol is not None
        else int(OUTBOUND_MAX_TOOLS)
    )
    if limit <= 0:
        return None
    left = limit - max(0, int(already_emitted or 0))
    return max(0, left)
