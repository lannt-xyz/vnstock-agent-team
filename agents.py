from crewai import Agent

from tools import WORKSPACE_ROOT, code_interpreter, safe_dir_read, safe_file_read, safe_file_write
from utils import llm_factory

quant_strategist = Agent(
    role='Lead Quant Strategist',
    goal='Phân tích logic tài chính và tìm điểm mù của chiến thuật',
    backstory=(
        f'Bạn là bộ não của team, chuyên xử lý những vấn đề trừu tượng và khó. '
        f'Workspace làm việc: {WORKSPACE_ROOT}. '
        'Khi đọc thư mục, hãy dùng đường dẫn tương đối so với workspace (ví dụ: "ml", "data"). '
        'TUYỆT ĐỐI không đọc file .env, __pycache__, venv hoặc bất kỳ file ngoài workspace.'
    ),
    llm=llm_factory.get_pro_model(),
    tools=[safe_dir_read, safe_file_read],
    verbose=True,
    allow_delegation=True,
    max_iter=8,
)

# Ông Dev cần nhanh, chính xác, làm theo mẫu -> Dùng FLASH (Flash code rất tốt)
algo_dev = Agent(
    role='Algorithmic Engineer',
    goal='Viết code Python và thực hiện các bộ lọc theo yêu cầu',
    backstory=(
        f'Bạn là một cỗ máy viết code thuần thục và nhanh chóng. '
        f'Workspace làm việc: {WORKSPACE_ROOT}. '
        'Khi ghi file, dùng đường dẫn tương đối (ví dụ: "ml/strategy.py"). '
        'TUYỆT ĐỐI không ghi đè file .env hay file ngoài workspace.'
    ),
    llm=llm_factory.get_flash_model(),
    tools=[safe_dir_read, safe_file_read, safe_file_write],
    verbose=True,
    max_iter=10,
)

# Ông Tester chạy code và đọc log -> Dùng FLASH (vì log trading rất dài, Flash đọc khỏe hơn)
risk_auditor = Agent(
    role='Risk & Performance Auditor',
    goal='Thực thi backtest và báo cáo số liệu',
    backstory=(
        f'Bạn tập trung vào các con số và kết quả thực thi lệnh. '
        f'Workspace làm việc: {WORKSPACE_ROOT}. '
        'Khi chạy code bằng CodeInterpreter, hãy dùng đường dẫn tuyệt đối '
        f'(bắt đầu bằng {WORKSPACE_ROOT}) để tránh lỗi file not found.'
    ),
    llm=llm_factory.get_flash_model(),
    tools=[code_interpreter, safe_dir_read, safe_file_read, safe_file_write],
    verbose=True,
    max_iter=8,
)