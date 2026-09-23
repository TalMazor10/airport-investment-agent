"""
Chat interface for the airport investment agent.

    streamlit run app.py

Each answer is followed by a collapsible list of the tool calls the model made
and their inputs, so the model's interpretation of the question (for example
which states a region name became) can be checked. The conversation is kept
for the browser session only. Questions can also be dictated into the chat box
in Chrome and Edge (see voice.py).
"""

import json
from urllib.parse import urlparse

import openai
import streamlit as st

import agent
import voice

EXAMPLES = [
    "Which airports in New England are strong candidates for terminal expansion?",
    "Compare LA and Santa Ana airport congestion levels.",
    "What is the percentage of long haul flights out of Anchorage airport?",
    "What is the unmet flight demand in SFO airport and why?",
]

st.set_page_config(page_title="Airport Investment Agent", page_icon="✈️", layout="centered")

if "history" not in st.session_state:
    st.session_state.history = []      # API messages, including tool calls
    st.session_state.turns = []        # what the page displays


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------

with st.sidebar:
    st.header("Airport Investment Agent")
    st.caption("Ranks and explains US airports as renovation candidates, from US Bureau of "
               "Transportation Statistics and Census data for 2016 to 2025. Every figure comes "
               "from deterministic code; the language model interprets questions and explains "
               "results.")

    settings = agent.load_settings()
    if settings["api_key"]:
        st.success("API key loaded from .env", icon="🔑")
    else:
        settings["api_key"] = st.text_input(
            "LLM API key", type="password",
            help="Used for this session only. To keep it, add it to .env as LLM_API_KEY.")
    if not settings["model"]:
        st.warning("No model chosen. Set LLM_MODEL in .env (see README.md).")

    voice.dictation_button()
    st.caption("🎤 Dictation (the microphone in the chat box) works in Chrome and Edge.")

    st.subheader("Example questions")
    for example in EXAMPLES:
        if st.button(example, width="stretch"):
            st.session_state.pending = example

    if st.button("New conversation", type="secondary", width="stretch"):
        st.session_state.history = []
        st.session_state.turns = []
        st.rerun()

    provider = urlparse(settings["base_url"]).netloc if settings["base_url"] else "api.openai.com"
    st.caption(f"Model: {settings['model'] or 'not set'} ({provider})")


# ----------------------------------------------------------------------------
# Conversation
# ----------------------------------------------------------------------------

def show_tool_calls(calls):
    if not calls:
        return
    with st.expander(f"Tools used ({len(calls)})"):
        for call in calls:
            args = ", ".join(f"{k}={json.dumps(v)}" for k, v in call["input"].items())
            marker = "  ⚠ returned an error" if call["error"] else ""
            st.code(f"{call['tool']}({args}){marker}", language="python")


for turn in st.session_state.turns:
    with st.chat_message(turn["role"]):
        st.markdown(turn["text"])
        if turn["role"] == "assistant":
            show_tool_calls(turn["tool_calls"])

question = st.chat_input("Ask about US airports") or st.session_state.pop("pending", None)

if question:
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        if not settings["api_key"] or not settings["model"]:
            st.warning("An LLM API key and a model are needed. See the sidebar and README.md.")
            st.stop()

        with st.status("Working on it...", expanded=True) as status:
            def on_tool_call(name, arguments):
                args = ", ".join(f"{k}={json.dumps(v)}" for k, v in arguments.items())
                status.write(f"Calling `{name}({args})`")

            try:
                result = agent.ask(agent.make_client(settings), settings["model"],
                                   st.session_state.history, question, on_tool_call)
            except openai.AuthenticationError:
                status.update(label="The API key was rejected.", state="error")
                st.stop()
            except openai.RateLimitError:
                status.update(label="Rate limited by the API. Try again shortly.", state="error")
                st.stop()
            except openai.APIConnectionError:
                status.update(label="Could not reach the API. Check the connection and LLM_BASE_URL.",
                              state="error")
                st.stop()
            except openai.APIStatusError as e:
                status.update(label=f"API error {e.status_code}: {e.message}", state="error")
                st.stop()
            status.update(label="Done", state="complete", expanded=False)

        st.markdown(result["answer"])
        show_tool_calls(result["tool_calls"])

    agent.log_query(question, result)
    st.session_state.history = result["history"]
    st.session_state.turns += [
        {"role": "user", "text": question},
        {"role": "assistant", "text": result["answer"], "tool_calls": result["tool_calls"]},
    ]
