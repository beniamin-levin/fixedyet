"""Token Factory client: chat with tool calling, retries, token and cost accounting, budget guard.

Verified against https://docs.tokenfactory.nebius.com on 2026-10-21:
  * base URL https://api.tokenfactory.nebius.com/v1/ (OpenAI compatible);
  * `tools` / `tool_choice` follow the OpenAI format (function-calling page);
  * rate limiting is HTTP 429 with a `Retry-After` header (rate-limits page);
  * the chat-completions schema allows extra body keys, and the Nemotron 3 model card documents
    `chat_template_kwargs={"enable_thinking": bool}` as the reasoning on/off switch. Token
    Factory's own docs do not mention that key for Nemotron, so `thinking=` is best effort: if the
    server answers 400 to it, the call is repeated without it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from fixedyet import ledger
from fixedyet.config import Settings, get_settings
from fixedyet.models import Usage

log = logging.getLogger(__name__)

# (input, output) US dollars per 1M tokens, Token Factory list prices.
PRICES_PER_M: dict[str, tuple[float, float]] = {
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
}
# Unknown model ids are billed at the dearest known price: the budget guard errs on the safe side.
FALLBACK_PRICE_PER_M = (0.30, 0.90)
# Real tokenizers average 3 to 4 characters per token on English and code; the pre-call cost
# estimate assumes 2, so it overshoots the real input size and the total cap errs on the safe side.
ESTIMATE_CHARS_PER_TOKEN = 2
ESTIMATE_OVERHEAD_TOKENS = 64  # chat-template tokens around the messages


class BudgetExceeded(RuntimeError):
    """The run has spent more than `settings.run_budget_usd`."""

    def __init__(self, spent: float, budget: float, message: str | None = None) -> None:
        super().__init__(message or f"run budget exceeded: spent ${spent:.4f} of ${budget:.2f}")
        self.spent = spent
        self.budget = budget


class TotalBudgetExceeded(BudgetExceeded):
    """The next call could take lifetime spend past `settings.total_budget_usd`.

    Here `spent` and `budget` are the ledger total and the total cap, not the run's. Nothing was
    sent to the API.
    """

    def __init__(
        self, spent: float, budget: float, needed: float = 0.0, message: str | None = None
    ) -> None:
        super().__init__(
            spent,
            budget,
            message
            or f"total budget reached: ${spent:.4f} of ${budget:.2f} spent in total, and the "
            f"next call could cost up to ${needed:.4f}",
        )
        self.needed = needed


class LLMError(RuntimeError):
    """The model API failed in a way retrying will not fix (or retries ran out)."""


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None  # set when the model sent arguments that were not valid JSON


class ChatResult(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    reasoning: str = ""
    finish_reason: str = ""
    model: str = ""
    usage: Usage = Field(default_factory=Usage)  # this call only; `LLM.usage` is the running total

    def assistant_message(self) -> dict[str, Any]:
        """The assistant turn in OpenAI message format, ready to append to the history."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in self.tool_calls
            ]
            if message["content"] is None:
                message["content"] = ""
        return message


def price_for(model: str) -> tuple[float, float]:
    return PRICES_PER_M.get(model, FALLBACK_PRICE_PER_M)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = price_for(model)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def estimate_max_cost_usd(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
) -> float:
    """The most one call can cost: a deliberately high input estimate plus all of `max_tokens`
    billed as output (reasoning tokens count against `max_tokens`)."""
    chars = len(json.dumps(messages, default=str)) + (len(json.dumps(tools)) if tools else 0)
    input_tokens = chars // ESTIMATE_CHARS_PER_TOKEN + 1 + ESTIMATE_OVERHEAD_TOKENS
    return cost_usd(model, input_tokens, max_tokens)


class LLM:
    """One instance per run: `usage` accumulates across calls and the budget applies to the run.

    Every call is also checked against, and recorded in, the persistent spend ledger, which caps
    the total spend of all runs (`settings.total_budget_usd`).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_attempts: int = 5,
        timeout_s: float = 180.0,
    ) -> None:
        self.settings = settings or get_settings()
        self._timeout_s = timeout_s  # per request; a long reasoning answer needs more
        self.usage = Usage()
        self._client = client
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._thinking_switch_works = True

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.settings.nebius_api_key:
                raise LLMError("NEBIUS_API_KEY is not set (see .env.example)")
            from openai import AsyncOpenAI

            # max_retries=0: retries are done here so they are visible and bounded.
            self._client = AsyncOpenAI(
                api_key=self.settings.nebius_api_key,
                base_url=self.settings.nebius_base_url,
                max_retries=0,
                timeout=self._timeout_s,
            )
        return self._client

    # -- public ----------------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        thinking: bool | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        """One chat completion. `thinking` turns the model's reasoning on or off (None: default)."""
        model = model or self.settings.model
        self._check_budget()
        reserved = self._reserve_total(model, messages, tools, max_tokens)
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if temperature is not None:
            kwargs["temperature"] = temperature
        if thinking is not None and self._thinking_switch_works:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": thinking}}

        billed = 0.0  # what goes into the ledger; a request that failed outright costs nothing
        try:
            response = await self._create(kwargs)
            billed = reserved  # an answer arrived: until its usage is read, assume the worst case
            result = self._parse(response, model, tools, messages)
            billed = result.usage.cost_usd
        except asyncio.CancelledError:
            billed = reserved  # cancelled in flight: the provider may still bill the whole call
            raise
        finally:
            self._settle_total(reserved, billed)
        self._record(result.usage)
        self._check_budget()
        return result

    # -- budget ----------------------------------------------------------------------------

    def _check_budget(self) -> None:
        if self.usage.cost_usd > self.settings.run_budget_usd:
            raise BudgetExceeded(self.usage.cost_usd, self.settings.run_budget_usd)

    def _reserve_total(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> float:
        """Set aside this call's worst-case cost under the total cap, or refuse before sending."""
        needed = estimate_max_cost_usd(model, messages, tools, max_tokens)
        cap = ledger.total_budget(self.settings)
        try:
            if ledger.reserve(self.settings, needed):
                return needed
            spent = ledger.spent_total(self.settings)
        except ledger.LedgerError as exc:  # cannot tell what was spent, so spend nothing
            log.error("%s", exc)
            raise TotalBudgetExceeded(
                cap, cap, needed,
                "total budget reached: the spend ledger cannot be read, so no more money is "
                f"spent ({exc})",
            ) from exc
        raise TotalBudgetExceeded(spent, cap, needed)

    def _settle_total(self, reserved: float, billed: float) -> None:
        """Write the real cost to the ledger, then give back the reservation (in that order, so
        the call is never briefly uncounted)."""
        try:
            if billed > 0:
                ledger.record(self.settings, billed)
        except ledger.LedgerError:
            log.exception("could not save %.6f USD to the spend ledger", billed)
        finally:
            ledger.release(self.settings, reserved)

    def _record(self, call_usage: Usage) -> None:
        self.usage.llm_calls += call_usage.llm_calls
        self.usage.input_tokens += call_usage.input_tokens
        self.usage.output_tokens += call_usage.output_tokens
        self.usage.cost_usd += call_usage.cost_usd

    # -- transport with retries --------------------------------------------------------------

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        client = self._get_client()
        attempt = 0
        while True:
            attempt += 1
            try:
                return await client.chat.completions.create(**kwargs)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if status == 400 and "extra_body" in kwargs and _mentions_thinking_switch(exc):
                    log.warning("server rejected the reasoning switch; retrying without it")
                    self._thinking_switch_works = False
                    kwargs = {k: v for k, v in kwargs.items() if k != "extra_body"}
                    attempt -= 1
                    continue
                if not _is_retryable(exc):
                    raise LLMError(_describe(exc)) from exc
                if attempt >= self._max_attempts:
                    raise LLMError(f"gave up after {attempt} attempts: {_describe(exc)}") from exc
                delay = _retry_delay(exc, attempt)
                log.info("model API error (%s); retry %d in %.1f s", status, attempt, delay)
                await self._sleep(delay)

    # -- response parsing -----------------------------------------------------------------

    def _parse(
        self,
        response: Any,
        model: str,
        tools: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
    ) -> ChatResult:
        if not response.choices:
            raise LLMError("the model API returned no choices")
        choice = response.choices[0]
        message = choice.message
        content = message.content or ""
        reasoning = (
            getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None) or ""
        )

        # Some deployments leave the chain of thought inline instead of in `reasoning_content`.
        if "</think>" in content:
            inline, _, content = content.partition("</think>")
            reasoning = reasoning or inline.replace("<think>", "").strip()
            content = content.strip()

        calls = [_native_tool_call(raw) for raw in (getattr(message, "tool_calls", None) or [])]
        if tools:
            known = {t["function"]["name"] for t in tools}
            for call in calls:
                call.name = _recover_tool_name(call.name, known)
        if tools and not calls and content:
            names = {t["function"]["name"] for t in tools}
            calls, content = parse_text_tool_calls(content, names)

        raw_usage = getattr(response, "usage", None)
        input_tokens = getattr(raw_usage, "prompt_tokens", None)
        output_tokens = getattr(raw_usage, "completion_tokens", None)
        if input_tokens is None or output_tokens is None:  # server sent no usage: estimate, never 0
            input_tokens = len(json.dumps(messages, default=str)) // 4
            output_tokens = (len(content) + len(reasoning)) // 4
        return ChatResult(
            content=content,
            tool_calls=calls,
            reasoning=reasoning,
            finish_reason=choice.finish_reason or "",
            model=model,
            usage=Usage(
                llm_calls=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd(model, input_tokens, output_tokens),
            ),
        )


# -- helpers -----------------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    """429, 5xx, and network-level failures are worth retrying; everything else is not."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError"} or isinstance(
        exc, (TimeoutError, ConnectionError)
    )


def _mentions_thinking_switch(exc: Exception) -> bool:
    """Is this 400 the server complaining about the reasoning switch rather than the request?"""
    text = str(getattr(exc, "message", None) or exc).lower()
    return any(word in text for word in ("chat_template", "enable_thinking", "extra inputs"))


def _retry_delay(exc: Exception, attempt: int) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(30.0, 2.0 ** (attempt - 1)) + random.uniform(0, 0.5)


def _describe(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return f"Token Factory rejected the API key (HTTP {status}); check NEBIUS_API_KEY"
    text = str(getattr(exc, "message", None) or exc)[:300]
    return f"Token Factory request failed{f' (HTTP {status})' if status else ''}: {text}"


def _native_tool_call(raw: Any) -> ToolCall:
    function = raw.function
    arguments, error = _decode_arguments(function.arguments)
    return ToolCall(
        id=raw.id or f"call_{uuid.uuid4().hex[:8]}",
        name=function.name,
        arguments=arguments,
        error=error,
    )


_LEAKED_FUNCTION = re.compile(r"<function=([\w.-]+)")


def _recover_tool_name(name: str, known: set[str]) -> str:
    """Nemotron sometimes writes its reasoning and a `<tool_call><function=run_test` fragment into
    the tool name while the arguments still arrive intact. Take the name from the last fragment."""
    if name in known:
        return name
    found = _LEAKED_FUNCTION.findall(name)
    return found[-1] if found and found[-1] in known else name


def _decode_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, dict):
        return raw, None
    if not raw:
        return {}, None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"arguments are not valid JSON ({exc.msg}): {str(raw)[:200]}"
    if not isinstance(decoded, dict):
        return {}, f"arguments must be a JSON object, got {type(decoded).__name__}"
    return decoded, None


# -- fallback: a tool call written as text ------------------------------------------------------

_XML_CALL = re.compile(r"<tool_call>\s*<function=([\w.-]+)>(.*?)</function>\s*</tool_call>", re.S)
_XML_PARAM = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.S)
_TAG_JSON_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def parse_text_tool_calls(content: str, tool_names: set[str]) -> tuple[list[ToolCall], str]:
    """Recover tool calls the server left in the message text. Returns (calls, remaining text).

    Handles three shapes seen with open models: Qwen/Nemotron XML
    (`<tool_call><function=name><parameter=x>..</parameter></function></tool_call>`),
    `<tool_call>{json}</tool_call>`, and a bare JSON object such as
    `{"name": "run_test", "arguments": {...}}` (possibly inside a code fence).
    Only names in `tool_names` count, so ordinary JSON in an answer is never mistaken for a call.
    """
    calls: list[ToolCall] = []
    remaining = content

    def add(name: str, arguments: dict[str, Any]) -> None:
        if name in tool_names:
            call_id = f"call_{uuid.uuid4().hex[:8]}"
            calls.append(ToolCall(id=call_id, name=name, arguments=arguments))

    for match in _XML_CALL.finditer(content):
        params = {key: value for key, value in _XML_PARAM.findall(match.group(2))}
        add(match.group(1), params)
    if calls:
        return calls, _XML_CALL.sub("", content).strip()

    for match in _TAG_JSON_CALL.finditer(content):
        parsed = _as_call(_loads(match.group(1)))
        if parsed:
            add(*parsed)
    if calls:
        return calls, _TAG_JSON_CALL.sub("", content).strip()

    decoder = json.JSONDecoder()
    position = 0
    spans: list[tuple[int, int]] = []
    while (start := content.find("{", position)) != -1:
        try:
            obj, end = decoder.raw_decode(content, start)
        except json.JSONDecodeError:
            position = start + 1
            continue
        parsed = _as_call(obj)
        if parsed and parsed[0] in tool_names:
            add(*parsed)
            spans.append((start, end))
        position = end
    for start, end in reversed(spans):
        remaining = remaining[:start] + remaining[end:]
    if spans:
        remaining = re.sub(r"```(?:json)?\s*```", "", remaining).strip()
    return calls, remaining if calls else content


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _as_call(obj: Any) -> tuple[str, dict[str, Any]] | None:
    """Normalise {"name": .., "arguments": ..} and the OpenAI {"function": {..}} shape."""
    if not isinstance(obj, dict):
        return None
    inner = obj["function"] if isinstance(obj.get("function"), dict) else obj
    name = inner.get("name") or inner.get("tool") or inner.get("tool_name")
    if not isinstance(name, str):
        return None
    arguments = next(
        (inner[k] for k in ("arguments", "parameters", "args", "input") if k in inner), {}
    )
    if isinstance(arguments, str):
        arguments = _loads(arguments)
    return (name, arguments) if isinstance(arguments, dict) else None
