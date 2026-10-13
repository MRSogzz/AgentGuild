import difflib
import re
import time
from typing import AsyncGenerator

from config import (
    get_llm_client, MODEL_NAME,
    MAX_CONTINUATIONS, CONTINUATION_ANCHOR_CHARS,
    WORKSPACE_DIR, ARTIFACT_DIR, MEMORY_HIGH_PRIORITY_LIMIT,
    MEMORY_INJECT_MAX_TOKENS, SYSTEM_BUDGET,
)
from tools import schemas_for, parse_tool_call, execute
from memory import long_term
from observability import logger
from token_budget import (
    count_tokens, estimate_tokens, truncate_to_token_budget,
)

client = get_llm_client()

# 帶路徑的程式碼塊：```python:path/to/file.py
CODE_BLOCK_WITH_PATH_RE = re.compile(
    r"```(\w+):([^\n`]+?)\s*\n([\s\S]*?)```",
    re.MULTILINE,
)
# 不帶路徑的普通程式碼塊（相容舊格式）
CODE_BLOCK_RE = re.compile(r"```(\w*)\s*\n([\s\S]*?)```", re.MULTILINE)


# ============ 續寫去重 ============
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


# ============ 智慧截斷分析 ============
def analyze_truncation(text: str, finish_reason: str | None = None) -> dict:
    text = text.rstrip()
    result = {
        "truncated": False,
        "reason": None,
        "anchor": text[-CONTINUATION_ANCHOR_CHARS:] if len(text) > CONTINUATION_ANCHOR_CHARS else text,
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


# ============ System prompt ============
def _build_system(agent) -> str:
    base = agent.system
    tool_names = agent.tools

    memory_hint = ""
    high = long_term.high_priority(limit=MEMORY_HIGH_PRIORITY_LIMIT)
    if high:
        facts_text = "\n".join(f"- {f['fact']}" for f in high)
        max_mem_tokens = min(MEMORY_INJECT_MAX_TOKENS, int(SYSTEM_BUDGET * 0.3))
        if estimate_tokens(facts_text) > max_mem_tokens:
            facts_text = truncate_to_token_budget(facts_text, max_mem_tokens)
        memory_hint = "\n\n[重要長期記憶 · 務必遵守]\n" + facts_text

    if not tool_names:
        return base + memory_hint

    tool_desc = schemas_for(list(tool_names))
    protocol = (
        "\n\n你可以使用以下工具：\n"
        f"{tool_desc}\n\n"
        "需要呼叫工具時，嚴格按以下格式輸出（一次只調一個）：\n"
        "<tool_call>\n"
        '{"name": "工具名", "args": {"引數名": "值"}}\n'
        "</tool_call>\n"
        "系統會把工具結果返回給你，你再繼續作答。"
        "不需要工具時正常回覆即可。"
    )
    return base + protocol + memory_hint


# ============ 流式生成 ============
async def _stream_with_meta(messages: list[dict], max_tokens: int,
                            temperature: float):
    stream = await client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        stream=True,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if delta and delta.content:
            yield {"token": delta.content, "finish_reason": None}
        if choice.finish_reason:
            yield {"token": "", "finish_reason": choice.finish_reason}


# ============ 工件落盤（多檔案專案） ============
def _extract_files(text: str) -> list[tuple[str, str, str]]:
    """
    從輸出中提取檔案，返回 [(path, lang, code), ...]。
    優先匹配帶路徑的程式碼塊；沒有則回退到普通程式碼塊（自動命名）。
    """
    files = []
    consumed_spans = []

    for m in CODE_BLOCK_WITH_PATH_RE.finditer(text):
        lang = m.group(1)
        path = m.group(2).strip()
        code = m.group(3).rstrip("\n")
        files.append((path, lang, code))
        consumed_spans.append((m.start(), m.end()))

    # 沒有帶路徑的程式碼塊，才回退
    if not files:
        for m in CODE_BLOCK_RE.finditer(text):
            lang = (m.group(1) or "python").strip()
            code = m.group(2).rstrip("\n")
            if lang and lang != "python":
                continue
            files.append(("", lang, code))

    return files


def _sanitize_relpath(path: str) -> str:
    """清洗路徑，禁止 ..、絕對路徑、特殊字元。"""
    if not path:
        return ""
    path = path.replace("\\", "/").strip().lstrip("/")
    parts = []
    for p in path.split("/"):
        if p in ("", ".", ".."):
            continue
        parts.append(p)
    return "/".join(parts)


def _fallback_name(code: str, index: int) -> str:
    """沒有路徑時，根據內容生成一個合理檔名。"""
    # 測試檔案優先
    if re.search(r"^\s*def\s+test_", code, re.MULTILINE):
        m = re.search(r"from\s+(\w+)\s+import", code)
        base = m.group(1) if m else "module"
        return f"test_{base}_{index}.py"

    m = re.search(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", code, re.MULTILINE)
    if m:
        return f"{m.group(1).lower()}_{index}.py"

    for m in re.finditer(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                          code, re.MULTILINE):
        name = m.group(1)
        if not (name.startswith("__") and name.endswith("__")):
            return f"{name.lower()}_{index}.py"

    return f"snippet_{index}.py"


def _persist_artifacts(agent, text: str, session_id: str,
                       round_no: int, version: int = 1) -> list[dict]:
    """
    解析帶路徑的程式碼塊，落盤到：
    - workspace/<path>                 （當前版本，供 pytest 使用）
    - workspace/artifacts/rN_vM/<path> （歷史快照）
    """
    if agent.id != "dev":
        return []

    files = _extract_files(text)
    if not files:
        return []

    # 歸一化去重
    seen_norm: set = set()
    project_root = WORKSPACE_DIR
    snapshot_root = ARTIFACT_DIR / f"r{round_no}_v{version}"

    saved = []
    for i, (path, lang, code) in enumerate(files):
        if not code.strip():
            continue
        norm = "\n".join(
            line.rstrip() for line in code.strip().splitlines() if line.strip()
        )
        if norm in seen_norm:
            logger.info("artifact_dedup_skipped", index=i,
                        reason="identical_content")
            continue
        seen_norm.add(norm)

        rel = _sanitize_relpath(path) or _fallback_name(code, i)
        if not rel.endswith(".py"):
            rel = rel + ".py" if not rel.endswith(".") else rel[:-1] + ".py"

        # 當前版本：寫到 workspace 根
        current_path = project_root / rel
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_text(code, encoding="utf-8")

        # 歷史快照
        snap_path = snapshot_root / rel
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(code, encoding="utf-8")

        saved.append({
            "path": rel,                     # 相對 workspace 的路徑
            "filename": rel,                 # 前端用
            "logical_name": rel,             # 分組 key
            "content": code,
            "version": version,
            "round_no": round_no,
            "snapshot_dir": f"r{round_no}_v{version}",
        })

    # 清理：移除本輪未出現的舊測試檔案，避免 pytest 跑舊測試
    if saved:
        current_test_files = {
            s["path"] for s in saved
            if s["path"].startswith("test_") or "/test_" in s["path"]
        }
        for old_test in project_root.rglob("test_*.py"):
            rel_old = str(old_test.relative_to(project_root)).replace("\\", "/")
            if rel_old not in current_test_files and "artifacts" not in rel_old:
                logger.info("stale_test_removed", file=rel_old)
                try:
                    old_test.unlink()
                except Exception:
                    pass

    return saved


# ============ Agent 主迴圈 ============
async def run_agent(agent, transcript: str, session_id: str,
                    round_no: int = 0, version: int = 1,
                    extra_context: str | None = None,
                    stop_check=None) -> AsyncGenerator[dict, None]:
    user_content = transcript
    if extra_context:
        user_content = transcript + "\n\n" + extra_context

    system_prompt = _build_system(agent)
    sys_tokens = await count_tokens(system_prompt)
    user_tokens = await count_tokens(user_content)

    from config import get_n_ctx, OUTPUT_RESERVE_DEFAULT, SAFETY_MARGIN
    n_ctx = await get_n_ctx()
    output_reserve = min(agent.max_tokens, OUTPUT_RESERVE_DEFAULT)
    available_history = n_ctx - output_reserve - SAFETY_MARGIN - sys_tokens

    if available_history < 500:
        logger.warn("budget_exhausted", agent=agent.id,
                    sys_tokens=sys_tokens, n_ctx=n_ctx)
        user_content = "[上下文預算已耗盡，請開始新一輪對話]"
    elif user_tokens > available_history:
        user_content = truncate_to_token_budget(user_content, available_history)
        logger.info("transcript_truncated", agent=agent.id,
                    original=user_tokens, trimmed_to=available_history)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    full = ""
    continuations = 0
    tool_calls_made = 0
    start_time = time.time()
    total_output_tokens = 0

    while True:
        if stop_check and stop_check():
            yield {"type": "stopped",
                   "duration_ms": int((time.time() - start_time) * 1000)}
            return

        buffer = ""
        finish_reason = None
        async for ev in _stream_with_meta(messages, agent.max_tokens,
                                          agent.temperature):
            if stop_check and stop_check():
                yield {"type": "stopped",
                       "duration_ms": int((time.time() - start_time) * 1000)}
                return
            if ev["finish_reason"]:
                finish_reason = ev["finish_reason"]
            if ev["token"]:
                buffer += ev["token"]
                total_output_tokens += 1
                yield {"type": "token", "token": ev["token"]}

        cleaned = _remove_overlap(full, buffer)
        if cleaned != buffer:
            yield {"type": "token",
                   "token": f"\n\n[已去除續寫重複的 {len(buffer) - len(cleaned)} 個字元]\n\n"}

        full += cleaned

        # 工具呼叫
        parsed = parse_tool_call(full)
        if parsed:
            tool_name, args = parsed
            if tool_name not in agent.tools:
                yield {"type": "token",
                       "token": f"\n[拒絕呼叫未授權工具: {tool_name}]"}
                break
            if tool_calls_made >= agent.max_tool_iterations:
                yield {"type": "token",
                       "token": f"\n[已達工具呼叫上限 {agent.max_tool_iterations} 次，停止]"}
                break

            logger.info("agent_tool_use", agent=agent.id, tool=tool_name, args=args)
            yield {"type": "tool", "name": tool_name, "args": args, "state": "start"}

            tool_start = time.time()
            result = execute(tool_name, args)
            tool_ms = int((time.time() - tool_start) * 1000)

            yield {"type": "tool", "name": tool_name, "args": args,
                   "state": "done", "result": result[:2000],
                   "duration_ms": tool_ms}

            tool_calls_made += 1

            messages.append({"role": "assistant", "content": full})
            messages.append({
                "role": "user",
                "content": f"[工具結果: {tool_name}]\n{result}\n\n請基於結果繼續作答。",
            })
            full = ""
            continue

        # 截斷續寫
        analysis = analyze_truncation(full, finish_reason)
        if analysis["truncated"] and continuations < MAX_CONTINUATIONS:
            continuations += 1
            anchor = analysis["anchor"]
            logger.warn("output_truncated", agent=agent.id,
                        continuation=continuations,
                        reason=analysis["reason"], length=len(full))

            if analysis["reason"] == "unclosed_tool_call":
                continuation_prompt = (
                    "[續寫錨點]\n"
                    "你上一條輸出在 <tool_call> JSON 中間被截斷。\n"
                    "請**丟棄**不完整的 <tool_call>，用完整格式重新輸出這個工具呼叫。"
                )
            else:
                anchor_long = full[-2000:] if len(full) > 2000 else full
                continuation_prompt = (
                    f"[續寫錨點]\n"
                    f"你上一條輸出被截斷（原因：{analysis['reason']}）。\n"
                    f"你**已經完整寫過**以下內容，**不要重複它們**：\n"
                    f"```\n{anchor_long}\n```\n"
                    f"請從最後一個字元之後**直接繼續**，要求：\n"
                    f"1. **絕對不要**重複上面已經出現過的內容；\n"
                    f"2. 不要重新開頭、不要寫 `import`、不要重新定義已定義的函式；\n"
                    f"3. 如果上一個 ``` 區塊還沒收尾，先把 ``` 補上，再開新檔案；\n"
                    f"4. 保持和前面一致的縮排。"
                )

            messages.append({"role": "assistant", "content": full})
            messages.append({"role": "user", "content": continuation_prompt})
            yield {"type": "token",
                   "token": f"\n\n[...續寫 · {analysis['reason']}...]\n\n"}
            continue

        break

    # 工件落盤
    if agent.id == "dev" and full:
        saved = _persist_artifacts(agent, full, session_id,
                                   round_no, version=version)
        if saved:
            file_list = ", ".join(f["path"] for f in saved)
            yield {"type": "token",
                   "token": f"\n\n[專案已落盤: {file_list}]"}
            yield {"type": "artifact", "files": saved}

    duration_ms = int((time.time() - start_time) * 1000)
    tps = round(total_output_tokens / (duration_ms / 1000), 1) if duration_ms > 0 else 0
    yield {
        "type": "done",
        "tokens": total_output_tokens,
        "duration_ms": duration_ms,
        "tps": tps,
    }


# ============ 事實抽取 ============
async def extract_facts(user_msg: str, responses: dict[str, str], session_id: str):
    explicit_markers = ("記住", "記住", "請記得", "請記得", "不要忘記", "不要忘記",
                        "remember that")
    if any(k in user_msg for k in explicit_markers) and 4 < len(user_msg) < 400:
        long_term.add(user_msg, tags=["user_explicit"],
                      weight=9, source_session=session_id)

    pref_markers = ("我的", "我喜歡", "我不喜歡","偏好", "習慣", "備註", "i prefer", "i like", "my name is")
    if any(k in user_msg for k in pref_markers) and 4 < len(user_msg) < 400:
        long_term.add(user_msg, tags=["user_preference"],
                      weight=7, source_session=session_id)

    for text in responses.values():
        for m in re.finditer(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text):
            long_term.add(f"函式 {m.group(1)} 已在會話中實現",
                          tags=["code"], weight=5, source_session=session_id)
