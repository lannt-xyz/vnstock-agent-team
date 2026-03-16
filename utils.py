import os
from dotenv import load_dotenv
from crewai import LLM

load_dotenv()

class LLMFactory:
    def __init__(self):
        # Gom hết các key vào một danh sách để xoay tua
        self.keys = [os.getenv(f"GEMINI_KEY_{i}") for i in range(1, 6)]
        self.keys = [k for k in self.keys if k]
        self.current_key_index = 0
        
        if not self.keys:
            raise ValueError("Thiếu GEMINI_KEY_1... trong .env")

    def _get_next_key(self):
        key = self.keys[self.current_key_index]
        self.current_key_index = (self.current_key_index + 1) % len(self.keys)
        return key

    def get_pro_model(self):
        """Dùng cho tư duy chiến lược, phân tích dữ liệu phức tạp"""
        return LLM(
            model="gemini/gemini-2.5-pro",
            api_key=self._get_next_key(),
            temperature=0.2
        )

    def get_flash_model(self):
        """Dùng cho thực thi code, đọc file, các task cần tốc độ"""
        return LLM(
            model="gemini/gemini-2.5-flash",
            api_key=self._get_next_key(),
            temperature=0.1
        )

llm_factory = LLMFactory()