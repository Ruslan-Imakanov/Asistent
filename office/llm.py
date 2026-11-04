"""LLM backends: real Claude (Anthropic SDK) and a scripted demo brain for running without a key."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

# $ per 1M tokens (input, output)
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    content: list[Any]                 # blocks to append back as the assistant turn
    tool_calls: list[ToolCall]
    text: str
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0
    extra: dict = field(default_factory=dict)


def estimate_cost(model: str, inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> float:
    pin, pout = PRICES.get(model, (4.0, 20.0))
    return (inp * pin + cache_write * pin * 1.25 + cache_read * pin * 0.1 + out * pout) / 1_000_000


def _echo_content(content: list[Any]) -> list[Any]:
    """After a server-side fallback, drop model-internal blocks that precede the last fallback marker."""
    idx = max((i for i, b in enumerate(content) if getattr(b, "type", None) == "fallback"), default=-1)
    if idx < 0:
        return list(content)
    drop = {"thinking", "redacted_thinking", "tool_use"}
    head = [b for b in content[:idx] if getattr(b, "type", None) not in drop]
    return head + list(content[idx + 1:])


class ClaudeLLM:
    """Real Claude via the official SDK (beta endpoint for server-side refusal fallbacks)."""

    def __init__(self) -> None:
        self.client = anthropic.Anthropic(max_retries=4)

    def create(self, *, model: str, system: str, messages: list, tools: list, effort: str = "medium",
               max_tokens: int = 16000) -> LLMResponse:
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},
        )
        if model in FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        resp = self.client.beta.messages.create(**kwargs)

        tool_calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else json.loads(b.input))
                      for b in resp.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        u = resp.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        extra = {}
        if resp.stop_reason == "refusal" and resp.stop_details:
            extra["refusal"] = getattr(resp.stop_details, "explanation", "") or "refused"
        return LLMResponse(
            content=_echo_content(resp.content),
            tool_calls=tool_calls,
            text=text,
            stop_reason=resp.stop_reason or "",
            input_tokens=u.input_tokens + cache_read + cache_write,
            output_tokens=u.output_tokens,
            model=resp.model,
            cost_usd=estimate_cost(resp.model, u.input_tokens, u.output_tokens, cache_read, cache_write),
            extra=extra,
        )


class DemoLLM:
    """Deterministic 'brain' for demo mode and tests: no API key, no cost.

    It walks through the tools an agent has in a plausible order so the office comes alive
    and the whole pipeline (tasks -> work -> review -> outbox -> sales) can be exercised.
    """

    def create(self, *, model: str, system: str, messages: list, tools: list, effort: str = "medium",
               max_tokens: int = 16000) -> LLMResponse:
        names = {t.get("name") for t in tools}
        step = sum(1 for m in messages if m["role"] == "assistant")
        first = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
        n = len(messages)
        calls: list[ToolCall] = []

        def call(name: str, **inp: Any) -> None:
            calls.append(ToolCall(f"demo_{n}_{len(calls)}", name, inp))

        if "review_task" in names:  # director agenda
            task_ids = [int(x) for x in _between_all(first, "[task #", "]")]
            msg_ids = [int(x) for x in _between_all(first, "[msg #", "]")]
            if step == 0:
                for tid in task_ids:
                    call("review_task", task_id=tid, verdict="approve",
                         notes="Проверено: результат соответствует задаче.")
                for mid in msg_ids:
                    call("review_outbound", message_id=mid, verdict="approve", note="Текст вежливый и по делу.")
                for emp in _between_all(first, "[idle ", "]"):
                    call("create_task", assignee=emp, priority=3,
                         title=f"Демо-задача для {emp}",
                         description="Сделайте небольшой полезный результат по своей роли и сдайте на проверку.")
                if calls:
                    return _tool_resp(calls, model)
            return LLMResponse([], [], "Повестка разобрана.", "end_turn", 20, 10, model)

        if step == 0 and "write_file" in names:
            call("write_file", path="demo/result.md", content="# Результат\n\nДемо-результат работы сотрудника.\n")
        elif step == 1 and "submit_for_review" in names:
            task_id = int((_between_all(first, "Задача #", ":") or ["0"])[0])
            call("submit_for_review", task_id=task_id, summary="Готово, файл demo/result.md", files=["demo/result.md"])
        if calls:
            return _tool_resp(calls, model)
        return LLMResponse([], [], "Готово.", "end_turn", 20, 10, model)


def _tool_resp(calls: list[ToolCall], model: str) -> LLMResponse:
    blocks = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
    return LLMResponse(blocks, calls, "", "tool_use", 50, 50, model)


def _between_all(text: str, start: str, end: str) -> list[str]:
    out, pos = [], 0
    while (i := text.find(start, pos)) != -1:
        j = text.find(end, i + len(start))
        if j == -1:
            break
        out.append(text[i + len(start):j].strip())
        pos = j + len(end)
    return out


def make_llm() -> ClaudeLLM | DemoLLM:
    if os.environ.get("OFFICE_DEMO") == "1":
        return DemoLLM()
    has_creds = any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"))
    return ClaudeLLM() if has_creds else DemoLLM()
