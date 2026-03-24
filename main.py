import ast
import os
import re
import json
import subprocess
import traceback
from datetime import datetime, timezone
from dotenv import load_dotenv
from crewai import Crew, Process

load_dotenv()

# Import WORKSPACE_ROOT trước để tạo thư mục đúng chỗ
from tools import WORKSPACE_ROOT, safe_file_write  # noqa: E402
import tools as _tools_mod  # noqa: E402  (used to set tools.rag_collection)

# Tạo các thư mục workspace cần thiết (có thể nằm ngoài project này)
(WORKSPACE_ROOT / "src").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "reports").mkdir(parents=True, exist_ok=True)
(WORKSPACE_ROOT / "tests").mkdir(parents=True, exist_ok=True)

from agents import create_agents  # noqa: E402
from tasks import create_dev_team_tasks  # noqa: E402
from utils import llm_factory  # noqa: E402


USER_REQUEST = """
Tạo trang web đọc google sheets để tạo bài kiểm tra từ vựng cho học sinh.
Yêu cầu: 
1) Giao diện đơn giản, responsive,
2) Tự động lấy dữ liệu từ google sheets, yêu cầu người dùng link sheet và chọn sheet cần lấy dữ liệu, sau đó hiển thị câu hỏi trắc nghiệm dựa trên dữ liệu đó.
3) Lưu kết quả làm bài của học sinh vào 1 sheet trên cùng file
4) Hướng dẫn chi tiết cách triển khai trên server (nếu cần).
5) Nếu dùng API keys thì phải có cơ chế bảo mật, không hardcode vào code.
Yêu cầu về kết quả:
- Code sạch, có comment giải thích,
- Cấu trúc thư mục rõ ràng,
- Có tài liệu hướng dẫn sử dụng và triển khai.
"""

MAX_CYCLES = 1  # 3 → 1: 1 cycle là đủ cho lần chạy đầu, tránh lặp vô ích
MAX_QC_RETRIES = 3  # Số lần retry tối đa khi QC FAIL (bao gồm lần đầu)

_rag_enabled: bool = False  # set by _build_rag_index after writing src/

IS_FRONTEND = bool(re.search(
    r'html|css|javascript|\bjs\b|frontend|google sheets? api|trang web|website|web app',
    USER_REQUEST, re.I
))

STATE_FILE   = WORKSPACE_ROOT / "state.json"
HISTORY_LOG  = WORKSPACE_ROOT / "crew_history.log"


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


def _save_state(data: dict) -> None:
    """Lưu state sau mỗi cycle để cycle sau có thể dùng."""
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── JSON-in-Final-Answer safety net ──────────────────────────────────────────
# DeepSeek sometimes outputs ReAct JSON in the Final Answer text instead of
# actually calling the tool through the framework.  We intercept and replay it.

def _normalize_path(raw: str) -> str:
    """Strip workspace/ prefix so safe_file_write gets a clean relative path."""
    cleaned = raw.strip().strip('"').strip("'")
    for prefix in ("workspace/", "./workspace/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned


def _extract_json_objects(text: str) -> list:
    """Return all top-level JSON objects found in text (bracket-counting parser).
    Handles '{' inside string values and nested objects correctly.
    """
    results, depth, start = [], 0, -1
    in_string = escape_next = False
    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                results.append(text[start : i + 1])
                start = -1
    return results


def _flush_write_calls(text: str) -> None:
    """Parse agent output for embedded JSON write calls and execute them."""
    for raw in _extract_json_objects(text):
        try:
            data = json.loads(raw)
        except Exception:
            continue

        # Nested ReAct format: {"action": "write_file", "action_input": {...}}
        action = str(data.get("action", "")).lower().replace(" ", "_")
        if action == "write_file":
            inp = data.get("action_input", {})
            fp, content = inp.get("file_path", ""), inp.get("content", "")
            if fp and content:
                _log(f"[flush] Intercepted ReAct write → {fp}")
                safe_file_write._run(
                    file_path=_normalize_path(fp),
                    content=content,
                    overwrite=inp.get("overwrite", True),
                )
            continue

        # Flat format: {"file_path": ..., "content": ...}
        fp, content = data.get("file_path", ""), data.get("content", "")
        if fp and content:
            _log(f"[flush] Intercepted flat write → {fp}")
            safe_file_write._run(
                file_path=_normalize_path(fp),
                content=content,
                overwrite=data.get("overwrite", True),
            )


def _task_callback(task_output) -> None:
    """Fires once per task — safety net for JSON writes."""
    _flush_write_calls(str(task_output))


_FILE_SEP = re.compile(r'^###\s*FILE\s*:\s*(.+?)\s*$', re.MULTILINE | re.IGNORECASE)
_FENCE_RE = re.compile(r'^```[^\n]*\n(.*?)```\s*$', re.DOTALL)


def _extract_and_write_src(text: str) -> list:
    """Parse '### FILE: src/...' blocks from text and write each to workspace/src/.
    Returns list of relative paths written.
    """
    matches = list(_FILE_SEP.finditer(text))
    written = []
    for i, m in enumerate(matches):
        fp_raw = m.group(1).strip().strip('`').strip('"').strip("'")
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.end():end].strip()
        fence = _FENCE_RE.match(seg)
        if fence:
            seg = fence.group(1).rstrip()
        else:
            seg = re.sub(r'\n```\s*$', '', seg).strip()
        if seg and ('.' in fp_raw or '/' in fp_raw):
            fp = _normalize_path(fp_raw)
            result = safe_file_write._run(file_path=fp, content=seg, overwrite=True)
            if not result.startswith(('[BLOCKED', '[ERROR')):
                written.append(fp)
                _log(f"[src] {len(seg)} bytes → {fp}")
    return written


def _run_single_task(agent, description: str, expected_output: str, out_rel: str) -> str:
    """Run one task as its own Crew, return the output file content."""
    from crewai import Task, Crew, Process  # noqa: PLC0415
    WS_REL = os.path.relpath(str(WORKSPACE_ROOT))
    task = Task(
        description=description,
        agent=agent,
        expected_output=expected_output,
        output_file=f"{WS_REL}/{out_rel}",
    )
    try:
        Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=True,
            task_callback=_task_callback,
        ).kickoff()
    except Exception as exc:
        _log(f"[single_task] {out_rel} lỗi: {exc}")
    out_path = WORKSPACE_ROOT / out_rel
    return out_path.read_text("utf-8") if out_path.exists() else ""


# ── Docker fallback Dockerfile (used when Architect doesn't generate one) ────
_FALLBACK_DOCKERFILE = """\
FROM python:3.11-slim

# Node.js 20
RUN apt-get update && apt-get install -y curl && \\
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\
    apt-get install -y nodejs && apt-get clean

# JS tools
RUN npm install -g eslint stylelint prettier

# Python tools
RUN pip install --no-cache-dir pytest pylint

# Non-root user for security
RUN useradd -m checker
USER checker
WORKDIR /workspace
"""


def _ensure_checker_image(cycle: int) -> str:
    """Build Docker image from Dockerfile.checker (fallback to hardcoded if missing).
    Returns image tag on success, '' on failure (caller uses local exec).
    """
    tag = f"project-checker:{cycle}"
    dockerfile = WORKSPACE_ROOT / "Dockerfile.checker"
    if not dockerfile.exists():
        dockerfile.write_text(_FALLBACK_DOCKERFILE, encoding="utf-8")
        _log("[docker] Dockerfile.checker missing — wrote hardcoded fallback")
    try:
        result = subprocess.run(
            ["docker", "build", "-t", tag, "-f", str(dockerfile), str(WORKSPACE_ROOT)],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            _log(f"[docker] Build failed: {result.stderr.decode('utf-8', errors='replace')[:500]}")
            return ""
        _log(f"[docker] Image built: {tag}")
        return tag
    except FileNotFoundError:
        _log("[docker] Docker not installed on host — local exec fallback")
        return ""
    except subprocess.TimeoutExpired:
        _log("[docker] docker build timed out after 300s — local exec fallback")
        return ""
    except Exception as exc:
        _log(f"[docker] Unexpected error during build: {exc}")
        return ""


def _start_checker_container(image_tag: str, cycle: int) -> str:
    """Start a persistent container with workspace volume mounted.
    Returns container name on success, '' on failure.
    """
    name = f"dev-checker-{cycle}"
    # Remove any stale container with same name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
    try:
        result = subprocess.run(
            [
                "docker", "run", "-d", "--name", name,
                "-v", f"{WORKSPACE_ROOT}:/workspace",
                "-w", "/workspace",
                image_tag, "tail", "-f", "/dev/null",
            ],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            _log(f"[docker] Container start failed: {result.stderr.decode('utf-8', errors='replace')[:200]}")
            return ""
        _log(f"[docker] Container started: {name}")
        return name
    except Exception as exc:
        _log(f"[docker] Container start error: {exc}")
        return ""


def _stop_checker_container(name: str) -> None:
    """Stop and remove the checker container. Always called in finally."""
    try:
        subprocess.run(["docker", "stop", name], capture_output=True, timeout=15)
        subprocess.run(["docker", "rm", name], capture_output=True, timeout=10)
        _log(f"[docker] Container stopped and removed: {name}")
    except Exception as exc:
        _log(f"[docker] Container cleanup warning: {exc}")


def _build_rag_index() -> bool:
    """Build / refresh chromadb index from src/ files.
    - < 20 files: skip (Read File is cheaper and more accurate).
    - >= 20 files: reset collection then embed all files.
    Sets global _rag_enabled and updates tools.rag_collection.
    Returns True if RAG is now active.
    """
    global _rag_enabled
    src_dir = WORKSPACE_ROOT / "src"
    src_files = [p for p in src_dir.rglob("*") if p.is_file()]

    if len(src_files) < 20:
        _rag_enabled = False
        _tools_mod.rag_collection = None
        _log(f"[RAG] {len(src_files)} file(s) — RAG disabled (threshold: 20)")
        return False

    try:
        import chromadb  # noqa: PLC0415
        client = chromadb.Client()
        # Reset: delete then recreate so agents always search fresh code
        try:
            client.delete_collection("codebase")
        except Exception:
            pass
        collection = client.get_or_create_collection("codebase")
        for fp in src_files:
            content = fp.read_text("utf-8", errors="replace")
            rel = fp.relative_to(WORKSPACE_ROOT).as_posix()
            collection.add(
                ids=[rel],
                documents=[content],
                metadatas=[{"path": rel}],
            )
        _tools_mod.rag_collection = collection
        _rag_enabled = True
        _log(f"[RAG] Index built: {len(src_files)} files")
        return True
    except Exception as exc:
        _log(f"[RAG] WARNING: {exc} — RAG disabled, pipeline continues")
        _rag_enabled = False
        _tools_mod.rag_collection = None
        return False


_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

_PRIORITY_PREFIXES = ("config", "constant", "util", "helper", "env", "setting")
_LAST_PREFIXES = ("index", "main", "app", "entry")


def _sort_file_inventory(files: list[dict]) -> list[dict]:
    """Sort: config/utils first, UI entry points last, everything else in between."""
    def key(f: dict) -> int:
        base = os.path.basename(f.get("name", "")).lower()
        if any(base.startswith(p) for p in _PRIORITY_PREFIXES):
            return 0
        if any(base.startswith(p) for p in _LAST_PREFIXES):
            return 2
        return 1
    return sorted(files, key=key)


def _parse_file_inventory(t3_output: str) -> list[dict]:
    """Extract file list from Architect JSON block. 3-layer fallback.
    Returns [] to signal single-shot fallback when all layers fail.
    """
    match = _JSON_BLOCK_RE.search(t3_output)
    if not match:
        _log("[inventory] No JSON block found — single-shot fallback")
        return []

    raw = match.group(1).strip()
    _log(f"[inventory] raw block preview: {raw[:200]}")
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return _sort_file_inventory(data)
    except json.JSONDecodeError:
        pass

    # Layer 2: ast.literal_eval (handles trailing comma, single quotes)
    try:
        data = ast.literal_eval(raw)
        if isinstance(data, list):
            _log("[inventory] json.loads failed, ast.literal_eval succeeded")
            return _sort_file_inventory(data)
    except Exception:
        pass

    # Layer 3: LLM repair via cheap model
    try:
        _log("[inventory] Attempting LLM JSON repair...")
        from langchain_core.messages import HumanMessage  # noqa: PLC0415
        repair_llm = llm_factory.get_flash_model()
        response = repair_llm.invoke([HumanMessage(
            content=f"Fix this JSON so it's valid. Return ONLY the JSON array, no explanation:\n{raw}"
        )])
        repair_text = str(response.content if hasattr(response, "content") else response).strip()
        repair_text = re.sub(r"^```[^\n]*\n?", "", repair_text)
        repair_text = re.sub(r"\n?```$", "", repair_text).strip()
        data = json.loads(repair_text)
        if isinstance(data, list):
            _log("[inventory] LLM JSON repair succeeded")
            return _sort_file_inventory(data)
    except Exception as exc:
        _log(f"[inventory] LLM repair failed: {exc}")

    _log("[inventory] All parsing layers failed — single-shot fallback")
    return []


def _sanitize_filename(name: str) -> str:
    """Convert 'src/js/app.js' → 'src_js_app_js' for use in report filenames."""
    return re.sub(r"[^\w]", "_", name).strip("_")


def _run_dev_pipeline(pm, plan_reviewer, architect, coder, qc, reviewer,
                      request, previous_result, is_frontend, cycle: int = 1) -> str:
    """Step-by-step pipeline with QC retry loop.
    - t3 (Architect) runs once.
    - t4 (Coder) → t5 (QC) loop up to MAX_QC_RETRIES times.
      On FAIL: feed qc_feedback back to Coder. On last attempt: Simple Mode.
    - t6 (Reviewer) and t7 (PM) run once after the QC loop.
    """
    global _rag_enabled

    def read(rel: str) -> str:
        p = WORKSPACE_ROOT / rel
        return p.read_text("utf-8") if p.exists() else "(chưa có)"

    container_name = ""

    # Guard already wrote t1 + t2 — just read them
    t1 = read("reports/t1_task_plan.md")
    t2 = read("reports/t2_plan_review.md")

    # ── t3: Architect ──────────────────────────────────────────────────────────
    t3 = _run_single_task(
        architect,
        f"[KẾ HOẠCH]\n{t1[:3000]}\n\n[REVIEW]\n{t2[:1500]}\n\n"
        "Thiết kế kiến trúc:\n"
        "1. Tech stack: HTML + JavaScript + Google Sheets API — KHÔNG backend.\n"
        "2. Liệt kê TẤT CẢ file cần tạo: đường dẫn src/... và mô tả nội dung.\n"
        "3. Design Pattern và lý do.\n"
        "4. Data flow giữa các thành phần.\n\n"
        "SAU PHẦN MÔ TẢ KIẾN TRÚC, xuất danh sách file theo đúng format sau:\n"
        "```json\n"
        "[\n"
        '  {"name": "src/config.js", "description": "Cấu hình API keys và constants"},\n'
        '  {"name": "src/app.js",    "description": "Entry point, khởi tạo app"}\n'
        "]\n"
        "```\n"
        "Không thêm text ngoài JSON block này.",
        "Danh sách file src/..., tech stack, design pattern, data flow, và JSON block.",
        "reports/t3_architecture.md",
    )
    _log("[pipeline] t3 done")

    # Parse file inventory cho per-file mode
    file_inventory = _parse_file_inventory(t3)
    _log(
        f"[pipeline] file_inventory: {len(file_inventory)} file(s) "
        f"{'(per-file mode)' if file_inventory else '(single-shot fallback)'}"
    )

    # ── QC retry loop: t4 → t5 ────────────────────────────────────────────────
    qc_feedback = ""
    qc_history: list[dict] = []
    t4_raw = ""
    t5 = ""

    for qc_attempt in range(1, MAX_QC_RETRIES + 1):
        _log(f"[pipeline] QC attempt {qc_attempt}/{MAX_QC_RETRIES}")

        # Build feedback block
        feedback_block = ""
        if qc_feedback:
            if qc_attempt == MAX_QC_RETRIES:
                feedback_block = (
                    f"\n\n[QC FEEDBACK — LẦN THỬ TRƯỚC]:\n{qc_feedback}\n\n"
                    "[SIMPLE MODE — LẦN THỬ CUỐI]: Viết phiên bản TỐI GIẢN nhất để pass QC. "
                    "Ưu tiên đúng hơn đẹp. Loại bỏ mọi feature phức tạp và abstraction thừa."
                )
            else:
                feedback_block = (
                    f"\n\n[QC FEEDBACK — LẦN THỬ TRƯỚC]:\n{qc_feedback}\n\n"
                    "Phân tích lỗi trên và viết lại TOÀN BỘ source code đã sửa."
                )

        # ── t4: Coder (per-file or single-shot) ───────────────────────────────
        if file_inventory:
            t4_outputs = []
            for file_spec in file_inventory:
                fname = file_spec.get("name", "")
                fdesc = file_spec.get("description", "")
                t4_outputs.append(_run_single_task(
                    coder,
                    f"[KIẾN TRÚC]\n{t3[:2000]}\n\n"
                    f"Viết DUY NHẤT file này:\n"
                    f"Tên: {fname}\n"
                    f"Mục đích: {fdesc}\n\n"
                    f"QUY TẮC BẮT BUỘC:\n"
                    f"- Dùng ES6 Modules (export/import). KHÔNG khai báo biến global (window/global/var ở module scope).\n"
                    f"- Mọi dependency dùng import từ file khác.\n"
                    f"- Nội dung thực tế, đầy đủ — không placeholder, không TODO.\n"
                    f"OUTPUT FORMAT — dùng chính xác:\n"
                    f"### FILE: {fname}\n"
                    "[nội dung đầy đủ, không placeholder, không TODO]\n\n"
                    "KHÔNG dùng ``` code fence. Đường dẫn src/... không có 'workspace/'."
                    + feedback_block,
                    f"Output '### FILE: {fname}' với nội dung đầy đủ.",
                    f"reports/t4_{_sanitize_filename(fname)}.md",
                ))
                _log(f"[pipeline] t4 file done: {fname} (attempt {qc_attempt})")
            t4_raw = "\n\n".join(t4_outputs)
        else:
            t4_raw = _run_single_task(
                coder,
                f"[KIẾN TRÚC]\n{t3[:3000]}\n\n"
                "Viết TOÀN BỘ source code cho tất cả file trong kiến trúc trên.\n"
                "Dùng CHÍNH XÁC format sau cho mỗi file:\n\n"
                "### FILE: src/index.html\n"
                "<!DOCTYPE html>\n<html lang='vi'>\n...nội dung THỰC ĐẦY ĐỦ...\n</html>\n\n"
                "### FILE: src/app.js\n"
                "// nội dung THỰC ĐẦY ĐỦ...\n\n"
                "### FILE: src/style.css\n"
                "/* nội dung THỰC ĐẦY ĐỦ */\n\n"
                "KHÔNG dùng ``` fence | KHÔNG placeholder | KHÔNG TODO | "
                "Đường dẫn src/... không có 'workspace/' | Cuối: 'DONE: X file'"
                + feedback_block,
                "Output ### FILE: src/... với nội dung đầy đủ thực tế từng file.",
                "reports/t4_code_summary.md",
            )
            _log(f"[pipeline] t4 done single-shot (attempt {qc_attempt})")

        # Write source files
        written = _extract_and_write_src(t4_raw)
        _log(f"[pipeline] src/ files written: {written}")

        # On first QC attempt: build Docker checker image (Dockerfile.checker written by Coder)
        if qc_attempt == 1 and not container_name:
            import tools as _tools_mod
            image_tag = _ensure_checker_image(cycle)
            if image_tag:
                container_name = _start_checker_container(image_tag, cycle)
                _tools_mod.checker_container = container_name

        # Build RAG index (enabled only when src/ ≥ 20 files)
        _build_rag_index()

        # RAG hint cho t5/t6
        rag_hint = (
            "Dùng 'Search Codebase' để tra cứu hàm/biến cụ thể, sau đó Read File để đọc toàn file khi cần."
            if _rag_enabled else
            "Dùng Read File đọc trực tiếp từng file trong src/ — project đủ nhỏ, không cần Search Codebase."
        )

        # Src preview (fallback/supplement)
        src_files = sorted(
            p.relative_to(WORKSPACE_ROOT).as_posix()
            for p in (WORKSPACE_ROOT / "src").rglob("*") if p.is_file()
        )
        src_preview = "\n\n".join(
            f"=== {f} ===\n" + read(f)[:600] for f in src_files[:5]
        )

        # ── t5: QC ────────────────────────────────────────────────────────────
        if is_frontend:
            t5_desc = (
                f"[FILE ĐÃ TẠO]: {src_files}\n\n[NỘI DUNG]\n{src_preview}\n\n"
                f"[NGỮ CẢNH CODEBASE]: {rag_hint}\n\n"
                "TRÌNH TỰ BẮT BUỘC:\n"
                "1. Dùng 'Run Checks' với lệnh: node --check src/js/*.js\n"
                "   (bỏ qua nếu không có Node.js — ghi rõ trong báo cáo)\n"
                "2. Dùng 'Run Checks' với lệnh: eslint src/js/ --format compact\n"
                "   (bỏ qua nếu eslint chưa cài — ghi rõ trong báo cáo)\n"
                "3. Kiểm tra HTML: DOCTYPE, charset, thẻ đóng/mở đúng.\n"
                "4. Kiểm tra bảo mật: không hardcode API key, không XSS.\n"
                "5. Kiểm tra responsive: meta viewport, media query.\n"
                "6. Kết luận PASS chỉ khi bước 1+2 không có syntax error.\n"
                "Xuất báo cáo: PASS hoặc FAIL kèm stdout thực tế từ tool."
            )
        else:
            t5_desc = (
                f"[FILE ĐÃ TẠO]: {src_files}\n\n[NỘI DUNG]\n{src_preview}\n\n"
                f"[NGỮ CẢNH CODEBASE]: {rag_hint}\n\n"
                "Kiểm tra:\n1. Chạy pytest và paste output thực tế.\n"
                "2. Kiểm tra bảo mật và logic.\n"
                "Kết luận: PASS hoặc FAIL kèm output thực tế."
            )
        t5 = _run_single_task(qc, t5_desc, "PASS hoặc FAIL kèm output thực tế.", "reports/t5_qc_report.md")
        _log(f"[pipeline] t5 done (attempt {qc_attempt})")

        # Verdict
        verdict = "FAIL" if "FAIL" in t5.upper() else "PASS"
        qc_history.append({
            "attempt": qc_attempt,
            "verdict": verdict,
            "reason": t5[:500] if verdict == "FAIL" else "",
        })

        if verdict == "PASS":
            _log(f"[pipeline] QC PASS on attempt {qc_attempt}")
            break
        if qc_attempt < MAX_QC_RETRIES:
            qc_feedback = t5[:1500]
            _log(f"[pipeline] QC FAIL — retry {qc_attempt + 1}/{MAX_QC_RETRIES}")
        else:
            _log(f"[pipeline] QC still FAIL after {MAX_QC_RETRIES} attempts — continuing to t6")

    # Save QC history to state.json
    existing_state = _load_state()
    existing_state["qc_history"] = qc_history
    _save_state(existing_state)

    # ── t6: Reviewer ──────────────────────────────────────────────────────────
    rag_hint_t6 = (
        "Dùng 'Search Codebase' để tra cứu, sau đó Read File để đọc toàn file khi cần."
        if _rag_enabled else
        "Dùng Read File đọc trực tiếp từng file trong src/."
    )
    src_files = sorted(
        p.relative_to(WORKSPACE_ROOT).as_posix()
        for p in (WORKSPACE_ROOT / "src").rglob("*") if p.is_file()
    )
    src_preview = "\n\n".join(
        f"=== {f} ===\n" + read(f)[:600] for f in src_files[:5]
    )
    t6 = _run_single_task(
        reviewer,
        f"[NỘI DUNG SRC]\n{src_preview}\n\n"
        f"[NGỮ CẢNH CODEBASE]: {rag_hint_t6}\n\n"
        "Review:\n1. Code Style: tên biến/hàm rõ ràng?\n"
        "2. Clean Code: code thừa, hàm quá dài?\n"
        "3. Security: hardcode key, XSS rõ?\n"
        "Kết luận: APPROVED hoặc REQUEST CHANGES kèm vị trí cụ thể.",
        "APPROVED hoặc REQUEST CHANGES kèm chi tiết.",
        "reports/t6_review.md",
    )
    _log("[pipeline] t6 done")

    # ── t7: PM Final Report ────────────────────────────────────────────────────
    t7 = _run_single_task(
        pm,
        f"Tóm tắt:\n- File tạo: {src_files}\n"
        f"- QC ({len(qc_history)} lần thử): {t5[:400]}\n"
        f"- Review: {t6[:400]}\n\n"
        f"Báo cáo cuối:\n1. Tóm tắt đã làm\n2. Danh sách file\n"
        f"3. Kết quả QC (số lần thử: {len(qc_history)})\n4. Nhận xét chất lượng\n5. Bước tiếp theo\n"
        f"Câu cuối: '✅ Xong! Đã tạo {len(src_files)} file. Ông check nhé!'",
        "Báo cáo ngắn gọn rõ ràng.",
        "reports/t7_final_report.md",
    )
    _log("[pipeline] t7 done")

    # Stop Docker checker container (cleanup after QC loop is done)
    if container_name:
        _stop_checker_container(container_name)
        import tools as _tools_mod
        _tools_mod.checker_container = None

    return t7


def _run_t1_t2_with_guard(
    pm, plan_reviewer, crew_agents, request, retry_context, is_frontend, max_inner=2
) -> tuple[list, str]:
    """Chạy t1+t2 với inner retry nếu Plan Reviewer yêu cầu sửa (REQUEST CHANGES).
    Trả về (tasks_list_đầy_đủ, t2_conclusion).
    """
    original_t1_desc = (
        f"Yêu cầu từ User:\n{request}{retry_context}\n"
        "Nhiệm vụ — xuất kế hoạch gồm:\n"
        "1. Phân tích yêu cầu, liệt kê cụ thể các User Stories.\n"
        "2. Xác định phạm vi và Done Criteria đo lường được.\n"
        "3. Liệt kê rủi ro tiềm ẩn."
    )

    feedback = ""
    for attempt in range(max_inner + 1):
        # Re-instantiate Tasks mỗi lần để tránh carry-over state
        from tasks import create_dev_team_tasks  # noqa: PLC0415
        from crewai import Task  # noqa: PLC0415

        t1_desc = original_t1_desc
        if feedback:
            t1_desc = f"{original_t1_desc}\n\n### FEEDBACK TỪ REVIEWER:\n{feedback}"
            _log(f"[t2 Guard] Inject feedback vào t1 (attempt {attempt}/{max_inner})")

        t1 = Task(
            description=t1_desc,
            agent=pm,
            expected_output="Kế hoạch gồm: User Stories, Done Criteria, rủi ro.",
            output_file=os.path.join(os.path.relpath(str(WORKSPACE_ROOT)), "reports", "t1_task_plan.md"),
        )
        t2 = Task(
            description=(
                "Dùng Read File đọc 'reports/t1_task_plan.md', đánh giá:\n"
                "1. User Stories có rõ ràng, khả thi kỹ thuật không?\n"
                "2. Có Task thiếu, dư, hoặc mâu thuẫn không?\n"
                "3. Done Criteria có đo lường được không?\n"
                "4. Rủi ro kỹ thuật nào PM chưa nhận ra?\n"
                "Xuất kết luận: APPROVED hoặc REQUEST CHANGES (kèm danh sách điểm cần sửa)."
            ),
            agent=plan_reviewer,
            context=[t1],
            expected_output="Kết luận APPROVED hoặc REQUEST CHANGES kèm lý do cụ thể.",
            output_file=os.path.join(os.path.relpath(str(WORKSPACE_ROOT)), "reports", "t2_plan_review.md"),
        )

        mini_crew = Crew(
            agents=crew_agents,
            tasks=[t1, t2],
            process=Process.sequential,
            verbose=True,
            task_callback=_task_callback,
        )
        try:
            mini_crew.kickoff()
        except Exception as exc:
            _log(f"[t2 Guard] mini_crew lỗi: {exc} — bỏ qua guard, tiếp tục")
            break

        # Đọc kết quả t2
        t2_report_path = WORKSPACE_ROOT / "reports" / "t2_plan_review.md"
        if t2_report_path.exists():
            t2_content = t2_report_path.read_text(encoding="utf-8")
        else:
            _log("[t2 Guard] Không tìm thấy t2_plan_review.md — bỏ qua guard")
            break

        if "REQUEST CHANGES" in t2_content.upper():
            if attempt < max_inner:
                feedback = t2_content[:1500]  # đưa toàn bộ review vào feedback
                _log(f"[t2 Guard] PLAN cần sửa — thử lại (attempt {attempt + 1}/{max_inner})")
                continue  # ← iterate to next attempt with new feedback
            else:
                _log("[t2 Guard] Đã thử hết inner retry — tiếp tục với plan hiện tại")
                break
        else:
            _log(f"[t2 Guard] PLAN APPROVED sau {attempt + 1} lần")
            break


if __name__ == "__main__":
    pm, plan_reviewer, architect, coder, qc, reviewer = create_agents()
    state = _load_state()
    _log(f"IS_FRONTEND={IS_FRONTEND}")

    last_result = state.get("last_result")
    for cycle in range(1, MAX_CYCLES + 1):
        _log(f"=== CYCLE {cycle}/{MAX_CYCLES} ===")

        retry_context = ""
        if last_result:
            retry_context = (
                f"\n\n[KẾT QUẢ CYCLE TRƯỚC — CẦN CẢI TIẾN]\n"
                f"{last_result[:2000]}\n"
                "Dựa trên kết quả trên, hãy xác định nguyên nhân và tập trung vào điểm chưa giải quyết.\n"
            )

        # Guard t1+t2 chỉ chạy ở cycle đầu tiên — cycle sau plan đã approved, không cần lặp lại
        if cycle == 1:
            _run_t1_t2_with_guard(
                pm, plan_reviewer,
                crew_agents=[pm, plan_reviewer, architect, coder, qc, reviewer],
                request=USER_REQUEST,
                retry_context=retry_context,
                is_frontend=IS_FRONTEND,
            )
        else:
            _log(f"[cycle {cycle}] Bỏ qua guard t1+t2 — dùng lại plan đã approved")

        # Step-by-step pipeline: inject file content, never ask model to read files
        try:
            result = _run_dev_pipeline(
                pm, plan_reviewer, architect, coder, qc, reviewer,
                USER_REQUEST, last_result, IS_FRONTEND, cycle,
            )
            last_result = result
            _save_state({"cycle": cycle, "last_result": last_result})
            _log(f"=== CYCLE {cycle} DONE ===")
        except Exception as exc:
            _log(f"=== CYCLE {cycle} FAILED: {exc} ===")
            traceback.print_exc()
            import tools as _tools_mod
            if _tools_mod.checker_container:
                _stop_checker_container(_tools_mod.checker_container)
                _tools_mod.checker_container = None
            _save_state({"cycle": cycle, "last_result": last_result or "", "error": str(exc)})
            _log("State đã được lưu. Chạy lại để vào cycle tiếp theo.")

    print("\n--- FINAL WORKFLOW REPORT ---\n")
    print(last_result)