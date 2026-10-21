"""LLM client tests. The OpenAI client is a scripted fake, so nothing touches the network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fixedyet import ledger
from fixedyet.config import Settings
from fixedyet.llm import (
    LLM,
    BudgetExceeded,
    ChatResult,
    LLMError,
    ToolCall,
    TotalBudgetExceeded,
    cost_usd,
    estimate_max_cost_usd,
    parse_text_tool_calls,
    price_for,
)

SUPER = "nvidia/nemotron-3-super-120b-a12b"
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
TOOLS = [
    {"type": "function", "function": {"name": "run_test", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "submit", "parameters": {"type": "object"}}},
]


class StatusError(Exception):
    """Stands in for openai.APIStatusError: the client only looks at status_code and headers."""

    def __init__(self, status: int, message: str = "boom", headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})


class APIConnectionError(Exception):
    """Same class name as openai's, which is how the client recognises a network failure."""


def completion(
    content: str | None = "hello",
    *,
    tool_calls: list[Any] | None = None,
    reasoning_content: str | None = None,
    reasoning: str | None = None,
    prompt: int = 1_000,
    out: int = 500,
    finish: str = "stop",
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning_content is not None:
        message.reasoning_content = reasoning_content
    if reasoning is not None:
        message.reasoning = reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=out),
    )


def native_call(name: str, arguments: Any, call_id: str = "c1") -> SimpleNamespace:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class FakeOpenAI:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_llm(*responses: Any, budget: float = 0.60, sleeps: list[float] | None = None,
             attempts: int = 5) -> tuple[LLM, FakeOpenAI]:
    client = FakeOpenAI(*responses)
    recorded = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)

    settings = Settings(nebius_api_key="k", run_budget_usd=budget)
    return LLM(settings, client=client, sleep=sleep, max_attempts=attempts), client


MESSAGES = [{"role": "user", "content": "hi"}]


# -- cost ---------------------------------------------------------------------------------------


def test_prices_per_million_tokens() -> None:
    assert price_for(SUPER) == (0.30, 0.90)
    assert price_for(NANO) == (0.06, 0.24)
    assert cost_usd(SUPER, 1_000_000, 1_000_000) == pytest.approx(1.20)
    assert cost_usd(NANO, 1_000_000, 1_000_000) == pytest.approx(0.30)
    assert cost_usd(SUPER, 1_000, 500) == pytest.approx(0.00075)


def test_unknown_model_is_billed_at_the_super_price() -> None:
    assert price_for("some/other-model") == (0.30, 0.90)


async def test_usage_accumulates_across_calls_and_models() -> None:
    llm, _ = make_llm(completion(prompt=1_000, out=500), completion(prompt=2_000, out=100))
    first = await llm.chat(MESSAGES)
    second = await llm.chat(MESSAGES, model=NANO)
    assert first.usage.cost_usd == pytest.approx(0.00075)
    assert second.usage.cost_usd == pytest.approx(2_000 * 0.06e-6 + 100 * 0.24e-6)
    assert llm.usage.llm_calls == 2
    assert (llm.usage.input_tokens, llm.usage.output_tokens) == (3_000, 600)
    assert llm.usage.cost_usd == pytest.approx(first.usage.cost_usd + second.usage.cost_usd)
    assert second.model == NANO


async def test_missing_usage_is_estimated_not_zero() -> None:
    response = completion("a" * 400)
    response.usage = None
    llm, _ = make_llm(response)
    result = await llm.chat([{"role": "user", "content": "x" * 800}])
    assert result.usage.input_tokens > 0 and result.usage.output_tokens == 100


# -- budget -------------------------------------------------------------------------------------


async def test_budget_exceeded_after_the_call_that_crosses_it() -> None:
    llm, client = make_llm(completion(prompt=1_000_000, out=0), budget=0.25)  # costs $0.30
    with pytest.raises(BudgetExceeded) as caught:
        await llm.chat(MESSAGES)
    assert caught.value.spent == pytest.approx(0.30) and caught.value.budget == 0.25
    assert llm.usage.cost_usd == pytest.approx(0.30)  # the spend is recorded
    assert len(client.calls) == 1


async def test_no_call_is_made_once_over_budget() -> None:
    llm, client = make_llm(completion(), budget=0.0001)
    with pytest.raises(BudgetExceeded):
        await llm.chat(MESSAGES)  # 0.00075 > 0.0001
    with pytest.raises(BudgetExceeded):
        await llm.chat(MESSAGES)
    assert len(client.calls) == 1


async def test_spending_exactly_the_budget_is_allowed() -> None:
    llm, _ = make_llm(completion(prompt=1_000_000, out=0), budget=0.30)
    await llm.chat(MESSAGES)


# -- request shape ------------------------------------------------------------------------------


async def test_request_carries_model_tools_and_max_tokens() -> None:
    llm, client = make_llm(completion())
    await llm.chat(MESSAGES, tools=TOOLS, max_tokens=123)
    sent = client.calls[0]
    assert sent["model"] == SUPER and sent["max_tokens"] == 123
    assert sent["tools"] == TOOLS and sent["tool_choice"] == "auto"
    assert "extra_body" not in sent and "temperature" not in sent


async def test_thinking_switch_goes_in_chat_template_kwargs() -> None:
    llm, client = make_llm(completion(), completion())
    await llm.chat(MESSAGES, thinking=False)
    await llm.chat(MESSAGES, thinking=True, temperature=0.2)
    assert client.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert client.calls[1]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert client.calls[1]["temperature"] == 0.2


async def test_server_rejecting_the_thinking_switch_is_remembered() -> None:
    rejection = StatusError(400, "unknown field chat_template_kwargs")
    llm, client = make_llm(rejection, completion("one"), completion("two"))
    assert (await llm.chat(MESSAGES, thinking=False)).content == "one"
    assert "extra_body" in client.calls[0] and "extra_body" not in client.calls[1]
    await llm.chat(MESSAGES, thinking=False)
    assert "extra_body" not in client.calls[2]  # not even tried again


async def test_missing_api_key_fails_clearly() -> None:
    llm = LLM(Settings(nebius_api_key=""))
    with pytest.raises(LLMError, match="NEBIUS_API_KEY"):
        await llm.chat(MESSAGES)


# -- retries ------------------------------------------------------------------------------------


async def test_429_honours_retry_after_then_succeeds() -> None:
    sleeps: list[float] = []
    llm, client = make_llm(
        StatusError(429, headers={"retry-after": "7"}), completion("ok"), sleeps=sleeps
    )
    assert (await llm.chat(MESSAGES)).content == "ok"
    assert sleeps == [7.0] and len(client.calls) == 2


async def test_5xx_and_connection_errors_back_off_exponentially() -> None:
    sleeps: list[float] = []
    llm, _ = make_llm(
        StatusError(502), APIConnectionError("reset"), StatusError(503), completion("ok"),
        sleeps=sleeps,
    )
    await llm.chat(MESSAGES)
    assert len(sleeps) == 3
    for delay, base in zip(sleeps, (1, 2, 4), strict=True):
        assert base <= delay <= base + 0.5  # 2**(attempt-1) plus up to half a second of jitter


async def test_retries_are_bounded() -> None:
    llm, client = make_llm(*[StatusError(500, "down") for _ in range(3)], attempts=3)
    with pytest.raises(LLMError, match="gave up after 3 attempts"):
        await llm.chat(MESSAGES)
    assert len(client.calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_not_retried(status: int) -> None:
    llm, client = make_llm(StatusError(status, "nope"), completion())
    with pytest.raises(LLMError):
        await llm.chat(MESSAGES)
    assert len(client.calls) == 1


async def test_auth_failure_names_the_key() -> None:
    llm, _ = make_llm(StatusError(401))
    with pytest.raises(LLMError, match="NEBIUS_API_KEY"):
        await llm.chat(MESSAGES)


# -- parsing ------------------------------------------------------------------------------------


async def test_native_tool_calls_are_parsed() -> None:
    raw = [native_call("run_test", json.dumps({"code": "x = 1"}), "a"), native_call("submit", {})]
    llm, _ = make_llm(completion(None, tool_calls=raw, finish="tool_calls"))
    result = await llm.chat(MESSAGES, tools=TOOLS)
    assert [c.name for c in result.tool_calls] == ["run_test", "submit"]
    assert result.tool_calls[0].arguments == {"code": "x = 1"} and result.tool_calls[0].id == "a"
    assert result.content == "" and result.finish_reason == "tool_calls"


async def test_a_tool_name_with_leaked_reasoning_is_recovered_from_its_function_fragment() -> None:
    leaked = (
        "write_file? Actually there is no write tool; embed the code in run_test.\n\n"
        "<tool_call>\n<function=run_test"
    )
    raw = [
        native_call(leaked, json.dumps({"code": "x = 1"})),
        native_call("nonsense <function=not_a_tool", {}),
        native_call("submit", {}),
    ]
    llm, _ = make_llm(completion(None, tool_calls=raw, finish="tool_calls"))
    result = await llm.chat(MESSAGES, tools=TOOLS)
    names = [c.name for c in result.tool_calls]
    assert names == ["run_test", "nonsense <function=not_a_tool", "submit"]
    assert result.tool_calls[0].arguments == {"code": "x = 1"}
    # without a tool list nothing is guessed
    llm, _ = make_llm(completion(None, tool_calls=[native_call(leaked, {})]))
    assert (await llm.chat(MESSAGES)).tool_calls[0].name == leaked


async def test_invalid_tool_arguments_are_reported_not_raised() -> None:
    llm, _ = make_llm(completion(None, tool_calls=[native_call("run_test", "{not json")]))
    call = (await llm.chat(MESSAGES, tools=TOOLS)).tool_calls[0]
    assert call.arguments == {} and "not valid JSON" in (call.error or "")


async def test_non_object_arguments_are_reported() -> None:
    llm, _ = make_llm(completion(None, tool_calls=[native_call("run_test", "[1, 2]")]))
    call = (await llm.chat(MESSAGES, tools=TOOLS)).tool_calls[0]
    assert "must be a JSON object" in (call.error or "")


async def test_reasoning_text_is_captured_from_either_field() -> None:
    llm, _ = make_llm(
        completion("a", reasoning_content="thought one"), completion("b", reasoning="thought two")
    )
    assert (await llm.chat(MESSAGES)).reasoning == "thought one"
    assert (await llm.chat(MESSAGES)).reasoning == "thought two"


async def test_inline_think_block_is_split_from_the_answer() -> None:
    llm, _ = make_llm(completion("<think>hmm, the key is x</think>\n{\"a\": 1}"))
    result = await llm.chat(MESSAGES)
    assert result.content == '{"a": 1}' and result.reasoning == "hmm, the key is x"


async def test_closing_think_tag_alone_is_handled() -> None:
    """Some chat templates open <think> themselves, so only the closing tag comes back."""
    llm, _ = make_llm(completion("reasoning text</think>answer"))
    result = await llm.chat(MESSAGES)
    assert (result.content, result.reasoning) == ("answer", "reasoning text")


async def test_json_tool_call_in_content_is_recovered_when_tools_were_offered() -> None:
    text = 'I will run it.\n```json\n{"name": "run_test", "arguments": {"code": "assert 1"}}\n```'
    llm, _ = make_llm(completion(text), completion(text))
    with_tools = await llm.chat(MESSAGES, tools=TOOLS)
    assert with_tools.tool_calls[0].name == "run_test"
    assert with_tools.tool_calls[0].arguments == {"code": "assert 1"}
    assert "run_test" not in with_tools.content and "I will run it." in with_tools.content
    without_tools = await llm.chat(MESSAGES)
    assert without_tools.tool_calls == [] and without_tools.content == text


# -- parse_text_tool_calls ------------------------------------------------------------------------

NAMES = {"run_test", "submit", "list_dir"}


def test_xml_function_format() -> None:
    text = (
        "<tool_call>\n<function=run_test>\n<parameter=code>\ndef test_x():\n    assert 1\n"
        "</parameter>\n</function>\n</tool_call>"
    )
    calls, rest = parse_text_tool_calls(text, NAMES)
    assert calls[0].name == "run_test"
    assert calls[0].arguments == {"code": "def test_x():\n    assert 1"}
    assert rest == ""


def test_tool_call_tag_with_json() -> None:
    calls, _ = parse_text_tool_calls(
        '<tool_call>{"name": "list_dir", "arguments": {"path": "src"}}</tool_call>', NAMES
    )
    assert calls[0].name == "list_dir" and calls[0].arguments == {"path": "src"}


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "submit", "arguments": {"code": "c"}},
        {"name": "submit", "parameters": {"code": "c"}},
        {"tool": "submit", "args": {"code": "c"}},
        {"type": "function", "function": {"name": "submit", "arguments": '{"code": "c"}'}},
        {"name": "submit", "arguments": '{"code": "c"}'},
    ],
)
def test_json_shapes(payload: dict[str, Any]) -> None:
    calls, _ = parse_text_tool_calls(json.dumps(payload), NAMES)
    assert [(c.name, c.arguments) for c in calls] == [("submit", {"code": "c"})]


def test_unknown_tool_names_and_ordinary_json_are_left_alone() -> None:
    text = '{"name": "rm_rf", "arguments": {}} and {"runnable": true, "name": 5}'
    calls, rest = parse_text_tool_calls(text, NAMES)
    assert calls == [] and rest == text


def test_two_calls_in_one_message() -> None:
    text = (
        '{"name": "list_dir", "arguments": {"path": "."}}\n'
        '{"name": "list_dir", "arguments": {"path": "src"}}'
    )
    calls, _ = parse_text_tool_calls(text, NAMES)
    assert [c.arguments["path"] for c in calls] == [".", "src"]


def test_assistant_message_round_trips_tool_calls() -> None:
    result = ChatResult(tool_calls=[ToolCall(id="c1", name="run_test", arguments={"code": "x"})])
    message = result.assistant_message()
    assert message["role"] == "assistant" and message["content"] == ""
    function = message["tool_calls"][0]["function"]
    assert message["tool_calls"][0]["id"] == "c1"
    assert function["name"] == "run_test" and json.loads(function["arguments"]) == {"code": "x"}
    assert ChatResult(content="hi").assistant_message() == {"role": "assistant", "content": "hi"}


# -- the real OpenAI client against a local server ------------------------------------------------


@pytest.fixture
def local_server():
    """A throw-away OpenAI-style endpoint on 127.0.0.1. Records requests, replays `responses`."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state: dict[str, Any] = {"requests": [], "responses": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "auth": self.headers["Authorization"],
                                      "body": body})
            status, payload = state["responses"].pop(0)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/v1/"
    yield state
    server.shutdown()
    server.server_close()


async def test_real_client_round_trip_with_tool_call_reasoning_and_usage(local_server) -> None:
    local_server["responses"].append((200, {
        "id": "x", "object": "chat.completion", "created": 1, "model": SUPER,
        "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": None,
                "reasoning_content": "I should run the test first.",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": "run_test", "arguments": json.dumps({"code": "assert 1"})}}],
            },
        }],
        "usage": {"prompt_tokens": 2_000, "completion_tokens": 100, "total_tokens": 2_100},
    }))
    llm = LLM(Settings(nebius_api_key="test-key", nebius_base_url=local_server["url"]))
    result = await llm.chat(MESSAGES, tools=TOOLS, thinking=True, max_tokens=64)

    request = local_server["requests"][0]
    assert request["path"] == "/v1/chat/completions" and request["auth"] == "Bearer test-key"
    assert request["body"]["model"] == SUPER and request["body"]["max_tokens"] == 64
    assert request["body"]["tools"] == TOOLS
    # extra_body keys are merged into the top level of the request
    assert request["body"]["chat_template_kwargs"] == {"enable_thinking": True}

    assert result.tool_calls[0].name == "run_test"
    assert result.tool_calls[0].arguments == {"code": "assert 1"}
    assert result.reasoning == "I should run the test first."
    assert result.usage.input_tokens == 2_000
    assert llm.usage.cost_usd == pytest.approx(2_000 * 0.3e-6 + 100 * 0.9e-6)


async def test_real_client_retries_a_429_with_retry_after_off_the_wire(local_server) -> None:
    ok = {"id": "x", "object": "chat.completion", "created": 1, "model": NANO,
          "choices": [{"index": 0, "finish_reason": "stop",
                       "message": {"role": "assistant", "content": "fine"}}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
    local_server["responses"] += [(429, {"error": {"message": "slow down"}}), (200, ok)]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    settings = Settings(nebius_api_key="k", nebius_base_url=local_server["url"])
    result = await LLM(settings, sleep=sleep).chat(MESSAGES, model=NANO)
    assert result.content == "fine" and len(local_server["requests"]) == 2
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 1.5  # exponential backoff, no Retry-After sent


# -- total spend cap (the persistent ledger) -----------------------------------------------------


def make_capped_llm(
    tmp_path: Path, *responses: Any, total: float = 20.0
) -> tuple[LLM, FakeOpenAI, Settings]:
    client = FakeOpenAI(*responses)
    settings = Settings(nebius_api_key="k", runs_dir=tmp_path / "runs", total_budget_usd=total)
    return LLM(settings, client=client), client, settings


def ledger_json(settings: Settings) -> dict[str, Any]:
    return json.loads((settings.runs_dir / "spend-ledger.json").read_text(encoding="utf-8"))


def test_the_estimate_is_an_upper_bound_that_grows_with_prompt_and_max_tokens() -> None:
    small = estimate_max_cost_usd(SUPER, MESSAGES, None, 100)
    assert estimate_max_cost_usd(SUPER, MESSAGES, None, 4_000) > small
    big = [{"role": "user", "content": "x" * 40_000}]
    assert estimate_max_cost_usd(SUPER, big, None, 100) > small
    assert estimate_max_cost_usd(SUPER, MESSAGES, TOOLS, 100) > small
    nano, super_ = (estimate_max_cost_usd(m, big, None, 100) for m in (NANO, SUPER))
    assert nano < super_  # the cheaper model estimates cheaper
    # 40k characters are about 10k real tokens; the estimate must not be below what they cost.
    assert estimate_max_cost_usd(SUPER, big, None, 0) >= cost_usd(SUPER, 12_000, 0)
    # The output side is priced as if all of max_tokens were used.
    assert estimate_max_cost_usd(SUPER, [], None, 1_000_000) >= 0.90


async def test_every_call_is_recorded_in_the_ledger(tmp_path: Path) -> None:
    llm, _, settings = make_capped_llm(
        tmp_path, completion(prompt=1_000, out=500), completion(prompt=2_000, out=100)
    )
    await llm.chat(MESSAGES)
    assert ledger.spent_total(settings) == pytest.approx(0.00075)
    await llm.chat(MESSAGES, model=NANO)
    expected = 0.00075 + 2_000 * 0.06e-6 + 100 * 0.24e-6
    assert ledger.spent_total(settings) == pytest.approx(expected)
    assert ledger_json(settings)["total_usd"] == pytest.approx(expected)
    assert sum(ledger_json(settings)["days"].values()) == pytest.approx(expected)
    assert ledger.reserved_total(settings) == 0.0  # nothing is left set aside


async def test_the_total_is_shared_by_separate_runs(tmp_path: Path) -> None:
    first, _, settings = make_capped_llm(tmp_path, completion(prompt=1_000, out=500))
    await first.chat(MESSAGES)
    second = LLM(settings, client=FakeOpenAI(completion(prompt=1_000, out=500)))
    await second.chat(MESSAGES)
    assert first.usage.cost_usd == pytest.approx(0.00075)  # per-run totals stay per run
    assert second.usage.cost_usd == pytest.approx(0.00075)
    assert ledger.spent_total(settings) == pytest.approx(0.0015)


async def test_a_call_that_could_pass_the_total_cap_is_refused_before_it_is_sent(
    tmp_path: Path,
) -> None:
    need = estimate_max_cost_usd(SUPER, MESSAGES, None, 4_096)
    llm, client, settings = make_capped_llm(tmp_path, completion(), total=need * 0.99)
    with pytest.raises(TotalBudgetExceeded) as caught:
        await llm.chat(MESSAGES)
    assert client.calls == []  # nothing was sent, so nothing can be billed
    assert isinstance(caught.value, BudgetExceeded)  # callers that handle BudgetExceeded cover it
    assert "total budget reached" in str(caught.value)
    assert caught.value.needed == pytest.approx(need)
    assert caught.value.budget == pytest.approx(need * 0.99)
    assert ledger.spent_total(settings) == 0.0
    assert ledger.reserved_total(settings) == 0.0


async def test_the_refusal_counts_what_was_already_spent(tmp_path: Path) -> None:
    need = estimate_max_cost_usd(SUPER, MESSAGES, None, 100)
    llm, client, settings = make_capped_llm(tmp_path, completion(), completion(), total=1.0)
    ledger.record(settings, 1.0 - need * 1.5)  # room for 1.5 calls
    await llm.chat(MESSAGES, max_tokens=100)  # costs 0.00075, far less than the estimate
    ledger.record(settings, need)  # other runs spend the rest of the headroom
    with pytest.raises(TotalBudgetExceeded) as caught:
        await llm.chat(MESSAGES, max_tokens=100)
    assert len(client.calls) == 1
    assert caught.value.spent == pytest.approx(ledger.spent_total(settings))


async def test_a_call_whose_worst_case_just_fits_the_cap_is_allowed(tmp_path: Path) -> None:
    need = estimate_max_cost_usd(SUPER, MESSAGES, None, 4_096)
    llm, client, _ = make_capped_llm(tmp_path, completion(), total=need)
    await llm.chat(MESSAGES)
    assert len(client.calls) == 1


async def test_the_cap_is_hard_across_many_calls_and_runs(tmp_path: Path) -> None:
    prompt = [{"role": "user", "content": "x" * 4_000}]  # about 1,000 real tokens
    est = estimate_max_cost_usd(SUPER, prompt, None, 200)
    real = cost_usd(SUPER, 1_000, 200)
    assert real < est  # the estimate is the safe side
    cap = 2.2 * est
    client = FakeOpenAI(*[completion(prompt=1_000, out=200) for _ in range(10)])
    settings = Settings(
        nebius_api_key="k", runs_dir=tmp_path / "runs", total_budget_usd=cap, run_budget_usd=100.0
    )
    sent = 0
    for _ in range(10):
        llm = LLM(settings, client=client)  # a fresh run each time: only the ledger remembers
        try:
            await llm.chat(prompt, max_tokens=200)
        except TotalBudgetExceeded:
            break
        sent += 1
        assert ledger.spent_total(settings) <= cap
    assert 0 < sent < 10 and len(client.calls) == sent
    assert ledger.spent_total(settings) == pytest.approx(sent * real)
    # Refused for good: the next call is still refused, and no request goes out.
    with pytest.raises(TotalBudgetExceeded):
        await LLM(settings, client=client).chat(prompt, max_tokens=200)
    assert len(client.calls) == sent


async def test_a_failed_request_costs_nothing_and_releases_its_reservation(
    tmp_path: Path,
) -> None:
    llm, _, settings = make_capped_llm(tmp_path, StatusError(400, "bad request"))
    with pytest.raises(LLMError):
        await llm.chat(MESSAGES)
    assert ledger.spent_total(settings) == 0.0
    assert ledger.reserved_total(settings) == 0.0


async def test_a_reply_that_cannot_be_read_is_billed_at_the_worst_case(tmp_path: Path) -> None:
    empty = SimpleNamespace(choices=[], usage=None)
    llm, _, settings = make_capped_llm(tmp_path, empty)
    with pytest.raises(LLMError, match="no choices"):
        await llm.chat(MESSAGES, max_tokens=100)
    assert ledger.spent_total(settings) == pytest.approx(
        estimate_max_cost_usd(SUPER, MESSAGES, None, 100)
    )
    assert ledger.reserved_total(settings) == 0.0


async def test_a_cancelled_call_is_billed_at_the_worst_case(tmp_path: Path) -> None:
    started = asyncio.Event()

    class HangingClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs: Any) -> Any:
            started.set()
            await asyncio.Event().wait()  # never answers

    settings = Settings(nebius_api_key="k", runs_dir=tmp_path / "runs")
    llm = LLM(settings, client=HangingClient())
    task = asyncio.create_task(llm.chat(MESSAGES, max_tokens=100))
    await started.wait()
    assert ledger.reserved_total(settings) > 0  # in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.spent_total(settings) == pytest.approx(
        estimate_max_cost_usd(SUPER, MESSAGES, None, 100)
    )
    assert ledger.reserved_total(settings) == 0.0


async def test_concurrent_calls_cannot_both_claim_the_last_of_the_budget(tmp_path: Path) -> None:
    need = estimate_max_cost_usd(SUPER, MESSAGES, None, 100)

    class SlowClient(FakeOpenAI):
        async def _create(self, **kwargs: Any) -> Any:
            await asyncio.sleep(0.01)  # the first call is still in flight when the second asks
            return await super()._create(**kwargs)

    client = SlowClient(completion(), completion())
    settings = Settings(
        nebius_api_key="k", runs_dir=tmp_path / "runs", total_budget_usd=need * 1.5
    )
    results = await asyncio.gather(
        LLM(settings, client=client).chat(MESSAGES, max_tokens=100),
        LLM(settings, client=client).chat(MESSAGES, max_tokens=100),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ChatResult) for r in results) == 1
    assert sum(isinstance(r, TotalBudgetExceeded) for r in results) == 1
    assert len(client.calls) == 1
    assert ledger.reserved_total(settings) == 0.0


async def test_an_unreadable_ledger_stops_all_spending(tmp_path: Path) -> None:
    llm, client, settings = make_capped_llm(tmp_path, completion())
    settings.runs_dir.mkdir()
    (settings.runs_dir / "spend-ledger.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(TotalBudgetExceeded, match="total budget reached.*cannot be read"):
        await llm.chat(MESSAGES)
    assert client.calls == []
    assert (settings.runs_dir / "spend-ledger.json").read_text(encoding="utf-8") == "{broken"


async def test_a_ledger_that_cannot_be_written_does_not_lose_the_answer_or_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm, _, settings = make_capped_llm(tmp_path, completion(prompt=1_000, out=500))

    def broken_write(path: Path, data: dict[str, Any]) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(ledger, "_write", broken_write)
    result = await llm.chat(MESSAGES)  # the reply is returned...
    assert result.content == "hello"
    assert ledger.spent_total(settings) == pytest.approx(0.00075)  # ...and still counted


async def test_a_zero_total_budget_is_a_kill_switch(tmp_path: Path) -> None:
    llm, client, _ = make_capped_llm(tmp_path, completion(), total=0.0)
    with pytest.raises(TotalBudgetExceeded):
        await llm.chat(MESSAGES, max_tokens=1)
    assert client.calls == []


async def test_the_per_run_budget_still_applies_under_the_total_cap(tmp_path: Path) -> None:
    client = FakeOpenAI(completion(prompt=1_000_000, out=0))
    settings = Settings(
        nebius_api_key="k", runs_dir=tmp_path / "runs", run_budget_usd=0.25, total_budget_usd=20.0
    )
    with pytest.raises(BudgetExceeded) as caught:
        await LLM(settings, client=client).chat(MESSAGES)
    assert not isinstance(caught.value, TotalBudgetExceeded)
    assert ledger.spent_total(settings) == pytest.approx(0.30)  # the call was made, so it counts
