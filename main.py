import re
import json
import traceback
from datetime import datetime, timezone
from dotenv import load_dotenv
from crewai import Crew, Process

load_dotenv()

# Import WORKSPACE_ROOT trước để tạo thư mục đúng chỗ
from tools import WORKSPACE_ROOT  # noqa: E402

# Tạo các thư mục workspace cần thiết (có thể nằm ngoài project này)
(WORKSPACE_ROOT / "src").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "reports").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "tests").mkdir(parents=True, exist_ok=True)

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

MAX_CYCLES = 3

IS_FRONTEND = bool(re.search(r'html|css|javascript|\bjs\b|frontend|google sheets api', USER_REQUEST, re.I))

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


def _save_state(data: dict) -> None:
    """Lưu state sau mỗi cycle để cycle sau có thể dùng."""
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_t1_t2_with_guard(
    pm, plan_reviewer, crew_agents, request, retry_context, is_frontend, max_inner=2
) -> tuple[list, str]:
    """Chạy t1+t2 với inner retry nếu Plan Reviewer yêu cầu sửa (REQUEST CHANGES).
    Trả về (tasks_list_đầy_đủ, t2_conclusion).
    """
    original_t1_desc = (
        f"Yêu cầu từ User:\n{request}{retry_context}\n"
        f"{__import__('tasks')._PATH_NOTE}\n"
        "Nhiệm vụ:\n"
        "1. Phân tích yêu cầu, liệt kê cụ thể các User Stories.\n"
        "2. Xác định phạm vi và Done Criteria đo lường được.\n"
        "3. Liệt kê rủi ro tiềm ẩn.\n"
        "4. Ghi kế hoạch vào 'reports/t1_task_plan.md' bằng Write File."
    )

    feedback = ""
    for attempt in range(max_inner + 1):
        # Re-instantiate Tasks mỗi lần để tránh carry-over state
        from tasks import create_dev_team_tasks  # noqa: PLC0415
        from crewai import Task  # noqa: PLC0415

        t1_desc = original_t1_desc
        if feedback:
            t1_desc = f"{original_t1_desc}\n\n### FEEDBACK TỪ REVIEWER:\n{feedback}"
            _log(f"[t2 Guard] Inject feedback vào t1 (attempt {attempt}/{max_inner})")

        t1 = Task(
            description=t1_desc,
            agent=pm,
            expected_output="File 'reports/t1_task_plan.md' chứa: User Stories, Done Criteria, rủi ro.",
        )
        t2 = Task(
            description=(
                "Dùng Read File đọc 'reports/t1_task_plan.md', đánh giá:\n"
                "1. User Stories có rõ ràng, khả thi kỹ thuật không?\n"
                "2. Có Task thiếu, dư, hoặc mâu thuẫn không?\n"
                "3. Done Criteria có đo lường được không?\n"
                "4. Rủi ro kỹ thuật nào PM chưa nhận ra?\n"
                "Kết luận: APPROVED hoặc REQUEST CHANGES (kèm danh sách điểm cần sửa).\n"
                "Ghi nhận xét vào 'reports/t2_plan_review.md' bằng Write File."
            ),
            agent=plan_reviewer,
            context=[t1],
            expected_output="File 'reports/t2_plan_review.md' ghi kết luận APPROVED hoặc REQUEST CHANGES kèm lý do.",
        )

        mini_crew = Crew(
            agents=crew_agents,
            tasks=[t1, t2],
            process=Process.sequential,
            verbose=True,
        )
        try:
            mini_crew.kickoff()
        except Exception as exc:
            _log(f"[t2 Guard] mini_crew lỗi: {exc} — bỏ qua guard, tiếp tục")
            break

        # Đọc kết quả t2
        t2_report_path = WORKSPACE_ROOT / "reports" / "t2_plan_review.md"
        if t2_report_path.exists():
            t2_content = t2_report_path.read_text(encoding="utf-8")
        else:
            _log("[t2 Guard] Không tìm thấy t2_plan_review.md — bỏ qua guard")
            break

        if "REQUEST CHANGES" in t2_content.upper():
            if attempt < max_inner:
                feedback = t2_content[:1500]  # đưa toàn bộ review vào feedback
                _log(f"[t2 Guard] PLAN cần sửa — thử lại (attempt {attempt + 1}/{max_inner})")
            else:
                _log("[t2 Guard] Đã thử hết inner retry — tiếp tục với plan hiện tại")
                break
        else:
            _log(f"[t2 Guard] PLAN APPROVED sau {attempt + 1} lần")
            break

        return t1, t2


if __name__ == "__main__":
    pm, plan_reviewer, architect, coder, qc, reviewer = create_agents()
    state = _load_state()
    _log(f"IS_FRONTEND={IS_FRONTEND}")

    last_result = state.get("last_result")
    for cycle in range(1, MAX_CYCLES + 1):
        _log(f"=== CYCLE {cycle}/{MAX_CYCLES} ===")

        retry_context = ""
        if last_result:
            retry_context = (
                f"\n\n[KẾT QUẢ CYCLE TRƯỚC — CẦN CẢI TIẾN]\n"
                f"{last_result[:2000]}\n"
                "Dựa trên kết quả trên, hãy xác định nguyên nhân và tập trung vào điểm chưa giải quyết.\n"
            )

        # Phase 4: t2 guard — chạy t1+t2 riêng với inner retry trước khi chạy toàn bộ
        _run_t1_t2_with_guard(
            pm, plan_reviewer,
            crew_agents=[pm, plan_reviewer, architect, coder, qc, reviewer],
            request=USER_REQUEST,
            retry_context=retry_context,
            is_frontend=IS_FRONTEND,
        )

        # Chạy pipeline đầy đủ t1→t7 (t1/t2 sẽ đọc lại file đã được guard approve)
        tasks = create_dev_team_tasks(
            pm, plan_reviewer, architect, coder, qc, reviewer,
            USER_REQUEST,
            previous_result=last_result,
            is_frontend=IS_FRONTEND,
        )
        crew = Crew(
            agents=[pm, plan_reviewer, architect, coder, qc, reviewer],
            tasks=tasks,
            process=Process.sequential,
            verbose=True,
        )
        try:
            result = crew.kickoff()
            last_result = str(result)
            _save_state({"cycle": cycle, "last_result": last_result})
            _log(f"=== CYCLE {cycle} DONE ===")
        except Exception as exc:
            _log(f"=== CYCLE {cycle} FAILED: {exc} ===")
            traceback.print_exc()
            _save_state({"cycle": cycle, "last_result": last_result or "", "error": str(exc)})
            _log("State đã được lưu. Chạy lại để vào cycle tiếp theo.")

    print("\n--- FINAL WORKFLOW REPORT ---\n")
    print(last_result)