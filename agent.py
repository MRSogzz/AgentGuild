import difflib
from contextlib import aclosing
import json
import re
import time
from pathlib import Path
from typing import AsyncGenerator

from config import (
    get_llm_client, MODEL_NAME,
    MAX_CONTINUATIONS, CONTINUATION_ANCHOR_CHARS,
    WORKSPACE_DIR, ARTIFACT_DIR,
    SYSTEM_BUDGET,
    JSON_CONTRACT_RETRIES, PATHOLOGICAL_REPETITION_RETRIES,
    PATHOLOGICAL_REPETITION_WINDOW, PATHOLOGICAL_REPEATED_CHAR_MIN,
    PATHOLOGICAL_CHAR_RATIO, PATHOLOGICAL_RETRY_FREQUENCY_PENALTY,
    LLM_STOP_SEQUENCES, VISUAL_QUALITY_MIN, VISUAL_QUALITY_DIMENSIONS,
    VISION_IMAGE_TOKEN_RESERVE,
)
from tools import schemas_for, parse_tool_call, execute
from memory import long_term
from observability import logger
from token_budget import (
    count_tokens, estimate_tokens, truncate_to_token_budget,
)
from prompts.builder import build_system_prompt
from prompts.skills import SKILL_ENVIRONMENT

client = get_llm_client()

CODE_BLOCK_WITH_PATH_RE = re.compile(
    r"```([a-zA-Z0-9_+-]*):([^\n`]+?)\s*\n([\s\S]*?)```",
    re.MULTILINE,
)
CODE_BLOCK_RE = re.compile(
    r"```([a-zA-Z0-9_+-]*)\s*\n([\s\S]*?)```",
    re.MULTILINE,
)


def _extract_summary_block(text: str, fallback_len: int = 1500) -> str:
    if not text:
        return ""
    m = re.search(r"<summary>([\s\S]*?)</summary>", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    from core.contracts import parse_agent_contract
    return parse_agent_contract(text).get("summary", "")


def _remove_overlap(existing: str, continuation: str,
                    min_overlap: int = 30, max_check: int = 3000) -> str:
    if not existing or not continuation:
        return continuation
    tail = existing[-max_check:]
    head = continuation[:max_check]
    sm = difflib.SequenceMatcher(None, tail, head, autojunk=False)
    match = sm.find_longest_match(0, len(tail), 0, len(head))
    if match.size >= min_overlap:
        if match.b <= max(50, len(head) // 5):
            cut = match.b + match.size
            logger.info("overlap_removed", matched=match.size, cut_at=cut)
            return continuation[cut:]
    return continuation


def analyze_truncation(text: str, finish_reason: str | None = None) -> dict:
    text = text.rstrip()
    result = {
        "truncated": False,
        "reason": None,
        "anchor": (text[-CONTINUATION_ANCHOR_CHARS:]
                   if len(text) > CONTINUATION_ANCHOR_CHARS else text),
    }
    if finish_reason == "length":
        result["truncated"] = True
    elif not text:
        return result
    else:
        if text.count("```") % 2 == 1:
            result["truncated"] = True
            result["reason"] = "unclosed_code_block"
            return result
        if text and text[-1] not in "。.!?！？\n`)}]；;：:":
            if re.search(r"[A-Za-z0-9_]$", text):
                result["truncated"] = True
                result["reason"] = "mid_word"
                return result
        return result

    if text.count("```") % 2 == 1:
        result["reason"] = "unclosed_code_block"
    elif text.count("<tool_call>") > text.count("</tool_call>"):
        result["reason"] = "unclosed_tool_call"
    elif re.search(r"[A-Za-z0-9_]$", text):
        result["reason"] = "mid_word"
    else:
        result["reason"] = "length"
    return result


def _has_code_block(text: str) -> bool:
    if not text:
        return False
    if CODE_BLOCK_WITH_PATH_RE.search(text):
        return True
    return "```" in text and text.count("```") >= 2


def _check_pathological_repetition(text: str) -> bool:
    if not text:
        return False
    tail = text[-PATHOLOGICAL_REPETITION_WINDOW:]
    if re.search(rf"([^\s])\1{{{max(1, PATHOLOGICAL_REPEATED_CHAR_MIN - 1)},}}", tail):
        return True
    ratio_window = min(100, PATHOLOGICAL_REPETITION_WINDOW)
    if len(tail) >= ratio_window:
        last_100 = tail[-ratio_window:]
        char_counts = {}
        for c in last_100:
            char_counts[c] = char_counts.get(c, 0) + 1
        max_count = max(char_counts.values())
        if max_count / len(last_100) > PATHOLOGICAL_CHAR_RATIO and ' ' not in char_counts:
            return True
    return False


def _build_empty_code_retry_hint(transcript: str,
                                task_skills: list[str] | None = None) -> str:
    file_candidates = re.findall(
        r"`?([A-Za-z0-9_\-./]+\.(?:py|js|ts|jsx|tsx|html|css|json|yaml|yml|md|sh))`?",
        transcript,
    )
    seen = set()
    uniq = []
    for f in file_candidates:
        base = f.split("/")[-1]
        if base in seen:
            continue
        seen.add(base)
        uniq.append(base)
    sample = uniq[:5]

    if sample:
        example_lines = []
        for name in sample:
            if name.endswith(".py"):
                lang = "python"
            elif name.endswith(".js") or name.endswith(".mjs"):
                lang = "javascript"
            elif name.endswith(".ts") or name.endswith(".tsx"):
                lang = "typescript"
            elif name.endswith(".html") or name.endswith(".htm"):
                lang = "html"
            elif name.endswith(".css"):
                lang = "css"
            elif name.endswith(".json"):
                lang = "json"
            else:
                lang = "text"
            example_lines.append(f"```{lang}:{name}\n<完整內容>\n```")
        example = "\n".join(example_lines)
    elif task_skills and any(s in task_skills for s in ("frontend", "visual", "visual3d")):
        example = (
            "```html:index.html\n<完整 HTML>\n```\n"
            "```css:styles.css\n<完整 CSS>\n```\n"
            "```javascript:app.js\n<完整 JavaScript>\n```"
        )
    elif task_skills and "python" in task_skills:
        example = (
            "```python:main.py\n<完整實作>\n```\n"
            "```python:test_main.py\n<完整測試>\n```"
        )
    else:
        example = "```語言:相對路徑/檔名\n<完整可執行檔案內容>\n```"

    return (
        "你的回覆沒有任何程式碼區塊。\n"
        "請依照使用者原始需求（以及若有提供的 PM 需求清單或實作規劃）"
        "直接產出完整、可執行的程式碼檔案。\n"
        "如果任務指定技術或檔名，請遵守；沒有指定時自行選擇最簡單合理的檔案結構。\n"
        "格式範例（請依實際需求替換檔名）：\n"
        f"{example}\n"
        "直接開始輸出程式碼。\n"
    )


def _select_agent_tools(agent, task_skills: list[str] | None,
                       is_refine: bool,
                       allow_workspace_tools: bool = False) -> list[str]:
    """Expose only tools required by this agent's current pipeline stage."""
    is_frontend = bool(task_skills and any(
        skill in task_skills for skill in ("frontend", "visual", "visual3d")
    ))
    if agent.id == "dev":
        # Sources are injected from shared state; Dev must not self-loop on file tools.
        return []
    if agent.id == "qa":
        # Tests are run by the pipeline before QA starts; QA only summarizes.
        return []
    if agent.id == "reviewer":
        # Visual evidence is captured by the orchestrator, not by an LLM tool call.
        return []
    if agent.id == "pm":
        return []
    return []


AGENT_SPECIFIC_RULES = {}


def _build_system(agent, task_skills: list[str] | None = None, is_refine: bool = False, current_session_id: str = "", allow_workspace_tools: bool = False, memory_hint: str = "") -> str:
    base = build_system_prompt(
        agent_id=agent.id, task_skills=task_skills, is_refine=is_refine, memory_hint=memory_hint,
    )

    if agent.id == "dev":
        if task_skills and any(s in task_skills for s in ("frontend", "visual", "visual3d")):
            base += (
                "\n\n## 你的角色：前端視覺工程師\n"
                "1. 你**不需要**撰寫任何 pytest 測試檔。\n"
                "2. 你的目標是產出能直接在瀏覽器打開的 HTML/CSS/JS。\n"
                "3. 依照本次需求檢查主體輪廓、比例、構圖、互動和視覺品質，不要套用其他任務的特定造型。\n"
            )
        elif task_skills and "python" in task_skills:
            base += (
                "\n\n## 你的角色：後端/演算法工程師\n"
                "1. 你**必須**為你的核心邏輯撰寫對應的 `test_xxx.py` (使用 pytest)。\n"
                "2. 測試必須覆蓋正常情況與邊界情況。\n"
            )
        else:
            base += (
                "\n\n## 未分類開發任務\n"
                "依 PM 規格選擇合適的實作語言與必要驗證方式，不要自行假設使用 Python 或前端框架。\n"
            )
    elif agent.id == "pm":
        base += (
            "\n\n## 新任務需求整理原則\n"
            "先把使用者需求轉成精簡、可驗收的規格，再交給 Dev。"
            "小型明確任務請直接採用合理預設，不要增加使用者未要求的功能，"
            "也不要為了形式提出澄清問題；只有缺少關鍵資訊、無法安全推定時才 blocked。"
            "規格保持精簡，列清楚交付物與可驗收條件即可。\n"
        )
        environment = [
            SKILL_ENVIRONMENT[skill]
            for skill in (task_skills or []) if skill in SKILL_ENVIRONMENT
        ]
        if environment:
            base += "\n\n## 本任務技術環境\n" + "\n".join(f"- {item}" for item in environment)

    if agent.id in AGENT_SPECIFIC_RULES:
        base += AGENT_SPECIFIC_RULES[agent.id]

    is_frontend_task = task_skills and any(s in task_skills for s in ("frontend", "visual", "visual3d"))
    if agent.id == "reviewer":
        if is_frontend_task:
            base += (
                "\n\n## 視覺驗收\n"
                "系統已附上實際截圖與場景統計，根據這些證據判斷是否符合使用者需求。"
                "不可只評語法或程式碼。將觀察寫入 JSON 的 `image_analysis` 欄位，"
                f"並在 JSON 輸出 1–10 的整數 `visual_quality`，達 {VISUAL_QUALITY_MIN} 分才可核准；"
                f"依設定評估維度 {', '.join(VISUAL_QUALITY_DIMENSIONS)}。"
                "未達門檻必須 changes_requested，提出具體視覺改進。可機器驗證的條件依輸入場景統計判斷，"
                "截圖用來評估構圖與觀感。逐項對照驗收標準輸出 checks，欄位為 criterion_id、criterion、布林值 pass、observation；"
                "畫面空白、全黑、破圖或截圖工具失敗時判 changes_requested。\n"
                "issues 必須列出可執行的修改事項，summary 不超過 300 字。最後只輸出 JSON。\n"
            )
        else:
            base += (
                "\n\n## 程式碼審查\n"
                "依使用者需求、實作與系統測試結果判斷。此任務不需要截圖或呼叫工具。"
                "明確列出阻擋問題；沒有阻擋問題才可 PASS。"
                "在 JSON 中輸出 verdict / issues / route_to / summary。最後只輸出 JSON。\n"
            )
    if agent.id == "qa" and is_frontend_task:
        base += (
            "\n\n## 前端 QA 限定職責\n"
            "只解讀系統提供的 `run_frontend_check` 結果，檢查 HTML/CSS/JS 語法與結構。\n"
            "不要撰寫或要求 pytest，也不要根據程式碼推論視覺效果或功能已完成。\n"
            "語法檢查通過時，summary 回報前端語法與結構通過；畫面交由 Reviewer 截圖驗收。只輸出 JSON。\n"
        )

    available_tools = _select_agent_tools(
        agent, task_skills, is_refine, allow_workspace_tools
    )
    if not available_tools:
        return base
    tool_desc = schemas_for(available_tools)
    protocol = (
        "\n\n你可以使用以下工具：\n"
        f"{tool_desc}\n\n"
        "需要調用工具時，嚴格按以下格式輸出：\n"
        "<tool_call>\n"
        '{"name": "工具名", "args": {"參數名": "值"}}\n'
        "</tool_call>\n"
    )
    return base + protocol


async def _stream_with_meta(messages: list[dict], max_tokens: int,
                            temperature: float, json_mode: bool = False,
                            frequency_penalty: float | None = None):
    request = {
        "model": MODEL_NAME, "messages": messages, "stream": True,
        "temperature": temperature, "max_tokens": max_tokens,
    }
    if LLM_STOP_SEQUENCES:
        request["stop"] = list(LLM_STOP_SEQUENCES)
    if frequency_penalty is not None:
        request["frequency_penalty"] = frequency_penalty
    if json_mode:
        request["response_format"] = {"type": "json_object"}
    try:
        stream = await client.chat.completions.create(**request)
    except Exception as exc:
        message = str(exc).lower()
        if not json_mode or not ("response_format" in message or "json_object" in message or isinstance(exc, TypeError)):
            raise
        logger.warn("json_mode_unsupported", error=str(exc)[:500])
        request.pop("response_format", None)
        stream = await client.chat.completions.create(**request)
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                yield {"token": delta.content, "finish_reason": None}
            if choice.finish_reason:
                yield {"token": "", "finish_reason": choice.finish_reason}
    finally:
        close = getattr(stream, "close", None)
        if close:
            await close()


def _extract_files(text: str) -> list[tuple[str, str, str]]:
    files = []
    for m in CODE_BLOCK_WITH_PATH_RE.finditer(text):
        files.append((m.group(2).strip(), m.group(1), m.group(3).rstrip("\n")))
    if not files:
        for m in CODE_BLOCK_RE.finditer(text):
            files.append(("", (m.group(1) or "python").strip(), m.group(2).rstrip("\n")))
    return files


def _sanitize_relpath(path: str) -> str:
    if not path:
        return ""
    path = path.replace("\\", "/").strip().lstrip("/")
    return "/".join(p for p in path.split("/") if p not in ("", ".", ".."))


EXT_MAP = {
    "python": ".py", "py": ".py", "html": ".html", "htm": ".html", "css": ".css",
    "scss": ".scss", "javascript": ".js", "js": ".js", "typescript": ".ts", "ts": ".ts",
    "json": ".json", "markdown": ".md", "md": ".md", "yaml": ".yaml", "yml": ".yaml",
}


def _fallback_name(code: str, index: int, lang: str = "") -> str:
    lang_lower = (lang or "").lower()
    if lang_lower in ("html", "htm"):
        return f"index_{index}.html" if index > 0 else "index.html"
    if lang_lower == "css":
        return f"styles_{index}.css" if index > 0 else "styles.css"
    if lang_lower in ("javascript", "js", "mjs"):
        return f"app_{index}.js" if index > 0 else "app.js"
    if lang_lower in ("python", "py", ""):
        if re.search(r"^\s*def\s+test_", code, re.MULTILINE):
            m = re.search(r"from\s+(\w+)\s+import", code)
            base = m.group(1) if m else "module"
            return f"test_{base}_{index}.py"
        m = re.search(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", code, re.MULTILINE)
        if m:
            return f"{m.group(1).lower()}_{index}.py"
        for m in re.finditer(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", code, re.MULTILINE):
            if not (m.group(1).startswith("__") and m.group(1).endswith("__")):
                return f"{m.group(1).lower()}_{index}.py"
        return f"snippet_{index}.py"
    return f"file_{index}.txt"


def _persist_artifacts(agent, text: str, session_id: str, round_no: int, version: int = 1) -> list[dict]:
    if agent.id != "dev":
        return []
    files = _extract_files(text)
    if not files:
        return []
    seen_norm, project_root, snapshot_root = set(), WORKSPACE_DIR, ARTIFACT_DIR / f"r{round_no}_v{version}"
    saved = []
    for i, (path, lang, code) in enumerate(files):
        if not code.strip():
            continue
        norm = "\n".join(line.rstrip() for line in code.strip().splitlines() if line.strip())
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        rel = _sanitize_relpath(path) or _fallback_name(code, i, lang)
        if "." not in rel.split("/")[-1]:
            rel += EXT_MAP.get((lang or "").lower(), ".txt")
        current_path = project_root / rel
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_text(code, encoding="utf-8")
        snap_path = snapshot_root / rel
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(code, encoding="utf-8")
        saved.append({"path": rel, "filename": rel, "content": code, "version": version, "round_no": round_no})
    
    if saved:
        current_test_files = {s["path"] for s in saved if s["path"].startswith("test_") or "/test_" in s["path"]}
        for old_test in project_root.rglob("test_*.py"):
            rel_old = str(old_test.relative_to(project_root)).replace("\\", "/")
            if rel_old not in current_test_files and "artifacts" not in rel_old:
                try:
                    old_test.unlink()
                except Exception:
                    pass
    return saved


def _build_dev_user_content(transcript: str, extra_context: str | None,
                           is_refine: bool,
                           task_skills: list[str] | None = None) -> str:
    if is_refine:
        instruction = (
            "[修改任務]\n"
            "依本輪提供的上一版程式碼、驗收標準、Reviewer issues 與 QA 結果修改。\n"
            "直接輸出修改後的完整檔案內容。\n"
        )
    else:
        if task_skills and any(s in task_skills for s in ("frontend", "visual", "visual3d")):
            file_example = "```html:index.html\n...\n```、```css:styles.css\n...\n```、```javascript:app.js\n...\n```"
            task_rule = "只輸出前端檔案，不要輸出 pytest 測試。"
        elif task_skills and "python" in task_skills:
            file_example = "```python:module.py\n...\n``` 與 ```python:test_module.py\n...\n```"
            task_rule = "輸出實作及對應 pytest 測試檔。"
        else:
            file_example = "```語言:相對路徑/檔名\n<完整內容>\n```"
            task_rule = "依 PM 規格選擇技術，並提供適合該技術的驗證方式。"
        instruction = (
            "[新任務]\n"
            "以使用者原始需求為準；有 PM 需求時一併參考，直接產出完整可執行程式碼。\n"
            f"依任務類型使用帶路徑的程式碼區塊，例如：{file_example}\n"
            f"{task_rule}\n"
        )
    if extra_context:
        return instruction + "\n---\n\n" + transcript + "\n\n---\n\n" + extra_context
    return instruction + "\n---\n\n" + transcript


async def run_agent(agent, transcript: str, session_id: str,
                    round_no: int = 0, version: int = 1,
                    extra_context: str | None = None,
                    stop_check=None,
                    task_skills: list[str] | None = None,
                    is_refine: bool = False,
                    allow_workspace_tools: bool = False,
                    image_context: dict | None = None) -> AsyncGenerator[dict, None]:
    if agent.id == "dev":
        user_content = _build_dev_user_content(
            transcript, extra_context, is_refine, task_skills
        )
    else:
        user_content = extra_context + "\n\n---\n\n" + transcript if extra_context else transcript

    if agent.id in ("pm", "dev") and task_skills:
        try:
            import rag
            hits = rag.search(user_content, top_k=2)
            if hits:
                retrieved = "\n\n".join(
                    f"[{hit['source']}]\n{hit['text']}" for hit in hits
                )[:3000]
                user_content += "\n\n[依任務相關性檢索的專案文件]\n" + retrieved
        except Exception as exc:
            logger.warn("task_knowledge_retrieval_failed", error=str(exc))

    available_tools = _select_agent_tools(
        agent, task_skills, is_refine, allow_workspace_tools
    )
    logger.info(
        "agent_tools_selected",
        agent=agent.id,
        tools=available_tools,
        is_refine=is_refine,
        allow_workspace_tools=allow_workspace_tools,
    )

    memory_hint = ""
    if agent.id in ("pm", "dev"):
        try:
            remembered = long_term.search(user_content)
            remembered = [
                item for item in remembered
                if {"user_explicit", "user_preference"} & set(item.get("tags", []))
            ]
            if remembered:
                memory_hint = "\n".join(f"- {item['fact']}" for item in remembered)[:2000]
        except Exception as exc:
            logger.warn("memory_injection_failed", error=str(exc))
    system_prompt = _build_system(
        agent, task_skills, is_refine, session_id, allow_workspace_tools, memory_hint
    )
    sys_tokens = await count_tokens(system_prompt)
    user_tokens = await count_tokens(user_content)
    image_text = image_context.get("text", "") if image_context else ""
    image_text_tokens = await count_tokens(image_text) if image_text else 0
    user_tokens += image_text_tokens

    from config import get_n_ctx, OUTPUT_RESERVE_DEFAULT, SAFETY_MARGIN
    n_ctx = await get_n_ctx()
    output_reserve = min(agent.max_tokens, OUTPUT_RESERVE_DEFAULT)
    image_count = len(image_context.get("images", [])) if image_context else 0
    image_reserve = image_count * VISION_IMAGE_TOKEN_RESERVE
    available_history = n_ctx - output_reserve - SAFETY_MARGIN - sys_tokens - image_reserve

    logger.info("agent_budget_debug",
                agent=agent.id,
                n_ctx=n_ctx,
                sys_tokens=sys_tokens,
                user_tokens=user_tokens,
                output_reserve=output_reserve,
                available_history=available_history)

    if available_history < 500:
        if available_history < 200:
            yield {"type": "error", "message": "目前上下文預算不足以安全處理本階段輸入與視覺證據；請縮短需求或降低圖片數量。"}
            return
        logger.warn("budget_exhausted", agent=agent.id, n_ctx=n_ctx,
                    image_tokens_reserved=image_reserve)
        fallback_budget = available_history
        user_content = truncate_to_token_budget(user_content, fallback_budget)
        logger.warn("budget_fallback_truncate", agent=agent.id, fallback_budget=fallback_budget)
    elif user_tokens > available_history:
        user_content = truncate_to_token_budget(
            user_content, max(0, available_history - image_text_tokens)
        )
        logger.info("transcript_truncated", agent=agent.id, original=user_tokens, trimmed_to=available_history)

    base_sys_msg = {"role": "system", "content": system_prompt}
    if image_context and image_context.get("images"):
        content = [{"type": "text", "text": user_content + ("\n\n" + image_text if image_text else "")}]
        content.extend({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{item['data']}"},
        } for item in image_context["images"] if item.get("data"))
        base_usr_msg = {"role": "user", "content": content}
    else:
        base_usr_msg = {"role": "user", "content": user_content}
    messages = [base_sys_msg, base_usr_msg]

    full = ""
    continuations = 0
    tool_calls_made = 0
    seen_tool_calls: set[tuple[str, str]] = set()
    empty_code_retries = 0
    MAX_EMPTY_CODE_RETRIES = 2
    start_time = time.time()
    total_output_tokens = 0
    json_agent = agent.id in ("pm", "qa", "reviewer")
    json_retries = 0
    repetition_retries = 0
    generation_temperature = agent.temperature
    generation_frequency_penalty = None

    while True:
        if stop_check and stop_check():
            yield {"type": "stopped", "duration_ms": int((time.time() - start_time) * 1000)}
            return

        buffer = ""
        display_pending = ""
        finish_reason = None
        repetition_detected = False
        stream_gen = _stream_with_meta(
            messages, agent.max_tokens, generation_temperature, json_mode=json_agent,
            frequency_penalty=generation_frequency_penalty,
        )
        async with aclosing(stream_gen):
            async for ev in stream_gen:
                if stop_check and stop_check():
                    yield {"type": "stopped", "duration_ms": int((time.time() - start_time) * 1000)}
                    return
                if ev["finish_reason"]:
                    finish_reason = ev["finish_reason"]
                if ev["token"]:
                    buffer += ev["token"]
                    total_output_tokens += 1
                    if _check_pathological_repetition(full + buffer):
                        repetition_detected = True
                        break
                    if not json_agent:
                        display_pending += ev["token"]
                        safe_length = max(0, len(display_pending) - 240)
                        if safe_length:
                            yield {"type": "token", "token": display_pending[:safe_length]}
                            display_pending = display_pending[safe_length:]

        if repetition_detected:
            logger.warn("pathological_repetition_stream_retry", agent=agent.id,
                        retry=repetition_retries + 1, tail=(full + buffer)[-100:])
            if repetition_retries < PATHOLOGICAL_REPETITION_RETRIES:
                repetition_retries += 1
                generation_temperature = max(0.05, agent.temperature * 0.5)
                generation_frequency_penalty = PATHOLOGICAL_RETRY_FREQUENCY_PENALTY
                messages = [base_sys_msg, base_usr_msg]
                full = ""
                yield {"type": "reset"}
                continue
            yield {"type": "error", "message": "模型連續輸出退化字元，已丟棄本次輸出並停止，沒有執行 QA。"}
            return

        if not json_agent and display_pending:
            yield {"type": "token", "token": display_pending}

        cleaned = _remove_overlap(full, buffer)
        full += cleaned

        if _check_pathological_repetition(full):
            preview = full[-120:]
            logger.error("pathological_repetition_detected", agent=agent.id, model=MODEL_NAME, output_preview=preview)
            yield {
                "type": "error",
                "message": (
                    f"模型 {MODEL_NAME} 連續輸出重複字元（{preview[:40]!r}），已停止本輪，避免把空結果當作完成。"
                    "請檢查本次 system/user prompt、停止序列與 API 請求參數；不要將此狀況直接判定為模型檔損壞。"
                ),
            }
            return

        if json_agent:
            from core.contracts import parse_agent_contract
            try:
                json_payload = json.loads(full.strip())
                valid_json = isinstance(json_payload, dict)
            except (json.JSONDecodeError, TypeError):
                json_payload = None
                valid_json = False
            if (
                (not valid_json or parse_agent_contract(full, agent.id).get("verdict") == "unknown")
                and json_retries < JSON_CONTRACT_RETRIES
            ):
                json_retries += 1
                logger.warn("agent_json_contract_retry", agent=agent.id)
                reminder = (
                    "請重新回答一次。只輸出單一有效 JSON object，不要 Markdown、工具呼叫或額外文字；"
                    "必須包含本角色契約要求的 verdict、route_to 與 summary 欄位。"
                )
                if isinstance(base_usr_msg["content"], list):
                    retry_content = list(base_usr_msg["content"])
                    retry_content[0] = dict(retry_content[0])
                    retry_content[0]["text"] += "\n\n" + reminder
                else:
                    retry_content = user_content + "\n\n" + reminder
                messages = [base_sys_msg, {"role": "user", "content": retry_content}]
                full = ""
                continue
            if not valid_json or parse_agent_contract(full, agent.id).get("verdict") == "unknown":
                yield {
                    "type": "error",
                    "message": f"{agent.name} 在 {json_retries} 次格式重試後仍未輸出有效 JSON 契約，流程已停止；此狀況不代表需求需要澄清。",
                }
                return
            full = json.dumps(json_payload, ensure_ascii=False)

        # Tool requests are a valid first response for Dev. Handle them before
        # deciding that the model failed to produce a code block.
        has_tool_request = bool(re.search(r"<\s*tool_call\s*>", full, re.IGNORECASE))
        if agent.id == "dev" and not has_tool_request and not parse_tool_call(full) and not _has_code_block(full):
            if empty_code_retries < MAX_EMPTY_CODE_RETRIES:
                empty_code_retries += 1
                logger.warn("dev_no_code_block", retry=empty_code_retries, output_preview=full[-300:])
                preview = full[-500:] if len(full) > 500 else full
                retry_hint = _build_empty_code_retry_hint(transcript, task_skills)
                messages = [
                    base_sys_msg, base_usr_msg,
                    {"role": "assistant", "content": f"...[前文已省略]...\n{preview}"},
                    {"role": "user", "content": retry_hint},
                ]
                full = ""
                continue
            else:
                logger.error("dev_no_code_block_give_up", output_preview=full[-300:])
                yield {"type": "error", "message": "Dev 重試後仍未輸出任何可保存的程式碼區塊；已停止流程，不會執行 QA 或改進循環。"}
                return

        parsed = parse_tool_call(full)
        if parsed:
            tool_name, args = parsed
            if tool_name not in available_tools:
                message = (
                    f"{agent.name} 嘗試調用未授權工具 `{tool_name}`；"
                    "本階段不需要此工具，已停止流程以避免空轉。"
                )
                yield {"type": "error", "message": message}
                return
            if tool_calls_made >= agent.max_tool_iterations:
                message = f"{agent.name} 已達工具調用上限 {agent.max_tool_iterations} 次，已停止本階段。"
                yield {"type": "error", "message": message}
                return

            call_key = (tool_name, str(sorted(args.items())))
            if call_key in seen_tool_calls:
                message = f"{agent.name} 重複調用 `{tool_name}`，已停止本階段以避免工具循環。"
                yield {"type": "error", "message": message}
                return
            seen_tool_calls.add(call_key)

            logger.info("agent_tool_use", agent=agent.id, tool=tool_name, args=args)
            yield {"type": "tool", "name": tool_name, "args": args, "state": "start"}
            tool_start = time.time()
            result = execute(tool_name, args)
            tool_ms = int((time.time() - tool_start) * 1000)
            yield {"type": "tool", "name": tool_name, "args": args, "state": "done", "result": result[:2000], "duration_ms": tool_ms}

            tool_calls_made += 1
            messages.append({"role": "assistant", "content": full})

            if result.startswith("__IMAGE_META__:") or result.startswith("__IMAGE_BASE64__:"):
                scene_meta = "unavailable"
                if result.startswith("__IMAGE_META__:"):
                    metadata_line, image_part = result.split("\n", 1)
                    scene_meta = metadata_line[len("__IMAGE_META__:"):]
                    b64 = image_part.removeprefix("__IMAGE_BASE64__:").strip()
                else:
                    b64 = result[len("__IMAGE_BASE64__:"):]
                logger.info("multimodal_message_injected", tool=tool_name, b64_len=len(b64))
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"[工具結果: {tool_name}]\n場景物件統計 scene_info（由 Three.js 場景圖直接計數，null 表示頁面未提供）：{scene_meta}\n請以統計核對物件存在與數量；以附圖評估構圖、比例、材質、光影、層次與整體觀感。不得假設截圖以外的行為。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                    ],
                })
            else:
                messages.append({"role": "user", "content": f"[工具結果: {tool_name}]\n{result}\n\n請基於結果繼續作答。"})

            full = ""
            continue

        analysis = analyze_truncation(full, finish_reason)
        if analysis["truncated"] and continuations < MAX_CONTINUATIONS:
            continuations += 1
            logger.warn("output_truncated", agent=agent.id, continuation=continuations, reason=analysis["reason"])
            
            if analysis["reason"] == "unclosed_tool_call":
                continuation_prompt = (
                    "[續寫錨點]\n"
                    "你上一條輸出在 <tool_call> JSON 中間被截斷。\n"
                    "請**丟棄**不完整的 <tool_call>，"
                    "用完整格式重新輸出這個工具調用。"
                )
            else:
                anchor_long = full[-CONTINUATION_ANCHOR_CHARS:]
                continuation_prompt = (
                    f"[續寫錨點]\n"
                    f"你上一條輸出被截斷。\n"
                    f"你已經完整寫過以下內容，**不要重複它們**：\n"
                    f"```\n{anchor_long}\n```\n"
                    f"請從最後一個字元之後直接繼續，不要重新開頭。"
                )

            tail_for_context = full[-CONTINUATION_ANCHOR_CHARS:]
            messages = [
                base_sys_msg, base_usr_msg,
                {"role": "assistant", "content": f"...[前文已省略]...\n{tail_for_context}"},
                {"role": "user", "content": continuation_prompt},
            ]
            yield {"type": "token", "token": f"\n\n[...續寫 · {analysis['reason']}...]\n\n"}
            continue

        break

    if agent.id == "dev" and full:
        saved = _persist_artifacts(agent, full, session_id, round_no, version=version)
        if saved:
            yield {"type": "token", "token": f"\n\n[專案已落盤: {', '.join(f['path'] for f in saved)}]"}
            yield {"type": "artifact", "files": saved}

    if not full.strip():
        message = f"{agent.name} 收到空白回覆，已停止流程，避免空輸出進入下一個 Agent。"
        logger.error("agent_empty_output", agent=agent.id, model=MODEL_NAME)
        yield {"type": "error", "message": message}
        return

    if json_agent:
        yield {"type": "token", "token": full}

    duration_ms = int((time.time() - start_time) * 1000)
    tps = (round(total_output_tokens / (duration_ms / 1000), 1) if duration_ms > 0 else 0)
    yield {"type": "done", "tokens": total_output_tokens, "duration_ms": duration_ms, "tps": tps}


async def extract_facts(user_msg: str, responses: dict[str, str], session_id: str):
    explicit_markers = ("記住", "记住", "請記得", "请记得", "不要忘記", "不要忘记", "remember that")
    if any(k in user_msg for k in explicit_markers) and 4 < len(user_msg) < 400:
        long_term.add(user_msg, tags=["user_explicit"], weight=9, source_session=session_id)

    pref_markers = ("我的", "我是", "我喜歡", "我喜欢", "我不喜歡", "我不喜欢", "偏好", "習慣", "习惯", "備註", "备注", "i prefer", "i like", "my name is")
    if any(k in user_msg for k in pref_markers) and 4 < len(user_msg) < 400:
        long_term.add(user_msg, tags=["user_preference"], weight=7, source_session=session_id)

    # Persist only durable user facts/preferences; implementation details belong
    # to the session artifacts and create noisy, misleading long-term memories.
