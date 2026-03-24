"""Telegram bot dashboard for my-dev-team pipeline.

Commands:
  /dev <request>  — start pipeline with given request
  /status         — show current pipeline state
  /cancel         — cancel running pipeline
  /push [branch]  — git add + commit + push (default: main)

Environment variables required:
  TG_BOT_TOKEN   — bot token from BotFather
  TG_USER_IDS    — comma-separated allowed user IDs (e.g. 123456,789012)

Optional (for /push):
  GIT_AUTHOR_NAME   — fallback if git config user.name is not set
  GIT_AUTHOR_EMAIL  — fallback if git config user.email is not set
"""

import asyncio
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable

from dotenv import load_dotenv
from telegram import Update
from telegram.error import RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

from tools import WORKSPACE_ROOT  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "")
_raw_ids: str = os.getenv("TG_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {
    int(x) for x in _raw_ids.split(",") if x.strip().isdigit()
}

STATE_FILE = WORKSPACE_ROOT / "state.json"

# ── Pipeline state ────────────────────────────────────────────────────────────
_pipeline_running: bool = False
_pipeline_lock = threading.Lock()
cancel_event = threading.Event()
_main_loop: asyncio.AbstractEventLoop | None = None
_app: Application | None = None  # set in main()

# ── Dashboard state ───────────────────────────────────────────────────────────
TASKS = ["tI", "t3", "t4", "t5", "t6", "t7"]
TASK_LABELS: dict[str, str] = {
    "tI": "Investigation",
    "t3": "Architect",
    "t4": "Coder",
    "t5": "QC",
    "t6": "Reviewer",
    "t7": "PM Report",
}

_task_status: dict[str, str] = {}   # "pending" | "running" | "pass" | "fail"
_task_files_done: int = 0            # files written in current t4 run
_task_files_total: int = 0           # total files planned in current t4 run
_qc_attempt: int = 0
_qc_total: int = 0
_dashboard_chat_id: int | None = None
_dashboard_msg_id: int | None = None
_file_counter: int = 0               # for throttle: edit every 5 files


# ── Status formatter ──────────────────────────────────────────────────────────
def _format_dashboard() -> str:
    lines = ["*🤖 Dev Pipeline Dashboard*\n"]
    for t in TASKS:
        status = _task_status.get(t, "pending")
        label = TASK_LABELS.get(t, t)
        if status == "pending":
            icon = "⏳"
        elif status == "running":
            icon = "🔄"
        elif status == "pass":
            icon = "✅"
        elif status == "fail":
            icon = "❌"
        else:
            icon = "•"
        extra = ""
        if t == "t4" and status == "running" and _task_files_total:
            extra = f" ({_task_files_done}/{_task_files_total} files)"
        if t == "t5" and status == "running" and _qc_total:
            extra = f" (attempt {_qc_attempt}/{_qc_total})"
        lines.append(f"{icon} {label}{extra}")
    return "\n".join(lines)


# ── Throttled Telegram edit ───────────────────────────────────────────────────
async def _do_edit(chat_id: int, msg_id: int, text: str) -> None:
    if _app is None:
        return
    try:
        await _app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            parse_mode="Markdown",
        )
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await _app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:
            pass  # skip — do not crash pipeline
    except Exception:
        pass


def _throttled_edit(event_type: str = "task") -> None:
    """Schedule a dashboard edit. Only fires on task transitions or every 5 files."""
    global _file_counter
    if _main_loop is None or _dashboard_chat_id is None or _dashboard_msg_id is None:
        return
    if event_type == "file":
        _file_counter += 1
        if _file_counter % 5 != 0:
            return
    else:
        _file_counter = 0  # reset counter on task transitions
    asyncio.run_coroutine_threadsafe(
        _do_edit(_dashboard_chat_id, _dashboard_msg_id, _format_dashboard()),
        _main_loop,
    )


def _send_message(text: str) -> None:
    """Fire-and-forget send (new message, not edit)."""
    if _main_loop is None or _dashboard_chat_id is None or _app is None:
        return
    asyncio.run_coroutine_threadsafe(
        _app.bot.send_message(_dashboard_chat_id, text, parse_mode="Markdown"),
        _main_loop,
    )


# ── Progress callback factory ─────────────────────────────────────────────────
def make_progress_callback() -> Callable[[str, str], None]:
    """Returns a progress_callback compatible with _run_dev_pipeline."""

    def _cb(event: str, detail: str = "") -> None:
        global _task_status, _task_files_done, _task_files_total
        global _qc_attempt, _qc_total, _file_counter

        if event == "task_start":
            # Mark any currently-running task as done before switching
            for k in list(_task_status):
                if _task_status[k] == "running":
                    _task_status[k] = "pass"
            _task_status[detail] = "running"
            _file_counter = 0
            _throttled_edit("task")

        elif event == "task_done":
            if detail in _task_status:
                _task_status[detail] = "pass"
            _throttled_edit("task")

        elif event == "task_fail":
            if detail in _task_status:
                _task_status[detail] = "fail"
            _throttled_edit("task")

        elif event == "t4_total":
            # detail = total file count as string
            try:
                _task_files_total = int(detail)
            except ValueError:
                pass
            _task_files_done = 0

        elif event == "file_done":
            _task_files_done += 1
            _throttled_edit("file")

        elif event == "qc_attempt":
            # detail = "N/M"
            try:
                a, b = detail.split("/")
                _qc_attempt, _qc_total = int(a), int(b)
            except Exception:
                pass
            _throttled_edit("task")

        elif event == "qc_fail":
            _task_status["t5"] = "fail"
            _throttled_edit("task")
            # Send separate pinned message for QC failure details
            snippet = detail[:500] if detail else ""
            _send_message(f"❌ *QC FAIL* (attempt {_qc_attempt}/{_qc_total})\n```\n{snippet}\n```")

        elif event == "done":
            # detail = number of files as string
            for k in _task_status:
                if _task_status[k] == "running":
                    _task_status[k] = "pass"
            _throttled_edit("task")
            _send_message(f"✅ Xong! {detail} file. /push để commit GitHub.")

    return _cb


# ── /dev handler ──────────────────────────────────────────────────────────────
async def cmd_dev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pipeline_running, _dashboard_chat_id, _dashboard_msg_id
    global _task_status, _task_files_done, _task_files_total
    global _file_counter, _qc_attempt, _qc_total

    user = update.effective_user
    if not user or user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Không có quyền.")
        return

    with _pipeline_lock:
        if _pipeline_running:
            await update.message.reply_text("⚠️ Pipeline đang chạy. Dùng /cancel để dừng.")
            return
        _pipeline_running = True

    request = " ".join(context.args) if context.args else ""
    if not request:
        await update.message.reply_text("Usage: /dev <yêu cầu>")
        with _pipeline_lock:
            _pipeline_running = False
        return

    # Reset dashboard state
    _task_status = {t: "pending" for t in TASKS}
    _task_files_done = 0
    _task_files_total = 0
    _file_counter = 0
    _qc_attempt = 0
    _qc_total = 0
    cancel_event.clear()

    # Send the one live-update dashboard message
    msg = await update.message.reply_text(_format_dashboard(), parse_mode="Markdown")
    _dashboard_chat_id = update.effective_chat.id
    _dashboard_msg_id = msg.message_id

    progress_cb = make_progress_callback()

    is_frontend_local = bool(re.search(
        r'html|css|javascript|\bjs\b|frontend|google sheets? api|trang web|website|web app',
        request, re.I,
    ))

    def _run_pipeline() -> None:
        global _pipeline_running
        try:
            from main import (  # noqa: PLC0415
                MAX_CYCLES, _load_state, _log, _run_dev_pipeline,
                _run_t1_t2_with_guard, _save_state,
            )
            from agents import create_agents  # noqa: PLC0415

            pm, plan_reviewer, architect, coder, qc_agent, reviewer = create_agents()
            state = _load_state()
            last_result = state.get("last_result")

            for cycle in range(1, MAX_CYCLES + 1):
                if cancel_event.is_set():
                    break
                _log(f"[bot] CYCLE {cycle}/{MAX_CYCLES}")
                if cycle == 1:
                    _run_t1_t2_with_guard(
                        pm, plan_reviewer,
                        crew_agents=[pm, plan_reviewer, architect, coder, qc_agent, reviewer],
                        request=request,
                        retry_context="",
                        is_frontend=is_frontend_local,
                    )
                result = _run_dev_pipeline(
                    pm, plan_reviewer, architect, coder, qc_agent, reviewer,
                    request, last_result, is_frontend_local, cycle,
                    progress_callback=progress_cb,
                )
                last_result = result
                _save_state({"cycle": cycle, "last_result": last_result})
        except Exception as exc:
            import traceback as _tb  # noqa: PLC0415
            try:
                from main import _log  # noqa: PLC0415
                _log(f"[bot] Pipeline error: {exc}\n{_tb.format_exc()}")
            except Exception:
                pass
            _send_message(f"💥 Lỗi pipeline:\n```\n{str(exc)[:400]}\n```")
        finally:
            with _pipeline_lock:
                _pipeline_running = False

    threading.Thread(target=_run_pipeline, daemon=True).start()


# ── /status handler ───────────────────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Không có quyền.")
        return

    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text("utf-8"))
            cycle = data.get("cycle", "?")
            qc_history = data.get("qc_history", [])
            last_verdict = qc_history[-1]["verdict"] if qc_history else "—"
            running_str = "🔄 Đang chạy" if _pipeline_running else "✅ Rảnh"
            msg = (
                f"*Pipeline Status*\n"
                f"Cycle: {cycle}\n"
                f"Trạng thái: {running_str}\n"
                f"QC gần nhất: {last_verdict}\n"
                f"QC attempts: {len(qc_history)}"
            )
        except Exception as e:
            msg = f"Lỗi đọc state: {e}"
    else:
        msg = "Chưa có state.json — pipeline chưa chạy lần nào."

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── /cancel handler ───────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Không có quyền.")
        return

    if not _pipeline_running:
        await update.message.reply_text("Không có pipeline nào đang chạy.")
        return

    cancel_event.set()
    # Cleanup Docker container if active
    try:
        import tools as _tools_mod  # noqa: PLC0415
        from main import _stop_checker_container  # noqa: PLC0415
        if _tools_mod.checker_container:
            _stop_checker_container(_tools_mod.checker_container)
            _tools_mod.checker_container = None
    except Exception:
        pass

    await update.message.reply_text("🛑 Đã gửi tín hiệu cancel. Pipeline sẽ dừng sau bước hiện tại.")


# ── /push handler ─────────────────────────────────────────────────────────────
_ALLOWED_BRANCH_RE = re.compile(r'^(main|master|dev/.+)$')


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Không có quyền.")
        return

    if _pipeline_running:
        await update.message.reply_text("⚠️ Pipeline đang chạy, chờ xong rồi push.")
        return

    branch = context.args[0] if context.args else "main"
    if not _ALLOWED_BRANCH_RE.match(branch):
        await update.message.reply_text(
            f"⛔ Branch `{branch}` không được phép. Chỉ dùng: `main`, `master`, `dev/*`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(f"🚀 Đang push lên branch `{branch}`...", parse_mode="Markdown")

    def _git_get(key: str) -> str:
        try:
            r = subprocess.run(["git", "config", key], capture_output=True, text=True, timeout=5)
            return r.stdout.strip()
        except Exception:
            return ""

    # Pre-check git identity; inject from env if not set
    git_extra: list[str] = []
    if not _git_get("user.name"):
        name = os.getenv("GIT_AUTHOR_NAME", "")
        if not name:
            await update.message.reply_text(
                "❌ `git config user.name` chưa set và `GIT_AUTHOR_NAME` env cũng trống. "
                "Set một trong hai rồi thử lại.",
                parse_mode="Markdown",
            )
            return
        git_extra += ["-c", f"user.name={name}"]

    if not _git_get("user.email"):
        email = os.getenv("GIT_AUTHOR_EMAIL", "")
        if not email:
            await update.message.reply_text(
                "❌ `git config user.email` chưa set và `GIT_AUTHOR_EMAIL` env cũng trống. "
                "Set một trong hai rồi thử lại.",
                parse_mode="Markdown",
            )
            return
        git_extra += ["-c", f"user.email={email}"]

    workspace = str(WORKSPACE_ROOT)
    try:
        subprocess.run(
            ["git", "add", "."],
            cwd=workspace, check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git"] + git_extra + ["commit", "-m", "chore: auto-commit from dev-team bot"],
            cwd=workspace, check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "push", "origin", branch],
            cwd=workspace, check=True, capture_output=True, timeout=60,
        )
        await update.message.reply_text(f"✅ Push thành công lên `{branch}`!", parse_mode="Markdown")
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", errors="replace")[:500] if e.stderr else str(e)
        await update.message.reply_text(f"❌ Git lỗi:\n```\n{err}\n```", parse_mode="Markdown")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("❌ Git timeout sau 60s.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    global _main_loop, _app

    if not BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN chưa set trong .env")
    if not ALLOWED_USER_IDS:
        raise RuntimeError(
            "TG_USER_IDS chưa set trong .env "
            "(dạng: TG_USER_IDS=123456789,987654321)"
        )

    _app = Application.builder().token(BOT_TOKEN).build()
    _app.add_handler(CommandHandler("dev", cmd_dev))
    _app.add_handler(CommandHandler("status", cmd_status))
    _app.add_handler(CommandHandler("cancel", cmd_cancel))
    _app.add_handler(CommandHandler("push", cmd_push))

    # Must store the event loop BEFORE run_polling captures it
    _main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_main_loop)
    _app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
