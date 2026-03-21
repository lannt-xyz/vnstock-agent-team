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


def _flush_write_calls(text: str) -> None:
    """Extract tất cả Write File JSON calls trong một đoạn text và thực thi hết.
    Hỗ trợ cả 3 format mà local model hay output:
      - ```json {...} ```  (markdown fenced block)
      - {...}              (bare JSON object inline)
      - Action Input: {...}
    """
    # Gom tất cả JSON object candidates (fenced hoặc bare)
    candidates: list[str] = []

    # 1. Fenced code blocks
    candidates += re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)

    # 2. "Action Input: {...}"
    candidates += re.findall(r'Action Input:\s*(\{.*?\})', text, re.DOTALL)

    # 3. Bare top-level JSON objects (fallback: tìm { ... } lớn nhất có file_path)
    for m in re.finditer(r'\{[^{}]*"file_path"[^{}]*\}', text, re.DOTALL):
        candidates.append(m.group(0))

    executed = set()
    for raw in candidates:
        raw = raw.strip()
        if raw in executed:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if "file_path" in data and "content" in data:
            fp = data["file_path"].strip()
            # Normalize: bỏ absolute path prefix nếu model quên dấu / đầu
            # VD: "home/lanntxyz/.../workspace/reports/x.md" → "reports/x.md"
            ws_str = str(WORKSPACE_ROOT)
            for prefix in (ws_str.lstrip("/"), ws_str):
                if fp.startswith(prefix):
                    fp = fp[len(prefix):].lstrip("/")
                    break
            # Bỏ tiền tố "workspace/" nếu model vẫn thêm vào
            if fp.startswith("workspace/"):
                fp = fp[len("workspace/"):]
            result = safe_file_write._run(
                file_path=fp,
                content=data["content"],
                overwrite=data.get("overwrite", True),
            )
            print(f"\n[auto-tool] Write File → {result}\n")
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