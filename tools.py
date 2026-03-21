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

import os
import re
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


# ── Shared tool instances (imported by agents.py) ────────────────────────────
safe_file_read   = SafeFileReadTool()
safe_dir_read    = SafeDirectoryReadTool()
safe_file_write  = SafeFileWriterTool()
code_interpreter = CodeInterpreterTool()
final_analysis   = FinalAnalysisTool()
