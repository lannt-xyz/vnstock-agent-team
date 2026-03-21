import re
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
from crewai import Crew, Process

load_dotenv()

# Import WORKSPACE_ROOT trước để tạo thư mục đúng chỗ
from tools import WORKSPACE_ROOT, safe_file_write  # noqa: E402

# Tạo các thư mục workspace cần thiết (có thể nằm ngoài project này)
(WORKSPACE_ROOT / "src").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "reports").mkdir(parents=True, exist_ok=True)

from agents import create_agents  # noqa: E402
from tasks import create_dev_team_tasks  # noqa: E402


USER_REQUEST = """
Tạo trang web đọc google sheets để tạo bài kiểm tra từ vựng cho học sinh.
Yêu cầu: 
1) Giao diện đơn giản, responsive,
2) Tự động lấy dữ liệu từ google sheets, yêu cầu người dùng link sheet và chọn sheet cần lấy dữ liệu, sau đó hiển thị câu hỏi trắc nghiệm dựa trên dữ liệu đó.
3) Lưu kết quả làm bài của học sinh vào 1 sheet trên cùng file
4) Hướng dẫn chi tiết cách triển khai trên server (nếu cần).
Yêu cầu về kết quả:
- Code sạch, có comment giải thích,
- Cấu trúc thư mục rõ ràng,
- Có tài liệu hướng dẫn sử dụng và triển khai.
"""

MAX_CYCLES = 1

STATE_FILE   = WORKSPACE_ROOT / "state.json"
HISTORY_LOG  = WORKSPACE_ROOT / "crew_history.log"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}\n"
    with open(HISTORY_LOG, "a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="")


def _load_state() -> dict:
    """Đọc state từ lần chạy trước (nếu có)."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _extract_json_objects(text: str) -> list[str]:
    """Extract all top-level JSON objects from text using bracket-counting.
    Correctly handles: nested braces, string literals, and escape sequences.
    This fixes the broken regex approach that fails on code content with {}.
    """
    objects: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '{':
            depth = 0
            in_string = False
            escape_next = False
            j = i
            while j < n:
                c = text[j]
                if escape_next:
                    escape_next = False
                elif in_string:
                    if c == '\\':
                        escape_next = True
                    elif c == '"':
                        in_string = False
                else:
                    if c == '"':
                        in_string = True
                    elif c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            objects.append(text[i:j + 1])
                            i = j  # outer loop will i+=1
                            break
                j += 1
        i += 1
    return objects


def _normalize_path(fp: str) -> str:
    """Strip absolute workspace prefix or 'workspace/' prefix from file_path."""
    fp = fp.strip()
    ws_str = str(WORKSPACE_ROOT)
    # Strip absolute path prefix (with or without leading slash)
    for prefix in (ws_str, ws_str.lstrip("/")):
        if fp.startswith(prefix):
            fp = fp[len(prefix):].lstrip("/")
            break
    # Strip relative 'workspace/' prefix
    if fp.startswith("workspace/"):
        fp = fp[len("workspace/"):]
    return fp


def _flush_write_calls(text: str) -> None:
    """Extract all Write File JSON calls from text and execute them.
    Uses a proper bracket-counting parser — works even when content has {} inside
    (e.g. JavaScript, HTML templates, Python dicts).
    """
    candidates = _extract_json_objects(text)

    executed: set[str] = set()
    for raw in candidates:
        if raw in executed:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if "file_path" in data and "content" in data:
            fp = _normalize_path(str(data["file_path"]))
            result = safe_file_write._run(
                file_path=fp,
                content=data["content"],
                overwrite=data.get("overwrite", True),
            )
            print(f"\n[auto-tool] Write File → {fp} | {result}\n")
            executed.add(raw)


def _task_callback(task_output) -> None:
    """Chạy sau khi mỗi task hoàn thành."""
    _flush_write_calls(str(task_output))


def _step_callback(step_output) -> None:
    """Chạy sau mỗi bước của agent — bắt cả intermediate Write File calls."""
    _flush_write_calls(str(step_output))


if __name__ == "__main__":
    pm, plan_reviewer, architect, coder, qc, reviewer = create_agents()
    state = _load_state()
    tasks = create_dev_team_tasks(
        pm, plan_reviewer, architect, coder, qc, reviewer,
        USER_REQUEST,
        previous_result=state.get("last_result"),
    )

    crew = Crew(
        agents=[pm, plan_reviewer, architect, coder, qc, reviewer],
        tasks=tasks,
        process=Process.sequential,
        task_callback=_task_callback,
        step_callback=_step_callback,
        verbose=True,
    )
    result = crew.kickoff()
    print("\n--- FINAL WORKFLOW REPORT ---\n")
    print(result)