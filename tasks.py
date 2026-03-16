from crewai import Task


def create_quant_tasks(quant_strategist, algo_dev, risk_auditor, request, previous_result=None):
    # Nếu là cycle retry, đính kèm kết quả cũ để agents không lặp lại sai lầm
    retry_context = ""
    if previous_result:
        retry_context = (
            f"\n\n[KẾT QUẢ CYCLE TRƯỚC — CẦN CẢI TIẾN]\n"
            f"{previous_result[:2000]}\n"
            f"Dựa trên kết quả trên, hãy xác định nguyên nhân win rate chưa đạt 65% "
            f"và tập trung vào điểm chưa giải quyết.\n"
        )

    t1 = Task(
        description=(
            f"Nhiệm vụ: {request}.{retry_context}\n"
            "Hãy đọc code trong thư mục ml/ để tìm lỗi logic. "
            "Nếu thư mục ml/ chưa có file nào, hãy phân tích lý thuyết về các nguyên nhân "
            "phổ biến khiến win rate dừng ở 60% trong chiến thuật momentum VN-index.\n"
            "Đưa ra TỐI THIỂU 3 giả thuyết cụ thể có thể kiểm chứng được."
        ),
        agent=quant_strategist,
        expected_output=(
            "Bản phân tích gồm:\n"
            "1. Danh sách ≥3 nguyên nhân khiến win rate dừng ở 60%.\n"
            "2. Đề xuất cụ thể cho từng nguyên nhân (bộ lọc, tham số, logic).\n"
            "3. Thứ tự ưu tiên implement."
        ),
        output_file="reports/t1_analysis.md",
    )

    t2 = Task(
        description=(
            "Dựa trên phân tích của t1, hãy viết file ml/optimized_strategy.py.\n"
            "Yêu cầu bắt buộc:\n"
            "- Thêm Regime Detection (trending vs ranging) dựa trên ADX hoặc slope của EMA200.\n"
            "- Tối ưu tham số RSI/EMA với logic rõ ràng.\n"
            "- Có hàm `run_backtest(prices: list[float]) -> dict` trả về dict chứa key 'win_rate' (float, 0-100).\n"
            "- KHÔNG dùng markdown code fence, chỉ Python thuần.\n"
            "- Nếu file đã tồn tại, hãy đọc trước rồi cải tiến, không viết lại từ đầu."
        ),
        agent=algo_dev,
        context=[t1],
        expected_output=(
            "File ml/optimized_strategy.py sạch sẽ, có thể import và chạy được, "
            "với hàm run_backtest() trả về dict có key 'win_rate'."
        ),
        output_file="reports/t2_code_summary.md",
    )

    t3 = Task(
        description=(
            "Sử dụng CodeInterpreterTool để thực thi ml/optimized_strategy.py.\n"
            "Nếu thư mục data/ chưa có dữ liệu thật, hãy tự sinh dữ liệu giả "
            "(VD: random walk với drift nhỏ, 500 điểm) để chạy backtest.\n"
            "Yêu cầu:\n"
            "1. Chạy hàm run_backtest() và thu thập win_rate.\n"
            "2. Nếu win_rate < 65.0: liệt kê chính xác các bộ lọc/tham số cần điều chỉnh "
            "   và ghi vào reports/fix_instructions.md để cycle tiếp theo dùng.\n"
            "3. Nếu win_rate >= 65.0: ghi báo cáo thành công vào reports/final_report.md.\n"
            "Luôn in ra dòng 'Win Rate: XX.X%' trong output."
        ),
        agent=risk_auditor,
        context=[t2],
        expected_output=(
            "Báo cáo backtest với:\n"
            "- Dòng 'Win Rate: XX.X%' (bắt buộc).\n"
            "- Nếu < 65%: danh sách fix cụ thể đã ghi vào reports/fix_instructions.md.\n"
            "- Nếu >= 65%: tuyên bố thành công và path của final_report.md."
        ),
        output_file="reports/t3_backtest_result.md",
    )

    return [t1, t2, t3]