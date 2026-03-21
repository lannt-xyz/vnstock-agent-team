from crewai import Task

from tools import WORKSPACE_ROOT


# ── Dev Team Workflow ──────────────────────────────────────────────────────────
def create_dev_team_tasks(pm, plan_reviewer, architect, coder, qc, reviewer, request, previous_result=None):
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
            f"Yêu cầu từ User: {request}.{retry_context}\n\n"
            "Phân tích yêu cầu, chia nhỏ thành danh sách User Stories / Task kỹ thuật rõ ràng. "
            "Xác định: phạm vi, tiêu chí hoàn thành (Done Criteria), và rủi ro tiềm ẩn. "
            "Ghi kế hoạch vào 'reports/t1_task_plan.md'."
            "LƯU Ý VỀ ĐƯỜNG DẪN: Khi gọi Write File, KHÔNG thêm 'workspace/' vào trước đường dẫn. "
            "Ví dụ đúng: file_path='reports/t1_task_plan.md'. Ví dụ SAI: file_path='workspace/reports/t1_task_plan.md'."
        ),
        agent=pm,
        expected_output=(
            "Tài liệu kế hoạch gồm: danh sách User Stories, tiêu chí hoàn thành, "
            "và rủi ro tiềm ẩn được ghi vào 'reports/t1_task_plan.md'."
        ),
        output_file=str(ws / "reports" / "t1_task_plan.md"),
    )

    t2 = Task(
        description=(
            "Đọc kế hoạch của PM tại 'reports/t1_task_plan.md' và đánh giá nghiêm túc:\n"
            "1. Các User Stories có đủ rõ ràng và khả thi về mặt kỹ thuật không?\n"
            "2. Có Task nào bị thiếu, dư thừa, hoặc mâu thuẫn nhau không?\n"
            "3. Done Criteria có đo lường được không?\n"
            "4. Rủi ro kỹ thuật nào chưa được PM nhận ra?\n"
            "Nếu plan cần sửa: liệt kê cụ thể điểm cần PM điều chỉnh.\n"
            "Nếu plan ổn: xác nhận APPROVED và giải thích lý do.\n"
            "Ghi nhận xét vào 'reports/t2_plan_review.md'."
            "LƯU Ý: file_path KHÔNG được có tiền tố 'workspace/'."
        ),
        agent=plan_reviewer,
        context=[t1],
        expected_output=(
            "Báo cáo review plan gồm: nhận xét từng User Story, danh sách điểm cần sửa (nếu có), "
            "và kết luận APPROVED hoặc REQUEST CHANGES kèm lý do."
        ),
        output_file=str(ws / "reports" / "t2_plan_review.md"),
    )

    t3 = Task(
        description=(
            "Dựa trên kế hoạch đã được review, thiết kế giải pháp kỹ thuật:\n"
            "QUAN TRỌNG: Technology stack phải xuất phát TRỰC TIẾP từ yêu cầu ở t1/t2. "
            "KHÔNG tự ý chọn framework (Flask, Django, SQLAlchemy...) nếu yêu cầu không đề cập.\n"
            "Ví dụ: nếu yêu cầu là web app đọc Google Sheets → tech stack là HTML+JS+Google Sheets API, không phải Flask.\n\n"
            "1. Đọc kỹ lại yêu cầu trong 'reports/t1_task_plan.md' và 'reports/t2_plan_review.md'.\n"
            "2. Xác định technology stack phù hợp với yêu cầu thực tế (front-end, back-end, third-party API...).\n"
            "3. Liệt kê TẤT CẢ file cần tạo với đường dẫn và mô tả nội dung cụ thể.\n"
            "4. Chọn Design Pattern phù hợp, giải thích tại sao.\n"
            "5. Mô tả data flow giữa các module.\n"
            "Ghi tài liệu vào 'reports/t3_architecture.md'.\n"
            "LƯU Ý: file_path KHÔNG được có tiền tố 'workspace/'."
        ),
        agent=architect,
        context=[t1, t2],
        expected_output=(
            "Tài liệu kiến trúc gồm: danh sách file cần thay đổi, design pattern được chọn, "
            "phân tích side effects, và data flow."
        ),
        output_file=str(ws / "reports" / "t3_architecture.md"),
    )

    t4 = Task(
        description=(
            "Dựa trên thiết kế của Architect, viết toàn bộ source code cho dự án.\n"
            "Tất cả file code phải nằm trong thư mục 'src/' (không có tiền tố 'workspace/').\n\n"
            "QUY TẮC VỀ ĐƯỜNG DẪN (QUAN TRỌNG):\n"
            "  - ĐÚNG: file_path='src/index.html', file_path='src/app.js'\n"
            "  - SAI:  file_path='workspace/src/index.html'\n\n"
            "QUY TRÌNH BẮT BUỘC — làm tuần tự từng bước:\n"
            "Bước 1: Đọc 'reports/t3_architecture.md' để lấy danh sách file cần tạo.\n"
            "Bước 2: Với MỖI file trong danh sách, gọi Write File để ghi nội dung thật:\n"
            "  - file_path: relative path bắt đầu bằng 'src/', ví dụ 'src/index.html'\n"
            "  - content: toàn bộ nội dung file đó (KHÔNG được để trống hoặc placeholder)\n"
            "  - Gọi Write File một lần cho mỗi file — KHÔNG gộp nhiều file vào 1 lần gọi.\n"
            "Bước 3: Sau khi ghi hết, ghi tóm tắt danh sách file đã tạo vào 'reports/t4_code_summary.md'.\n\n"
            "NGHIÊM CẤM:\n"
            "- Bỏ qua bất kỳ file nào đã được Architect chỉ định.\n"
            "- Dùng markdown code fence (``` ```) khi gọi Write File.\n"
            "- Để nội dung file trống hoặc dùng placeholder như '# TODO'.\n"
        ),
        agent=coder,
        context=[t1, t2, t3],
        expected_output=(
            "Toàn bộ source code đã được ghi xuống disk trong thư mục 'src/'. "
            "File 'reports/t4_code_summary.md' liệt kê đường dẫn và mô tả ngắn của từng file đã tạo."
            "Phải tạo file hướng dẫn sử dụng và triển khai nếu Architect yêu cầu. Code phải sạch, có comment giải thích, "
        ),
        output_file=str(ws / "reports" / "t4_code_summary.md"),
    )

    t5 = Task(
        description=(
            "Kiểm thử toàn bộ code vừa được Coder viết:\n"
            "1. Viết Unit Test cho các function/class chính.\n"
            "   - File test PHẢI lưu vào 'tests/test_*.py' (ví dụ: 'tests/test_models.py').\n"
            "   - KHÔNG lưu file test vào thư mục gốc workspace.\n"
            "2. Chạy pytest và ghi lại toàn bộ output.\n"
            "3. Nếu có test FAIL: ghi rõ nguyên nhân và yêu cầu Coder sửa lại.\n"
            "4. Chỉ đánh dấu PASS khi 100% test xanh.\n"
            "5. Kiểm tra thêm: lỗi bảo mật rõ ràng, lỗi logic, vấn đề hiệu năng.\n"
            "Ghi báo cáo vào 'reports/t5_qc_report.md'. "
            "LƯU Ý: file_path KHÔNG được có tiền tố 'workspace/'."
        ),
        agent=qc,
        context=[t4],
        expected_output=(
            "Báo cáo QC gồm: kết quả pytest (số pass/fail), "
            "danh sách bug tìm được (nếu có), và xác nhận 100% pass cuối cùng."
        ),
        output_file=str(ws / "reports" / "t5_qc_report.md"),
    )

    t6 = Task(
        description=(
            "Review toàn bộ code mà Coder vừa viết "
            "(không cần quan tâm code chạy được không — đó là việc của QC):\n"
            "1. Code Style: tên biến/hàm có rõ ràng, nhất quán không?\n"
            "2. Clean Code: có code thừa, hàm quá dài (>30 dòng), logic lồng nhau quá sâu không?\n"
            "3. Security: có lỗ hổng hiển nhiên (hardcode secret, SQL injection, v.v.) không?\n"
            "4. Liệt kê cụ thể: file, dòng, vấn đề, và hướng sửa.\n"
            "Ghi nhận xét vào 'reports/t6_review.md'. "
            "LƯU Ý: file_path KHÔNG được có tiền tố 'workspace/'."
        ),
        agent=reviewer,
        context=[t4, t5],
        expected_output=(
            "Báo cáo review gồm: danh sách vấn đề style/clean code/security (nếu có), "
            "và kết luận: APPROVED hoặc REQUEST CHANGES."
        ),
        output_file=str(ws / "reports" / "t6_review.md"),
    )

    t7 = Task(
        description=(
            "Tổng hợp kết quả từ toàn bộ pipeline và tạo báo cáo cuối cùng cho User:\n"
            "1. Tóm tắt những gì đã làm được.\n"
            "2. Liệt kê file đã tạo/sửa.\n"
            "3. Kết quả test (số lượng pass/fail từ QC).\n"
            "4. Nhận xét chất lượng code (từ Reviewer).\n"
            "5. Bước tiếp theo nếu có.\n"
            "Ghi tóm tắt vào 'reports/t7_final_report.md'.\n"
            "LƯU Ý: file_path KHÔNG được có tiền tố 'workspace/'.\n"
            "Dòng cuối báo cáo phải theo format: "
            "'✅ Xong! Đã [action]. [X] file thay đổi. [Y] test pass. Ông check nhé!'"
        ),
        agent=pm,
        context=[t3, t4, t5, t6],
        expected_output=(
            "Báo cáo tóm tắt cuối cùng, ngắn gọn, rõ ràng: "
            "việc đã xong, số file thay đổi, kết quả test, chất lượng code."
        ),
        output_file=str(ws / "reports" / "t7_final_report.md"),
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
        output_file=str(ws / "reports" / "t1_analysis.md"),
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
        output_file=str(ws / "reports" / "t2_code_summary.md"),
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
        output_file=str(ws / "reports" / "t3_backtest_result.md"),
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
        output_file=str(ws / "reports" / "cafef_t1_crawl.md"),
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
        output_file=str(ws / "reports" / "cafef_t2_classify.md"),
    )

    t3 = Task(
        description=(
            "Đọc file 'data/classified_cafef.json', chuyển đổi và lưu thành 'data/cafef_news.csv' "
            "với các cột: title, link, summary, category, date. Nếu thiếu trường thì để trống."
        ),
        agent=coder,
        context=[t2],
        expected_output="File 'data/cafef_news.csv' chứa dữ liệu tin tức đã phân loại, dạng CSV.",
        output_file=str(ws / "reports" / "cafef_t3_csv.md"),
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
        output_file=str(ws / "reports" / "cafef_pipeline_report.md"),
    )
    return [t]