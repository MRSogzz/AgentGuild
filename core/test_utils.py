"""測試相關工具：檔案凍結、數字提取、系統校驗推送。"""
import re
import time

from config import WORKSPACE_DIR
from observability import logger


def is_test_file(name: str) -> bool:
    base = name.split("/")[-1]
    return base.startswith("test_") or base.endswith("_test.py")


def write_frozen_tests(frozen: dict[str, str]):
    """把凍結測試寫到 workspace 磁碟，覆蓋 Dev 的任何改動。"""
    for fname, content in frozen.items():
        path = WORKSPACE_DIR / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def extract_test_summary(text: str) -> tuple[int, int] | None:
    """
    從 pytest 或前端檢查的原始輸出提取 (passed, failed)。
    優先逐行統計，退回匯總行。返回 None 表示無法判定。
    """
    if not text:
        return None

    passed = len(re.findall(r"::\S+\s+PASSED\b", text, re.IGNORECASE))
    failed = len(re.findall(r"::\S+\s+FAILED\b", text, re.IGNORECASE))
    if passed > 0 or failed > 0:
        return (passed, failed)

    p, f = 0, 0
    m = re.search(r"(?:^|=|\s)(\d+)\s+passed\b", text,
                  re.IGNORECASE | re.MULTILINE)
    if m:
        p = int(m.group(1))
    m = re.search(r"(?:^|=|\s)(\d+)\s+failed\b", text,
                  re.IGNORECASE | re.MULTILINE)
    if m:
        f = int(m.group(1))
    if p or f:
        return (p, f)
    return None


# ============================================================
# ★ 新增：健壯解析 pytest 輸出（給 QA 與後端守門用）
# ============================================================
def parse_pytest_output(output: str) -> dict:
    """
    解析 pytest 輸出，抽出 pass/fail 統計與失敗清單。

    回傳：
        {
            "raw": str,
            "total": int,
            "passed": int,
            "failed": int,
            "errors": int,
            "all_passed": bool,     # 有測試且全通過才 True
            "saw_pytest": bool,     # 是否真的看到 pytest 摘要
            "failures": [{"test": str, "message": str}],
            "summary_line": str | None,
        }
    """
    result = {
        "raw": output or "",
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "all_passed": False,
        "saw_pytest": False,
        "failures": [],
        "summary_line": None,
    }
    if not output:
        return result

    # 1) 抓摘要行：「= 2 failed, 6 passed in 0.08s =」
    m = re.search(
        r"^=+\s*(.+?)\s+in\s+[\d.]+s\s*=+\s*$",
        output,
        re.MULTILINE,
    )
    if m:
        result["saw_pytest"] = True
        result["summary_line"] = m.group(1).strip()
        for cm in re.finditer(
            r"(\d+)\s+(passed|failed|errors?|skipped)",
            result["summary_line"],
        ):
            n = int(cm.group(1))
            kind = cm.group(2)
            if kind == "passed":
                result["passed"] = n
            elif kind == "failed":
                result["failed"] = n
            elif kind in ("error", "errors"):
                result["errors"] = n
        result["total"] = (
            result["passed"] + result["failed"] + result["errors"]
        )
        result["all_passed"] = (
            result["total"] > 0
            and result["failed"] == 0
            and result["errors"] == 0
        )

    # 2) 抓失敗清單：「FAILED test_x.py::test_name - message」
    for fm in re.finditer(r"^FAILED\s+(.+)$", output, re.MULTILINE):
        line_content = fm.group(1).strip()
        parts = line_content.split(" - ", 1)
        result["failures"].append({
            "test": parts[0].strip(),
            "message": parts[1].strip() if len(parts) > 1 else "",
        })

    # 3) fallback：逐行數 PASSED / FAILED
    if not result["saw_pytest"]:
        for line in output.splitlines():
            if " PASSED" in line:
                result["passed"] += 1
                result["saw_pytest"] = True
            elif " FAILED" in line:
                result["failed"] += 1
                result["saw_pytest"] = True
            elif " ERROR" in line:
                result["errors"] += 1
                result["saw_pytest"] = True
        result["total"] = (
            result["passed"] + result["failed"] + result["errors"]
        )
        result["all_passed"] = (
            result["total"] > 0
            and result["failed"] == 0
            and result["errors"] == 0
        )

    return result


def detect_project_kind() -> dict:
    """
    掃描 workspace，判斷專案類型並回傳摘要。
    """
    py_tests = [p for p in WORKSPACE_DIR.glob("test_*.py")]
    py_files = [p for p in WORKSPACE_DIR.glob("*.py")
                if not p.name.startswith("test_")]
    html_files = list(WORKSPACE_DIR.glob("*.html"))
    css_files = list(WORKSPACE_DIR.glob("*.css"))
    js_files = list(WORKSPACE_DIR.glob("*.js"))

    files_summary = []
    if py_files:
        files_summary.append(f"Python: {', '.join(p.name for p in py_files)}")
    if py_tests:
        files_summary.append(f"測試: {', '.join(p.name for p in py_tests)}")
    if html_files:
        files_summary.append(f"HTML: {', '.join(p.name for p in html_files)}")
    if css_files:
        files_summary.append(f"CSS: {', '.join(p.name for p in css_files)}")
    if js_files:
        files_summary.append(f"JS: {', '.join(p.name for p in js_files)}")

    has_python = bool(py_files or py_tests)
    has_frontend = bool(html_files or css_files or js_files)

    if has_python and has_frontend:
        kind = "mixed"
    elif has_python:
        kind = "python"
    elif has_frontend:
        kind = "frontend"
    else:
        kind = "empty"

    return {
        "kind": kind,
        "summary": "\n".join(files_summary) if files_summary else "(工作區為空)",
    }


def auto_run_tests_for_project(project_info: dict,
                                task_skills: list[str] | None = None) -> list[dict]:
    """
    根據 task_skills 與 project_info 決定跑什麼測試工具。

    優先順序：
      1. task_skills 明確指定 python → 只跑 run_tests（pytest）
      2. task_skills 明確指定 frontend/visual/visual3d
         → 只跑 run_frontend_check
      3. task_skills 為空 → 回退到看 project_info
         - python / mixed → run_tests
         - frontend / mixed → run_frontend_check
    """
    from tools import execute as execute_tool

    task_skills = task_skills or []
    is_python_task = "python" in task_skills
    is_frontend_task = any(s in task_skills
                            for s in ("frontend", "visual", "visual3d"))

    tools_to_run: list[str] = []

    if is_python_task and not is_frontend_task:
        tools_to_run = ["run_tests"]
    elif is_frontend_task and not is_python_task:
        tools_to_run = ["run_frontend_check"]
    elif is_python_task and is_frontend_task:
        tools_to_run = ["run_tests", "run_frontend_check"]
    else:
        kind = project_info.get("kind", "empty")
        if kind in ("python", "mixed"):
            tools_to_run.append("run_tests")
        if kind in ("frontend", "mixed"):
            tools_to_run.append("run_frontend_check")

    results = []
    for tool_name in tools_to_run:
        t0 = time.time()
        try:
            result = execute_tool(tool_name, {})
        except Exception as e:
            logger.error("auto_run_failed",
                         tool=tool_name, error=str(e))
            result = f"[自動執行失敗: {e}]"
        results.append({
            "name": tool_name,
            "args": {},
            "state": "done",
            "result": result,
            "duration_ms": int((time.time() - t0) * 1000),
        })

    logger.info("auto_run_tests_done",
                task_skills=task_skills,
                tools=[r["name"] for r in results],
                count=len(results))
    return results


# ============================================================
# ★ 重寫：把 pass/fail 明確放在最上方，讓 QA 無法迴避
# ============================================================
def build_qa_analysis_context(project_info: dict,
                               auto_results: list[dict],
                               task_skills: list[str] | None = None) -> str:
    """
    構建給 QA 的分析上下文。

    ★ 關鍵修正：
      - 把真實 pass/fail 摘要放在最頂端（QA 一定先看到）
      - 失敗時列出失敗測試名稱與錯誤訊息
      - 加入強制判定規則，禁止 QA 在測試失敗時說「全部通過」
      - 原始輸出仍保留在下方，但縮短避免注意力被分散
    """
    task_skills = task_skills or []

    if any(skill in task_skills for skill in ("frontend", "visual", "visual3d")):
        lines = [
            "[前端自動檢查結果]",
            "此任務以 run_frontend_check 的 HTML/CSS/JS 語法、頁面載入及瀏覽器執行期結果為依據；"
            "不要求 pytest。自動檢查不代表視覺品質已驗收。",
        ]
        if not auto_results:
            lines.append("狀態：BLOCKED，系統沒有執行前端檢查。")
        else:
            for result in auto_results:
                raw = result.get("result", "") or ""
                count = re.search(r"(\d+)\s+passed,\s*(\d+)\s+failed", raw, re.IGNORECASE)
                if result.get("name") != "run_frontend_check" or not count:
                    lines.append(f"{result.get('name', '檢查')}：結果無法判讀。")
                    continue
                passed, failed = int(count.group(1)), int(count.group(2))
                lines.append(f"狀態：{'PASS' if failed == 0 else 'FAIL'}；{passed} passed, {failed} failed")
                lines.append(raw[-2500:])
        lines.append("請核對語法與 runtime 檢查結果；若任一失敗則 changes_requested，檢查缺漏則 blocked。")
        lines.append("最後只輸出 QA JSON 契約，不要補跑工具或評價視覺畫面。")
        return "\n\n".join(lines)

    # ── 先彙總所有 pytest 輸出 ──
    any_pytest = False
    overall_passed = 0
    overall_failed = 0
    failures_detail: list[tuple[str, str, str]] = []

    for r in auto_results:
        parsed = parse_pytest_output(r.get("result", "") or "")
        if not parsed["saw_pytest"]:
            continue
        any_pytest = True
        overall_passed += parsed["passed"]
        overall_failed += parsed["failed"] + parsed["errors"]
        for f in parsed["failures"]:
            failures_detail.append((
                r.get("name", "?"),
                f["test"],
                f["message"],
            ))

    lines: list[str] = []

    # ── ① 頂端摘要（最重要） ──
    lines.append("╔══════════════════════════════════════════════════════╗")
    lines.append("║  系統自動測試結果（QA 必須依此判定，不可臆測）        ║")
    lines.append("╚══════════════════════════════════════════════════════╝")

    if any_pytest:
        if overall_failed > 0:
            lines.append(
                f"❌ 測試狀態：**部分失敗** —— 通過 {overall_passed} 個，"
                f"失敗 {overall_failed} 個"
            )
            lines.append("")
            lines.append("### 失敗清單（你必須在報告中逐項列出）")
            for tool, test, msg in failures_detail:
                lines.append(f"- [{tool}] `{test}`")
                if msg:
                    lines.append(f"  - 錯誤訊息：`{msg}`")
        else:
            lines.append(
                f"✅ 測試狀態：**全部通過** —— 通過 {overall_passed} 個，"
                f"失敗 0 個"
            )
    else:
        lines.append("⚠️ 沒有偵測到 pytest 摘要。你必須回報「無法驗證」。")

    lines.append("")
    lines.append("### 判定規則（強制）")
    lines.append("1. 你**只能**根據上方「系統自動測試結果」判定 pass/fail。")
    lines.append("2. 若上方顯示「部分失敗」，你的結論必須是「測試未通過」，"
                 "並逐項列出失敗測試名稱與錯誤訊息。")
    lines.append("3. 讀下方測試原始碼只能幫你理解測試意圖，"
                 "**不能**作為「測試通過」的依據。")
    lines.append("4. **禁止**在測試失敗時說「全部通過」或「所有測試均通過」。")
    lines.append("5. 若無法判定，請明確說「無法驗證」，不要猜測。")
    lines.append("")
    lines.append("─" * 56)
    lines.append("")

    # ── ② 工作區與技能資訊 ──
    lines.append("[工作區結構]")
    lines.append(project_info["summary"])
    lines.append(f"專案類型：{project_info['kind']}")
    lines.append(
        f"本次任務技能：{', '.join(task_skills) if task_skills else '(未指定)'}"
    )
    lines.append("")

    # ── ③ 原始輸出（截斷避免注意力分散） ──
    lines.append("[原始測試輸出（僅供參考，判定請以上方摘要為準）]")
    if not auto_results:
        lines.append("(本次任務不需要或無法執行測試)")
    else:
        for r in auto_results:
            lines.append(f"===== {r['name']} =====")
            raw = r.get("result", "") or ""
            if len(raw) > 4000:
                lines.append(raw[:4000])
                lines.append(f"...[原始輸出截斷，共 {len(raw)} 字]...")
            else:
                lines.append(raw)
            lines.append("")

    # ── ④ 任務說明 ──
    lines.append("[你的任務]")
    lines.append("系統已經跑過測試。你只需要：")
    lines.append("1. **先**看最上方的「系統自動測試結果」，確定 pass/fail。")
    lines.append("2. 對照 PM 的需求清單，指出哪些需求已被測試覆蓋。")
    lines.append("3. 若有失敗，逐項列出失敗測試名稱與錯誤訊息。")
    lines.append("4. 給出簡短建議。")
    lines.append("")
    lines.append("**不要自己呼叫測試工具**——測試已經跑過了。")
    lines.append("**若測試與任務不匹配，明確標記 [BLOCKER]**，"
                 "不要試圖把無關的測試結果硬套到需求上。")

    return "\n".join(lines)


async def push_test_verification(emit, res: dict):
    """從測試工具輸出提取真實數字，推送系統校驗消息。"""
    test_tools_seen = []
    for t in res.get("tools_called", []):
        if t.get("name") not in ("run_tests", "run_frontend_check"):
            continue
        if t.get("state") != "done":
            continue
        test_tools_seen.append(t.get("name"))

        summary = extract_test_summary(t.get("result") or "")
        if not summary:
            continue
        p, f = summary
        badge = "✅ 全部通過" if f == 0 else f"⚠️ {f} 項失敗"
        tool_label = "pytest" if t["name"] == "run_tests" else "前端檢查"
        await emit({
            "type": "system",
            "content": f"🔎 系統校驗（{tool_label}）：{p} 通過 · {f} 失敗 — {badge}",
        })
        logger.info("test_verification_injected",
                    tool=t["name"], passed=p, failed=f)

    if not test_tools_seen:
        await emit({
            "type": "system",
            "content": "⚠️ 無測試結果",
        })
