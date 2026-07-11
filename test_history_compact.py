"""Unit tests for inbound history compaction (no network)."""

from __future__ import annotations

import history_compact as hc


def _big(n: int = 20_000) -> str:
    return ("x" * 80 + "\n") * (n // 81 + 1)


def test_keeps_recent_tool_rounds_full():
    big = _big(30_000)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "start"},
    ]
    for i in range(8):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": f'{{"file_path":"/f{i}"}}',
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": f"ROUND{i}:" + big,
            }
        )
    messages.append({"role": "user", "content": "continue"})

    out, stats = hc.compact_openai_messages(
        messages,
        enabled=True,
        keep_tool_rounds=3,
        max_tool_result_chars=5_000,
        max_messages_chars=50_000,
    )
    assert stats["enabled"] is True
    assert stats["applied"] is True
    assert stats["tool_rounds"] == 8
    assert stats["after_chars"] < stats["before_chars"]

    # Oldest tool results should be placeholders
    tool_contents = [
        m["content"] for m in out if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert any(str(c).startswith(hc._PLACEHOLDER_PREFIX) for c in tool_contents)
    # Latest rounds still contain ROUND markers (possibly truncated but not placeholder-only)
    last3 = tool_contents[-3:]
    for i, c in enumerate(last3):
        assert "ROUND" in str(c)
        assert not str(c).startswith(hc._PLACEHOLDER_PREFIX)


def test_single_huge_tool_result_truncated_under_budget():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": _big(40_000)},
    ]
    out, stats = hc.compact_openai_messages(
        messages,
        enabled=True,
        keep_tool_rounds=6,
        max_tool_result_chars=8_000,
        max_messages_chars=500_000,  # under budget → only per-tool clamp
    )
    assert stats["truncated_tool_msgs"] >= 1
    tool = next(m for m in out if m.get("role") == "tool")
    assert len(tool["content"]) < 20_000
    assert "truncated" in tool["content"]


def test_disabled_noop():
    messages = [
        {"role": "tool", "tool_call_id": "c", "content": _big(10_000)},
    ]
    out, stats = hc.compact_openai_messages(messages, enabled=False)
    assert stats["enabled"] is False
    assert stats["applied"] is False
    assert out[0]["content"] == messages[0]["content"]


def test_does_not_mutate_input():
    original = {"role": "tool", "tool_call_id": "c", "content": _big(25_000)}
    messages = [original]
    hc.compact_openai_messages(
        messages,
        enabled=True,
        max_tool_result_chars=1_000,
        max_messages_chars=500_000,
    )
    assert len(original["content"]) > 20_000


def test_cap_outbound_tools():
    tools = [{"index": i} for i in range(5)]
    # default OUTBOUND_MAX_TOOLS is 0 → unlimited
    assert hc.cap_outbound_tools(tools) is tools or hc.cap_outbound_tools(tools) == tools


def test_compact_upstream_body_strips_size():
    body = {
        "model": "grok-4.5",
        "messages": [
            {"role": "user", "content": "u"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "Read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": _big(50_000)},
        ],
    }
    stats = hc.compact_upstream_body(body)
    assert stats["after_chars"] < stats["before_chars"] or stats["truncated_tool_msgs"]


if __name__ == "__main__":
    test_keeps_recent_tool_rounds_full()
    test_single_huge_tool_result_truncated_under_budget()
    test_disabled_noop()
    test_does_not_mutate_input()
    test_cap_outbound_tools()
    test_compact_upstream_body_strips_size()
    print("ok")
