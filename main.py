import os
import re
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
from crewai import Crew, Process

load_dotenv()

# Import WORKSPACE_ROOT trước để tạo thư mục đúng chỗ
from tools import WORKSPACE_ROOT  # noqa: E402

# Tạo các thư mục workspace cần thiết (có thể nằm ngoài project này)
(WORKSPACE_ROOT / "ml").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "data").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "reports").mkdir(parents=True, exist_ok=True)

from agents import create_agents  # noqa: E402
from tasks import create_quant_tasks  # noqa: E402
from utils import llm_factory  # noqa: E402

USER_REQUEST = (
    "Tối ưu mô hình trade chứng khoán VN. "
    "Hãy tìm cách tăng win rate từ 60% lên 65% "
    "bằng cách thêm bộ lọc xu hướng và quản lý vốn."
)
WIN_RATE_TARGET = 65.0
MAX_CYCLES = 3

STATE_FILE   = WORKSPACE_ROOT / "state.json"       # lưu kết quả cycle cuối cùng
HISTORY_LOG  = WORKSPACE_ROOT / "crew_history.log" # human-readable log


def _parse_win_rate(text: str) -> float | None:
    matches = re.findall(r'win[\s_-]?rate[:\s=]+(\d+(?:\.\d+)?)\s*%', text, re.IGNORECASE)
    return max(float(m) for m in matches) if matches else None


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


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    state = _load_state()
    last_result: str | None = state.get("last_result")
    last_win_rate: float | None = state.get("last_win_rate")

    _log("=" * 50)
    if last_result:
        _log(f"SESSION RESUME — last win rate: {last_win_rate or '?'}%")
    else:
        _log("SESSION START (no previous state)")
    _log(f"Target: {WIN_RATE_TARGET}%")

    for cycle in range(1, MAX_CYCLES + 1):
        print(f"\n{'='*55}")
        print(f"  QUANT TEAM — CYCLE {cycle}/{MAX_CYCLES}")
        print(f"{'='*55}")
        _log(f"Cycle {cycle}/{MAX_CYCLES} started")

        # Tạo agents mới mỗi cycle → key rotation (mỗi get_pro/flash_model() lấy key tiếp theo)
        quant_strategist, algo_dev, risk_auditor = create_agents()

        tasks = create_quant_tasks(
            quant_strategist, algo_dev, risk_auditor,
            USER_REQUEST,
            previous_result=last_result,
        )

        # Retry với key tiếp theo nếu gặp 429 trong cycle này
        retry_attempts = len(llm_factory.keys)
        result = None
        for attempt in range(retry_attempts):
            try:
                quant_crew = Crew(
                    agents=[quant_strategist, algo_dev, risk_auditor],
                    tasks=tasks,
                    process=Process.hierarchical,
                    manager_llm=llm_factory.get_pro_model(),
                    verbose=True,
                )
                result = quant_crew.kickoff()
                break  # thành công
            except Exception as e:
                err = str(e)
                if '429' in err or 'RESOURCE_EXHAUSTED' in err or 'quota' in err.lower():
                    if attempt < retry_attempts - 1:
                        _log(f"429 on attempt {attempt + 1} — rotating to next key...")
                        # Tạo lại agents với key mới
                        quant_strategist, algo_dev, risk_auditor = create_agents()
                        tasks = create_quant_tasks(
                            quant_strategist, algo_dev, risk_auditor,
                            USER_REQUEST,
                            previous_result=last_result,
                        )
                    else:
                        _log(f"All keys exhausted on cycle {cycle}. Saving state and stopping.")
                        raise
                else:
                    raise

        last_result = str(result)

        win_rate = _parse_win_rate(last_result)
        _save_state({"last_result": last_result, "last_win_rate": win_rate})

        if win_rate is not None:
            _log(f"Cycle {cycle} — Win rate: {win_rate:.1f}%")
            if win_rate >= WIN_RATE_TARGET:
                _log(f"TARGET REACHED ({win_rate:.1f}% >= {WIN_RATE_TARGET}%). Stopping.")
                break
            else:
                _log(f"Not reached yet. Continuing to cycle {cycle + 1}...")
        else:
            _log(f"Cycle {cycle} — Could not parse win rate from output.")

    _log("SESSION END")
    print("\n--- FINAL STRATEGY REPORT ---\n")
    print(last_result)

    print("\n--- FINAL STRATEGY REPORT ---\n")
    print(last_result)