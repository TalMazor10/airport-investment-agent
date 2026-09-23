# Airport Investment Agent

![tests](https://github.com/TalMazor10/airport-investment-agent/actions/workflows/tests.yml/badge.svg)

A chat assistant that helps an investment firm find US airports where renovation would pay off.
It ranks and compares airports from ten years of public US government aviation data, explains
its reasoning, and answers follow-up questions. Every figure it quotes is calculated by ordinary,
repeatable code; the AI model reads the question and explains the answer.

Example questions:

- Which airports in New England are strong candidates for terminal expansion?
- Compare LA and Santa Ana airport congestion levels.
- What is the percentage of long haul flights out of Anchorage airport?
- What is the unmet flight demand in SFO airport and why?

How it works, and why it was built this way, is explained in **[DESIGN.md](DESIGN.md)**.

---

## What is needed before starting

| Needed | Why | How to check or get it |
|---|---|---|
| **A computer** running Windows, macOS or Linux | | |
| **Python 3.10 or newer** | The program is written in Python | Open a terminal (Command Prompt on Windows, Terminal on macOS) and type `python --version`. If the number is 3.10 or higher, it is ready. If not, install it from [python.org/downloads](https://www.python.org/downloads/). On Windows, tick **"Add Python to PATH"** during installation. |
| **Git** (optional) | To download the project | Type `git --version`. Without Git, download the project as a ZIP file from GitHub (green **Code** button, then **Download ZIP**) and unzip it. |
| **An API key from an AI provider** | The AI model (a large language model, LLM) runs on the provider's servers | Any provider that offers the standard chat completions interface works: OpenAI, Anthropic, Google Gemini, Mistral, OpenRouter and others. The table in step 4 lists where to get a key. A small prepaid credit is enough: a question cost about one US cent in testing. A model run locally with Ollama needs no key at all. |
| **An internet connection** | To reach the AI provider | The airport data itself is included and stays on the computer. |
| **Chrome or Edge** (optional) | Only for dictating questions by voice | Any browser works for typing. |

No database, cloud account or other setup is required. The data is included in the project.

---

## Setup, step by step

Commands are shown for Windows and for macOS or Linux where they differ. Type each one in the
terminal and press Enter.

**1. Download the project**

```
git clone https://github.com/TalMazor10/airport-investment-agent.git
cd airport-investment-agent
```

(Or unzip the downloaded ZIP file and open a terminal in that folder.)

**2. Create a private Python environment for the project**

This keeps the project's libraries separate from anything else on the computer.

| Windows | macOS or Linux |
|---|---|
| `python -m venv .venv` | `python3 -m venv .venv` |
| `.venv\Scripts\activate` | `source .venv/bin/activate` |

After this, the terminal line starts with `(.venv)`.

**3. Install the libraries the project uses**

```
python -m pip install -r requirements.txt
```

**4. Choose the AI provider and add the key**

Make a copy of the file `.env.example` and name the copy `.env`. Open `.env` in any text editor
and fill in three lines, with no spaces and no quotes:

```
LLM_BASE_URL=   the provider's address, from the table below
LLM_MODEL=      the model's name, exactly as the provider lists it
LLM_API_KEY=    the key from the provider's website
```

| Provider | `LLM_BASE_URL` | Where to get a key |
|---|---|---|
| OpenAI | leave empty | [platform.openai.com](https://platform.openai.com) |
| Anthropic | `https://api.anthropic.com/v1/` | [console.anthropic.com](https://console.anthropic.com), creating the key inside a workspace |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | [aistudio.google.com](https://aistudio.google.com) |
| Mistral | `https://api.mistral.ai/v1` | [console.mistral.ai](https://console.mistral.ai) |
| OpenRouter (many providers through one key) | `https://openrouter.ai/api/v1` | [openrouter.ai](https://openrouter.ai) |
| Ollama (a model running on this computer) | `http://localhost:11434/v1` | No key needed: type any word, for example `ollama` |

The model must support **tool calling** (also called function calling): the agent works by asking
the model which lookups to run. Most current models from these providers do. The provider's
documentation lists its model names. Development testing used Anthropic's address; the other
providers accept the same standard interface.

The `.env` file never leaves the computer: it is excluded from the repository. Alternatively, the
key can be pasted into the box in the application's sidebar each time; the address and model
still come from `.env`.

**5. Start the application**

```
python -m streamlit run app.py
```

Then open **http://localhost:8501** in a web browser. For security, the application accepts
connections from this computer only; otherwise anyone on the same network could open it and use
the API key.

To stop it, press `Ctrl+C` in the terminal.

---

## Using it

- Type a question in the box at the bottom and press Enter, or click one of the example questions
  in the sidebar.
- Under each answer, **Tools used** shows exactly what the assistant looked up and with which
  inputs, for example which states it took "New England" to mean.
- In Chrome or Edge, the **microphone** in the question box dictates a question. Speech appears in
  the box as text, where it can be edited before sending. The browser's speech recognition runs on
  its maker's servers (Google or Microsoft); typed questions go only to the chosen AI provider.
- Follow-up questions refer back to the conversation. **New conversation** in the sidebar starts
  again.
- Each question and answer is also saved to `query_log.jsonl` on this computer only.

---

## Running the tests

```
python -m pytest -v
```

The tests need no API key and cost nothing: the AI model is replaced by a scripted stand-in. One
test compares the stored data with the live government source and is skipped without an internet
connection. GitHub runs the same tests automatically on every change, on Python 3.10 and 3.14
(the badge at the top shows the latest result).

---

## Rebuilding the data (optional)

The database in `data/airports.db` is already built. To rebuild it from the public sources:

```
python prepare_data.py
```

The first run downloads about 3 GB of government files into `cache/` and took about 110 minutes
when measured. Later runs reuse the downloads: `python prepare_data.py --skip-download`.

---

## What is in the project

| File | What it does |
|---|---|
| `app.py` | The chat page |
| `agent.py` | Passes questions to the AI model and runs the lookups it asks for. Works with any provider that offers the standard chat completions interface |
| `tools.py` | The four lookups the model can use: find airports, score airports, airport profile, flight distance mix |
| `scoring.py` | The ranking method: every number and every judgment in the score |
| `voice.py` | Voice dictation in the question box |
| `prepare_data.py` | Builds the database from the public sources |
| `data/airports.db` | The built database: US airport traffic, delays and population, 2016 to 2025 |
| `tests/` | Automated checks on the data, the scoring, the lookups, the agent and the page |
| `DESIGN.md` | How the system works and why it was designed this way |
