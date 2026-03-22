import os
import re
import json
import traceback
from datetime import datetime, timezone
from dotenv import load_dotenv
from crewai import Crew, Process

load_dotenv()

# Import WORKSPACE_ROOT trước để tạo thư mục đúng chỗ
from tools import WORKSPACE_ROOT, safe_file_write  # noqa: E402

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

IS_FRONTEND = bool(re.search(
    r'html|css|javascript|\bjs\b|frontend|google sheets? api|trang web|website|web app',
    USER_REQUEST, re.I
))

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


# ── JSON-in-Final-Answer safety net ──────────────────────────────────────────
# DeepSeek sometimes outputs ReAct JSON in the Final Answer text instead of
# actually calling the tool through the framework.  We intercept and replay it.

def _normalize_path(raw: str) -> str:
    """Strip workspace/ prefix so safe_file_write gets a clean relative path."""
    cleaned = raw.strip().strip('"').strip("'")
    for prefix in ("workspace/", "./workspace/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned


def _extract_json_objects(text: str) -> list:
    """Return all top-level JSON objects found in text (bracket-counting parser).
    Handles '{' inside string values and nested objects correctly.
    """
    results, depth, start = [], 0, -1
    in_string = escape_next = False
    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                results.append(text[start : i + 1])
                start = -1
    return results


def _flush_write_calls(text: str) -> None:
    """Parse agent output for embedded JSON write calls and execute them."""
    for raw in _extract_json_objects(text):
        try:
            data = json.loads(raw)
        except Exception:
            continue

        # Nested ReAct format: {"action": "write_file", "action_input": {...}}
        action = str(data.get("action", "")).lower().replace(" ", "_")
        if action == "write_file":
            inp = data.get("action_input", {})
            fp, content = inp.get("file_path", ""), inp.get("content", "")
            if fp and content:
                _log(f"[flush] Intercepted ReAct write → {fp}")
                safe_file_write._run(
                    file_path=_normalize_path(fp),
                    content=content,
                    overwrite=inp.get("overwrite", True),
                )
            continue

        # Flat format: {"file_path": ..., "content": ...}
        fp, content = data.get("file_path", ""), data.get("content", "")
        if fp and content:
            _log(f"[flush] Intercepted flat write → {fp}")
            safe_file_write._run(
                file_path=_normalize_path(fp),
                content=content,
                overwrite=data.get("overwrite", True),
            )


def _task_callback(task_output) -> None:
    """Fires once per task — safety net for JSON writes."""
    _flush_write_calls(str(task_output))


_FILE_SEP = re.compile(r'^###\s*FILE\s*:\s*(.+?)\s*$', re.MULTILINE | re.IGNORECASE)
_FENCE_RE = re.compile(r'^```[^\n]*\n(.*?)```\s*$', re.DOTALL)


def _extract_and_write_src(text: str) -> list:
    """Parse '### FILE: src/...' blocks from text and write each to workspace/src/.
    Returns list of relative paths written.
    """
    matches = list(_FILE_SEP.finditer(text))
    written = []
    for i, m in enumerate(matches):
        fp_raw = m.group(1).strip().strip('`').strip('"').strip("'")
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.end():end].strip()
        fence = _FENCE_RE.match(seg)
        if fence:
            seg = fence.group(1).rstrip()
        else:
            seg = re.sub(r'\n```\s*$', '', seg).strip()
        if seg and ('.' in fp_raw or '/' in fp_raw):
            fp = _normalize_path(fp_raw)
            result = safe_file_write._run(file_path=fp, content=seg, overwrite=True)
            if not result.startswith(('[BLOCKED', '[ERROR')):
                written.append(fp)
                _log(f"[src] {len(seg)} bytes → {fp}")
    return written


def _run_single_task(agent, description: str, expected_output: str, out_rel: str) -> str:
    """Run one task as its own Crew, return the output file content."""
    from crewai import Task, Crew, Process  # noqa: PLC0415
    WS_REL = os.path.relpath(str(WORKSPACE_ROOT))
    task = Task(
        description=description,
        agent=agent,
        expected_output=expected_output,
        output_file=f"{WS_REL}/{out_rel}",
    )
    try:
        Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=True,
            task_callback=_task_callback,
        ).kickoff()
    except Exception as exc:
        _log(f"[single_task] {out_rel} lỗi: {exc}")
    out_path = WORKSPACE_ROOT / out_rel
    return out_path.read_text("utf-8") if out_path.exists() else ""


def _run_dev_pipeline(pm, plan_reviewer, architect, coder, qc, reviewer,
                      request, previous_result, is_frontend) -> str:
    """Step-by-step pipeline: Python injects file content between tasks.
    Model does NOT need to call Read File — content is already in the prompt.
    Model outputs ### FILE: blocks — Python writes them to src/.
    """

    def read(rel: str) -> str:
        p = WORKSPACE_ROOT / rel
        return p.read_text("utf-8") if p.exists() else "(chưa có)"

    # Guard already wrote t1 + t2 — just read them
    t1 = read("reports/t1_task_plan.md")
    t2 = read("reports/t2_plan_review.md")

    # ── t3: Architect — context injected, no tool read needed ─────────────────
    t3 = _run_single_task(
        architect,
        f"[KẾ HOẠCH]\n{t1[:3000]}\n\n[REVIEW]\n{t2[:1500]}\n\n"
        "Thiết kế kiến trúc:\n"
        "1. Tech stack: HTML + JavaScript + Google Sheets API — KHÔNG backend.\n"
        "2. Liệt kê TẤT CẢ file cần tạo: đường dẫn src/... và mô tả nội dung.\n"
        "3. Design Pattern và lý do.\n"
        "4. Data flow giữa các thành phần.",
        "Danh sách file src/..., tech stack, design pattern, data flow.",
        "reports/t3_architecture.md",
    )
    _log("[pipeline] t3 done")

    # ── t4: Coder — inject architecture, output ### FILE: blocks ───────────────
    t4_raw = _run_single_task(
        coder,
        f"[KIẾN TRÚC]\n{t3[:3000]}\n\n"
        "Viết TOÀN BỘ source code cho tất cả file trong kiến trúc trên.\n"
        "Dùng CHÍNH XÁC format sau cho mỗi file:\n\n"
        "### FILE: src/index.html\n"
        "<!DOCTYPE html>\n<html lang='vi'>\n...nội dung THỰC ĐẦY ĐỦ...\n</html>\n\n"
        "### FILE: src/app.js\n"
        "// nội dung THỰC ĐẦY ĐỦ...\n\n"
        "### FILE: src/style.css\n"
        "/* nội dung THỰC ĐẦY ĐỦ */\n\n"
        "KHÔNG dùng ``` fence | KHÔNG placeholder | KHÔNG TODO | "
        "Đường dẫn src/... không có 'workspace/' | Cuối: 'DONE: X file'",
        "Output ### FILE: src/... với nội dung đầy đủ thực tế từng file.",
        "reports/t4_code_summary.md",
    )
    _log("[pipeline] t4 done")

    # Write source files parsed from coder output
    written = _extract_and_write_src(t4_raw)
    _log(f"[pipeline] src/ files written: {written}")

    # ── t5: QC — inject file list + preview ───────────────────────────────────
    src_files = sorted(
        p.relative_to(WORKSPACE_ROOT).as_posix()
        for p in (WORKSPACE_ROOT / "src").rglob("*") if p.is_file()
    )
    src_preview = "\n\n".join(
        f"=== {f} ===\n" + read(f)[:600] for f in src_files[:5]
    )
    if is_frontend:
        t5_desc = (
            f"[FILE ĐÃ TẠO]: {src_files}\n\n[NỘI DUNG]\n{src_preview}\n\n"
            "Kiểm tra:\n1. HTML: DOCTYPE, charset, thẻ đóng mở đúng\n"
            "2. JS: không syntax error rõ ràng\n3. Security: không hardcode API key\n"
            "4. Responsive: meta viewport, media query\nKết luận: PASS hoặc FAIL."
        )
    else:
        t5_desc = (
            f"[FILE ĐÃ TẠO]: {src_files}\n\n[NỘI DUNG]\n{src_preview}\n\n"
            "Kiểm tra và kết luận PASS hoặc FAIL kèm chi tiết."
        )
    t5 = _run_single_task(qc, t5_desc, "PASS hoặc FAIL kèm chi tiết.", "reports/t5_qc_report.md")
    _log("[pipeline] t5 done")

    # ── t6: Reviewer — inject src preview ─────────────────────────────────────
    t6 = _run_single_task(
        reviewer,
        f"[NỘI DUNG SRC]\n{src_preview}\n\n"
        "Review:\n1. Code Style: tên biến/hàm rõ ràng?\n"
        "2. Clean Code: code thừa, hàm quá dài?\n"
        "3. Security: hardcode key, XSS rõ?\n"
        "Kết luận: APPROVED hoặc REQUEST CHANGES kèm vị trí cụ thể.",
        "APPROVED hoặc REQUEST CHANGES kèm chi tiết.",
        "reports/t6_review.md",
    )
    _log("[pipeline] t6 done")

    # ── t7: PM Final Report ────────────────────────────────────────────────────
    t7 = _run_single_task(
        pm,
        f"Tóm tắt:\n- File tạo: {src_files}\n- QC: {t5[:400]}\n- Review: {t6[:400]}\n\n"
        "Báo cáo cuối:\n1. Tóm tắt đã làm\n2. Danh sách file\n"
        "3. Kết quả QC\n4. Nhận xét chất lượng\n5. Bước tiếp theo\n"
        "Câu cuối: '✅ Xong! Đã tạo {len(src_files)} file. Ông check nhé!'",
        "Báo cáo ngắn gọn rõ ràng.",
        "reports/t7_final_report.md",
    )
    _log("[pipeline] t7 done")
    return t7


def _run_t1_t2_with_guard(
    pm, plan_reviewer, crew_agents, request, retry_context, is_frontend, max_inner=2
) -> tuple[list, str]:
    """Chạy t1+t2 với inner retry nếu Plan Reviewer yêu cầu sửa (REQUEST CHANGES).
    Trả về (tasks_list_đầy_đủ, t2_conclusion).
    """
    original_t1_desc = (
        f"Yêu cầu từ User:\n{request}{retry_context}\n"
        "Nhiệm vụ — xuất kế hoạch gồm:\n"
        "1. Phân tích yêu cầu, liệt kê cụ thể các User Stories.\n"
        "2. Xác định phạm vi và Done Criteria đo lường được.\n"
        "3. Liệt kê rủi ro tiềm ẩn."
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
            expected_output="Kế hoạch gồm: User Stories, Done Criteria, rủi ro.",
            output_file=os.path.join(os.path.relpath(str(WORKSPACE_ROOT)), "reports", "t1_task_plan.md"),
        )
        t2 = Task(
            description=(
                "Dùng Read File đọc 'reports/t1_task_plan.md', đánh giá:\n"
                "1. User Stories có rõ ràng, khả thi kỹ thuật không?\n"
                "2. Có Task thiếu, dư, hoặc mâu thuẫn không?\n"
                "3. Done Criteria có đo lường được không?\n"
                "4. Rủi ro kỹ thuật nào PM chưa nhận ra?\n"
                "Xuất kết luận: APPROVED hoặc REQUEST CHANGES (kèm danh sách điểm cần sửa)."
            ),
            agent=plan_reviewer,
            context=[t1],
            expected_output="Kết luận APPROVED hoặc REQUEST CHANGES kèm lý do cụ thể.",
            output_file=os.path.join(os.path.relpath(str(WORKSPACE_ROOT)), "reports", "t2_plan_review.md"),
        )

        mini_crew = Crew(
            agents=crew_agents,
            tasks=[t1, t2],
            process=Process.sequential,
            verbose=True,
            task_callback=_task_callback,
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
                continue  # ← iterate to next attempt with new feedback
            else:
                _log("[t2 Guard] Đã thử hết inner retry — tiếp tục với plan hiện tại")
                break
        else:
            _log(f"[t2 Guard] PLAN APPROVED sau {attempt + 1} lần")
            break


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

        # Step-by-step pipeline: inject file content, never ask model to read files
        try:
            result = _run_dev_pipeline(
                pm, plan_reviewer, architect, coder, qc, reviewer,
                USER_REQUEST, last_result, IS_FRONTEND,
            )
            last_result = result
            _save_state({"cycle": cycle, "last_result": last_result})
            _log(f"=== CYCLE {cycle} DONE ===")
        except Exception as exc:
            _log(f"=== CYCLE {cycle} FAILED: {exc} ===")
            traceback.print_exc()
            _save_state({"cycle": cycle, "last_result": last_result or "", "error": str(exc)})
            _log("State đã được lưu. Chạy lại để vào cycle tiếp theo.")

    print("\n--- FINAL WORKFLOW REPORT ---\n")
    print(last_result)