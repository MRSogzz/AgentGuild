import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from config import WORKSPACE_DIR, ARTIFACT_DIR
from guardrails import (
    safe_path, check_python_ast, truncate_output, GuardrailError,
    check_write_target, check_file_content_size,
)
from observability import logger


def _subprocess_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    return env


TOOL_SCHEMAS = {
    "list_dir": {
        "description": "列出工作區指定目錄下的檔案。",
        "args": {"path": "相對於工作區的路徑，預設 '.'"},
    },
    "read_file": {
        "description": "讀取工作區內的一個文字檔。",
        "args": {"path": "檔案相對路徑"},
    },
    "write_file": {
        "description": "把文字寫入工作區內的檔案（會覆蓋）。",
        "args": {"path": "檔案相對路徑", "content": "寫入內容"},
    },
    "edit_file": {
        "description": "對工作區檔案做精確字串取代。old 必須在檔案中唯一出現。",
        "args": {"path": "檔案相對路徑", "old": "被取代的原文", "new": "取代後的內容"},
    },
    "run_python": {
        "description": "在沙箱中執行一段 Python 程式並回傳 stdout。禁止 import os/sys/subprocess 等。",
        "args": {"code": "Python 程式碼字串"},
    },
    "run_tests": {
        "description": "在 workspace 目錄下執行 pytest，回傳測試結果。"
                       "工程師應該會產出 test_*.py 檔案。",
        "args": {"target": "測試路徑，預設 '.'（跑全部 test_*.py）"},
    },
    "run_latest_artifact": {
        "description": "（已棄用）執行工作區最新的 .py 工件。",
        "args": {},
    },
    "search_docs": {
        "description": "在 docs/ 目錄的檔案裡做關鍵詞檢索，回傳相關片段。",
        "args": {"query": "查詢關鍵詞", "top_k": "回傳片段數，預設 3"},
    },
}


# ---------- 檔案操作 ----------
def _list_dir(path: str = ".") -> str:
    p = safe_path(path)
    if not p.exists():
        return f"[目錄不存在: {path}]"
    if p.is_file():
        return f"{path} 是一個檔案"
    entries = []
    for child in sorted(p.iterdir()):
        if child.name == "artifacts":
            continue
        tag = "DIR " if child.is_dir() else "FILE"
        size = child.stat().st_size if child.is_file() else 0
        entries.append(f"  {tag}  {child.name}  ({size}B)")
    if not entries:
        return f"[空目錄: {path}]"
    return f"{path}/\n" + "\n".join(entries)


def _read_file(path: str) -> str:
    p = safe_path(path)
    if not p.exists() or not p.is_file():
        return f"[檔案不存在: {path}]"
    if p.stat().st_size > 200_000:
        return f"[檔案過大: {p.stat().st_size}B]"
    try:
        return truncate_output(p.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return "[二進位檔案，無法讀取]"


def _write_file(path: str, content: str) -> str:
    p = safe_path(path)
    check_write_target(p)
    check_file_content_size(content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"[已寫入 {len(content)} 位元組到 {path}]"


def _edit_file(path: str, old: str, new: str) -> str:
    p = safe_path(path)
    if not p.exists() or not p.is_file():
        return f"[檔案不存在: {path}]"
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return f"[edit_file 失敗: old 字串未在 {path} 中找到]"
    if count > 1:
        return f"[edit_file 失敗: old 在 {path} 中出現 {count} 次，必須唯一]"
    new_text = text.replace(old, new)
    check_file_content_size(new_text)
    p.write_text(new_text, encoding="utf-8")
    return f"[已替換 1 處，檔案現在 {len(new_text)} 位元組]"


# ---------- 執行 ----------
def _run_python(code: str) -> str:
    check_python_ast(code)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir=WORKSPACE_DIR, delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, tmp],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            cwd=WORKSPACE_DIR,
            env=_subprocess_env(),
            stdin=subprocess.DEVNULL,
        )
        out = r.stdout or ""
        err = r.stderr or ""
        result = out
        if err:
            result += f"\n[stderr]\n{err}"
        if r.returncode != 0:
            result += f"\n[退出碼 {r.returncode}]"
        return truncate_output(result, limit=6000) or "[無輸出]"
    except subprocess.TimeoutExpired:
        return "[執行超時（15 秒）]"
    finally:
        try:
            Path(tmp).unlink()
        except Exception:
            pass


def run_python_code(code: str) -> str:
    return _run_python(code)


def _run_tests(target: str = ".") -> str:
    safe_path(target)

    test_files = list(WORKSPACE_DIR.glob("test_*.py"))
    if not test_files:
        return ("[未找到 test_*.py 檔案]\n"
                "工程師應該產出 test_*.py 檔案。"
                "請確認工程師使用 ```python:test_xxx.py 格式輸出。")

    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest",
             target, "-v", "--tb=short", "--no-header",
             "--ignore=artifacts",
             "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            cwd=WORKSPACE_DIR,
            env=_subprocess_env(),
            stdin=subprocess.DEVNULL,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return truncate_output(out, limit=6000) or f"[pytest 退出碼 {r.returncode}]"
    except FileNotFoundError:
        return "[pytest 未安裝。請執行: pip install pytest]"
    except subprocess.TimeoutExpired:
        return "[pytest 超時（60 秒）]"
    except Exception as e:
        return f"[pytest 執行失敗: {e}]"


def _run_latest_artifact() -> str:
    if not ARTIFACT_DIR.exists():
        return "[工作區尚無工件]"
    candidates = list(ARTIFACT_DIR.rglob("*.py"))
    if not candidates:
        return "[工作區尚無工件]"
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest = candidates[0]
    try:
        code = latest.read_text(encoding="utf-8")
    except Exception as e:
        return f"[讀取工件失敗: {e}]"
    return _run_python(code)


def _search_docs(query: str, top_k: int = 3) -> str:
    from rag import search
    hits = search(query, top_k=int(top_k))
    if not hits:
        return "[未檢索到相關檔案]"
    return "\n\n".join(
        f"[{h['source']} | {h['score']:.2f}]\n{h['text']}" for h in hits
    )


_HANDLERS = {
    "list_dir": _list_dir,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "run_python": _run_python,
    "run_tests": _run_tests,
    "run_latest_artifact": _run_latest_artifact,
    "search_docs": _search_docs,
}


def execute(tool_name: str, args: dict) -> str:
    if tool_name not in _HANDLERS:
        return f"[未知工具: {tool_name}]"
    try:
        logger.info("tool_call", tool=tool_name, args=args)
        return _HANDLERS[tool_name](**(args or {}))
    except GuardrailError as e:
        return f"[護欄攔截: {e}]"
    except TypeError as e:
        return f"[引數錯誤: {e}]"
    except Exception as e:
        logger.error("tool_error", tool=tool_name, error=str(e))
        return f"[工具執行失敗: {e}]"


def schemas_for(tool_names: list[str]) -> str:
    lines = []
    for name in tool_names:
        s = TOOL_SCHEMAS.get(name)
        if not s:
            continue
        if not s["args"]:
            lines.append(f"- {name}: {s['description']} 無需引數。")
        else:
            args_desc = "；".join(f"{k}: {v}" for k, v in s["args"].items())
            lines.append(f"- {name}: {s['description']} 引數：{args_desc}")
    return "\n".join(lines)


# ============ 工具呼叫解析（多格式 + 巢狀 JSON） ============
def _find_matching_brace(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif in_string:
            if c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _try_parse_json(raw: str):
    import json
    if not raw:
        return None
    raw = raw.strip()

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    open_braces = raw.count("{") - raw.count("}")
    if open_braces > 0:
        try:
            data = json.loads(raw + "}" * open_braces)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    candidates = []
    quote_count = 0
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            i += 2
            continue
        if raw[i] == '"':
            quote_count += 1
        i += 1
    if quote_count % 2 == 1:
        candidates.append(raw + '"')
        if open_braces > 0:
            candidates.append(raw + '"' + "}" * open_braces)

    trimmed = raw.rstrip()
    for cut in range(0, min(120, len(trimmed)), 10):
        if cut == 0:
            continue
        candidates.append(trimmed[: len(trimmed) - cut])

    for c in candidates:
        for attempt in (c, c + "}", c.rstrip("}") + "}"):
            try:
                data = json.loads(attempt)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue

    return None


def _find_gemma_tool_calls(text: str) -> list[tuple[str, dict]]:
    results = []
    starts = [m.start() for m in re.finditer(r"<\|tool_call>", text)]
    for start in starts:
        segment = text[start:]
        m = re.match(
            r"<\|tool_call>\s*call:\s*([A-Za-z_][A-Za-z0-9_]*)",
            segment,
        )
        if not m:
            continue
        name = m.group(1).strip()
        rest = segment[m.end():]
        brace_idx = rest.find("{")
        if brace_idx < 0:
            continue
        abs_start = start + m.end() + brace_idx
        abs_end = _find_matching_brace(text, abs_start)
        if abs_end is None:
            raw = text[abs_start:]
        else:
            raw = text[abs_start : abs_end + 1]
        args = _try_parse_json(raw)
        if args is not None:
            results.append((name, args))
    return results


def _find_system_tool_calls(text: str) -> list[tuple[str, dict]]:
    results = []
    for m in re.finditer(r"<tool_call>\s*(\{)", text, re.DOTALL):
        brace_start = m.start(1)
        end = _find_matching_brace(text, brace_start)
        if end is None:
            continue
        raw = text[brace_start : end + 1]
        data = _try_parse_json(raw)
        if data and "name" in data:
            results.append((data["name"], data.get("args", {}) or {}))
    return results


def parse_tool_call(text: str):
    """解析工具呼叫。優先從後往前找（續寫場景下最後一個通常完整）。"""
    if not text:
        return None

    system_calls = _find_system_tool_calls(text)
    if system_calls:
        return system_calls[-1]

    gemma_calls = _find_gemma_tool_calls(text)
    if gemma_calls:
        return gemma_calls[-1]

    # 寬鬆匹配 call:NAME {...}
    matches = list(re.finditer(r"call:\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{", text))
    for m in reversed(matches):
        name = m.group(1).strip()
        brace_start = m.end() - 1
        end = _find_matching_brace(text, brace_start)
        if end is None:
            raw = text[brace_start:]
        else:
            raw = text[brace_start : end + 1]
        args = _try_parse_json(raw)
        if args is not None:
            return name, args

    return None


# ============ Verdict 寬容解析 ============
def parse_verdict_tag(text: str) -> str | None:
    """
    寬容匹配 verdict 標籤：
      <verdict>PASS</verdict>
      <verdict>PASS</verdict           （缺 >）
      <verdict> PASS </verdict>
      [verdict]PASS[/verdict]
    """
    if not text:
        return None
    patterns = [
        r"<verdict>\s*(PASS|REVISE)\s*</verdict\s*>?",
        r"\[verdict\]\s*(PASS|REVISE)\s*\[/verdict\s*\]?",
        r"<verdict>\s*(PASS|REVISE)\s*$",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).upper()
    return None
