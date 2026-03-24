import os
import time
from itertools import cycle
from dotenv import load_dotenv
from crewai import LLM
from pydantic import PrivateAttr

load_dotenv()

class RobustGeminiLLM(LLM):
    _key_cycle: any = PrivateAttr()
    _current_key: str = PrivateAttr()

    def __init__(self, **kwargs):
        # 1. Lấy danh sách keys ra
        keys = kwargs.pop('keys', [])
        if not keys:
            raise ValueError("Phải truyền danh sách keys vào RobustGeminiLLM")
        
        # 2. Trick: Truyền 1 key đầu tiên vào kwargs để 'vượt mặt' validation của CrewAI
        first_key = keys[0]
        kwargs['api_key'] = first_key 
        
        # 3. Khởi tạo class cha (LLM)
        super().__init__(**kwargs)
        
        # 4. Thiết lập hệ thống xoay key nội bộ
        self._key_cycle = cycle(keys)
        self._current_key = first_key
        # Đảm bảo đồng bộ key
        self.api_key = self._current_key

    def call(self, *args, **kwargs):
        max_retries = 5
        for i in range(max_retries):
            try:
                # Ép dùng key hiện tại trong vòng xoay
                self.api_key = self._current_key
                result = super().call(*args, **kwargs)
                # Gemini đôi khi trả về None/empty — retry với key khác
                if not result:
                    raise Exception("NONE_OR_EMPTY_RESPONSE")
                return result
            except Exception as e:
                err_msg = str(e).upper()
                # Bắt lỗi 429, Quota, hoặc empty response
                if any(x in err_msg for x in ["429", "RESOURCE_EXHAUSTED", "QUOTA", "NONE_OR_EMPTY"]):
                    self._current_key = next(self._key_cycle)
                    wait_time = (i + 1) * 3
                    print(f"--- [Retry {i+1}] Key: ...{self._current_key[-4:]} | Thử lại sau {wait_time}s ---")
                    time.sleep(wait_time)
                else:
                    raise e
        raise Exception("Đã retry hết lần mà vẫn không có response!")

class LLMFactory:
    def __init__(self):
        # Lấy từ 1 đến 10
        raw_keys = [os.getenv(f"GEMINI_KEY_{i}") for i in range(1, 11)]
        self.keys = [k for k in raw_keys if k]
        if not self.keys:
            # Fallback nếu ông lỡ tay đặt tên biến là GEMINI_API_KEY
            if os.getenv("GEMINI_API_KEY"):
                self.keys = [os.getenv("GEMINI_API_KEY")]
            else:
                raise ValueError("Check lại file .env: Cần GEMINI_KEY_1, GEMINI_KEY_2...")

    def get_local_model(self):
        return LLM(
            model="ollama/qwen3.5-32k:latest",
            base_url="http://localhost:11434",
            temperature=0.2,
            timeout=120,  # 900s → 120s: fail nhanh thay vì ngồi chờ 15 phút
        )

    def get_deepseek_model(self):
        return LLM(
            model="deepseek/gemini-2.0-flash:latest",
            base_url="https://api.deepseek.com",
            temperature=0.1,
            timeout=120,
        )

    def get_flash_model(self, is_pro=False):
        model_name = "gemini/gemini-2.5-flash" if not is_pro else "gemini/gemini-2.5-pro"

        return RobustGeminiLLM(
            model=model_name,
            keys=self.keys,
            api_key=self.keys[0],  # cần cho LLM.__new__ chạy trước __init__
            temperature=0.1 if not is_pro else 0.2,
            max_tokens=8192,  # tránh output bị cắt cụt giữa chừng
        )

    def get_pro_model(self):
        return self.get_flash_model(is_pro=True)

# Khởi tạo factory
llm_factory = LLMFactory()
