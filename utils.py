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
                return super().call(*args, **kwargs)
            except Exception as e:
                err_msg = str(e).upper()
                # Bắt lỗi 429 hoặc Quota
                if any(x in err_msg for x in ["429", "RESOURCE_EXHAUSTED", "QUOTA"]):
                    self._current_key = next(self._key_cycle)
                    wait_time = (i + 1) * 3
                    print(f"--- [Rate Limit] Đổi sang Key: ...{self._current_key[-4:]} | Thử lại sau {wait_time}s ---")
                    time.sleep(wait_time)
                else:
                    raise e
        raise Exception("Đã dùng hết sạch các API Keys hiện có mà vẫn bị chặn!")

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
            model="ollama/deepseek-coder-v2:16b-lite-instruct-q4_K_M",
            base_url="http://localhost:11434",
            temperature=0.2,
            extra_body={"options": {"num_ctx": 16000}},
        )

    def get_flash_model(self, is_pro=False):
        # Gemini 2.0 Flash thường mượt hơn trong giai đoạn này
        model_name = "gemini/gemini-2.0-flash" if not is_pro else "gemini/gemini-2.5-pro"

        return RobustGeminiLLM(
            model=model_name,
            keys=self.keys,
            api_key=self.keys[0],  # cần cho LLM.__new__ chạy trước __init__
            temperature=0.1 if not is_pro else 0.2,
        )

    def get_pro_model(self):
        return self.get_flash_model(is_pro=True)

# Khởi tạo factory
llm_factory = LLMFactory()
