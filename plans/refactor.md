## Plan: Refactor Dev Team Agents (Revised)

**TL;DR** — 4 phase, 17 bước cụ thể. Cập nhật lần cuối: (a) buộc agent dùng tool bằng `system_prompt`, (b) truncate return value của `safe_file_write` + thêm console log bytes, (c) QC branch do Python quyết định, (d) LiteLLM timeout syntax đúng, (e) t2 guard dùng separator + re-instantiate Task.

---

### Phase 1 — Dọn cơ chế ghi file (main.py + tasks.py + tools.py)

1. Xóa `output_file=` khỏi toàn bộ 7 tasks trong tasks.py
2. Xóa `_flush_write_calls()`, `_extract_json_objects()`, `_normalize_path()` khỏi main.py
3. Xóa `_task_callback()`, `_step_callback()` khỏi main.py và bỏ params đó trong `Crew()`
4. **`safe_file_write` return value** → sửa trong tools.py: chỉ trả `"SUCCESS: {relative_path}"` thay vì full content. Nếu agent nhận lại cả file vừa ghi, nó ngốn hết context ngay lập tức.
   - Giữ nguyên: file **phải được ghi xuống disk** trước khi return.
   - Thêm console log (không đưa vào return): `print(f"[TOOL] Writing {len(content)} bytes to {path}...")` — để ông quan sát agent có đang ghi đúng/đủ không mà không làm rác context.
5. Thêm `system_prompt` vào **PM** và **Coder** (trong agents.py): *"Bạn BẮT BUỘC phải dùng tool `safe_file_write` để lưu mọi file. Tuyệt đối không nhả code hoặc nội dung file vào chat."* — không có cái này, khi xóa `_flush`, toàn bộ kết quả Coder viết sẽ bốc hơi.

---

### Phase 2 — Fix Agent Config (agents.py)

6. **PM**: `allow_delegation=True` → `False`
7. **PM**: `max_iter=8` → `10`
8. **Coder**: `max_iter=12` → `20` — kèm thêm `timeout=300` trong `LLM()` config để tránh Ollama ngắt kết nối giữa chừng.
   - **Syntax đúng cho LiteLLM** (CrewAI dùng LiteLLM làm bridge):
     ```python
     LLM(
         model="ollama/deepseek-coder-v2:16b-lite-instruct-q4_K_M",
         base_url="http://localhost:11434",
         temperature=0.2,
         timeout=300,
         extra_body={"options": {"num_ctx": 16000}},
     )
     ```
   - Để timeout thấp → Ollama đang "rặn" file khó → bị cắt ngang → `Empty Response` → hỏng cả chuỗi task.
   - **Coder `system_prompt` thêm rule chống Logic Loop**: *"Nếu bạn thử sửa một lỗi quá 3 lần không thành công, hãy dừng và báo cáo vấn đề cụ thể thay vì tiếp tục thử lại."*
9. Thêm `memory=False` explicit cho tất cả agents
10. Thêm `max_rpm=8` cho tất cả agents

---

### Phase 3 — Rút gọn Prompt + Context Chain (tasks.py)

11. Tách path rule ra 1 hằng `_PATH_NOTE`, chỉ inject vào `t1`. Các task còn lại bỏ lặp.
12. Rút ngắn từng task description xuống ≤10 bullet ngắn gọn.
13. **`t4` Coder**: thêm ví dụ tool call dạng JSON (DeepSeek handle JSON format tốt hơn free-text). Thêm rule: *"Nếu file > 300 dòng, tách module — đừng nhồi code vào 1 file."*
14. **Giảm context chain**:
    - `t4.context = [t3]` — t3 đã tổng hợp đủ từ t1/t2
    - `t7.context = [t4, t5, t6]` — bỏ t3

---

### Phase 4 — Python-side QC Branch + Guard + Retry (main.py + tasks.py)

15. Tạo `workspace/tests/` lúc startup (bên cạnh `src/` và `reports/`)
16. **IS_FRONTEND detection** trong main.py:
    ```
    IS_FRONTEND = bool(re.search(r'html|css|js|frontend|google sheets api', USER_REQUEST, re.I))
    ```
    Pass flag này vào `create_dev_team_tasks()` → `t5` description phân nhánh:
    - `IS_FRONTEND=True` → QC validate HTML structure, kiểm tra JS syntax (eslint/node --check), không chạy pytest
    - `IS_FRONTEND=False` → chạy pytest như cũ

17. **t2 Guard**: sau khi crew chạy xong t2, đọc `t2_plan_review.md` — nếu có "REQUEST CHANGES" → inject feedback vào `t1.description` và re-run chỉ t1+t2 (inner retry, max 2 vòng).
    - Dùng separator rõ ràng để agent không lú giữa yêu cầu cũ và feedback:
      ```python
      t1.description = f"{original_t1_desc}\n\n### FEEDBACK TỪ REVIEWER:\n{plan_feedback}"
      ```
    - **Bắt buộc re-instantiate `Task`** (không dùng lại object cũ) — CrewAI giữ state nội bộ trong Task instance, tái dùng sẽ mang trạng thái từ iteration trước vào.
18. `MAX_CYCLES = 1` → `3`, lưu/load `state.json` đúng chuẩn sau mỗi cycle

---

### Relevant files

- main.py — xóa dual write, thêm `tests/` mkdir, IS_FRONTEND detect, t2 guard, fix retry
- agents.py — `allow_delegation`, `max_iter`, `memory`, `max_rpm`, `system_prompt`
- tasks.py — xóa `output_file`, rút gọn prompt, context chain, IS_FRONTEND branch t5
- tools.py — truncate return value của `safe_file_write`

---

### Verification

1. Chạy `python main.py` → không còn log `[auto-tool] Write File →` nào
2. Check `workspace/reports/t1_task_plan.md` tồn tại → nội dung là markdown thuần (không phải Thought/Action rác)
3. Test với `USER_REQUEST` nhỏ → confirm t2 guard kick in nếu report có "REQUEST CHANGES"
4. Check log: `safe_file_write` chỉ return `"SUCCESS: src/..."` (không phải full content)
5. `IS_FRONTEND=True` → t5 không gọi pytest

---

### Decisions cập nhật từ review

- **QC branch**: Python-side detection, không để agent tự quyết — ổn định hơn nhiều
- **`safe_file_write` return**: Đây là bug ẩn nghiêm trọng, phải fix ở tools.py trước Phase khác
- **Ollama timeout**: Thêm `timeout=300` vào `LLM()`, không phải `max_rpm` một mình có thể giải quyết