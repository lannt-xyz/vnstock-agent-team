from crewai import Agent
from crewai_tools import CodeInterpreterTool, DirectoryReadTool, FileReadTool, FileWriterTool

from utils import llm_factory

# Scope tools vào các thư mục cụ thể — tránh agents đọc .env / source code
ml_dir_tool   = DirectoryReadTool(directory='./ml')
data_dir_tool = DirectoryReadTool(directory='./data')
file_read     = FileReadTool()
file_write    = FileWriterTool()
code_tool     = CodeInterpreterTool()

quant_strategist = Agent(
    role='Lead Quant Strategist',
    goal='Phân tích logic tài chính và tìm điểm mù của chiến thuật',
    backstory='Bạn là bộ não của team, chuyên xử lý những vấn đề trừu tượng và khó.',
    llm=llm_factory.get_pro_model(),
    tools=[ml_dir_tool, data_dir_tool, file_read],
    verbose=True,
    allow_delegation=True,
    max_iter=8,
)

# Ông Dev cần nhanh, chính xác, làm theo mẫu -> Dùng FLASH (Flash code rất tốt)
algo_dev = Agent(
    role='Algorithmic Engineer',
    goal='Viết code Python và thực hiện các bộ lọc theo yêu cầu',
    backstory='Bạn là một cỗ máy viết code thuần thục và nhanh chóng.',
    llm=llm_factory.get_flash_model(),
    tools=[ml_dir_tool, file_read, file_write],
    verbose=True,
    max_iter=10,
)

# Ông Tester chạy code và đọc log -> Dùng FLASH (vì log trading rất dài, Flash đọc khỏe hơn)
risk_auditor = Agent(
    role='Risk & Performance Auditor',
    goal='Thực thi backtest và báo cáo số liệu',
    backstory='Bạn tập trung vào các con số và kết quả thực thi lệnh.',
    llm=llm_factory.get_flash_model(),
    tools=[code_tool, data_dir_tool, ml_dir_tool, file_read, file_write],
    verbose=True,
    max_iter=8,
)