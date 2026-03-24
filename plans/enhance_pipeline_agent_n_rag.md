## Plan: Nâng cấp `create_dev_team_tasks` pipeline + RAG

**Scope:** Chỉ đụng đến `create_dev_team_tasks` (t1–t7) trong `tasks.py` và các hàm điều phối dev team trong `main.py` (`_run_dev_pipeline`, `_run_single_task`, `_run_t1_t2_with_guard`, `_extract_and_write_src`). Không thay đổi `create_quant_tasks`, `create_cafef_news_tasks`, hay bất kỳ workflow nào khác.

**Quyết định đã chốt:**
- QC FAIL → regenerate **toàn bộ source** (không chỉ file lỗi)
- Mức QC: **Syntax + lint + test đầy đủ** (không chỉ static prompt)
- RAG mode: **agent tự gọi search tool** khi cần (không auto-inject)
- Lộ trình: **MVP 2 phase** — chạy được sớm, không over-engineer

---

## Phase 1 — Nền + Kiểm chứng thật

### Step 1A · QC Feedback Loop (retry t4→t5→t6)

**Mục tiêu:** Khi t5 QC báo FAIL, tự động feed lý do lỗi ngược lại t4 Coder và chạy lại, tối đa `MAX_QC_RETRIES` lần.

**Thay đổi:**

`main.py` — hàm `_run_dev_pipeline` (line 204):
- Bọc cụm t4 → t5 → t6 trong vòng `for qc_attempt in range(1, MAX_QC_RETRIES + 1)`.
- Nếu t5 output chứa `"FAIL"` (case-insensitive): trích lý do fail, nối vào description của t4 ở lần sau.
- **Lần retry cuối (`qc_attempt == MAX_QC_RETRIES`)**: chuyển description t4 sang **Simple Mode** — yêu cầu Coder viết bản tối giản nhất có thể để pass (không feature phức tạp, không abstraction thừa, ưu tiên đúng hơn đẹp).
- Khi pass hoặc hết lần retry: tiếp tục sang t7 bình thường.
- Thêm constant ở đầu file: `MAX_QC_RETRIES = 3`.

`tasks.py` — task t4 trong `create_dev_team_tasks` (line ~88):
- Thêm placeholder `{qc_feedback}` vào description t4:
  ```
  [QC FEEDBACK — LẦN THỬ TRƯỚC]:
  {qc_feedback}
  Phân tích lỗi trên và viết lại TOÀN BỘ source code đã sửa.
  ```
- Khi không có feedback (lần đầu), placeholder được thay bằng chuỗi rỗng.

`workspace/state.json` — mở rộng schema:
```json
{
  "cycle": 1,
  "last_result": "...",
  "qc_history": [
    {"attempt": 1, "verdict": "FAIL", "reason": "..."},
    {"attempt": 2, "verdict": "PASS", "reason": ""}
  ]
}
```

---

### Step 1B · Real Execution Checks trong t5 QC

**Mục tiêu:** t5 QC không chỉ nhận xét bằng prompt mà phải thực thi kiểm tra thật và trả bằng chứng cụ thể.

**Thay đổi:**

`tools.py` — thêm `ExecutionCheckerTool`:
```python
class ExecutionCheckerTool(BaseTool):
    name = "Run Checks"
    description = "Chạy lint/syntax/test trên src/ và trả kết quả."

    def _run(self, command: str) -> str:
        result = subprocess.run(
            command, shell=True,
            cwd=str(WORKSPACE_ROOT),
            capture_output=True, timeout=30
        )
        return (result.stdout + result.stderr).decode()
```
- Chỉ cho phép whitelist command prefix: `eslint`, `stylelint`, `node --check`, `pytest`, `pylint`.
- **Chặn shell injection**: reject ngay nếu command chứa `;`, `&&`, `||`, `|`, `` ` ``, `$(` — raise `ValueError` trước khi gọi `subprocess`.
- `cwd` **luôn bị khóa cứng** vào `WORKSPACE_ROOT`, không nhận từ input của agent.
- Validate bằng `shlex.split()` và kiểm tra token đầu tiên nằm trong whitelist trước khi chạy.
- **Wildcard expansion**: `shlex.split()` không tự bung `*`. Sau khi validate whitelist, dùng `glob.glob()` để expand các token chứa `*` thành danh sách file thực trước khi truyền vào `subprocess.run(args, shell=False)` — tránh dùng `shell=True` để không mở lại lỗ hổng injection.

`agents.py` — QC agent (dev team):
- Thêm `ExecutionCheckerTool` vào `tools=[...]` của QC agent.
- Cập nhật `backstory`: bắt buộc chạy tool trước khi kết luận PASS/FAIL.

`tasks.py` — task t5 trong `create_dev_team_tasks`, nhánh `is_frontend=True` (line ~120):
```
Trình tự bắt buộc:
1. Dùng "Run Checks" với lệnh: node --check src/js/*.js
2. Dùng "Run Checks" với lệnh: eslint src/js/ --format compact (nếu có eslint)
3. Dùng Read File đọc các file src/ và kiểm tra HTML/CSS thủ công.
4. Chỉ kết luận PASS khi bước 1+2 không có lỗi.
Xuất báo cáo: PASS hoặc FAIL, kèm output thực tế từ tool.
```
Nhánh `is_frontend=False` (pytest backend): giữ nguyên logic hiện tại, chỉ thêm yêu cầu paste output thực tế.

---

### Step 1C · RAG — Codebase Search Tool

**Mục tiêu:** Thay `src_preview` cắt cứng bằng semantic search, agent tự gọi khi cần ngữ cảnh về codebase.

**Thay đổi:**

`tools.py` — thêm `CodebaseSearchTool` (dùng `chromadb` đã có trong `requirements.txt`):
```python
class CodebaseSearchTool(BaseTool):
    name = "Search Codebase"
    description = "Tìm kiếm semantic trong src/. Dùng khi cần hiểu cấu trúc, tìm hàm, hoặc xác nhận logic."

    def _run(self, query: str) -> str:
        results = _rag_collection.query(query_texts=[query], n_results=3)
        # trả về các đoạn code liên quan nhất
```

`main.py`:
- Thêm hàm `_build_rag_index()`: sau khi `_extract_and_write_src` ghi xong file, build index từ toàn bộ `src/`.
- Gọi `_build_rag_index()` ngay sau dòng `written = _extract_and_write_src(t4_raw)` (line 253).
- Nếu chromadb lỗi: log cảnh báo và tiếp tục (không làm gãy pipeline).

`agents.py` — dev team agents có nhu cầu context codebase (QC, Reviewer, Architect):
- Thêm `CodebaseSearchTool` vào `tools=[...]`.

`tasks.py` — task t5 và t6 trong `create_dev_team_tasks`:
- Thêm hướng dẫn có điều kiện:
  - Nếu project < 20 file: *"Dùng Read File đọc trực tiếp từng file trong src/ — không cần Search Codebase."*
  - Nếu project ≥ 20 file: *"Dùng 'Search Codebase' để tra cứu hàm/biến cụ thể, sau đó dùng Read File để đọc toàn file nếu cần ngữ cảnh đầy đủ."*

`main.py` — trong `_build_rag_index()`:
- Đếm số file trong `src/` sau khi ghi xong.
- Nếu < 20 file: không build RAG index, set flag `_rag_enabled = False`.
- Nếu ≥ 20 file: **xóa (reset) collection ChromaDB trước** (`client.delete_collection("codebase")` rồi `get_or_create_collection`) để đảm bảo agent luôn search trên phiên bản code mới nhất, không bị nhiễu bởi index từ cycle trước — sau đó build index bình thường.
- Truyền flag này vào description t5/t6 qua template để agent biết nên dùng mode nào.

---

## Phase 2 — Per-File Code Generation

### Step 2A · Parse file inventory từ t3 và generate theo từng file

**Mục tiêu:** Thay vì t4 viết toàn bộ codebase trong 1 lần gọi (dễ bị cắt output), tách thành N lần gọi, mỗi lần 1 file theo danh sách kiến trúc.

**Phụ thuộc:** Phase 1 phải hoàn thành và ổn định trước.

**Thay đổi:**

`tasks.py` — task t3 trong `create_dev_team_tasks`:
- **Ép Architect output danh sách file dưới dạng JSON block** thay vì markdown tự do:
  ````
  Sau phần mô tả kiến trúc, xuất danh sách file theo đúng format sau:
  ```json
  [
    {"name": "src/config.js", "description": "Cấu hình API keys và constants"},
    {"name": "src/app.js",    "description": "Entry point, khởi tạo app"}
  ]
  ```
  Không thêm text ngoài JSON block này.
  ````

`main.py` — thêm hàm `_parse_file_inventory(t3_output: str) -> list[dict]`:
- Extract JSON block bằng regex ```` ```json\n(.*?)``` ```` (non-greedy, DOTALL).
- Parse bằng `json.loads()` — không dùng regex cầu may trên toàn bộ text.
- **Fallback 3 lớp** nếu `json.loads()` lỗi:
  1. Thử `ast.literal_eval()` — xử lý được JSON không chuẩn (trailing comma, single quote).
  2. Gọi một model rẻ (VD: `gemini-flash` qua `utils.get_llm()`) với prompt ngắn: *"Sửa JSON sau cho hợp lệ, chỉ trả về JSON thuần: `{raw_block}`"* — parse lại kết quả.
  3. Nếu vẫn lỗi: log warning, trả về `[]` để description t4 gốc được dùng thay thế (graceful degradation).

`main.py` — trong `_run_dev_pipeline`, thay 1 lần gọi t4 bằng:
```python
file_inventory = _parse_file_inventory(t3)
t4_outputs = []
for file_spec in file_inventory:
    t4_single = _run_single_task(
        coder,
        f"[KIẾN TRÚC]\n{t3[:2000]}\n\n"
        f"Viết DUY NHẤT file này:\n"
        f"Tên: {file_spec['name']}\n"
        f"Mục đích: {file_spec['description']}\n\n"
        f"Output: ### FILE: {file_spec['name']}\n[nội dung đầy đủ]",
        "Output dạng ### FILE: path với nội dung đầy đủ.",
        f"reports/t4_{sanitize(file_spec['name'])}.md"
    )
    t4_outputs.append(t4_single)
t4_raw = "\n\n".join(t4_outputs)
```

`tasks.py` — task t4 trong `create_dev_team_tasks`:
- Description gốc vẫn giữ làm fallback khi `file_inventory` rỗng hoặc parse thất bại.
- Khi per-file mode, task description được override động từ `main.py` như trên; không thay đổi hàm `create_dev_team_tasks` signature.

---

### Step 2B · Dependency ordering và report tổng hợp

**Mục tiêu:** Đảm bảo file config/utils được generate trước file phụ thuộc vào chúng, và báo cáo phản ánh đúng từng file.

**Thay đổi:**

`main.py` — trong `_parse_file_inventory`:
- Sắp xếp file theo thứ tự ưu tiên: config → utils → services → UI entry points.
- Heuristic đơn giản: file có tên `config`, `utils`, `constants` lên đầu.

`workspace/reports/t4_code_summary.md` và `t5_qc_report.md`:
- Khi per-file mode: t4 report là index tổng hợp link đến từng `t4_<file>.md`.
- t5 report giữ nguyên format PASS/FAIL nhưng thêm section per-file nếu chạy lint từng file.

---

## Relevant files (chỉ dev team scope)

| File | Hàm/Section bị thay đổi |
|------|--------------------------|
| `tasks.py` | `create_dev_team_tasks` — description t4, t5, t6 |
| `main.py` | `_run_dev_pipeline`, `_run_single_task`, `_extract_and_write_src`, `_run_t1_t2_with_guard`, thêm `_build_rag_index`, `_parse_file_inventory` |
| `agents.py` | Dev team agents: QC, Reviewer, Architect — thêm tools |
| `tools.py` | Thêm `ExecutionCheckerTool`, `CodebaseSearchTool` |
| `workspace/state.json` | Mở rộng schema thêm `qc_history` |

**Không thay đổi:** `create_quant_tasks`, `create_cafef_news_tasks`, quant agents, cafef agents.

---

## Verification

1. Chạy 1 cycle với project cố tình có lỗi JS syntax → xác nhận t5 báo FAIL, t4 retry, `qc_history` trong `state.json` ghi đúng attempt + reason.
2. Lần retry t4 phải có phần `[QC FEEDBACK]` trong description → xác nhận bằng log `_run_single_task`.
3. Output t5 phải chứa stdout thực tế từ `node --check` hoặc `eslint`, không chỉ nhận định văn xuôi.
4. Gọi `Search Codebase` từ QC agent phải trả về đoạn code liên quan (không phải toàn bộ file).
5. Khi chromadb lỗi: pipeline không crash, log warning, t5/t6 fallback về Read File thông thường.
6. Sau Phase 2: số file ghi vào `src/` phải khớp với số file trong `_parse_file_inventory`, không thiếu, không cắt cụt.