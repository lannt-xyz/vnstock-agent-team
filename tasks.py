import os
from crewai import Task

from tools import WORKSPACE_ROOT

# Relative path for output_file (CrewAI strips leading '/' from absolute paths)
_WS_REL = os.path.relpath(str(WORKSPACE_ROOT))

# ── Path rule — inject once (t1 only) ─────────────────────────────────────────
_PATH_NOTE = (
    "QUY TẮc ĐƯỜNG DẪN (dùng cho mọi lần gọi Write File):\n"
    "  ĐÚNG: file_path='reports/t1_task_plan.md'\n"
    "  SAI:  file_path='workspace/reports/t1_task_plan.md'\n"
    "Không thêm tiền tố 'workspace/' vào đường dẫn.\n"
)


# ── Dev Team Workflow ──────────────────────────────────────────────────────────
def create_dev_team_tasks(pm, plan_reviewer, architect, coder, qc, reviewer, request, previous_result=None, is_frontend=False, rag_hint=""):
    """6-agent dev team pipeline:
      t1 PM            → phân tích yêu cầu, lên kế hoạch task
      t2 Plan Reviewer → review & phê duyệt kế hoạch của PM (pro model)
      t3 Architect     → thiết kế kiến trúc, chỉ định file cần sửa (local model)
      t4 Coder         → viết / sửa code theo chỉ định
      t5 QC            → viết test, chạy pytest, báo lỗi nếu fail
      t6 Reviewer      → review code style / clean code / security
      t7 PM            → tổng hợp và báo cáo cuối cùng cho User
    """
    retry_context = ""
    if previous_result:
        retry_context = (
            f"\n\n[KẾT QUẢ CYCLE TRƯỚC — CẦN CẢI TIẾN]\n"
            f"{previous_result[:2000]}\n"
            "Dựa trên kết quả trên, hãy xác định nguyên nhân và tập trung vào điểm chưa giải quyết.\n"
        )

    ws = WORKSPACE_ROOT

    t1 = Task(
        description=(
            f"Yêu cầu từ User:\n{request}{retry_context}\n"
            "Nhiệm vụ — xuất kế hoạch gồm:\n"
            "1. Phân tích yêu cầu, liệt kê cụ thể các User Stories.\n"
            "2. Xác định phạm vi và Done Criteria đo lường được.\n"
            "3. Liệt kê rủi ro tiềm ẩn."
        ),
        agent=pm,
        expected_output="Kế hoạch gồm: User Stories, Done Criteria, rủi ro.",
        output_file=f"{_WS_REL}/reports/t1_task_plan.md",
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
        output_file=f"{_WS_REL}/reports/t2_plan_review.md",
    )

    t3 = Task(
        description=(
            "Dùng Read File đọc 'reports/t1_task_plan.md' và 'reports/t2_plan_review.md'.\n"
            "Xuất thiết kế kiến trúc gồm:\n"
            "1. Tech stack TRỰC TIẾP từ yêu cầu — xác định ngôn ngữ, framework và thư viện phù hợp nhất.\n"
            "2. Thiết kế theo chuẩn module hiện đại của ngôn ngữ/framework đã chọn — tách biệt trách nhiệm rõ ràng (separation of concerns), không dùng global state nếu không cần thiết.\n"
            "3. Danh sách TẤT CẢ file cần tạo với đường dẫn 'src/...' và mô tả nội dung.\n"
            "4. Design Pattern và lý do.\n"
            "5. Data flow giữa các module.\n\n"
            "SAU PHẦN MÔ TẢ KIẼN TRÚC, xuất JSON duy nhất theo đúng format sau:\n"
            "```json\n"
            "{\n"
            '  "files": [\n'
            '    {"name": "src/config.js", "description": "Cấu hình API keys và constants"},\n'
            '    {"name": "src/app.js",    "description": "Entry point, khởi tạo app"}\n'
            '  ],\n'
            '  "qa_suite": {\n'
            '    "syntax_cmd": "<lệnh kiểm tra syntax phù hợp tech stack, vd: node --check src/js/*.js>",\n'
            '    "lint_cmd":   "<lệnh lint, vd: eslint src/js/ --format compact>",\n'
            '    "test_cmd":   "<lệnh test, vd: pytest tests/ hoặc chuỗi rỗng nếu không có>"\n'
            '  }\n'
            "}\n"
            "```\n"
            "⚠️ BẮT BUỘC: Chỉ xuất JSON object này, không thêm text sau JSON.\n"
            "⚠️ `qa_suite`: điền lệnh phù hợp với tech stack đã thiết kế. Để chuỗi rỗng nếu không có lệnh tương ứng."
        ),
        agent=architect,
        context=[t1, t2],
        expected_output="Danh sách file src/..., design pattern, data flow, và JSON block chuẩn.",
        output_file=f"{_WS_REL}/reports/t3_architecture.md",
    )

    t4 = Task(
        description=(
            "Dùng Read File đọc 'reports/t3_architecture.md'.\n"
            "{qc_feedback}"
            "Viết TOÀN BỘ source code cho mọi file trong danh sách kiến trúc.\n"
            "\n"
            "OUTPUT FORMAT — dùng chính xác cấu trúc này cho mỗi file:\n"
            "### FILE: src/index.html\n"
            "[nội dung HTML đầy đủ]\n"
            "\n"
            "### FILE: src/app.js\n"
            "[nội dung JS đầy đủ]\n"
            "\n"
            "Quy tắc bắt buộc:\n"
            "- Mỗi file BẮT BUỘC có nội dung thực tế, đầy đủ (không placeholder, không TODO)\n"
            "- Đường dẫn: 'src/...' không có tiền tố workspace/\n"
            "- Không dùng ``` code fence trong output\n"
            "- Cuối cùng: liệt kê ngắn gọn các file đã tạo"
        ),
        agent=coder,
        context=[t3],
        expected_output=(
            "Output dạng '### FILE: src/...' cho mỗi file source có nội dung đầy đủ. "
            "Cuối: danh sách file đã tạo."
        ),
        output_file=f"{_WS_REL}/reports/t4_code_summary.md",
    )

    t5_desc = (
        "Kiểm tra chất lượng code do Coder viết.\n"
        "TRÌNH TỰ BẮt BUỘC:\n"
        "1. Dùng Read File đọc 'reports/t3_architecture.md', tìm JSON block chứa 'qa_suite'.\n"
        "   Lấy các giá trị: syntax_cmd, lint_cmd, test_cmd.\n"
        "2. Với mỗi lệnh không rỗng, dùng 'Run Checks' để chạy và ghi lại toàn bộ output thực tế.\n"
        "3. Dùng Read File đọc các file src/ kiểm tra thêm:\n"
        "   - Bảo mật: không hardcode secret, không injection rõ ràng.\n"
        "   - Logic: không có code thừa, dead code.\n"
        "4. Kết luận PASS chỉ khi mọi lệnh chạy được đều không báo error/lỗi.\n"
        "QUAN TRỌNG — Nếu output tool bắt đầu bằng [TOOL_NOT_INSTALLED]:\n"
        "  → Ghi chú vào báo cáo: '<tool>: not installed — bỏ qua'\n"
        "  → KHÔNG tính là FAIL. Chỉ FAIL khi tool chạy được và output chứa lỗi thật.\n"
        "Xuất báo cáo: PASS hoặc FAIL kèm stdout thực tế từ các lệnh đã chạy."
    )
    t5_expected = "Báo cáo QC: PASS hoặc FAIL kèm output thực tế từ các lệnh qa_suite."

    t5 = Task(
        description=t5_desc,
        agent=qc,
        context=[t3, t4],
        expected_output=t5_expected,
        output_file=f"{_WS_REL}/reports/t5_qc_report.md",
    )

    _rag_line = f"\n[NGỦ CẢNH CODEBASE]: {rag_hint}" if rag_hint else ""

    t6 = Task(
        description=(
            "Dùng Read File đọc các file trong 'src/', xuất nhận xét:\n"
            "1. Code Style: tên biến/hàm có rõ ràng, nhất quán không?\n"
            "2. Clean Code: code thừa, hàm >30 dòng, logic lồng nhau quá sâu?\n"
            "3. Security: hardcode secret, injection rõ ràng?\n"
            "4. Liệt kê cụ thể: file, dòng, vấn đề, hướng sửa.\n"
            f"Kết luận: APPROVED hoặc REQUEST CHANGES.{_rag_line}"
        ),
        agent=reviewer,
        context=[t4, t5],
        expected_output="Danh sách vấn đề và kết luận APPROVED hoặc REQUEST CHANGES.",
        output_file=f"{_WS_REL}/reports/t6_review.md",
    )

    t7 = Task(
        description=(
            "Tổng hợp kết quả pipeline, xuất báo cáo cuối cho User gồm:\n"
            "1. Tóm tắt những gì đã làm được.\n"
            "2. Liệt kê file đã tạo/sửa.\n"
            "3. Kết quả test/QC (từ t5).\n"
            "4. Nhận xét chất lượng code (từ t6).\n"
            "5. Bước tiếp theo (nếu có).\n"
            "Câu cuối: '✅ Xong! Đã [action]. [X] file thay đổi. [Y] test pass. Ông check nhé!'"
        ),
        agent=pm,
        context=[t4, t5, t6],
        expected_output="Báo cáo tóm tắt ngắn gọn, rõ ràng về kết quả pipeline.",
        output_file=f"{_WS_REL}/reports/t7_final_report.md",
    )

    return [t1, t2, t3, t4, t5, t6, t7]

