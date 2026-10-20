import os
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from config import (
    WORKSPACE_DIR, ARTIFACT_DIR, VISUAL_SCREENSHOT_WIDTH,
    VISUAL_SCREENSHOT_HEIGHT, VISUAL_SCREENSHOT_JPEG_QUALITY,
    VISUAL_SCREENSHOT_READY_TIMEOUT_MS, VISUAL_SCREENSHOT_MAX_IMAGES,
    VISUAL_SCREENSHOT_WAIT_MS, VISUAL_IMAGE_MAX_BYTES,
    VISUAL_VIEW_SWITCH_WAIT_MS, VISUAL_EMPTY_PIXEL_RATIO, BROWSER_LAUNCH_ARGS,
    BROWSER_PAGE_LOAD_TIMEOUT_MS, FRONTEND_RUNTIME_WAIT_MS,
    BROWSER_READY_TIMEOUT_MS, FRONTEND_RUNTIME_TIMEOUT_SECONDS,
    SCREENSHOT_SUBPROCESS_TIMEOUT_SECONDS,
)
from guardrails import (
    safe_path, check_python_ast, GuardrailError,
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


def truncate_output(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    head_size = limit // 2
    tail_size = limit - head_size
    return (text[:head_size]
            + f"\n\n...[中略 {len(text) - limit} 字元]...\n\n"
            + text[-tail_size:])


TOOL_SCHEMAS = {
    "list_dir": {"description": "列出工作區指定目錄下的檔案。", "args": {"path": "相對於工作區的路徑，預設 '.'"}},
    "read_file": {"description": "讀取工作區內的一個文字檔。", "args": {"path": "檔案相對路徑"}},
    "write_file": {"description": "把文字寫入工作區內的檔案（會覆蓋）。", "args": {"path": "檔案相對路徑", "content": "寫入內容"}},
    "edit_file": {"description": "對工作區檔案做精確字串取代。old 必須在檔案中唯一出現。", "args": {"path": "檔案相對路徑", "old": "被取代的原文", "new": "取代後的內容"}},
    "run_python": {"description": "在沙箱中執行一段 Python 程式並回傳 stdout。", "args": {"code": "Python 程式碼字串"}},
    "run_tests": {"description": "在 workspace 目錄執行 pytest，回傳完整原始輸出。", "args": {"target": "測試路徑（檔案或目錄），預設 '.'"}},
    "get_test_summary": {"description": "跑一次 pytest 並回傳結構化摘要。", "args": {"target": "測試路徑，預設 '.'"}},
    "run_lint": {"description": "對 Python 專案執行 ruff 檢查，回傳 lint 結果。", "args": {"target": "檢查路徑，預設 '.'"}},
    "run_frontend_check": {"description": "檢查工作區內所有前端檔案語法與結構。", "args": {"target": "檢查的目錄，預設 '.'"}},
    "screenshot_page": {"description": "用無頭瀏覽器對 HTML 頁面截圖，回傳圖片的 base64。", "args": {"path": "HTML 檔案相對路徑，預設 'index.html'", "width": "寬度，預設 800", "height": "高度，預設 600", "views": "可選視角名稱清單"}},
    "run_latest_artifact": {"description": "（已棄用）", "args": {}},
    "search_docs": {"description": "在 docs/ 目錄的文件裡做關鍵詞檢索。", "args": {"query": "查詢關鍵詞", "top_k": "回傳片段數，預設 3"}},
}


# ---------- 文件操作 ----------
def _list_dir(path: str = ".") -> str:
    p = safe_path(path)
    if not p.exists(): return f"[目錄不存在: {path}]"
    if p.is_file(): return f"{path} 是一個檔案"
    entries = []
    for child in sorted(p.iterdir()):
        if child.name == "artifacts": continue
        tag = "DIR " if child.is_dir() else "FILE"
        size = child.stat().st_size if child.is_file() else 0
        entries.append(f"  {tag}  {child.name}  ({size}B)")
    if not entries: return f"[空目錄: {path}]"
    return f"{path}/\n" + "\n".join(entries)


def _read_file(path: str) -> str:
    p = safe_path(path)
    if not p.exists() or not p.is_file(): return f"[檔案不存在: {path}]"
    if p.stat().st_size > 200_000: return f"[檔案過大: {p.stat().st_size}B]"
    try: return truncate_output(p.read_text(encoding="utf-8"), limit=12000)
    except UnicodeDecodeError: return "[二進位檔案，無法讀取]"


def _write_file(path: str, content: str) -> str:
    p = safe_path(path)
    check_write_target(p)
    check_file_content_size(content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"[已寫入 {len(content)} 位元組到 {path}]"


def _edit_file(path: str, old: str, new: str) -> str:
    p = safe_path(path)
    if not p.exists() or not p.is_file(): return f"[檔案不存在: {path}]"
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0: return f"[edit_file 失敗: old 字串未在 {path} 中找到]"
    if count > 1: return f"[edit_file 失敗: old 在 {path} 中出現 {count} 次，必須唯一]"
    new_text = text.replace(old, new)
    check_file_content_size(new_text)
    p.write_text(new_text, encoding="utf-8")
    return f"[已替換 1 處，檔案現在 {len(new_text)} 位元組]"


# ---------- 執行 ----------
def _run_python(code: str) -> str:
    check_python_ast(code)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", dir=WORKSPACE_DIR, delete=False, encoding="utf-8") as f:
        f.write(code); tmp = f.name
    try:
        r = subprocess.run([sys.executable, tmp], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, cwd=WORKSPACE_DIR, env=_subprocess_env(), stdin=subprocess.DEVNULL)
        out = r.stdout or ""; err = r.stderr or ""
        result = out
        if err: result += f"\n[stderr]\n{err}"
        if r.returncode != 0: result += f"\n[退出碼 {r.returncode}]"
        return truncate_output(result, limit=6000) or "[無輸出]"
    except subprocess.TimeoutExpired: return "[執行超時（15 秒）]"
    finally:
        try: Path(tmp).unlink()
        except Exception: pass


def run_python_code(code: str) -> str: return _run_python(code)


# ---------- 測試與 Lint ----------
def _is_test_name(name: str) -> bool:
    base = name.split("/")[-1]
    return base.startswith("test_") or base.endswith("_test.py")


def _find_test_files(target: str = ".") -> list[Path]:
    base = safe_path(target)
    if not base.exists(): return []
    if base.is_file(): return [base] if _is_test_name(base.name) else []
    return [p for p in base.rglob("*.py") if "artifacts" not in p.parts and _is_test_name(p.name)]


def _build_pytest_args(target: str, verbose: bool) -> list[str]:
    args = [sys.executable, "-m", "pytest", target]
    args += ["-v", "--tb=short", "--no-header"] if verbose else ["-q", "--tb=line", "--no-header"]
    if target in (".", "./"): args.append("--ignore=artifacts")
    args += ["-p", "no:cacheprovider"]
    return args


def _run_pytest(target: str, verbose: bool) -> tuple[str, int | None]:
    try:
        r = subprocess.run(_build_pytest_args(target, verbose), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=WORKSPACE_DIR, env=_subprocess_env(), stdin=subprocess.DEVNULL)
        return (r.stdout or "") + (r.stderr or ""), r.returncode
    except subprocess.TimeoutExpired: return "[pytest 超時（60 秒）]", None
    except FileNotFoundError: return "[pytest 未安裝]", None
    except Exception as e: return f"[pytest 失敗: {e}]", None


def _run_tests(target: str = ".") -> str:
    if not safe_path(target).exists(): return f"[路徑不存在: {target}]"
    if not _find_test_files(target): return "[未找到測試檔]"
    out, code = _run_pytest(target, verbose=True)
    return truncate_output(out, limit=8000) if code is not None else out


def _get_test_summary(target: str = ".") -> str:
    from core.test_utils import parse_pytest_output
    if not safe_path(target).exists(): return f"[路徑不存在: {target}]"
    if not _find_test_files(target): return "[未找到測試檔]"
    
    out, code = _run_pytest(target, verbose=False)
    if code is None: return out
    
    parsed = parse_pytest_output(out)
    if not parsed["saw_pytest"]:
        return f"[無法解析 pytest 摘要]\n\n原始輸出：\n{truncate_output(out, limit=2000)}"
        
    lines = [f"狀態：{'✅ 全部通過' if parsed['all_passed'] else '❌ 部分失敗（實作需修正）'}",
             f"通過：{parsed['passed']} / {parsed['total']}", f"失敗：{parsed['failed']}"]
    if parsed["errors"]: lines.append(f"錯誤：{parsed['errors']}")
    if parsed["summary_line"]: lines.append(f"摘要：`{parsed['summary_line']}`")
    if parsed["failures"]:
        lines.append("\n失敗清單：")
        for f in parsed["failures"]:
            lines.append(f"- `{f['test']}`\n  錯誤：{f['message']}")
    if not parsed["all_passed"]: lines.append("\n**測試未全部通過，實作需要修正。**")
    return "\n".join(lines)


def _run_lint(target: str = ".") -> str:
    try:
        r = subprocess.run([sys.executable, "-m", "ruff", "check", target], capture_output=True, text=True, timeout=15, cwd=WORKSPACE_DIR, env=_subprocess_env())
        if r.returncode == 0: return "✅ Lint 通過，無錯誤。"
        return f"❌ Lint 發現問題：\n{r.stdout}\n{r.stderr}"
    except FileNotFoundError: return "[未安裝 ruff，跳過 lint 檢查]"
    except Exception as e: return f"[Lint 執行失敗: {e}]"


# ---------- 前端檢查 ----------
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

def _check_html(path: Path) -> tuple:
    rel = str(path.relative_to(WORKSPACE_DIR))
    try:
        from html.parser import HTMLParser
        class Checker(HTMLParser):
            def __init__(self): super().__init__(); self.stack = []; self.errors = []
            def handle_starttag(self, tag, attrs):
                if tag.lower() not in VOID_TAGS: self.stack.append(tag.lower())
            def handle_endtag(self, tag):
                tag = tag.lower()
                if not self.stack: self.errors.append(f"Unexpected </{tag}>")
                elif self.stack[-1] != tag:
                    self.errors.append(f"Mismatched: expected </{self.stack[-1]}>, got </{tag}>")
                    if tag in self.stack:
                        while self.stack and self.stack[-1] != tag: self.stack.pop()
                        if self.stack: self.stack.pop()
                else: self.stack.pop()
        text = path.read_text(encoding="utf-8")
        checker = Checker(); checker.feed(text)
        if checker.stack: checker.errors.append(f"Unclosed tags: {checker.stack}")
        return (rel, "html_structure", "fail", "\n".join(checker.errors)) if checker.errors else (rel, "html_structure", "pass", "")
    except Exception as e: return (rel, "html_structure", "fail", str(e))

def _check_css(path: Path) -> tuple:
    rel = str(path.relative_to(WORKSPACE_DIR))
    try: text = path.read_text(encoding="utf-8")
    except Exception as e: return (rel, "css_balance", "fail", str(e))
    text = re.sub(r'/\*[\s\S]*?\*/', '', text)
    stack, in_string, escape = [], None, False
    for i, c in enumerate(text):
        if escape: escape = False; continue
        if c == '\\': escape = True; continue
        if in_string:
            if c == in_string: in_string = None
            continue
        if c in '"\'"': in_string = c; continue
        if c in '{([': stack.append((c, i))
        elif c in '})]':
            if not stack: return (rel, "css_balance", "fail", f"Unexpected '{c}' at {i}")
            open_c, open_i = stack.pop()
            if c != {'{': '}', '(': ')', '[': ']'}[open_c]:
                return (rel, "css_balance", "fail", f"Mismatched: '{open_c}' at {open_i} closed by '{c}' at {i}")
    if stack: return (rel, "css_balance", "fail", f"Unclosed: {stack}")
    if in_string: return (rel, "css_balance", "fail", "Unclosed string")
    return (rel, "css_balance", "pass", "")

def _check_js_balance(text: str) -> tuple:
    stack, in_string, in_template, in_line_comment, in_block_comment, escape, i, n = [], None, False, False, False, False, 0, len(text)
    while i < n:
        c, nxt = text[i], text[i + 1] if i + 1 < n else ''
        if in_line_comment:
            if c == '\n': in_line_comment = False
            i += 1; continue
        if in_block_comment:
            if c == '*' and nxt == '/': in_block_comment = False; i += 2; continue
            i += 1; continue
        if escape: escape = False; i += 1; continue
        if c == '\\': escape = True; i += 1; continue
        if in_string:
            if c == in_string: in_string = None
            i += 1; continue
        if in_template:
            if c == '`': in_template = False
            i += 1; continue
        if c == '/' and nxt == '/': in_line_comment = True; i += 2; continue
        if c == '/' and nxt == '*': in_block_comment = True; i += 2; continue
        if c in '"\'"': in_string = c; i += 1; continue
        if c == '`': in_template = True; i += 1; continue
        if c in '{([': stack.append((c, i))
        elif c in '})]':
            if not stack: return ("fail", f"Unexpected '{c}' at {i}")
            open_c, open_i = stack.pop()
            if c != {'{': '}', '(': ')', '[': ']'}[open_c]:
                return ("fail", f"Mismatched: '{open_c}' at {open_i} closed by '{c}' at {i}")
        i += 1
    if stack: return ("fail", f"Unclosed brackets: {[c for c, _ in stack]}")
    if in_string or in_template: return ("fail", "Unclosed string or template literal")
    return ("pass", "")

def _try_node_check(path: Path):
    try:
        r = subprocess.run(['node', '--check', str(path)], capture_output=True, text=True, timeout=5, encoding='utf-8', errors='replace')
        return (True, "") if r.returncode == 0 else (False, (r.stderr or r.stdout).strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError): return (None, "")

def _check_js(path: Path) -> tuple:
    rel = str(path.relative_to(WORKSPACE_DIR))
    try: text = path.read_text(encoding="utf-8")
    except Exception as e: return (rel, "js_syntax", "fail", str(e))
    node_ok, node_msg = _try_node_check(path)
    if node_ok is True: return (rel, "js_syntax", "pass", "")
    if node_ok is False: return (rel, "js_syntax", "fail", node_msg)
    status, msg = _check_js_balance(text)
    return (rel, "js_syntax", status, msg)

def _run_frontend_check(target: str = ".") -> str:
    root = safe_path(target)
    if not root.exists(): return f"[目錄不存在: {target}]"
    checks = []
    for ext, checker in [('.html', _check_html), ('.htm', _check_html), ('.css', _check_css), ('.js', _check_js), ('.mjs', _check_js)]:
        for f in root.rglob(f"*{ext}"):
            if "artifacts" not in f.parts: checks.append(checker(f))
    html_paths = [f.resolve().as_uri() for ext in ("*.html", "*.htm")
                  for f in root.rglob(ext) if "artifacts" not in f.parts]
    if html_paths:
        checks.extend(_check_frontend_runtime(html_paths))
        primary_html = next(
            (f for ext in ("*.html", "*.htm") for f in root.rglob(ext)
             if "artifacts" not in f.parts),
            None,
        )
        if primary_html:
            try:
                relative = primary_html.resolve().relative_to(WORKSPACE_DIR.resolve()).as_posix()
                screenshot_result = _screenshot_page(relative)
                if not screenshot_result.startswith("__SCREENSHOT_BUNDLE__:"):
                    checks.append((relative, "visual_capture", "fail", screenshot_result[:1000]))
                else:
                    bundle = json.loads(screenshot_result[len("__SCREENSHOT_BUNDLE__:"):])
                    logs = bundle.get("logs", {})
                    browser_errors = (
                        list(logs.get("console", []))
                        + list(logs.get("pageerror", []))
                        + list(logs.get("requestfailed", []))
                    )
                    checks.append((
                        relative, "browser_diagnostics",
                        "fail" if browser_errors else "pass",
                        "; ".join(str(error) for error in browser_errors[:8]),
                    ))
                    results = bundle.get("results", [])
                    blank = bool(results) and all(item.get("is_blank") for item in results)
                    checks.append((
                        relative, "pixel_content", "fail" if blank else "pass",
                        "所有視角皆低於非背景像素門檻" if blank else "像素統計未顯示全白／全空畫面",
                    ))
            except Exception as exc:
                checks.append(("browser", "visual_capture", "fail", str(exc)))
    if not checks: return "[未找到任何前端檔案]"
    lines = ["===== Frontend Check =====", ""]; passed, failed, failures = 0, 0, []
    for file, name, status, message in checks:
        lines.append(f"{file}::{name} {'PASSED' if status == 'pass' else 'FAILED'}")
        if status == "pass": passed += 1
        else: failed += 1; failures.append((file, name, message))
    lines.append("")
    if failures:
        lines.append("=============================== FAILURES ===============================")
        for file, name, message in failures: lines.append(f"___________________________ {file}::{name} ___________________________\n{message}\n")
    lines.append(f"{passed} passed, {failed} failed")
    return "\n".join(lines)


def _check_frontend_runtime(file_urls: list[str]) -> list[tuple[str, str, str, str]]:
    """Run local pages in Playwright and capture browser/runtime failures."""
    script_path = WORKSPACE_DIR / "_frontend_runtime_check.js"
    script = r'''const { chromium } = require('playwright');
const pages = __PAGE_URLS__;
(async () => {
  let browser;
  try {
    browser = await chromium.launch({ args: __BROWSER_ARGS__ });
    for (const url of pages) {
      const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
      const errors = [];
      page.on('pageerror', e => errors.push('pageerror: ' + e.message));
      page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      page.on('requestfailed', r => errors.push('requestfailed: ' + r.url() + ' ' + (r.failure()?.errorText || '')));
      try {
        const response = await page.goto(url, { waitUntil: 'load', timeout: __LOAD_TIMEOUT__ });
        if (!response || !response.ok()) errors.push('page load failed: ' + (response && response.status()));
        await page.waitForTimeout(__RUNTIME_WAIT__);
        await page.waitForFunction(() => window.__READY__ === true, null, { timeout: __READY_TIMEOUT__ }).catch(() => {});
        const state = await page.evaluate(() => ({
          hasSceneKit: typeof window.__sceneInfo === 'function',
          ready: window.__READY__ === true,
          objects: typeof window.__sceneInfo === 'function' ? window.__sceneInfo() : null
        }));
        if (state.hasSceneKit && !state.ready) errors.push('SceneKit did not signal __READY__');
        if (state.hasSceneKit && (!state.objects || !Object.keys(state.objects).length)) errors.push('__sceneInfo has no meshes');
        console.log('RUNTIME_RESULT:' + JSON.stringify({ url, errors, state }));
      } catch (e) {
        console.log('RUNTIME_RESULT:' + JSON.stringify({ url, errors: errors.concat(e.message), state: null }));
      } finally { await page.close(); }
    }
  } catch (e) {
    console.log('RUNTIME_ERROR:' + e.message);
    process.exitCode = 2;
  } finally { if (browser) await browser.close(); }
})();'''.replace("__PAGE_URLS__", json.dumps(file_urls))
    script = (script.replace("__BROWSER_ARGS__", json.dumps(list(BROWSER_LAUNCH_ARGS)))
              .replace("__LOAD_TIMEOUT__", str(BROWSER_PAGE_LOAD_TIMEOUT_MS))
              .replace("__RUNTIME_WAIT__", str(FRONTEND_RUNTIME_WAIT_MS))
              .replace("__READY_TIMEOUT__", str(BROWSER_READY_TIMEOUT_MS)))
    script_path.write_text(script, encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", str(script_path)], capture_output=True, text=True,
            timeout=FRONTEND_RUNTIME_TIMEOUT_SECONDS, encoding="utf-8", errors="replace",
            cwd=str(WORKSPACE_DIR), env=_subprocess_env(),
        )
        checks = []
        runtime_results = []
        for line in (result.stdout or "").splitlines():
            if line.startswith("RUNTIME_RESULT:"):
                try:
                    runtime_results.append(json.loads(line[len("RUNTIME_RESULT:"):]))
                except json.JSONDecodeError:
                    pass
            elif line.startswith("RUNTIME_ERROR:"):
                checks.append(("browser", "runtime", "fail", line[len("RUNTIME_ERROR:"):]))
        for item in runtime_results:
            file_name = item.get("url", "page").split("/")[-1]
            errors = item.get("errors") or []
            checks.append((file_name, "runtime", "fail" if errors else "pass", "; ".join(errors[:8])))
        if not runtime_results and not checks:
            details = (result.stderr or result.stdout or "Playwright 未回傳執行結果")[-1200:]
            checks.append(("browser", "runtime", "fail", details))
        return checks
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return [("browser", "runtime", "fail", str(exc))]
    finally:
        try: script_path.unlink()
        except OSError: pass


# ---------- 截圖（增加等待時間確保 WebGL 渲染）----------
def _screenshot_page(path: str = "index.html",
                    width: int = VISUAL_SCREENSHOT_WIDTH,
                    height: int = VISUAL_SCREENSHOT_HEIGHT,
                    views: list[str] | None = None) -> str:
    import base64
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import quote
    safe_path(path)
    html_path = WORKSPACE_DIR / path
    if not html_path.exists():
        return f"[檔案不存在: {path}]"
    width = max(320, min(int(width), VISUAL_SCREENSHOT_WIDTH))
    height = max(240, min(int(height), VISUAL_SCREENSHOT_HEIGHT))
    relative_path = html_path.resolve().relative_to(WORKSPACE_DIR.resolve()).as_posix()
    supported_views = ("front", "side", "top", "iso")
    requested_views = [view for view in (views or supported_views) if view in supported_views]
    views = (requested_views or ["iso"])[:max(1, VISUAL_SCREENSHOT_MAX_IMAGES)]
    screenshot_dir = WORKSPACE_DIR / "_visual_evidence"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    script_path = WORKSPACE_DIR / "_screenshot.js"
    script = r'''const { chromium } = require('playwright');
const fs = require('fs');
const pageUrl = __PAGE_URL__;
const width = __WIDTH__, height = __HEIGHT__;
const names = __VIEWS__;
(async () => {
  let browser;
  try {
    browser = await chromium.launch({ args: __BROWSER_ARGS__ });
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
    const logs = { console: [], warnings: [], pageerror: [], requestfailed: [] };
    page.on('console', m => {
      if (m.type() === 'error') logs.console.push(m.text());
      if (m.type() === 'warning') logs.warnings.push(m.text());
    });
    page.on('pageerror', e => logs.pageerror.push(e.message));
    page.on('requestfailed', r => logs.requestfailed.push(r.url() + ' ' + (r.failure()?.errorText || '')));
    const response = await page.goto(pageUrl, { waitUntil: 'load', timeout: __LOAD_TIMEOUT__ });
    if (!response || !response.ok()) logs.pageerror.push('Page load failed: ' + (response && response.status()));
    await page.waitForTimeout(__WAIT_MS__);
    let readyTimedOut = false;
    await page.waitForFunction(() => window.__READY__ === true, null,
      { timeout: __READY_TIMEOUT__ }).catch(() => { readyTimedOut = true; });
    const state = await page.evaluate(timedOut => ({
      ready: window.__READY__ === true,
      readyTimedOut: timedOut,
      hasSceneKit: typeof window.__sceneInfo === 'function',
      hasSetView: typeof window.__setView === 'function',
      objects: typeof window.__sceneInfo === 'function' ? window.__sceneInfo() : null
    }), readyTimedOut);
    const results = [];
    const captures = state.hasSetView ? names : ['default'];
    for (const name of captures) {
      if (state.hasSetView) {
        await page.evaluate(view => window.__setView(view), name);
        await page.waitForTimeout(__VIEW_WAIT__);
      }
      const filename = '__SCREEN_DIR__/' + name + '.jpg';
      let quality = __JPEG_QUALITY__;
      let imageBuffer = await page.screenshot({ type: 'jpeg', quality, fullPage: false });
      while (imageBuffer.length > __MAX_IMAGE_BYTES__ && quality > 35) {
        quality = Math.max(35, quality - 10);
        imageBuffer = await page.screenshot({ type: 'jpeg', quality, fullPage: false });
      }
      let scaledWidth = width, scaledHeight = height;
      while (imageBuffer.length > __MAX_IMAGE_BYTES__ && scaledWidth > 320) {
        scaledWidth = Math.max(320, Math.floor(scaledWidth * 0.8));
        scaledHeight = Math.max(240, Math.floor(scaledHeight * 0.8));
        const resized = await page.evaluate(async ({ dataUrl, targetWidth, targetHeight }) => {
          const image = new Image();
          image.src = dataUrl;
          await new Promise((resolve, reject) => {
            image.onload = resolve;
            image.onerror = reject;
          });
          const canvas = document.createElement('canvas');
          canvas.width = targetWidth; canvas.height = targetHeight;
          canvas.getContext('2d').drawImage(image, 0, 0, targetWidth, targetHeight);
          return canvas.toDataURL('image/jpeg', 0.65).split(',')[1];
        }, {
          dataUrl: 'data:image/jpeg;base64,' + imageBuffer.toString('base64'),
          targetWidth: scaledWidth,
          targetHeight: scaledHeight
        });
        imageBuffer = Buffer.from(resized, 'base64');
      }
      if (imageBuffer.length > __MAX_IMAGE_BYTES__) {
        throw new Error('Screenshot exceeds configured image byte limit');
      }
      fs.writeFileSync(filename, imageBuffer);
      const pixelRatio = await page.evaluate(async dataUrl => {
        const image = new Image();
        image.src = dataUrl;
        await new Promise((resolve, reject) => {
          image.onload = resolve;
          image.onerror = reject;
        });
        const canvas = document.createElement('canvas');
        canvas.width = 80; canvas.height = 60;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
        const background = [pixels[0], pixels[1], pixels[2]];
        let changed = 0;
        for (let i = 0; i < pixels.length; i += 4) {
          if (Math.abs(pixels[i] - background[0]) +
              Math.abs(pixels[i + 1] - background[1]) +
              Math.abs(pixels[i + 2] - background[2]) > 36) changed++;
        }
        return changed / (canvas.width * canvas.height);
      }, 'data:image/jpeg;base64,' + imageBuffer.toString('base64'));
      results.push({
        view: name, path: filename,
        non_background_ratio: Math.round(pixelRatio * 10000) / 10000,
        is_blank: pixelRatio < __EMPTY_PIXEL_RATIO__,
        encoded_bytes: imageBuffer.length
      });
    }
    console.log('SCREENSHOT_RESULT:' + JSON.stringify({ state, logs, results }));
  } catch (e) {
    console.error('SCREENSHOT_ERROR:' + e.stack);
    process.exitCode = 1;
    } finally { if (browser) await browser.close(); }
})();'''
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            return

    handler = functools.partial(QuietHandler, directory=str(WORKSPACE_DIR.resolve()))
    local_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=local_server.serve_forever, daemon=True)
    server_thread.start()
    html_url = f"http://127.0.0.1:{local_server.server_port}/{quote(relative_path)}"
    script = (script.replace("__PAGE_URL__", json.dumps(html_url))
              .replace("__WIDTH__", str(width)).replace("__HEIGHT__", str(height))
              .replace("__VIEWS__", json.dumps(views))
              .replace("__SCREEN_DIR__", screenshot_dir.as_posix().replace("'", "\\'"))
              .replace("__READY_TIMEOUT__", str(VISUAL_SCREENSHOT_READY_TIMEOUT_MS))
              .replace("__WAIT_MS__", str(VISUAL_SCREENSHOT_WAIT_MS))
              .replace("__VIEW_WAIT__", str(VISUAL_VIEW_SWITCH_WAIT_MS))
              .replace("__LOAD_TIMEOUT__", str(BROWSER_PAGE_LOAD_TIMEOUT_MS))
              .replace("__JPEG_QUALITY__", str(max(1, min(100, VISUAL_SCREENSHOT_JPEG_QUALITY))))
              .replace("__MAX_IMAGE_BYTES__", str(VISUAL_IMAGE_MAX_BYTES))
              .replace("__EMPTY_PIXEL_RATIO__", str(VISUAL_EMPTY_PIXEL_RATIO))
              .replace("__BROWSER_ARGS__", json.dumps(list(BROWSER_LAUNCH_ARGS))))
    try:
        script_path.write_text(script, encoding="utf-8")
        run = subprocess.run(
            ["node", str(script_path)], capture_output=True, text=True,
            timeout=SCREENSHOT_SUBPROCESS_TIMEOUT_SECONDS,
            encoding="utf-8", errors="replace", cwd=str(WORKSPACE_DIR), env=_subprocess_env(),
        )
        if run.returncode:
            return f"[截圖失敗: {(run.stderr or run.stdout or '')[-2000:]}]"
        payload = None
        for line in (run.stdout or "").splitlines():
            if line.startswith("SCREENSHOT_RESULT:"):
                try:
                    payload = json.loads(line[len("SCREENSHOT_RESULT:"):])
                except json.JSONDecodeError:
                    pass
        if not payload:
            return f"[截圖失敗: 瀏覽器未回傳結果 {(run.stderr or '')[-1000:]}]"
        try:
            from PIL import Image
            from io import BytesIO
            for item in payload.get("results", []):
                image_path = Path(item["path"])
                with Image.open(image_path) as image:
                    rgb = image.convert("RGB").resize((80, 60))
                    background = rgb.getpixel((0, 0))
                    pixels = list(rgb.getdata())
                    changed = sum(1 for px in pixels if sum(abs(px[i] - background[i]) for i in range(3)) > 36)
                    item["non_background_ratio"] = round(changed / max(len(pixels), 1), 4)
                    item["is_blank"] = item["non_background_ratio"] < VISUAL_EMPTY_PIXEL_RATIO
                    full_image = Image.open(image_path).convert("RGB")
                    quality = max(35, min(100, VISUAL_SCREENSHOT_JPEG_QUALITY))
                    encoded = b""
                    for _ in range(5):
                        buffer = BytesIO()
                        full_image.save(buffer, format="JPEG", quality=quality, optimize=True)
                        encoded = buffer.getvalue()
                        if len(encoded) <= VISUAL_IMAGE_MAX_BYTES:
                            break
                        full_image = full_image.resize(
                            (max(320, int(full_image.width * 0.8)),
                             max(240, int(full_image.height * 0.8)))
                        )
                        quality = max(35, quality - 10)
                    item["data"] = base64.b64encode(encoded).decode("ascii")
                    item["encoded_bytes"] = len(encoded)
                    full_image.close()
                image_path.unlink(missing_ok=True)
        except Exception as exc:
            payload.setdefault("logs", {}).setdefault("diagnostics", []).append(f"PIL image optimization unavailable; using browser pixel analysis: {exc}")
            for item in payload.get("results", []):
                image_path = Path(item["path"])
                if image_path.exists():
                    item["data"] = base64.b64encode(image_path.read_bytes()).decode("ascii")
                    image_path.unlink(missing_ok=True)
        logger.info("screenshot_captured", path=path, views=len(payload.get("results", [])))
        return "__SCREENSHOT_BUNDLE__:" + json.dumps(payload, ensure_ascii=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return f"[截圖失敗: {exc}]"
    finally:
        local_server.shutdown()
        local_server.server_close()
        server_thread.join(timeout=2)
        try: script_path.unlink()
        except OSError: pass
        try: screenshot_dir.rmdir()
        except OSError: pass

def _run_latest_artifact() -> str:
    if not ARTIFACT_DIR.exists(): return "[無工件]"
    candidates = list(ARTIFACT_DIR.rglob("*.py"))
    if not candidates: return "[無工件]"
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return _run_python(candidates[0].read_text(encoding="utf-8"))

def _search_docs(query: str, top_k: int = 3) -> str:
    from rag import search
    hits = search(query, top_k=int(top_k))
    return "\n\n".join(f"[{h['source']} | {h['score']:.2f}]\n{h['text']}" for h in hits) if hits else "[未檢索到相關文件]"


_HANDLERS = {
    "list_dir": _list_dir, "read_file": _read_file, "write_file": _write_file, "edit_file": _edit_file,
    "run_python": _run_python, "run_tests": _run_tests, "get_test_summary": _get_test_summary,
    "run_lint": _run_lint, "run_frontend_check": _run_frontend_check, "screenshot_page": _screenshot_page,
    "run_latest_artifact": _run_latest_artifact, "search_docs": _search_docs,
}


def execute(tool_name: str, args: dict) -> str:
    if tool_name not in _HANDLERS: return f"[未知工具: {tool_name}]"
    try:
        logger.info("tool_call", tool=tool_name, args=args)
        return _HANDLERS[tool_name](**(args or {}))
    except GuardrailError as e: return f"[護欄攔截: {e}]"
    except TypeError as e: return f"[參數錯誤: {e}]"
    except Exception as e:
        logger.error("tool_error", tool=tool_name, error=str(e))
        return f"[工具執行失敗: {e}]"

def schemas_for(tool_names: list[str]) -> str:
    lines = []
    for name in tool_names:
        s = TOOL_SCHEMAS.get(name)
        if not s: continue
        if not s["args"]: lines.append(f"- {name}: {s['description']} 無需參數。")
        else: lines.append(f"- {name}: {s['description']} 參數：{'；'.join(f'{k}: {v}' for k, v in s['args'].items())}")
    return "\n".join(lines)


# ---------- Tool call parsing ----------
def _find_matching_brace(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "{": return None
    depth, in_string, escape, i = 0, False, False, start
    while i < len(text):
        c = text[i]
        if escape: escape = False
        elif c == "\\": escape = True
        elif in_string:
            if c == '"': in_string = False
        else:
            if c == '"': in_string = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0: return i
        i += 1
    return None

def _try_parse_json(raw: str):
    import json
    if not raw: return None
    raw = raw.strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict): return data
    except Exception: pass
    open_braces = raw.count("{") - raw.count("}")
    if open_braces > 0:
        try:
            data = json.loads(raw + "}" * open_braces)
            if isinstance(data, dict): return data
        except Exception: pass
    candidates, quote_count, i = [], 0, 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw): i += 2; continue
        if raw[i] == '"': quote_count += 1
        i += 1
    if quote_count % 2 == 1:
        candidates.append(raw + '"')
        if open_braces > 0: candidates.append(raw + '"' + "}" * open_braces)
    trimmed = raw.rstrip()
    for cut in range(0, min(120, len(trimmed)), 10):
        if cut == 0: continue
        candidates.append(trimmed[: len(trimmed) - cut])
    for c in candidates:
        for attempt in (c, c + "}", c.rstrip("}") + "}"):
            try:
                data = json.loads(attempt)
                if isinstance(data, dict): return data
            except Exception: continue
    return None

def _find_gemma_tool_calls(text: str) -> list[tuple[str, dict]]:
    results = []
    for start in [m.start() for m in re.finditer(r"<\|tool_call>", text)]:
        segment = text[start:]
        m = re.match(r"<\|tool_call>\s*call:\s*([A-Za-z_][A-Za-z0-9_]*)", segment)
        if not m: continue
        name, rest = m.group(1).strip(), segment[m.end():]
        brace_idx = rest.find("{")
        if brace_idx < 0: continue
        abs_start = start + m.end() + brace_idx
        abs_end = _find_matching_brace(text, abs_start)
        raw = text[abs_start:] if abs_end is None else text[abs_start:abs_end + 1]
        args = _try_parse_json(raw)
        if args is not None: results.append((name, args))
    return results

def _find_system_tool_calls(text: str) -> list[tuple[str, dict]]:
    results = []
    for m in re.finditer(r"<tool_call>\s*(\{)", text, re.DOTALL):
        brace_start = m.start(1)
        end = _find_matching_brace(text, brace_start)
        if end is None: continue
        data = _try_parse_json(text[brace_start:end + 1])
        if data and "name" in data: results.append((data["name"], data.get("args", {}) or {}))
    return results

def parse_tool_call(text: str):
    if not text: return None
    sys_calls = _find_system_tool_calls(text)
    if sys_calls: return sys_calls[-1]
    gemma_calls = _find_gemma_tool_calls(text)
    if gemma_calls: return gemma_calls[-1]
    for m in reversed(list(re.finditer(r"call:\s*([A-Za-z_][A-Za-z0-9_]*)\s*\{", text))):
        name, brace_start = m.group(1).strip(), m.end() - 1
        end = _find_matching_brace(text, brace_start)
        raw = text[brace_start:] if end is None else text[brace_start:end + 1]
        args = _try_parse_json(raw)
        if args is not None: return name, args
    return None

def parse_verdict_tag(text: str) -> str | None:
    if not text: return None
    for p in [r"<verdict>\s*(PASS|REVISE)\s*</verdict\s*>?", r"\[verdict\]\s*(PASS|REVISE)\s*\[/verdict\s*\]?", r"<verdict>\s*(PASS|REVISE)\s*$"]:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m: return m.group(1).upper()
    return None
