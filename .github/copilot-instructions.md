# GitHub Copilot Instructions — my-dev-team

## Project Overview

`my-dev-team` is an AI-powered automated development pipeline built with **CrewAI** and **Google Gemini** LLMs. It orchestrates a team of 6 specialized AI agents that collaborate to analyze requirements, design architecture, write code, run QC tests, and produce a final deliverable — all inside a sandboxed `workspace/` directory.

A **Telegram bot** (`bot.py`) serves as the user-facing dashboard to trigger and monitor pipelines.

---

## Architecture

```
my-dev-team/
├── main.py          # Entry point: pipeline orchestration, cycle loop, RAG setup
├── agents.py        # 6 CrewAI agent definitions (PM, PlanReviewer, Architect, Coder, QC, Reviewer)
├── tasks.py         # 7-task pipeline: t1→t2→t3→t4→t5→t6→t7
├── tools.py         # Security-filtered custom tools for agents (read/write/search/exec)
├── utils.py         # LLMFactory with API key rotation (RobustGeminiLLM)
├── bot.py           # Telegram bot: /dev, /status, /cancel, /push commands
├── config.js        # Frontend Google API config (workspace output, not agent config)
├── requirements.txt # Python dependencies
└── workspace/       # Agent output sandbox (configurable via AGENT_WORKSPACE env var)
    ├── src/         # Generated source code
    ├── reports/     # Agent reports (t1–t7 markdown files)
    └── tests/       # Generated test files
```

### Agent Pipeline (6 agents, 7 tasks)

| Task | Agent          | Responsibility                                             |
|------|----------------|------------------------------------------------------------|
| t1   | PM             | Analyze request → User Stories + Done Criteria + risks     |
| t2   | Plan Reviewer  | Review & approve PM's plan before execution                |
| t3   | Architect      | Design directory structure, choose patterns, flag risks    |
| t4   | Coder          | Write/modify code files per Architect's spec               |
| t5   | QC             | Write unit tests, run pytest, report failures              |
| t6   | Reviewer       | Code style, clean code, security review                    |
| t7   | PM             | Synthesize final report for the user                       |

---

## Key Design Decisions

### LLM Strategy
- **Flash model** (`get_flash_model`): Used for PM, Plan Reviewer, Architect — fast, cost-efficient tasks.
- **Pro model** (`get_pro_model`): Used for Coder and QC — complex reasoning required.
- **Key rotation**: `RobustGeminiLLM` in `utils.py` cycles through `GEMINI_KEY_1..10` on 429/quota errors with exponential backoff.

### Security (tools.py)
- All agent file I/O is sandboxed inside `WORKSPACE_ROOT` (set via `AGENT_WORKSPACE` env var, defaults to `./workspace`).
- `_BLOCKED_PATTERNS` in `tools.py` blocks access to `.env`, API keys, certs, `.git`, `venv`, `node_modules`, and IDE files.
- Never add hardcoded credentials anywhere in source code.
- The Telegram bot (`bot.py`) restricts commands to `TG_USER_IDS` (allowlist).

### State & Logging
- Pipeline state is stored in `workspace/state.json`.
- All pipeline history is written to `workspace/crew_history.log`.
- Reports are written to `workspace/reports/tN_*.md`.

---

## Environment Variables

| Variable           | Required | Description                                      |
|--------------------|----------|--------------------------------------------------|
| `GEMINI_KEY_1..10` | Yes      | Gemini API keys (at least 1 required)            |
| `AGENT_WORKSPACE`  | No       | Absolute path for agent output (default: `./workspace`) |
| `TG_BOT_TOKEN`     | Bot only | Telegram bot token from BotFather                |
| `TG_USER_IDS`      | Bot only | Comma-separated allowed Telegram user IDs        |
| `GIT_AUTHOR_NAME`  | No       | Fallback git author name for `/push` command     |
| `GIT_AUTHOR_EMAIL` | No       | Fallback git author email for `/push` command    |

Store all secrets in `.env` — never hardcode them.

---

## Running the Project

```bash
# Run the pipeline once (one cycle)
python main.py

# Run Telegram bot dashboard
python bot.py

# Run only syntax/compile check
python -m py_compile tools.py agents.py tasks.py main.py bot.py
```

---

## Code Conventions

### Python
- Python 3.10+ syntax; use `match/case`, `|` union types, `X | None` over `Optional[X]`.
- Use `pathlib.Path` everywhere — avoid raw string path manipulation.
- Load environment variables with `python-dotenv` (`load_dotenv()` at module top).
- Agents must use tools (e.g., `safe_file_write`) to write files — never print file content to chat.
- Do not use markdown code fences inside file content written by agents.
- Keep `max_iter` and `max_rpm` conservative to prevent agent spin loops.

### CrewAI Patterns
- All agents have `allow_delegation=False` and `memory=False` to keep pipeline deterministic.
- Tasks use `output_file` with workspace-relative paths (no `workspace/` prefix in the path string — CrewAI resolves relative to `_WS_REL`).
- Inject `_PATH_NOTE` instruction into task descriptions to enforce correct path format.

### Tools
- `safe_file_write` / `safe_file_read`: Workspace-sandboxed, security-filtered I/O.
- `codebase_search`: Semantic search over workspace files (backed by ChromaDB RAG).
- `execution_checker`: Runs shell commands in a restricted, sandboxed environment.
- `code_interpreter`: Used only by the Coder agent.

### Frontend Output (workspace/src/)
- The default `USER_REQUEST` generates a vanilla JS + HTML + CSS Google Sheets quiz app.
- No build tools — pure browser-native code only.
- API keys must never be hardcoded; use environment-level injection or a backend proxy.

---

## Adding New Agents or Tasks

1. Define the agent in `agents.py` using `Agent(...)` and assign an appropriate LLM tier.
2. Add the task in `tasks.py` with a clear `description`, `expected_output`, and `output_file`.
3. Update the `Crew` instantiation in `main.py` to include the new agent and task in order.
4. Update `TASKS` / `TASK_LABELS` in `bot.py` if the task needs dashboard visibility.

---

## Do NOT

- Do not hardcode API keys, tokens, or credentials anywhere.
- Do not write to paths outside `WORKSPACE_ROOT` from within agents.
- Do not add markdown fences (` ``` `) to file content written via `safe_file_write`.
- Do not increase `max_iter` beyond 10 without a documented reason — it causes infinite loops.
- Do not skip the Plan Reviewer step (t2) — it acts as a guardrail against bad architectures.
- Do not modify `_BLOCKED_PATTERNS` in `tools.py` to weaken security restrictions.
- Do not hardcode any stack or architecture assumptions in the agents' prompts — they should be adaptable to any codebase structure.
