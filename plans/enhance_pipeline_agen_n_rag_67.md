## Phân tích Root Cause

**3 bug cụ thể, không phải 1:**

| Bug | Vị trí gây ra | Root Cause |
|-----|--------------|------------|
| `index.html` ở root lẫn `src/` | `_normalize_path()` | Chỉ strip workspace prefix, không force `src/` → `### FILE: index.html` → ghi vào index.html |
| CSS path mismatch (styles/main.css vs css/styles.css) | t3 Architect | Không biết src/ đang có gì → design path mới không khớp HTML cũ |
| MIME error | Trình duyệt | Fetch `src/styles/main.css`, server trả 200 nhưng là HTML (fallback) → nosniff block |

src hiện có: `index.html`, `js/` (11 files), `css/`, `api/`, `config.js`, `modules/`, `services/`, `ui/`, `utils/`. Architect hoàn toàn mù với điều này.

---

## Plan: Sprint 6 — Investigation-First Pipeline (~90 phút)

**Mục tiêu:** Architect không bao giờ thiết kế "trắng tay" — luôn biết codebase đang có gì trước khi vẽ kiến trúc. Delta design thay vì Full-rewrite design.

**Thay đổi luồng:**
```
Trước: t1(PM) → t2(Review) → t3(Architect fresh) → t4 → t5 → t6 → t7
Sau:   t1(PM) → t2(Review) → tI(Investigation) → t3(Architect delta) → t4 → t5 → t6 → t7
```

| # | Status | ~Thời gian | Việc cần làm | File |
|---|--------|-----------|-------------|------|
| S6-0 | ✅ DONE | 10p | **Quick path fix** (`_normalize_path` trong main.py): `os.path.normpath` + `_SRC_EXTS` frozenset. Bare filename có ext web → force `src/`. File khác (Dockerfile, package.json) giữ nguyên. | main.py |
| S6-1 | ✅ DONE | 15p | **`_build_codebase_snapshot()`**: Scan `src/` — file tree + dependency map. Extract dòng chứa `<link`, `<script`, `import`, `export`, `require` (max 40/file). Ưu tiên 8 file: index.html trước, rồi config*, package.json. | main.py |
| S6-2 | ✅ DONE | 30p | **tI Investigation task**: `_run_single_task` với Architect agent — đọc src/, audit index.html, phát hiện mismatch, xuất Current State Report. Output: `reports/t0_codebase_audit.md`. Log: `[investigation] Current State: N file(s) found in src/`. | main.py |
| S6-3 | ✅ DONE | 20p | **t3 prompt — delta constraint**: Inject `[HIỆN TRẠNG CODEBASE]` + audit_summary vào đầu t3 prompt. 2 ràng buộc cứng: ① path phải khớp file thực ② giải thích TẠI SAO khi sửa file cũ. | main.py |
| S6-4 | ✅ DONE | 15p | **t4 per-file — modify mode**: `existing_src_files` set từ snapshot. Khi `fname in existing_src_files` → inject note giữ nguyên CSS class/JS function names, chỉ sửa phần liên quan. | main.py |

**Done khi:**
- ✅ Log `[investigation] Current State: N file(s) found in src/` xuất hiện trước t3
- ✅ t3 nhận `[HIỆN TRẠNG CODEBASE]` + 2 ràng buộc delta
- ✅ Không còn `index.html` nằm ngoài `src/` (S6-0 fix)

---

## Plan: Sprint 7 — Telegram Dashboard (~150 phút, Full scope)

**Mục tiêu:** One-message live dashboard — `/dev <yêu cầu>` → 1 tin nhắn tự edit real-time → `/push` để commit GitHub.

**File mới:** `bot.py`. **Phụ thuộc:** `python-telegram-bot>=20.0` (thêm vào requirements.txt).

**Technical constraints:**
- CrewAI chạy blocking synchronous → phải chạy trong `threading.Thread`
- Telegram bot dùng asyncio event loop
- Callback từ thread → asyncio: dùng `asyncio.run_coroutine_threadsafe(coro, main_loop)`
- Rate limit edit: Telegram 429 nếu edit quá nhanh — **chỉ edit khi chuyển Task hoặc sau mỗi 5 file**

| # | Status | ~Thời gian | Việc cần làm | File |
|---|--------|-----------|-------------|------|
| S7-1 | ✅ DONE | 20p | **Bot setup + security**: `ALLOWED_USER_IDS` từ `TG_USER_IDS` env. `/dev` handler: validate user ID, check `_pipeline_running` flag, reject nếu busy. | `bot.py` |
| S7-2 | ✅ DONE | 25p | **Thread bridge**: `threading.Thread(target=_run_pipeline, daemon=True)`. Store `_main_loop` trước `app.run_polling()`. Callback dùng `asyncio.run_coroutine_threadsafe(_do_edit(...), _main_loop)`. | `bot.py`, main.py |
| S7-3 | ✅ DONE | 25p | **Status formatter + rate-limit**: Emoji `⏳/🔄/✅/❌`. Hiện `(N/M)` file và QC attempt. `_throttled_edit()`: chỉ trigger khi task transition hoặc `file_counter % 5 == 0`. 429: `RetryAfter` → sleep + retry 1x → skip. | `bot.py` |
| S7-4 | ✅ DONE | 20p | **Callback injection**: `progress_callback: Callable[[str, str], None] \| None = None` vào `_run_dev_pipeline`. `_cb()` wrapper. Gọi tại: tI/t3/t4/t5/t6/t7 start+done, mỗi file trong per-file loop, QC FAIL. | main.py |
| S7-5 | ✅ DONE | 20p | **QC report summary**: `qc_fail` event → `_send_message()` tin riêng với 500 ký tự đầu t5. `done` event → `"✅ Xong! N file. /push để commit GitHub."` | `bot.py` |
| S7-6 | ✅ DONE | 20p | **/status**: Đọc `state.json`. **/cancel**: `cancel_event.set()` + cleanup container. **/push**: pre-check `git config` → env fallback `GIT_AUTHOR_NAME/EMAIL` → inject `-c` flag → fail-fast thay vì treo. `shell=False`. Whitelist: `main`, `master`, `dev/*`. | `bot.py` |

**Done khi:**
- ✅ `/dev <yêu cầu>` → 1 tin nhắn, tự update khi chuyển task, throttle 5 file/edit
- ✅ QC FAIL → tin nhắn riêng báo attempt + snippet
- ✅ `/push` không treo dù chưa set git config

---

## Thứ tự implement

1. **S6-0** — fix path bug ngay ✅ DONE
2. **Sprint 6** đầy đủ — investigation pipeline ✅ DONE
3. **Sprint 7** — Telegram dashboard ✅ DONE