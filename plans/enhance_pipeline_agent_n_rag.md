## Plan: Nâng cấp `create_dev_team_tasks` pipeline + RAG

**Scope:** Chỉ đụng đến `create_dev_team_tasks` (t1–t7) trong `tasks.py` và các hàm điều phối dev team trong `main.py` (`_run_dev_pipeline`, `_run_single_task`, `_run_t1_t2_with_guard`, `_extract_and_write_src`). Không thay đổi `create_quant_tasks`, `create_cafef_news_tasks`, hay bất kỳ workflow nào khác.

**Quyết định đã chốt:**
- QC FAIL → regenerate **toàn bộ source** (không chỉ file lỗi)
- Mức QC: **Syntax + lint + test đầy đủ** (không chỉ static prompt)
- RAG mode: **agent tự gọi search tool** khi cần (không auto-inject)
- Lộ trình: **MVP 2 phase** — chạy được sớm, không over-engineer

---

## Phase 1 — Nền + Kiểm chứng thật ✅ DONE

### Step 1A · QC Feedback Loop (retry t4→t5→t6) ✅ DONE

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

### Step 1B · Real Execution Checks trong t5 QC ✅ DONE

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

### Step 1C · RAG — Codebase Search Tool ✅ DONE

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

## Phase 2 — Per-File Code Generation ✅ DONE

### Step 2A · Parse file inventory từ t3 và generate theo từng file ✅ DONE

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

### Step 2B · Dependency ordering và report tổng hợp ✅ DONE

**Mục tiêu:** Đảm bảo file config/utils được generate trước file phụ thuộc vào chúng, và báo cáo phản ánh đúng từng file.

**Thay đổi:**

`main.py` — trong `_parse_file_inventory`:
- Sắp xếp file theo thứ tự ưu tiên: config → utils → services → UI entry points.
- Heuristic đơn giản: file có tên `config`, `utils`, `constants` lên đầu.

`workspace/reports/t4_code_summary.md` và `t5_qc_report.md`:
- Khi per-file mode: t4 report là index tổng hợp link đến từng `t4_<file>.md`.
- t5 report giữ nguyên format PASS/FAIL nhưng thêm section per-file nếu chạy lint từng file.

---

## Bugs Discovered (Post Run #1)

| # | Triệu chứng | Root Cause | Fix | Sprint |
|---|-------------|-----------|-----|--------|
| B1 | QC FAIL 3/3 lần dù `node --check` PASS | `eslint` chưa cài → `ExecutionCheckerTool` trả `[ERROR] Command not found` → QC agent đọc → đánh FAIL Coder oan | Thêm token `[TOOL_NOT_INSTALLED]` cho loại lỗi này; QC task rule: skip ≠ FAIL | Sprint 4 B1 |
| B2 | Comment bị cắt cụt (`đã đượ`); global variable | Architect không output JSON block → `_parse_file_inventory` trả `[]` → fallback single-shot 18 file → vượt token limit → LLM truncate | Mạnh hơn t3 prompt + debug log xác nhận inventory; ES6 constraint cho Coder | Sprint 4 B2+B3 |
| B3 | Global var `gapi`, `API_KEY` trên `window` | Không có ES6 constraint trong per-file Coder prompt; LLM dùng pattern cũ | Thêm `"ES6 Modules, no global var"` vào Coder prompt | Sprint 4 B3 |

---

## Phase 3 — Docker Execution Environment

**Mục tiêu:** Chuyển toàn bộ execution (lint, syntax check, test) sang Docker container — đảm bảo tính nhất quán 100%, không phụ thuộc môi trường host. Chính Agent (Architect + Coder) tự tạo `Dockerfile.checker` phù hợp với tech stack của từng project.

---

### Step 3A · Dockerfile.checker — Infrastructure as Code by Agent

**Ý tưởng cốt lõi:** Architect biết tech stack → biết cần tool gì → tự đưa `Dockerfile.checker` vào JSON file inventory. Coder viết nội dung. Pipeline tự build image và quản lý container.

`tasks.py` — t3 Architect description (bổ sung):
```
Dựa trên tech stack đã thiết kế, thêm MỘT ENTRY vào JSON inventory cho file "Dockerfile.checker":
{"name": "Dockerfile.checker", "description": "Docker QC environment — cài đủ runtime và linter phù hợp tech stack"}
```

`tasks.py` — t4 Coder per-file: treat `Dockerfile.checker` như bất kỳ file nào — Coder viết nội dung Dockerfile đầy đủ phù hợp tech stack đã thiết kế.

**Fallback `_FALLBACK_DOCKERFILE`** (hardcoded trong `main.py`, dùng khi Architect không include hoặc `docker build` thất bại):
```dockerfile
FROM python:3.11-slim

# Node.js 20
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && apt-get clean

# JS tools
RUN npm install -g eslint stylelint prettier

# Python tools
RUN pip install --no-cache-dir pytest pylint

# Non-root user
RUN useradd -m checker
USER checker
WORKDIR /workspace
```

---

### Step 3B · DockerCheckerManager trong `main.py`

**Module-level state** (trong `tools.py` để `ExecutionCheckerTool` truy cập):
```python
checker_container: str | None = None  # set bởi _run_dev_pipeline sau docker run
```

**3 hàm mới trong `main.py`:**
```python
_FALLBACK_DOCKERFILE = "..."  # string constant ở đầu file

def _ensure_checker_image(cycle: int) -> str:
    """Build Docker image. Tự write fallback Dockerfile nếu Architect không tạo."""
    tag = f"project-checker:{cycle}"
    dockerfile = WORKSPACE_ROOT / "Dockerfile.checker"
    if not dockerfile.exists():
        dockerfile.write_text(_FALLBACK_DOCKERFILE)
        _log("[docker] Dockerfile.checker missing — wrote hardcoded fallback")
    result = subprocess.run(
        ["docker", "build", "-t", tag, "-f", str(dockerfile), str(WORKSPACE_ROOT)],
        capture_output=True, timeout=180
    )
    if result.returncode != 0:
        _log(f"[docker] Build failed: {result.stderr.decode()[:500]}")
        return ""  # caller sẽ skip Docker, fallback về local exec
    _log(f"[docker] Image built: {tag}")
    return tag

def _start_checker_container(image_tag: str, cycle: int) -> str:
    """docker run -d với volume mount. Returns container name hoặc '' nếu lỗi."""
    name = f"dev-checker-{cycle}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # xóa stale
    result = subprocess.run([
        "docker", "run", "-d", "--name", name,
        "-v", f"{WORKSPACE_ROOT}:/workspace",
        "-w", "/workspace",
        image_tag, "tail", "-f", "/dev/null"
    ], capture_output=True, timeout=30)
    if result.returncode != 0:
        _log(f"[docker] Container start failed: {result.stderr.decode()[:200]}")
        return ""
    _log(f"[docker] Container started: {name}")
    return name

def _stop_checker_container(name: str) -> None:
    subprocess.run(["docker", "stop", name], capture_output=True, timeout=15)
    subprocess.run(["docker", "rm", name], capture_output=True, timeout=10)
    _log(f"[docker] Container stopped and removed: {name}")
```

**Tích hợp vào `_run_dev_pipeline`** — bọc toàn bộ trong `try/finally`:
```python
container_name = ""
try:
    ...  # t3, t4 ghi file như bình thường
    # Ngay sau t4 ghi file xong, trước vòng QC:
    image_tag = _ensure_checker_image(cycle)
    if image_tag:
        container_name = _start_checker_container(image_tag, cycle)
        _tools_mod.checker_container = container_name
    # t5, t6, t7 tiếp tục — ExecutionCheckerTool tự detect container
    ...
finally:
    if container_name:
        _stop_checker_container(container_name)
    _tools_mod.checker_container = None  # reset state cho cycle tiếp theo
```

---

### Step 3C · Update `ExecutionCheckerTool` — docker exec mode

```python
# tools.py — module-level
checker_container: str | None = None  # set bởi _run_dev_pipeline

class ExecutionCheckerTool(BaseTool):
    def _run(self, command: str) -> str:
        # ... existing whitelist + injection checks ...
        # ... existing glob expansion → expanded list ...

        # Chọn executor: docker exec ưu tiên, local làm fallback
        if checker_container:
            cmd = ["docker", "exec", checker_container] + expanded
        else:
            cmd = expanded  # local execution

        try:
            proc = subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), capture_output=True, timeout=30)
            out = proc.stdout.decode("utf-8", errors="replace")
            err = proc.stderr.decode("utf-8", errors="replace")
            if proc.returncode == 127:  # shell "command not found" exit code
                return f"[TOOL_NOT_INSTALLED] '{expanded[0]}' not found in container."
            return (out + err).strip() or f"(exit code {proc.returncode}, no output)"
        except FileNotFoundError:
            return f"[TOOL_NOT_INSTALLED] '{expanded[0]}' not installed on host."
        except subprocess.TimeoutExpired:
            return "[ERROR] Command timed out after 30 seconds."
```

**QC task t5 — rule bổ sung:**
```
QUAN TRỌNG — Nếu output của tool bắt đầu bằng [TOOL_NOT_INSTALLED]:
→ Ghi chú vào báo cáo: "<tool>: not installed"
→ BỎ QUA check đó
→ KHÔNG tính là FAIL
Chỉ tính FAIL khi tool chạy ĐƯỢC và output thực sự chứa lỗi.
```

---

### Step 3D · Startup & Cleanup Strategy

| Scenario | Hành vi |
|----------|---------|
| Docker không cài trên host | `FileNotFoundError` khi gọi `docker build` → log warning → local exec fallback |
| `docker build` thất bại | Log stderr, `_ensure_checker_image` trả `""` → container không start → local exec |
| Container crash giữa chừng | `docker exec` fail → `ExecutionCheckerTool` catch → local exec + log warning |
| Pipeline crash (Python exception) | `try/finally` trong `_run_dev_pipeline` đảm bảo `_stop_checker_container` luôn được gọi |
| Stale container từ run trước | `docker rm -f {name}` trước `docker run` — tự dọn dẹp không cần thủ công |

**Security notes:**
- Mọi lệnh vào container vẫn qua whitelist + injection check trước khi `docker exec`
- Volume mount `WORKSPACE_ROOT:/workspace` — container không thể access filesystem ngoài workspace
- Dockerfile fallback dùng non-root user (`USER checker`) — container không chạy với root
- `cycle` dùng làm image tag → image cũ vẫn còn (không tự xóa) — cần `docker image prune` thủ công nếu disk tight

---

## Relevant files (chỉ dev team scope)

| File | Hàm/Section bị thay đổi |
|------|--------------------------|
| `tasks.py` | `create_dev_team_tasks` — description t3, t4, t5, t6 |
| `main.py` | `_run_dev_pipeline` (try/finally + Docker), `_run_single_task`, `_extract_and_write_src`, `_build_rag_index`, `_parse_file_inventory`, thêm `_ensure_checker_image`, `_start_checker_container`, `_stop_checker_container`, `_FALLBACK_DOCKERFILE` |
| `agents.py` | Dev team agents: QC, Reviewer, Architect — thêm tools |
| `tools.py` | `ExecutionCheckerTool` (docker exec mode + `[TOOL_NOT_INSTALLED]`), `CodebaseSearchTool`, thêm `checker_container` module var |
| `workspace/state.json` | Mở rộng schema thêm `qc_history` |
| `Dockerfile.checker` | File mới — Coder agent viết; fallback hardcoded trong `main.py` |

**Không thay đổi:** `create_quant_tasks`, `create_cafef_news_tasks`, quant agents, cafef agents.

---

## Roadmap Thực Thi

> Thứ tự Sprint = thứ tự phụ thuộc kỹ thuật: Sprint 1 (execution checks + retry loop) phải xong trước Sprint 2 (RAG), Sprint 2 phải xong trước Sprint 3 (per-file generation).

### Sprint 1 — Lắp "Mắt" và "Tay" · ~60 phút ✅ DONE
**Mục tiêu:** Agent tự chạy kiểm tra thật và tự sửa khi thất bại.

| # | Thời gian | Việc cần làm | File | Status |
|---|-----------|--------------|------|--------|
| 1 | 15p | Thêm `ExecutionCheckerTool` vào `tools.py`. Whitelist prefix: `node`, `eslint`, `stylelint`, `pytest`, `pylint`. Validate bằng `shlex.split()`, chặn `;`, `&&`, `\|`, `\|​\|`, `` ` ``, `$(`. Sau validate, dùng `glob.glob()` expand wildcard, gọi `subprocess.run(args, shell=False)`. | `tools.py` | ✅ |
| 2 | 15p | Sửa task t5 trong `create_dev_team_tasks` (nhánh `is_frontend=True`): thêm trình tự bắt buộc gọi `"Run Checks"` — `node --check`, `eslint` — trước khi kết luận PASS/FAIL. Thêm `ExecutionCheckerTool` vào `tools` của QC agent trong `agents.py`. | `tasks.py`, `agents.py` | ✅ |
| 3 | 30p | Trong `_run_dev_pipeline` (`main.py`): bọc cụm t4→t5→t6 trong `for qc_attempt in range(1, MAX_QC_RETRIES + 1)`. Nếu t5 chứa `"FAIL"`: trích lý do, nối vào description t4 lần sau. Lần retry cuối bật **Simple Mode** cho t4. Ghi lịch sử vào `qc_history` trong `state.json`. Thêm `MAX_QC_RETRIES = 3` ở đầu file. | `main.py` | ✅ |

**Done:** Hệ thống chạy thử code thực và tự sửa thay vì chỉ nhận xét văn xuôi.

---

### Sprint 2 — Nâng cấp "Bộ nhớ" · ~45 phút ✅ DONE
**Mục tiêu:** Agent không bị mất ngữ cảnh khi project lớn hơn 20 file.

> **Phụ thuộc:** Sprint 1 phải hoàn thành và ổn định.

| # | Thời gian | Việc cần làm | File | Status |
|---|-----------|--------------|------|--------|
| 1 | 15p | Thêm `_build_rag_index()` trong `main.py`. Đếm file trong `src/`: nếu < 20 → set `_rag_enabled = False`, skip; nếu ≥ 20 → **reset ChromaDB collection trước** (`delete_collection` rồi `get_or_create_collection`) rồi embed toàn bộ `src/`. Gọi ngay sau `_extract_and_write_src`. | `main.py` | ✅ |
| 2 | 15p | Thêm `CodebaseSearchTool` vào `tools.py` dùng `chromadb`, `n_results=3`. Thêm vào `tools` của QC, Reviewer, Architect trong `agents.py`. Nếu chromadb lỗi: log warning và trả về empty string, pipeline không crash. | `tools.py`, `agents.py` | ✅ |
| 3 | 15p | Sửa description t5, t6 trong `create_dev_team_tasks`: inject hướng dẫn có điều kiện — nếu `_rag_enabled=False` thì dùng Read File trực tiếp; nếu `True` thì dùng `"Search Codebase"` để tra cứu, Read File để đọc toàn file khi cần. | `tasks.py` | ✅ |

**Done:** Agent tra cứu code cũ trước khi viết code mới, không còn bị mâu thuẫn giữa các file.

---

### Sprint 3 — Chia nhỏ "Dây chuyền sản xuất" · ~75 phút ✅ DONE
**Mục tiêu:** Phá vỡ giới hạn output token, viết được app nhiều file không bị cắt cụt.

> **Phụ thuộc:** Sprint 1 + 2 phải hoàn thành và ổn định.

| # | Thời gian | Việc cần làm | File | Status |
|---|-----------|--------------|------|--------|
| 1 | 20p | Sửa task t3 trong `create_dev_team_tasks`: ép Architect output JSON block chuẩn ở cuối response (format `\`\`\`json [...]\`\`\``). Kèm ví dụ trong prompt để giảm ngáo format. | `tasks.py` | ✅ |
| 2 | 25p | Viết `_parse_file_inventory(t3_output)` trong `main.py`. Fallback 3 lớp: `json.loads()` → `ast.literal_eval()` → gọi Gemini Flash sửa JSON → trả `[]` nếu vẫn lỗi. Sắp xếp kết quả: `config/constants/utils` lên đầu, UI entry points xuống cuối. | `main.py` | ✅ |
| 3 | 30p | Sửa `_run_dev_pipeline`: nếu `file_inventory` không rỗng, thay 1 lần gọi t4 bằng vòng lặp per-file. Mỗi lần gọi `_run_single_task` với description chỉ đề cập 1 file. Nếu `file_inventory` rỗng: fallback về description t4 gốc (single-shot). Report t4 là index tổng hợp link đến từng `t4_<file>.md`. | `main.py` | ✅ |

**Done:** Hệ thống tạo ra hàng chục file hoàn chỉnh, mỗi file được Coder tập trung 100% — không còn đầu voi đuôi chuột.

---

### Sprint 4 — Docker Environment + Bug Fixes · ~90 phút ✅ DONE
**Mục tiêu:** Triệt tiêu FAIL oan do thiếu tool; chạy mọi check trong Docker container nhất quán; Architect tự thiết kế Dockerfile theo tech stack; fix các bug phát hiện từ Run #1.

> **Phụ thuộc:** Sprint 1 + 2 + 3 phải hoàn thành và ổn định.

| # | Thời gian | Việc cần làm | File | Status |
|---|-----------|--------------|------|--------|
| B1 | 10p | Fix `[TOOL_NOT_INSTALLED]`: sửa `ExecutionCheckerTool._run()` — `FileNotFoundError` (local) → trả `[TOOL_NOT_INSTALLED] '<cmd>'...`; exit code 127 (docker exec) → idem. Cập nhật t5 QC task: thêm rule "output `[TOOL_NOT_INSTALLED]` = bỏ qua, **không tính FAIL**." | `tools.py`, `tasks.py` | ✅ |
| B2 | 15p | Fix per-file trigger: thêm `_log(f"[inventory] raw block: {raw[:200]}")` trước `json.loads` trong `_parse_file_inventory`. Mạnh hơn t3 JSON instruction: thêm dòng `"⚠️ BẮT BUỘC: Kết thúc response bằng JSON block. Không thêm text nào sau JSON."` | `main.py`, `tasks.py` | ✅ |
| B3 | 15p | Fix ES6 — **2 điểm**: ① Sửa t3 Architect description: thêm `"Thiết kế kiến trúc theo chuẩn ES Modules hiện đại — mỗi file là một module độc lập với export/import rõ ràng."` (nếu Architect thiết kế đúng, Coder sẽ ít tự ý dùng `window.xxx`). ② Bổ sung vào per-file Coder prompt: `"Dùng ES6 Modules (export/import). Tuyệt đối KHÔNG khai báo biến global (window/global/var ở module scope). Mọi dependency dùng import."` | `tasks.py`, `main.py` | ✅ |
| 4 | 20p | Dockerfile IaC: sửa t3 prompt — yêu cầu Architect thêm `"Dockerfile.checker"` vào JSON inventory với description phù hợp tech stack. Viết `_FALLBACK_DOCKERFILE` constant trong `main.py`. | `tasks.py`, `main.py` | ✅ |
| 5 | 35p | DockerCheckerManager: thêm 3 hàm vào `main.py` — `_ensure_checker_image(cycle)`, `_start_checker_container(image_tag, cycle)`, `_stop_checker_container(name)`. Thêm `checker_container: str | None = None` vào `tools.py`. Bọc `_run_dev_pipeline` trong `try/finally`. Gọi build+run sau khi t4 ghi file xong, trước vòng QC. | `main.py`, `tools.py` | ✅ |
| 6 | 20p | Update `ExecutionCheckerTool`: nếu `checker_container` không rỗng → thay `cmd = expanded` bằng `cmd = ["docker", "exec", checker_container] + expanded`. Giữ local exec làm fallback. Exit code 127 → `[TOOL_NOT_INSTALLED]`. | `tools.py` | ✅ |

**Done khi:** Log hiện `[docker] Container started: dev-checker-N`; t5 chạy `eslint` trong container không còn `TOOL_NOT_INSTALLED`; log hiện `[inventory] file_inventory: N file(s) (per-file mode)` với N > 0.

---

## Verification

**Phase 1–2 (đã done):**
1. Chạy 1 cycle với project cố tình có lỗi JS syntax → xác nhận t5 báo FAIL, t4 retry, `qc_history` trong `state.json` ghi đúng attempt + reason.
2. Lần retry t4 phải có phần `[QC FEEDBACK]` trong description → xác nhận bằng log `_run_single_task`.
3. Output t5 phải chứa stdout thực tế từ `node --check` hoặc `eslint`, không chỉ nhận định văn xuôi.
4. Gọi `Search Codebase` từ QC agent phải trả về đoạn code liên quan (không phải toàn bộ file).
5. Khi chromadb lỗi: pipeline không crash, log warning, t5/t6 fallback về Read File thông thường.
6. Log hiện `[inventory] file_inventory: N file(s) (per-file mode)` với N > 0; số file `src/` khớp với N.

**Phase 3 — Docker (cần verify sau Sprint 4):**
7. Sau khi `_run_dev_pipeline` build xong t4: `docker ps` hiện container `dev-checker-{cycle}` đang chạy.
8. Sau khi pipeline kết thúc (dù PASS, FAIL, hay exception): `docker ps` không còn container đó.
9. t5 output chứa stdout `eslint` thực tế từ container — không còn `[TOOL_NOT_INSTALLED]`.
10. Tắt Docker trên host: pipeline vẫn chạy (log `[docker] Build failed` hoặc `FileNotFoundError`), local exec fallback, không crash.
11. Stale container từ run trước: `_start_checker_container` tự `docker rm -f` không cần thủ công.
12. Coder không dùng global variable: mọi JS file dùng `export`/`import`, không có `window.xxx = ...`.