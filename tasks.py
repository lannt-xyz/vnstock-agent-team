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
def create_dev_team_tasks(pm, plan_reviewer, architect, coder, qc, reviewer, request, previous_result=None, is_frontend=False):
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
            "1. Tech stack TRỰC TIẾP từ yêu cầu — web app Google Sheets → HTML+JS+Google Sheets API.\n"
            "2. Danh sách TẤT CẢ file cần tạo với đường dẫn 'src/...' và mô tả nội dung.\n"
            "3. Design Pattern và lý do.\n"
            "4. Data flow giữa các module."
        ),
        agent=architect,
        context=[t1, t2],
        expected_output="Danh sách file cần tạo (src/...), design pattern, data flow.",
        output_file=f"{_WS_REL}/reports/t3_architecture.md",
    )

    t4 = Task(
        description=(
            "Dùng Read File đọc 'reports/t3_architecture.md'.\n"
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

    if is_frontend:
        t5_desc = (
            "Kiểm tra chất lượng code frontend do Coder viết:\n"
            "1. Dùng Read File đọc tất cả file trong 'src/'.\n"
            "2. Kiểm tra HTML: thẻ đóng mở đúng, có DOCTYPE và charset.\n"
            "3. Kiểm tra JS: không có syntax error rõ ràng, không có biến chưa khai báo.\n"
            "4. Kiểm tra bảo mật: không hardcode API key, không XSS rõ ràng.\n"
            "5. Kiểm tra responsive: có meta viewport, CSS media query.\n"
            "Xuất báo cáo với kết luận PASS hoặc FAIL kèm chi tiết."
        )
        t5_expected = "Báo cáo QC kết quả kiểm tra HTML/JS/CSS: PASS hoặc FAIL kèm chi tiết."
    else:
        t5_desc = (
            "Kiểm thử toàn bộ code do Coder viết:\n"
            "1. Viết Unit Test, lưu vào 'tests/test_*.py' (không lưu vào thư mục gốc).\n"
            "2. Chạy pytest, ghi lại toàn bộ output.\n"
            "3. Nếu có test FAIL: ghi rõ nguyên nhân.\n"
            "4. Chỉ kết luận PASS khi 100% test xanh.\n"
            "5. Kiểm tra thêm: lỗi bảo mật, lỗi logic, vấn đề hiệu năng.\n"
            "Xuất báo cáo kết quả pytest."
        )
        t5_expected = "Báo cáo pytest (pass/fail) và xác nhận 100% pass."

    t5 = Task(
        description=t5_desc,
        agent=qc,
        context=[t4],
        expected_output=t5_expected,
        output_file=f"{_WS_REL}/reports/t5_qc_report.md",
    )

    t6 = Task(
        description=(
            "Dùng Read File đọc các file trong 'src/', xuất nhận xét:\n"
            "1. Code Style: tên biến/hàm có rõ ràng, nhất quán không?\n"
            "2. Clean Code: code thừa, hàm >30 dòng, logic lồng nhau quá sâu?\n"
            "3. Security: hardcode secret, injection rõ ràng?\n"
            "4. Liệt kê cụ thể: file, dòng, vấn đề, hướng sửa.\n"
            "Kết luận: APPROVED hoặc REQUEST CHANGES."
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


# ── Quant Workflow ─────────────────────────────────────────────────────────────
def create_quant_tasks(architect, coder, qc, request, previous_result=None):
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
            f"Nhiệm vụ: {request}.{retry_context}\n"
            f"Workspace: {ws}\n"
            "Phân tích logic tài chính, xác định nguyên nhân hạn chế win rate, "
            "và lên kế hoạch cải tiến chiến lược trading."
        ),
        agent=architect,
        expected_output=(
            "Bản phân tích gồm:\n"
            "1. Danh sách ≥3 nguyên nhân khiến win rate dừng ở 60%.\n"
            "2. Đề xuất cụ thể cho từng nguyên nhân (bộ lọc, tham số, logic).\n"
            "3. Thứ tự ưu tiên implement."
        ),
        output_file=f"{_WS_REL}/reports/t1_analysis.md",
    )

    t2 = Task(
        description=(
            f"Dựa trên phân tích của t1, hãy viết file tại {ws}/ml/optimized_strategy.py.\n"
            "Khi gọi 'Write File', dùng file_path = 'ml/optimized_strategy.py' (relative to workspace).\n"
            "Yêu cầu bắt buộc:\n"
            "- Thêm Regime Detection (trending vs ranging) dựa trên ADX hoặc slope của EMA200.\n"
            "- Tối ưu tham số RSI/EMA với logic rõ ràng.\n"
            "- Có hàm `run_backtest(prices: list[float]) -> dict` trả về dict chứa key 'win_rate' (float, 0-100).\n"
            "- KHÔNG dùng markdown code fence, chỉ Python thuần.\n"
            "- Nếu file đã tồn tại, hãy đọc trước rồi cải tiến, không viết lại từ đầu."
        ),
        agent=coder,
        context=[t1],
        expected_output=(
            f"File {ws}/ml/optimized_strategy.py sạch sẽ, có thể import và chạy được, "
            "với hàm run_backtest() trả về dict có key 'win_rate'."
        ),
        output_file=f"{_WS_REL}/reports/t2_code_summary.md",
    )

    t3 = Task(
        description=(
            "Sử dụng CodeInterpreterTool để thực thi optimized_strategy.py.\n"
            f"Đường dẫn tuyệt đối của file: {ws}/ml/optimized_strategy.py\n"
            "Trong code chạy, hãy dùng đường dẫn tuyệt đối này khi import/exec file.\n"
            f"Nếu thư mục {ws}/data/ chưa có dữ liệu thật, hãy tự sinh dữ liệu giả "
            "(VD: random walk với drift nhỏ, 500 điểm) để chạy backtest.\n"
            "Yêu cầu:\n"
            "1. Chạy hàm run_backtest() và thu thập win_rate.\n"
            "2. Nếu win_rate < 65.0: liệt kê chính xác các bộ lọc/tham số cần điều chỉnh "
            "   và ghi vào 'reports/fix_instructions.md' để cycle tiếp theo dùng.\n"
            "3. Nếu win_rate >= 65.0: ghi báo cáo thành công vào 'reports/final_report.md'.\n"
            "Luôn in ra dòng 'Win Rate: XX.X%' trong output."
        ),
        agent=qc,
        context=[t2],
        expected_output=(
            "Báo cáo backtest với:\n"
            "- Dòng 'Win Rate: XX.X%' (bắt buộc).\n"
            "- Nếu < 65%: danh sách fix cụ thể đã ghi vào reports/fix_instructions.md.\n"
            "- Nếu >= 65%: tuyên bố thành công và path của final_report.md."
        ),
        output_file=f"{_WS_REL}/reports/t3_backtest_result.md",
    )

    return [t1, t2, t3]


# ── Cafef News Workflow ────────────────────────────────────────────────────────
def create_cafef_news_tasks(architect, coder, qc):
    ws = WORKSPACE_ROOT

    t1 = Task(
        description=(
            "Viết script Python crawl các bài viết mới nhất từ trang chủ hoặc chuyên mục của cafef.vn. "
            "Lấy các trường: tiêu đề, link, tóm tắt/ngày/thời gian nếu có. "
            f"Lưu file JSON vào 'data/raw_cafef.json' (relative to workspace: {ws}). "
            "Không lấy dữ liệu ngoài cafef.vn."
        ),
        agent=coder,
        expected_output="File 'data/raw_cafef.json' chứa danh sách bài viết dạng JSON (list of dict).",
        output_file=f"{_WS_REL}/reports/cafef_t1_crawl.md",
    )

    t2 = Task(
        description=(
            "Đọc file 'data/raw_cafef.json', phân loại tin tức theo các chủ đề: "
            "Chứng khoán, Bất động sản, Kinh tế vĩ mô, Doanh nghiệp, v.v. "
            "(dựa vào từ khóa hoặc nội dung tiêu đề). "
            "Lưu file kết quả vào 'data/classified_cafef.json' (list of dict, thêm trường 'category')."
        ),
        agent=architect,
        context=[t1],
        expected_output="File 'data/classified_cafef.json' chứa danh sách bài viết đã phân loại (có trường 'category').",
        output_file=f"{_WS_REL}/reports/cafef_t2_classify.md",
    )

    t3 = Task(
        description=(
            "Đọc file 'data/classified_cafef.json', chuyển đổi và lưu thành 'data/cafef_news.csv' "
            "với các cột: title, link, summary, category, date. Nếu thiếu trường thì để trống."
        ),
        agent=coder,
        context=[t2],
        expected_output="File 'data/cafef_news.csv' chứa dữ liệu tin tức đã phân loại, dạng CSV.",
        output_file=f"{_WS_REL}/reports/cafef_t3_csv.md",
    )

    return [t1, t2, t3]


def create_cafef_news_pipeline_task(qc):
    ws = WORKSPACE_ROOT
    t = Task(
        description=(
            "Viết và thực thi script Python để tự động crawl các bài viết mới nhất từ cafef.vn, "
            "phân loại tin tức theo chủ đề (Chứng khoán, Bất động sản, Kinh tế vĩ mô, Doanh nghiệp, v.v.), "
            "và lưu kết quả ra file CSV 'data/cafef_news.csv' với các cột: title, link, summary, category, date. "
            "Script phải tự động thực hiện toàn bộ pipeline, không cần thao tác tay. "
            "Nếu thiếu trường thì để trống. Không lấy dữ liệu ngoài cafef.vn."
        ),
        agent=qc,
        expected_output="File 'data/cafef_news.csv' chứa dữ liệu tin tức đã phân loại, dạng CSV.",
        output_file=f"{_WS_REL}/reports/cafef_pipeline_report.md",
    )
    return [t]