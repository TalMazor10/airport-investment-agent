"""
Tests for app.py, run headlessly with Streamlit's testing harness.

The agent is replaced by a stand-in, so the page can be exercised without an
API key: it renders, asks for a key when none is configured, sends a question
to the agent, shows the answer with the tool calls behind it, and keeps the
conversation for the next question.

    pytest -v tests/test_app.py
"""

import pytest
from streamlit.testing.v1 import AppTest

import agent

ANSWER = "Boston ranks first of ten in New England."
CALLS = [{"tool": "find_airports", "input": {"states": ["MA", "RI"]}, "error": False}]


@pytest.fixture
def stand_in_agent(monkeypatch, tmp_path):
    asked = []

    def ask(client, model, history, question, on_tool_call=None):
        asked.append((list(history), question))
        if on_tool_call:
            on_tool_call("find_airports", {"states": ["MA", "RI"]})
        return {"answer": ANSWER, "tool_calls": CALLS,
                "history": history + [{"role": "user", "content": question},
                                      {"role": "assistant", "content": ANSWER}]}

    monkeypatch.setattr(agent, "load_settings",
                        lambda: {"base_url": None, "model": "test-model", "api_key": "sk-test"})
    monkeypatch.setattr(agent, "make_client", lambda settings: object())
    monkeypatch.setattr(agent, "ask", ask)
    monkeypatch.setattr(agent, "QUERY_LOG", tmp_path / "query_log.jsonl")
    return asked


def test_the_page_asks_for_a_key_when_none_is_configured(monkeypatch):
    monkeypatch.setattr(agent, "load_settings",
                        lambda: {"base_url": None, "model": None, "api_key": None})
    at = AppTest.from_file("../app.py").run()
    assert not at.exception
    assert at.sidebar.text_input[0].label == "LLM API key"


def test_a_question_shows_the_answer_and_the_tools_used(stand_in_agent):
    at = AppTest.from_file("../app.py").run()
    at.chat_input[0].set_value("Best airports in New England?").run()
    assert not at.exception
    assert any(ANSWER in m.value for m in at.markdown)
    assert "find_airports" in at.code[0].value


def test_an_example_button_asks_its_question(stand_in_agent):
    at = AppTest.from_file("../app.py").run()
    at.sidebar.button[0].click().run()
    assert stand_in_agent[0][1].startswith("Which airports in New England")


def test_a_follow_up_receives_the_earlier_conversation(stand_in_agent):
    at = AppTest.from_file("../app.py").run()
    at.chat_input[0].set_value("Best airports in New England?").run()
    at.chat_input[0].set_value("Why?").run()
    history, question = stand_in_agent[1]
    assert question == "Why?"
    assert history[0]["content"] == "Best airports in New England?"
