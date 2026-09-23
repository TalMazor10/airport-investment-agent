"""
Tests for agent.py, using a scripted stand-in for the language model.

The stand-in returns prepared chat completions responses in order, so the
loop's behaviour can be checked without an API key, a network connection or
any cost: that tool calls are executed and their results returned under the
right id, that malformed arguments are reported rather than run, that the loop
stops at its limit, that a failed call leaves the conversation intact, and
that each question is logged locally.

    pytest -v tests/test_agent.py
"""

import json
from types import SimpleNamespace

import pytest

import agent
import tools

MODEL = "test-model"


def call(name, arguments, id_="call_1", raw=None):
    arguments = raw if raw is not None else json.dumps(arguments)
    return SimpleNamespace(id=id_, type="function",
                           function=SimpleNamespace(name=name, arguments=arguments))


def reply(content=None, calls=(), finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=list(calls) or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])


class ScriptedModel:
    """Returns prepared responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_a_tool_call_is_executed_and_its_result_returned_under_the_same_id():
    model = ScriptedModel(
        reply(calls=[call("haul_mix", {"airport": "ANC"}, "abc")], finish="tool_calls"),
        reply("41.7% of departures are long haul."),
    )
    result = agent.ask(model, MODEL, [], "Long haul share at Anchorage?")

    assert result["answer"] == "41.7% of departures are long haul."
    assert result["tool_calls"] == [{"tool": "haul_mix", "input": {"airport": "ANC"}, "error": False}]
    tool_message = model.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "abc"
    assert json.loads(tool_message["content"])["all_flights"]["long_haul_pct"] == pytest.approx(41.7)


def test_several_tool_calls_in_one_reply_are_all_answered():
    model = ScriptedModel(
        reply(calls=[call("airport_profile", {"airport": "LAX"}, "a"),
                     call("airport_profile", {"airport": "SNA"}, "b")], finish="tool_calls"),
        reply("done"),
    )
    agent.ask(model, MODEL, [], "Compare LAX and SNA")
    sent = model.requests[1]["messages"]
    assert [m["tool_call_id"] for m in sent if m["role"] == "tool"] == ["a", "b"]


def test_a_failed_tool_call_is_flagged_to_the_model():
    model = ScriptedModel(
        reply(calls=[call("haul_mix", {"airport": "ZZZ"})], finish="tool_calls"),
        reply("No data for that airport."),
    )
    result = agent.ask(model, MODEL, [], "Long haul at ZZZ?")
    assert result["tool_calls"][0]["error"] is True
    assert "error" in json.loads(model.requests[1]["messages"][-1]["content"])


def test_malformed_arguments_are_reported_and_not_run():
    model = ScriptedModel(
        reply(calls=[call("haul_mix", None, raw="{not json")], finish="tool_calls"),
        reply("Retried."),
    )
    result = agent.ask(model, MODEL, [], "Long haul at Anchorage?")
    assert result["tool_calls"][0]["error"] is True
    assert "not valid JSON" in json.loads(model.requests[1]["messages"][-1]["content"])["error"]


def test_every_request_carries_the_instructions_the_tools_and_the_chosen_model():
    model = ScriptedModel(reply("hello"))
    agent.ask(model, MODEL, [], "hi")
    request = model.requests[0]
    assert request["model"] == MODEL
    assert request["messages"][0] == {"role": "system", "content": agent.SYSTEM_PROMPT}
    assert request["tools"] == agent.FUNCTION_TOOLS
    assert [t["function"]["name"] for t in agent.FUNCTION_TOOLS] == \
        [d["name"] for d in tools.TOOL_DEFINITIONS]


def test_the_conversation_carries_over_to_the_next_question():
    first = agent.ask(ScriptedModel(reply("Boston.")), MODEL, [], "Best in New England?")
    model = ScriptedModel(reply("Because of delay."))
    agent.ask(model, MODEL, first["history"], "Why?")
    sent = model.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "Best in New England?"


def test_the_loop_stops_at_its_limit():
    endless = [reply(calls=[call("haul_mix", {"airport": "ANC"})], finish="tool_calls")
               for _ in range(agent.MAX_TOOL_ROUNDS + 1)]
    result = agent.ask(ScriptedModel(*endless), MODEL, [], "loop forever")
    assert "limit" in result["answer"]
    assert result["history"] == []


def test_an_api_failure_leaves_the_conversation_untouched():
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
    snapshot = list(history)
    with pytest.raises(RuntimeError):
        agent.ask(ScriptedModel(RuntimeError("network down")), MODEL, history, "next")
    assert history == snapshot


def test_a_truncated_answer_is_marked():
    result = agent.ask(ScriptedModel(reply("Partial", finish="length")), MODEL, [], "q")
    assert "incomplete" in result["answer"]


def test_each_question_is_logged_locally_with_its_model(tmp_path):
    log = tmp_path / "query_log.jsonl"
    result = {"answer": "Boston.", "model": MODEL,
              "tool_calls": [{"tool": "find_airports", "input": {"states": ["MA"]}, "error": False}]}
    agent.log_query("Best in New England?", result, path=log)
    agent.log_query("Why?", result, path=log)
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["question"] for e in lines] == ["Best in New England?", "Why?"]
    assert lines[0]["model"] == MODEL and lines[0]["tool_calls"][0]["tool"] == "find_airports"


def test_settings_are_read_from_the_env_file_and_the_environment_takes_precedence(tmp_path, monkeypatch):
    for name in agent.SETTINGS:
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("# comment\nLLM_BASE_URL=https://llm.example/v1\n"
                   "LLM_MODEL=model-a\nLLM_API_KEY=key-123\n", encoding="utf-8")
    monkeypatch.setattr(agent, "ENV_FILE", env)
    assert agent.load_settings() == {"base_url": "https://llm.example/v1",
                                     "model": "model-a", "api_key": "key-123"}
    monkeypatch.setenv("LLM_MODEL", "model-b")
    assert agent.load_settings()["model"] == "model-b"


def test_a_missing_key_or_model_is_reported_before_any_request(tmp_path, monkeypatch):
    for name in agent.SETTINGS:
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=\nLLM_MODEL=\nLLM_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(agent, "ENV_FILE", env)
    settings = agent.load_settings()
    assert settings == {"base_url": None, "model": None, "api_key": None}
    with pytest.raises(ValueError, match="API key"):
        agent.make_client(settings)
    with pytest.raises(ValueError, match="model"):
        agent.make_client({**settings, "api_key": "key-123"})
