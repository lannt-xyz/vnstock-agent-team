import json
from datetime import datetime, timezone
from dotenv import load_dotenv
from crewai import Crew, Process

load_dotenv()

# Import WORKSPACE_ROOT trước để tạo thư mục đúng chỗ
from tools import WORKSPACE_ROOT  # noqa: E402

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
        verbose=True,
    )
    result = crew.kickoff()
    print("\n--- FINAL WORKFLOW REPORT ---\n")
    print(result)