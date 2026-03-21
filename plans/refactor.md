## Plan: Refactor Dev Team Agents (Revised)

**TL;DR** — 4 phase, 17 bước cụ thể. Cập nhật lần cuối: (a) buộc agent dùng tool bằng `system_prompt`, (b) truncate return value của `safe_file_write` + thêm console log bytes, (c) QC branch do Python quyết định, (d) LiteLLM timeout syntax đúng, (e) t2 guard dùng separator + re-instantiate Task.

---

### ~~Phase 1 — Dọn cơ chế ghi file~~ ✅ DONE

1. ~~Xóa `output_file=` khỏi toàn bộ 7 tasks trong tasks.py~~
2. ~~Xóa `_flush_write_calls()`, `_extract_json_objects()`, `_normalize_path()` khỏi main.py~~
3. ~~Xóa `_task_callback()`, `_step_callback()` khỏi main.py và bỏ params đó trong `Crew()`~~
4. ~~**`safe_file_write` return value** → chỉ trả `"SUCCESS: {relative_path}"` + `print("[TOOL] Writing N bytes...")`~~
5. ~~Thêm enforcement rule vào **backstory** của PM và Coder (CrewAI 1.10.1 không có `system_prompt` field)~~

---

### ~~Phase 2 — Fix Agent Config~~ ✅ DONE

6. ~~**PM**: `allow_delegation=True` → `False`~~
7. ~~**PM**: `max_iter=8` → `10`~~
8. ~~**Coder**: `max_iter=12` → `20`, `timeout=300` trong `LLM()`~~
9. ~~`memory=False` cho tất cả agents~~
10. ~~`max_rpm=8` cho tất cả agents~~

---

### ~~Phase 3 — Rút gọn Prompt + Context Chain~~ ✅ DONE

11. ~~`_PATH_NOTE` constant, inject vào t1 một lần~~
12. ~~Rút ngắn task descriptions~~
13. ~~t4: thêm rule tách module >300 dòng, `t4.context = [t3]`~~
14. ~~`t7.context = [t4, t5, t6]`, IS_FRONTEND branch t5, `import re` + detect trong main.py~~

---

### ~~Phase 4 — Python-side QC Branch + Guard + Retry~~ ✅ DONE

15. ~~Tạo `workspace/tests/` lúc startup~~
16. ~~`IS_FRONTEND` detection + pass vào `create_dev_team_tasks()` + t5 branch~~
17. ~~t2 Guard: `_run_t1_t2_with_guard()` — re-instantiate Task, separator `### FEEDBACK TỪ REVIEWER:`, inner retry max 2~~
18. ~~`MAX_CYCLES = 3`, `_save_state()` sau mỗi cycle~~

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