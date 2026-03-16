import os
import re
from dotenv import load_dotenv
from crewai import Crew, Process
from crewai.memory.unified_memory import Memory

load_dotenv()

# Import WORKSPACE_ROOT trước để tạo thư mục đúng chỗ
from tools import WORKSPACE_ROOT  # noqa: E402

# Tạo các thư mục workspace cần thiết (có thể nằm ngoài project này)
(WORKSPACE_ROOT / "ml").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "data").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "reports").mkdir(parents=True, exist_ok=True)

from agents import quant_strategist, algo_dev, risk_auditor  # noqa: E402
from tasks import create_quant_tasks  # noqa: E402
from utils import llm_factory  # noqa: E402

USER_REQUEST = (
    "Tối ưu mô hình trade chứng khoán VN. "
    "Hãy tìm cách tăng win rate từ 60% lên 65% "
    "bằng cách thêm bộ lọc xu hướng và quản lý vốn."
)
WIN_RATE_TARGET = 65.0
MAX_CYCLES = 3


def _parse_win_rate(text: str) -> float | None:
    """Trích win rate (%) từ chuỗi kết quả. Trả về None nếu không parse được."""
    matches = re.findall(r'win[\s_-]?rate[:\s=]+(\d+(?:\.\d+)?)\s*%', text, re.IGNORECASE)
    if matches:
        return max(float(m) for m in matches)
    return None


if __name__ == "__main__":
    last_result: str | None = None

    for cycle in range(1, MAX_CYCLES + 1):
        print(f"\n{'='*55}")
        print(f"  QUANT TEAM — CYCLE {cycle}/{MAX_CYCLES}")
        print(f"{'='*55}")

        tasks = create_quant_tasks(
            quant_strategist, algo_dev, risk_auditor,
            USER_REQUEST,
            previous_result=last_result,
        )

        embedder_fn = llm_factory.make_embedder()
        crew_memory = Memory(
            llm=llm_factory.get_flash_model(),
            embedder=embedder_fn,
        )

        quant_crew = Crew(
            agents=[quant_strategist, algo_dev, risk_auditor],
            tasks=tasks,
            process=Process.hierarchical,
            manager_llm=llm_factory.get_pro_model(),
            verbose=True,
            memory=crew_memory,
        )

        result = quant_crew.kickoff()
        last_result = str(result)

        win_rate = _parse_win_rate(last_result)
        if win_rate is not None:
            print(f"\n[Cycle {cycle}] Win rate phát hiện: {win_rate:.1f}%")
            if win_rate >= WIN_RATE_TARGET:
                print(f"Target {WIN_RATE_TARGET}% đạt được. Dừng sớm.")
                break
            else:
                print(f"Chưa đạt target. Tiếp tục cycle {cycle + 1}...")
        else:
            print(f"\n[Cycle {cycle}] Không parse được win rate — xem report thủ công.")

    print("\n--- FINAL STRATEGY REPORT ---\n")
    print(last_result)