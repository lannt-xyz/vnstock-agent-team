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

| # | ~Thời gian | Việc cần làm | File |
|---|-----------|-------------|------|
| S6-0 | 10p | **Quick path fix** (`_normalize_path` trong main.py): Dùng `os.path.normpath` để chuẩn hóa trước, sau đó check `startswith('src/')`. Nếu không → prepend `src/`. Tránh edge case `./index.html` → `src/./index.html`. Pattern: `p = os.path.normpath(cleaned); return p if p.startswith('src/') else 'src/' + p`. | main.py |
| S6-1 | 15p | **`_build_codebase_snapshot()`**: Scan `src/` — trả về file tree + **"bản đồ gene"** của project. Thay vì lấy 300 ký tự đầu, **extract có chọn lọc**: với mỗi file text, lấy tất cả dòng chứa `<link`, `<script`, `import`, `export`, `require` (tối đa 40 dòng/file) — đây là dependency map thực tế cho Architect biết file nào đang link với file nào. Ưu tiên ≤8 file: `index.html`, `*.css`, `config*`, `package.json`. | main.py |
| S6-2 | 30p | **tI Investigation task**: `_run_single_task` với Architect agent — dùng `safe_dir_read` + `safe_file_read` để: (1) liệt kê cấu trúc thư mục thực tế, (2) đọc `src/index.html` xác định CSS/JS đang được link, (3) phát hiện tech stack thực, (4) xuất **Current State Report**. Output: `reports/t0_codebase_audit.md`. | main.py |
| S6-3 | 20p | **t3 prompt — delta constraint**: Inject `[HIỆN TRẠNG CODEBASE]\n{snapshot}` + ref `reports/t0_codebase_audit.md`. Thêm **2 ràng buộc cứng**: ① *"CHỈ liệt kê vào `files` những file cần TẠO MỚI hoặc SỬA. Mọi `src/` path PHẢI khớp tên file thực từ Current State Report."* ② *"Nếu định thay đổi file đã tồn tại, BẮT BUỘC giải thích TẠI SAO trong phần mô tả — file đó đang có vấn đề gì."* (Ép Agent suy nghĩ trước khi phá cấu trúc cũ.) | main.py, tasks.py |
| S6-4 | 15p | **t4 per-file — modify mode**: Khi `fname` đã tồn tại trong snapshot: thêm *"File này đã tồn tại. Giữ nguyên CSS class names, JS function names. Chỉ thêm/sửa phần liên quan đến yêu cầu mới."* | main.py |

**Done khi:**
- Log `[investigation] Current State: N files found in src/` xuất hiện trước t3
- t3 không còn tạo `src/styles/main.css` khi HTML đang link `css/styles.css`
- Không còn `index.html` nằm ngoài `src/`

---

## Plan: Sprint 7 — Telegram Dashboard (~150 phút, Full scope)

**Mục tiêu:** One-message live dashboard — `/dev <yêu cầu>` → 1 tin nhắn tự edit real-time → `/push` để commit GitHub.

**File mới:** `bot.py`. **Phụ thuộc:** `python-telegram-bot>=20.0` (thêm vào requirements.txt).

**Technical constraints:**
- CrewAI chạy blocking synchronous → phải chạy trong `threading.Thread`
- Telegram bot dùng asyncio event loop
- Callback từ thread → asyncio: dùng `asyncio.run_coroutine_threadsafe(coro, main_loop)`
- Rate limit edit: Telegram 429 nếu edit quá nhanh — **chỉ edit khi chuyển Task hoặc sau mỗi 5 file**

| # | ~Thời gian | Việc cần làm | File |
|---|-----------|-------------|------|
| S7-1 | 20p | **Bot setup + security**: `ALLOWED_USER_IDS = set(getenv("TG_USER_IDS","").split(","))`. dev handler: validate user, check `_pipeline_running` flag, reject nếu busy. | `bot.py` |
| S7-2 | 25p | **Thread bridge**: `pipeline_thread = threading.Thread(target=..., daemon=True)`. Store `main_loop` trước `app.run_polling()`. Callback dùng `asyncio.run_coroutine_threadsafe(bot.edit_message_text(...), main_loop)`. | `bot.py`, main.py |
| S7-3 | 25p | **Status formatter + rate-limit**: Emoji `⏳/✅/❌` cho từng task. Hiện `(N/M)` file và QC attempt. `_throttled_edit()`: **chỉ trigger khi chuyển Task hoặc sau mỗi 5 file** — tránh 429. Khi nhận 429: backoff 5s + retry 1 lần, nếu vẫn fail → skip (không crash pipeline). | `bot.py` |
| S7-4 | 20p | **Callback injection**: Thêm `progress_callback: Callable[[str, str], None] \| None = None` vào `_run_dev_pipeline`. Gọi tại: chuyển task, mỗi 5 file trong per-file loop, QC FAIL. | main.py |
| S7-5 | 20p | **QC report summary**: Sau t5, extract verdict + 500 ký tự đầu → `bot.send_message()` (tin riêng, không edit — làm mốc lưu vết). Final: `"✅ Xong! {N} file. /push để commit GitHub."` | `bot.py`, main.py |
| S7-6 | 20p | **/status**: Đọc `state.json` → format trạng thái. **/cancel**: `cancel_event` (threading.Event) + cleanup container. **/push**: Pre-check `git config user.name/email`; nếu chưa set → đọc từ env `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, inject qua `-c` flag; thiếu → báo lỗi thay vì treo. `shell=False`. Whitelist branch: chỉ `main`, `master`, `dev/*`. | `bot.py` |

**Done khi:**
- `/dev <yêu cầu>` → 1 tin nhắn, tự update khi chuyển task
- QC FAIL → báo attempt
- `/push` không treo dù chưa set git config

---

## Thứ tự implement

1. **S6-0** — fix path bug ngay (10 phút, deploy được ngay)
2. **Sprint 6** đầy đủ — investigation pipeline
3. **Sprint 7** — Telegram dashboard