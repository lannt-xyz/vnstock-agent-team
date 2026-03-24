"""
Workspace-aware, security-filtered tools for CrewAI agents.

Configuration (via .env or environment variable):
    AGENT_WORKSPACE=/absolute/path/to/your/workspace
    → Agents can only read/write inside this directory.
    → If not set, defaults to ./workspace relative to the project.

Security rules (see _BLOCKED_PATTERNS):
    Blocked reads/writes: .env files, API keys, certs, __pycache__,
    compiled Python, .git internals, venv, node_modules, IDE files.
"""

import glob as _glob
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from crewai_tools import CodeInterpreterTool
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ConfigDict

load_dotenv()

# ── Workspace config ──────────────────────────────────────────────────────────
# Set AGENT_WORKSPACE to any absolute path (can be outside this project):
#   export AGENT_WORKSPACE=/home/user/trading-workspace
WORKSPACE_ROOT: Path = Path(os.getenv("AGENT_WORKSPACE", "./workspace")).resolve()

# ── Security: blocked path patterns ──────────────────────────────────────────
# Matched against the full resolved POSIX path string (case-insensitive).
_BLOCKED_PATTERNS: list[str] = [
    # ── Environment & secrets ──────────────────────────────────────────────
    r"(^|[\\/])\.env(\.|$)",              # .env  .env.local  .env.production
    r"(^|[\\/])\.envrc$",                 # direnv config
    r".*\.(key|pem|p12|pfx|crt|cer|der)$",  # Crypto keys & certificates
    r"(^|[\\/])(secrets?|passwords?|credentials?|tokens?|api_?keys?)[\\/]",

    # ── Python artifacts ───────────────────────────────────────────────────
    r"(^|[\\/])__pycache__[\\/]",
    r".*\.py[cod]$",                      # .pyc .pyo .pyd

    # ── Version control ────────────────────────────────────────────────────
    r"(^|[\\/])\.git[\\/]",
    r"(^|[\\/])\.svn[\\/]",
    r"(^|[\\/])\.hg[\\/]",

    # ── Virtual environments & package managers ────────────────────────────
    r"(^|[\\/])(venv|\.venv|env|virtualenv|\.virtualenv)[\\/]",
    r"(^|[\\/])node_modules[\\/]",

    # ── IDE & OS junk ──────────────────────────────────────────────────────
    r"(^|[\\/])\.idea[\\/]",
    r"(^|[\\/])\.vscode[\\/]",
    r".*\.DS_Store$",
    r"(^|[\\/])Thumbs\.db$",
    r".*\.swp$",                          # Vim swap files
]


def _is_blocked(path: Path) -> bool:
    """Return True if the path matches any security exclusion pattern."""
    s = path.as_posix()
    return any(re.search(p, s, re.IGNORECASE) for p in _BLOCKED_PATTERNS)


def _is_within_workspace(path: Path) -> bool:
    """Return True if the resolved path is inside WORKSPACE_ROOT."""
    try:
        path.resolve().relative_to(WORKSPACE_ROOT)
        return True
    except ValueError:
        return False


def _resolve(raw: str) -> Path:
    """
    Resolve a user-supplied path:
    - If relative → rooted at WORKSPACE_ROOT
    - If absolute → used as-is (still validated by _is_within_workspace)
    """
    p = Path(raw).expanduser()
    return (WORKSPACE_ROOT / p).resolve() if not p.is_absolute() else p.resolve()


# ── Tool: Read File ───────────────────────────────────────────────────────────

class _ReadInput(BaseModel):
    file_path: str = Field(
        ...,
        description=(
            "Path to the file to read. "
            "Can be relative to workspace root (e.g. 'ml/strategy.py') "
            "or an absolute path inside the workspace."
        ),
    )


class SafeFileReadTool(BaseTool):
    name: str = "Read File"
    description: str = (
        f"Read the content of a file inside the workspace at {WORKSPACE_ROOT}. "
        "Files outside the workspace or matching security rules "
        "(.env, __pycache__, .pyc, .git, venv, crypto keys…) are automatically blocked."
    )
    args_schema: Type[BaseModel] = _ReadInput

    def _run(self, file_path: str) -> str:
        target = _resolve(file_path)
        if not _is_within_workspace(target):
            return (
                f"[BLOCKED] '{target}' is outside the allowed workspace "
                f"({WORKSPACE_ROOT}). Use a path inside the workspace."
            )
        if _is_blocked(target):
            return f"[BLOCKED] '{target.name}' is denied by the security policy."
        if not target.exists():
            return f"[ERROR] File not found: {target}"
        if not target.is_file():
            return f"[ERROR] Not a file: {target}"
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[ERROR] Cannot read file: {exc}"


# ── Tool: List Directory ──────────────────────────────────────────────────────

class _ListInput(BaseModel):
    directory_path: str = Field(
        default="",
        description=(
            "Subdirectory to list, relative to workspace root "
            "(e.g. 'ml', 'data', 'reports'). "
            "Leave empty to list the workspace root itself."
        ),
    )


class SafeDirectoryReadTool(BaseTool):
    name: str = "List Directory"
    description: str = (
        f"List files and subdirectories inside the workspace ({WORKSPACE_ROOT}). "
        "Sensitive items (.env, __pycache__, .pyc, .git, venv, node_modules…) "
        "are automatically hidden from the listing."
    )
    args_schema: Type[BaseModel] = _ListInput

    def _run(self, directory_path: str = "") -> str:
        target = _resolve(directory_path) if directory_path else WORKSPACE_ROOT
        if not _is_within_workspace(target):
            return (
                f"[BLOCKED] '{target}' is outside the allowed workspace "
                f"({WORKSPACE_ROOT})."
            )
        if not target.exists():
            return f"[ERROR] Directory not found: {target}"
        if not target.is_dir():
            return f"[ERROR] Not a directory: {target}"

        lines = [f"Contents of {target}:"]
        for item in sorted(target.iterdir()):
            if _is_blocked(item):
                continue
            tag = "[DIR] " if item.is_dir() else "[FILE]"
            lines.append(f"  {tag} {item.name}")

        if len(lines) == 1:
            lines.append("  (empty, or all items hidden by security policy)")
        return "\n".join(lines)


# ── Tool: Write File ──────────────────────────────────────────────────────────

class _WriteInput(BaseModel):
    file_path: str = Field(
        ...,
        description=(
            "Destination file path, relative to workspace root "
            "(e.g. 'ml/optimized_strategy.py') or absolute inside workspace."
        ),
    )
    content: str = Field(..., description="Full text content to write to the file.")
    overwrite: bool = Field(default=True, description="Overwrite if the file already exists.")


class SafeFileWriterTool(BaseTool):
    name: str = "Write File"
    description: str = (
        f"Write text content to a file inside the workspace ({WORKSPACE_ROOT}). "
        "Cannot write outside the workspace or to blocked paths. "
        "Parent directories are created automatically."
    )
    args_schema: Type[BaseModel] = _WriteInput

    def _run(self, file_path: str, content: str, overwrite: bool = True) -> str:
        target = _resolve(file_path)
        if not _is_within_workspace(target):
            return (
                f"[BLOCKED] '{target}' is outside the allowed workspace "
                f"({WORKSPACE_ROOT})."
            )
        if _is_blocked(target):
            return f"[BLOCKED] Writing to '{target.name}' is denied by security policy."
        if target.exists() and not overwrite:
            return f"[SKIPPED] File already exists: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"[TOOL] Writing {len(content)} bytes to {target}...")
        target.write_text(content, encoding="utf-8")
        rel = target.relative_to(WORKSPACE_ROOT).as_posix()
        return f"SUCCESS: {rel}"


# ── Tool: Final Analysis (compatibility) ─────────────────────────────────────

class _FinalAnalysisInput(BaseModel):
    """Accept flexible payloads from LLM function-calls.

    Some models may attempt to call a `final_analysis` tool even when it wasn't
    provided. This schema allows arbitrary keys and optional known fields so the
    call succeeds and returns a concise normalized summary.
    """

    model_config = ConfigDict(extra="allow")

    reasons_for_low_win_rate: str | None = Field(default=None)
    proposed_improvements: str | None = Field(default=None)
    validation_plan: str | None = Field(default=None)


class FinalAnalysisTool(BaseTool):
    name: str = "final_analysis"
    description: str = (
        "Compatibility analysis tool. Accepts a free-form analysis payload and "
        "returns a normalized markdown summary so agent workflows don't fail "
        "when they invoke 'final_analysis'."
    )
    args_schema: Type[BaseModel] = _FinalAnalysisInput

    def _run(self, **kwargs) -> str:
        reasons = kwargs.get("reasons_for_low_win_rate") or "(not provided)"
        improvements = kwargs.get("proposed_improvements") or "(not provided)"
        validation = kwargs.get("validation_plan") or "(not provided)"

        return (
            "# Final Analysis\n\n"
            f"## Reasons For Low Win Rate\n{reasons}\n\n"
            f"## Proposed Improvements\n{improvements}\n\n"
            f"## Validation Plan\n{validation}\n"
        )


# ── RAG state (updated by main.py after _build_rag_index) ───────────────────────
rag_collection = None  # set to chromadb Collection when RAG is enabled

# ── Docker checker container (set by main.py during _run_dev_pipeline) ──────────
checker_container: str | None = None  # container name; None = use local exec


# ── Tool: Execution Checker ────────────────────────────────────────────────────
# Base whitelist — extended per-project via register_qa_commands()
_EXEC_WHITELIST: set = {"eslint", "stylelint", "node", "pytest", "pylint", "python"}
_EXEC_FORBIDDEN: tuple = (";", "&&", "||", "`", "$(")


def register_qa_commands(qa_suite: dict) -> None:
    """Extract binary names from qa_suite and add to _EXEC_WHITELIST.
    Called after _parse_file_inventory to whitelist project-specific commands.
    """
    import shlex as _shlex
    for cmd_str in qa_suite.values():
        if not cmd_str:
            continue
        try:
            tokens = _shlex.split(str(cmd_str))
        except ValueError:
            continue
        if tokens:
            binary = os.path.basename(tokens[0])
            if binary:
                _EXEC_WHITELIST.add(binary)


class _ExecInput(BaseModel):
    command: str = Field(
        ...,
        description=(
            "Command to run. First token must be a binary in the allowed list "
            "(configured by Architect's qa_suite, e.g. node, eslint, pytest, pylint, cargo, go). "
            "Shell operators (; && || | ` $() are forbidden)."
        ),
    )


class ExecutionCheckerTool(BaseTool):
    name: str = "Run Checks"
    description: str = (
        f"Run syntax/lint/test checks inside the workspace ({WORKSPACE_ROOT}). "
        "Run EXACTLY the commands provided by the Architect in qa_suite — do not substitute or invent alternatives. "
        "Wildcards (*.js) are auto-expanded. Shell operators are blocked for security."
    )
    args_schema: Type[BaseModel] = _ExecInput

    def _run(self, command: str) -> str:  # noqa: C901
        # 1. Block forbidden shell operators before any parsing
        for op in _EXEC_FORBIDDEN:
            if op in command:
                return f"[BLOCKED] Command contains forbidden operator '{op}'."
        # Single pipe (not ||) is also forbidden
        if re.search(r'(?<!\|)\|(?!\|)', command):
            return "[BLOCKED] Command contains forbidden operator '|'."

        # 2. Parse with shlex (handles quoted args correctly)
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            return f"[ERROR] Cannot parse command: {exc}"
        if not tokens:
            return "[ERROR] Empty command."

        # 3. Whitelist check on the base name of the first token
        base_cmd = os.path.basename(tokens[0])
        if base_cmd not in _EXEC_WHITELIST:
            return (
                f"[BLOCKED] '{base_cmd}' is not in the allowed list: "
                f"{sorted(_EXEC_WHITELIST)}."
            )

        # 4. Expand wildcards (shlex does NOT expand *)
        #    Always resolve relative paths against WORKSPACE_ROOT.
        expanded: list[str] = []
        for tok in tokens:
            if "*" in tok or "?" in tok:
                p = Path(tok)
                pattern = str(WORKSPACE_ROOT / p) if not p.is_absolute() else tok
                matches = sorted(_glob.glob(pattern))
                expanded.extend(matches if matches else [tok])
            else:
                expanded.append(tok)

        # 5. Choose executor: docker exec (preferred) or local
        import tools as _self_mod
        if _self_mod.checker_container:
            # Remap absolute host paths → /workspace/ container paths
            # (wildcard expansion above produces host-absolute paths; container mounts at /workspace)
            remapped: list[str] = []
            for tok in expanded:
                p = Path(tok)
                if p.is_absolute():
                    try:
                        rel = p.relative_to(WORKSPACE_ROOT)
                        remapped.append("/workspace/" + rel.as_posix())
                    except ValueError:
                        remapped.append(tok)
                else:
                    remapped.append(tok)
            cmd = ["docker", "exec", "-w", "/workspace", _self_mod.checker_container] + remapped
        else:
            cmd = expanded

        # Execute — shell=False prevents any further injection
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(WORKSPACE_ROOT),
                capture_output=True,
                timeout=30,
            )
            out = proc.stdout.decode("utf-8", errors="replace")
            err = proc.stderr.decode("utf-8", errors="replace")
            combined = (out + err).strip()
            if proc.returncode == 127:
                return f"[TOOL_NOT_INSTALLED] '{expanded[0]}' not found (exit 127)."
            return combined or f"(exit code {proc.returncode}, no output)"
        except subprocess.TimeoutExpired:
            return "[ERROR] Command timed out after 30 seconds."
        except FileNotFoundError:
            return f"[TOOL_NOT_INSTALLED] '{expanded[0]}' not found on host. Is it installed?"
        except Exception as exc:
            return f"[ERROR] {exc}"


# ── Tool: Codebase Search (RAG) ─────────────────────────────────────────────────

class _SearchInput(BaseModel):
    query: str = Field(
        ...,
        description=(
            "Natural language query to semantically search the codebase. "
            "E.g. 'Google Sheets authentication', 'quiz score calculation'."
        ),
    )


class CodebaseSearchTool(BaseTool):
    name: str = "Search Codebase"
    description: str = (
        "Semantically search src/ files using the RAG index. "
        "Use when you need to find a specific function, variable, or understand "
        "part of the codebase without reading every file. "
        "Only available when the project has ≥20 source files."
    )
    args_schema: Type[BaseModel] = _SearchInput

    def _run(self, query: str) -> str:
        import tools as _self_mod  # access module-level rag_collection
        if _self_mod.rag_collection is None:
            return (
                "[INFO] RAG index not available (project < 20 files or index not built). "
                "Use 'Read File' to inspect src/ files directly."
            )
        try:
            results = _self_mod.rag_collection.query(query_texts=[query], n_results=3)
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            if not docs:
                return "(no results — try a different query)"
            parts = []
            for doc, meta in zip(docs, metas):
                path = meta.get("path", "unknown")
                parts.append(f"=== {path} ===\n{doc[:800]}")
            return "\n\n".join(parts)
        except Exception as exc:
            return f"[ERROR] Search failed: {exc}"


# ── Shared tool instances (imported by agents.py) ────────────────────────────────────────
safe_file_read   = SafeFileReadTool()
safe_dir_read    = SafeDirectoryReadTool()
safe_file_write  = SafeFileWriterTool()
code_interpreter = CodeInterpreterTool()
final_analysis   = FinalAnalysisTool()
execution_checker = ExecutionCheckerTool()
codebase_search   = CodebaseSearchTool()
