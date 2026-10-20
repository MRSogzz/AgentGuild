import asyncio
import json
import re
import shutil
import time
import traceback
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import (
    AGENTS, PIPELINE, SESSIONS_DIR,
    MAX_REFINE_ROUNDS, MAX_REFINE_HARD_LIMIT,
    MAX_QUALITY_ROUNDS, VISUAL_QUALITY_MIN,
    PM_BLOCK_ON_UNRESOLVABLE,
    VISUAL_SCREENSHOT_MAX_IMAGES, VISUAL_IMAGE_TOTAL_MAX_BYTES,
    VISUAL_DEV_MAX_IMAGES, REFINE_PREVIOUS_CODE_CHARS,
    PIVOT_ROUND, HIGH_RISK_PATTERNS,
    ensure_dirs, ensure_vendor,
    get_n_ctx, SYSTEM_BUDGET, BASE_DIR, WORKSPACE_DIR,
)
from memory import Session, list_sessions, long_term
from guardrails import check_user_input, GuardrailError
from observability import logger
from agent import extract_facts, _extract_summary_block
from tools import parse_verdict_tag, execute as execute_tool
from token_budget import check_llama_health, count_tokens, get_llama_runtime_info
import rag

from core.router import (
    parse_agent_directive,
    apply_agent_dependencies,
    classify_request,
    agents_for_task,
)
from core.refine import (
    extract_test_counts,
    check_refine_stop,
)
from core.test_utils import (
    is_test_file,
    write_frozen_tests,
    push_test_verification,
    detect_project_kind,
    auto_run_tests_for_project,
    build_qa_analysis_context,
    parse_pytest_output, 
)
from core.summary import build_final_summary
from core.runner import run_and_stream
from core.contracts import parse_agent_contract
from core.pipeline import (
    PipelineState, choose_next_route, workspace_snapshot,
    evaluate_scene_assertions,
)

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

VERDICT_RE = re.compile(r"<verdict>\s*(PASS|REVISE)\s*</verdict>", re.IGNORECASE)
UNLOCK_RE = re.compile(r"<\s*unlock_test\s*>", re.IGNORECASE)
TEST_ISSUE_RE = re.compile(r"\[\s*TEST_ISSUE\s*\]", re.IGNORECASE)


class AgentGenerationError(RuntimeError):
    """A model returned empty or corrupted output; do not continue the pipeline."""

ALLOWED_ORIGINS = {
    "http://127.0.0.1:8000", "http://localhost:8000",
    "http://127.0.0.1:3000", "http://localhost:3000",
}

CONTINUE_KW = (
    "繼續", "繼續", "改成", "修改", "修正", "調整", "調整",
    "再加", "再加一個", "再加一個", "補充", "補充",
    "重做", "重新做", "換成", "換成", "延續", "延續",
)


def parse_verdict(text: str) -> str:
    contract = parse_agent_contract(text, "reviewer")
    if contract["verdict"] == "approved":
        return "PASS"
    if contract["verdict"] in ("changes_requested", "blocked"):
        return "REVISE"
    v = parse_verdict_tag(text)
    if v:
        return v
    return "REVISE"


def _all_tests_passed(collected_tests: list[dict]) -> tuple[bool, str]:
    latest_parsed = None
    latest_ts = -1
    for t in collected_tests:
        output = t.get("output") or ""
        parsed = parse_pytest_output(output)
        if not parsed["saw_pytest"]:
            continue
        ts = t.get("ts", 0)
        if ts > latest_ts:
            latest_ts = ts
            latest_parsed = parsed

    if latest_parsed is None:
        return True, ""

    if latest_parsed["failed"] > 0 or latest_parsed["errors"] > 0:
        return False, f"pytest 最新執行：{latest_parsed['failed']} 個失敗、{latest_parsed['errors']} 個錯誤"
    return True, ""


def _has_high_risk_code(code: str) -> bool:
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return True
    return False


def guard_visual_verdict(output: str, task_type: str,
                        screenshot_captured: bool = True,
                        requirement: dict | None = None,
                        screenshot_metadata: dict | None = None) -> tuple[str, str | None]:
    """Require explicit, non-failing image analysis before visual PASS."""
    if task_type != "frontend":
        return parse_verdict(output), None
    if not screenshot_captured:
        return "REVISE", "Reviewer 沒有成功呼叫截圖工具，無法完成視覺驗收。"
    if screenshot_metadata and screenshot_metadata.get("all_blank") is True:
        return "REVISE", "像素統計顯示所有截圖皆為空白畫面。"
    if screenshot_metadata and screenshot_metadata.get("pixel_analysis_available") is False:
        return "REVISE", "缺少截圖像素分析能力，無法完成空白畫面客觀檢查。"
    screenshot_state = (screenshot_metadata or {}).get("state", {})
    if screenshot_state.get("hasSceneKit") and screenshot_state.get("readyTimedOut"):
        return "REVISE", "場景腳手架未在設定時間內回報完成訊號。"
    contract = parse_agent_contract(output, "reviewer")
    description = contract.get("image_analysis", "")
    if not description:
        return "REVISE", "Reviewer 缺少 JSON image_analysis 畫面描述，無法完成視覺驗收。"
    quality = contract.get("visual_quality")
    try:
        quality = int(quality)
    except (TypeError, ValueError):
        return "REVISE", "Reviewer 缺少 1–10 的 visual_quality 品質分數。"
    if not 1 <= quality <= 10:
        return "REVISE", "Reviewer 的 visual_quality 必須是 1–10 的整數。"
    if quality < VISUAL_QUALITY_MIN:
        return "REVISE", f"視覺品質分數 {quality}/10 未達 {VISUAL_QUALITY_MIN} 分門檻。"
    checks = contract.get("checks", [])
    if not checks:
        return "REVISE", "Reviewer 缺少依需求逐項判定的 checks 結構。"
    required_ids = {
        str(item.get("id"))
        for item in (requirement or {}).get("requirements", [])
        if isinstance(item, dict) and item.get("id")
    }
    checked_ids = {
        str(item.get("criterion_id")) for item in checks
        if item.get("criterion_id") is not None
    }
    if required_ids and not required_ids.issubset(checked_ids):
        return "REVISE", "Reviewer checks 未覆蓋全部驗收項目：" + ", ".join(sorted(required_ids - checked_ids))
    if required_ids and not checked_ids.issubset(required_ids):
        return "REVISE", "Reviewer checks 含有不屬於本任務 requirement 的 criterion_id。"
    invalid_checks = [
        check for check in checks
        if not isinstance(check.get("pass"), bool) or not check.get("pass")
    ]
    if invalid_checks:
        observations = "; ".join(
            str(check.get("observation", "未通過"))[:160]
            for check in invalid_checks[:3]
        )
        return "REVISE", f"視覺驗收項目未全部通過：{observations}"
    return parse_verdict(output), None


def is_new_task(user_msg: str, session: Session) -> bool:
    if not session.history:
        return True
    text = (user_msg or "").strip()
    reset_markers = ("新任務", "新任務", "重新開始", "重新開始", "從頭開始", "從頭開始", "new task")
    last_state = (session.history[-1].get("shared_state") or {})
    pending_checkpoint = (last_state.get("human_checkpoint") or {}).get("status") == "pending"
    decision = text.split("[使用者釐清／決策]")[-1].lower()
    if any(k in decision for k in reset_markers):
        return True
    if pending_checkpoint:
        return False
    if any(k in text.lower() for k in reset_markers):
        return True
    if any(k in text for k in CONTINUE_KW):
        return False
    return True


def clear_workspace():
    if not WORKSPACE_DIR.exists():
        return
    removed = 0
    for item in WORKSPACE_DIR.iterdir():
        if item.name in ("vendor", "artifacts"):
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
            removed += 1
        except Exception as e:
            logger.warn("clear_workspace_failed", item=item.name, error=str(e))
    if removed:
        logger.info("workspace_cleared", removed=removed)


def cleanup_empty_sessions():
    count = 0
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not data.get("history"):
                p.unlink()
                count += 1
        except Exception:
            pass
    if count:
        logger.info("cleanup_empty_sessions", removed=count)


def _log_task_exception(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.error("chat_task_failed", error=str(exc), exc_type=type(exc).__name__)
    print("\n" + "=" * 60)
    print("  ✗ _run_chat 崩潰（完整 traceback）")
    print("=" * 60)
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    print("=" * 60 + "\n")


@app.get("/")
async def index():
    return HTMLResponse((BASE_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/sessions")
async def api_list_sessions():
    return list_sessions()


@app.get("/api/sessions/{sid}")
async def api_get_session(sid: str):
    p = SESSIONS_DIR / f"{sid}.json"
    if not p.exists():
        raise HTTPException(404, "session not found")
    return Session(sid).to_dict()


@app.delete("/api/sessions/{sid}")
async def api_delete_session(sid: str):
    p = SESSIONS_DIR / f"{sid}.json"
    if not p.exists():
        raise HTTPException(404, "session not found")
    p.unlink()
    return {"ok": True}


@app.get("/api/memory")
async def api_memory():
    return {"facts": long_term.all(), "stats": long_term.stats()}


@app.get("/api/metrics")
async def api_metrics():
    return logger.metrics()


@app.post("/api/rag/rebuild")
async def api_rag_rebuild():
    idx = rag.rebuild()
    return {"indexed_chunks": idx["built"]}


@app.get("/api/health")
async def api_health():
    ok, msg = await check_llama_health()
    n_ctx = await get_n_ctx()
    runtime = await get_llama_runtime_info()
    return {"llama_server": ok, "detail": msg, "n_ctx": n_ctx, **runtime}


async def _auto_run_tests(emit, project_info: dict, task_skills: list, collected_tests: list) -> list[dict]:
    auto_results = auto_run_tests_for_project(project_info, task_skills)
    for r in auto_results:
        await emit({"type": "tool", "agent_id": "qa", "name": r["name"], "args": r["args"], "state": "start"})
        await emit({"type": "tool", "agent_id": "qa", "name": r["name"], "args": r["args"], "state": "done", "result": r["result"], "duration_ms": r["duration_ms"], "_auto": True})
        collected_tests.append({"agent_id": "qa", "name": r["name"], "output": r["result"], "ts": int(time.time() * 1000)})
    return auto_results


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    origin = ws.headers.get("origin", "")
    if origin and origin not in ALLOWED_ORIGINS:
        logger.warn("ws_origin_rejected", origin=origin)
        await ws.close(code=1008, reason="Origin not allowed")
        return

    await ws.accept()
    session: Session | None = None
    stop_flag = {"stop": False}
    current_task: asyncio.Task | None = None

    async def emit(payload: dict):
        await ws.send_json(payload)

    async def _run_chat(user_msg: str):
        nonlocal session
        if session.history:
            last_state = session.history[-1].get("shared_state") or {}
            checkpoint = last_state.get("human_checkpoint") or {}
            if checkpoint.get("status") == "pending" and not any(
                marker in user_msg.lower()
                for marker in ("新任務", "新任務", "重新開始", "重新開始", "從頭開始", "從頭開始", "new task")
            ):
                prior_request = last_state.get("user_request", "")
                if prior_request:
                    user_msg = f"{prior_request}\n\n[使用者釐清／決策]\n{user_msg}"
        trace_id = logger.start_trace("chat_round", session=session.session_id)
        transcript = session.build_transcript(user_msg)
        responses: dict[str, str] = {}
        artifacts: dict[str, list[dict]] = {}
        all_messages: list[dict] = []
        collected_tests: list[dict] = []
        reviewer_screenshot_captured = False
        visual_evidence: dict | None = None
        round_no = len(session.history) + 1

        frozen_tests: dict[str, str] = dict(session.frozen_tests or {})

        explicit_agents, cleaned_msg = parse_agent_directive(user_msg)
        is_explicit = bool(explicit_agents)
        fresh_task = is_new_task(user_msg, session)
        allow_workspace_tools = bool(session.history) and not fresh_task
        pending_human_checkpoint = bool(
            session.history
            and ((session.history[-1].get("shared_state") or {})
                 .get("human_checkpoint") or {}).get("status") == "pending"
        )
        project_info_init = (
            {"kind": "empty", "summary": "(新任務，不沿用舊工作區分類)"}
            if fresh_task and not is_explicit else detect_project_kind()
        )

        if not is_explicit and fresh_task:
            clear_workspace()
            frozen_tests = {}
            await emit({"type": "system", "content": "🧹 偵測到新任務，已清空工作區"})

        if is_explicit:
            agents_to_run = apply_agent_dependencies(explicit_agents, explicit=True)
            user_msg = cleaned_msg
            task_type, task_skills = classify_request(user_msg, project_info_init)
            session.meta["task_type"] = task_type
            session.meta["task_skills"] = task_skills
            logger.info("agent_directive", agents=agents_to_run, source="explicit")
            await emit({"type": "system", "content": f"🎯 使用者指定：{', '.join(agents_to_run)}"})
        else:
            await emit({"type": "system", "content": "🎯 正在分析任務類型與最佳 Agent 組合..."})
            previous_state = (
                (session.history[-1].get("shared_state") or {})
                if session.history and not fresh_task else {}
            )
            if previous_state.get("task_type"):
                task_type = previous_state["task_type"]
                task_skills = list(previous_state.get("task_skills") or
                                   session.meta.get("task_skills") or [])
            else:
                task_type, task_skills = classify_request(user_msg, project_info_init)
            agents_to_run = agents_for_task(
                task_type, include_pm=(fresh_task or pending_human_checkpoint)
            )
            pm_label = "PM → " if (fresh_task or pending_human_checkpoint) else ""
                
            logger.info("task_routing_classified", task_type=task_type)
            session.meta["task_type"] = task_type
            session.meta["task_skills"] = task_skills
            
            if task_type == "frontend":
                await emit({"type": "system", "content": f"⚡ 識別為『前端視覺任務』，啟動 {pm_label}開發、語法檢查與視覺評審。"})
            elif task_type in ("backend", "database"):
                await emit({"type": "system", "content": f"⚡ 識別為『{task_type} 任務』，啟動 {pm_label}開發與功能測試。"})
            else:
                pm_label = "PM → " if (fresh_task or pending_human_checkpoint) else "沿用既有規格，"
                await emit({"type": "system", "content": f"🏗️ 識別為『複雜專案』，{pm_label}啟動完整開發與驗收流程。"})

        routed_type = session.meta.get("task_type")
        visual_required = any(
            skill in task_skills for skill in ("frontend", "visual", "visual3d")
        )
        session.meta["task_skills"] = task_skills
        session.save()

        pipeline_state = PipelineState(
            task_id=f"{session.session_id}:{round_no}",
            user_request=user_msg,
            task_type=routed_type or "complex",
            agents=list(agents_to_run),
            task_skills=list(task_skills),
        )
        pipeline_state.requirement = {
            "requirements": [{"id": "R1", "description": user_msg,
                              "acceptance": "依使用者需求與本任務適用的自動驗收結果判定"}],
            "assumptions": [],
            "questions": [],
        }

        def acceptance_context() -> str:
            fields = ("verdict", "requirements", "assumptions", "questions", "summary")
            compact = {
                key: pipeline_state.requirement[key]
                for key in fields if key in pipeline_state.requirement
            }
            return json.dumps(compact, ensure_ascii=False)

        def artifact_files_from_result(result: dict) -> list[dict]:
            return [
                item
                for versions in result.get("round_artifacts", {}).values()
                for item in versions
            ]

        async def capture_visual_evidence() -> dict | None:
            await emit({"type": "tool", "agent_id": "reviewer", "name": "screenshot_page",
                        "args": {"path": "index.html", "views": ["front", "side", "top", "iso"]},
                        "state": "start"})
            raw = execute_tool("screenshot_page", {
                "path": "index.html", "views": ["front", "side", "top", "iso"],
            })
            if not raw.startswith("__SCREENSHOT_BUNDLE__:"):
                await emit({"type": "tool", "agent_id": "reviewer", "name": "screenshot_page",
                            "args": {"path": "index.html"}, "state": "done", "result": raw[:1800]})
                return {"images": [], "text": f"截圖失敗：{raw[:1200]}", "captured": False}
            try:
                bundle = json.loads(raw[len("__SCREENSHOT_BUNDLE__:"):])
            except json.JSONDecodeError:
                return {"images": [], "text": "截圖工具回傳格式錯誤。", "captured": False}
            images = [
                {"view": item.get("view", "default"), "data": item["data"],
                 "non_background_ratio": item.get("non_background_ratio"),
                 "is_blank": item.get("is_blank", False)}
                for item in bundle.get("results", []) if item.get("data")
            ]
            images = images[:max(1, VISUAL_SCREENSHOT_MAX_IMAGES)]
            bounded_images = []
            total_bytes = 0
            for image in images:
                byte_count = len(image["data"]) * 3 // 4
                if bounded_images and total_bytes + byte_count > VISUAL_IMAGE_TOTAL_MAX_BYTES:
                    continue
                bounded_images.append(image)
                total_bytes += byte_count
            images = bounded_images
            summary = {
                "state": bundle.get("state", {}),
                "logs": bundle.get("logs", {}),
                "views": [{key: value for key, value in item.items() if key != "data"}
                          for item in bundle.get("results", [])],
            }
            evidence_text = "系統視覺證據（JSON）：" + json.dumps(summary, ensure_ascii=False)
            await emit({"type": "tool", "agent_id": "reviewer", "name": "screenshot_page",
                        "args": {"path": "index.html", "views": [item["view"] for item in images]},
                        "state": "done", "result": evidence_text[:1800]})
            return {"images": images, "text": evidence_text,
                    "captured": bool(images), "metadata": {
                        **summary,
                        "all_blank": bool(images) and all(i.get("is_blank") for i in images),
                        "pixel_analysis_available": bool(images) and all(
                            isinstance(i.get("non_background_ratio"), (int, float))
                            for i in images
                        ),
                    }}

        def visual_evidence_for_dev(evidence: dict | None) -> dict | None:
            if not evidence:
                return evidence
            images = list(evidence.get("images", []))
            count = max(1, VISUAL_DEV_MAX_IMAGES)
            return {**evidence, "images": images[-count:]}

        def attach_scene_assertion_checks() -> None:
            if not visual_evidence:
                return
            scene_info = (
                (visual_evidence.get("metadata") or {})
                .get("state", {}).get("objects")
            )
            automatic = evaluate_scene_assertions(
                pipeline_state.requirement, scene_info
            )
            if not automatic:
                return
            review = pipeline_state.reviewer
            review["checks"] = list(review.get("checks", [])) + automatic
            failed = [item for item in automatic if not item.get("pass")]
            if failed:
                review["verdict"] = "changes_requested"
                review["route_to"] = "dev"
                review["issues"] = list(review.get("issues", [])) + [
                    {"severity": "high", "criterion_id": item["criterion_id"],
                     "desc": item["observation"]}
                    for item in failed
                ]

        logger.info("task_skills_detected", skills=task_skills, user_msg=user_msg[:50])
        await emit({"type": "system", "content": f"🧩 任務技能：{', '.join(task_skills)}"})

        if "qa" in agents_to_run and not is_explicit:
            has_any_code = (any(WORKSPACE_DIR.glob("*.html")) or any(WORKSPACE_DIR.glob("*.py")) or any(WORKSPACE_DIR.glob("*.js")))
            if not has_any_code and "dev" not in agents_to_run:
                agents_to_run.insert(max(0, agents_to_run.index("qa")), "dev")
                await emit({"type": "system", "content": "ℹ️ 工作區尚無檔案，已自動補上工程師"})

        if is_explicit and "qa" in agents_to_run:
            has_any_code = (any(WORKSPACE_DIR.glob("*.html")) or any(WORKSPACE_DIR.glob("*.py")) or any(WORKSPACE_DIR.glob("*.js")))
            if not has_any_code:
                await emit({"type": "system", "content": "⚠️ 你指定了 @qa，但工作區目前沒有檔案。請先用 @dev 產出程式碼，再回來 @qa 測試。"})

        has_reviewer = "reviewer" in agents_to_run

        def collect_tests(aid: str, res: dict):
            for t in res.get("tools_called", []):
                if t.get("state") != "done":
                    continue
                if t.get("name") not in ("run_tests", "run_frontend_check", "run_python", "run_latest_artifact", "get_test_summary"):
                    continue
                result = (t.get("result") or "").strip()
                if not result:
                    continue
                collected_tests.append({"agent_id": aid, "name": t.get("name"), "output": result, "ts": int(time.time() * 1000)})

        async def _handle_dev_output(res: dict, is_first_round: bool):
            nonlocal frozen_tests
            round_arts = res.get("round_artifacts", {})
            round_tests = {k: (v[-1].get("content") or "") for k, v in round_arts.items() if is_test_file(k) and v}

            if is_first_round:
                if round_tests:
                    frozen_tests = round_tests
                    await emit({"type": "system", "content": f"🔒 已凍結 {len(frozen_tests)} 個測試檔案（{', '.join(frozen_tests.keys())}）"})
                return

            if not round_tests:
                if frozen_tests:
                    write_frozen_tests(frozen_tests)
                    await emit({"type": "system", "content": f"🔒 保持凍結測試（{len(frozen_tests)} 個）"})
                return

            if TEST_ISSUE_RE.search(res["content"]):
                frozen_tests = round_tests
                await emit({"type": "system", "content": f"🔓 接受 Dev 的測試修改（{len(frozen_tests)} 個）"})
            else:
                write_frozen_tests(frozen_tests)
                await emit({"type": "system", "content": "🔒 已忽略 Dev 對測試檔的修改"})

        try:
            transcript_tokens = await count_tokens(transcript)
            n_ctx = await get_n_ctx()
            ctx_pct = round(transcript_tokens / max(n_ctx - SYSTEM_BUDGET, 1) * 100, 1)
            await emit({"type": "metrics", "ctx_pct": min(100, ctx_pct), "transcript_tokens": transcript_tokens, "n_ctx": n_ctx})
        except Exception:
            pass

        try:
            # ============================================================
            # 首輪
            # ============================================================
            with logger.span(trace_id, "phase:initial", phase="initial"):
                for aid in agents_to_run:
                    next_phase = {"dev": "implementing", "qa": "testing", "reviewer": "reviewing"}.get(aid)
                    if next_phase and next_phase != pipeline_state.phase:
                        pipeline_state.transition(next_phase, f"開始 {aid} 階段", route_to=aid)
                    agent = AGENTS[aid]
                    qa_hint = None
                    agent_image_context = None

                    if aid == "qa":
                        project_info = detect_project_kind()
                        await emit({"type": "system", "content": f"📁 工作區：{project_info['kind']} 專案\n{project_info['summary']}"})
                        auto_results = await _auto_run_tests(emit, project_info, task_skills, collected_tests)
                        test_context = build_qa_analysis_context(project_info, auto_results, task_skills)
                        qa_hint = pipeline_state.qa_context(acceptance_context(), test_context)
                        await push_test_verification(emit, {"tools_called": [{"name": r["name"], "state": "done", "result": r["result"]} for r in auto_results]})
                    elif aid == "pm" and visual_required:
                        qa_hint = (
                            "請依 system prompt 的本任務技術環境整理需求。簡單任務採合理假設繼續，"
                            "不要因非必要細節 blocked。"
                        )
                    elif aid == "dev":
                        if frozen_tests:
                            write_frozen_tests(frozen_tests)
                        t_type = session.meta.get("task_type", "complex")
                        if t_type == "frontend":
                            pm_output = (responses.get("pm") or "").strip()
                            pm_context = f"[PM 需求清單]\n{pm_output}\n\n" if pm_output else ""
                            qa_hint = (f"[使用者需求]\n{user_msg}\n\n{pm_context}"
                                       "這是前端／視覺任務。請依需求與驗收規格產出完整 HTML/CSS/JS；"
                                       "不需要 pytest，且不可用未指定的替代技術取代使用者指定技術。")
                        elif "python" in task_skills:
                            pm_output = (responses.get("pm") or "").strip()
                            if pm_output:
                                qa_hint = f"[PM 需求清單]\n{pm_output}\n\n[原始使用者需求]\n{user_msg}\n\n請嚴格依照需求輸出完整專案程式碼，並包含對應的 pytest 測試檔。"
                            else:
                                qa_hint = f"[使用者需求]\n{user_msg}\n\n請輸出完整專案程式碼，並包含對應的 pytest 測試檔。"
                        else:
                            pm_output = (responses.get("pm") or "").strip()
                            qa_hint = (
                                f"[使用者需求]\n{user_msg}\n\n"
                                + (f"[PM 需求清單]\n{pm_output}\n\n" if pm_output else "")
                                + "請依共享驗收標準選擇合適技術，輸出完整實作與相符的驗證方式。"
                            )
                        qa_hint += (
                            "\n\n[共享驗收標準]\n" + acceptance_context()
                            + "\n此驗收標準供 Dev、QA、Reviewer 共用，請勿改寫成不同目標。"
                        )
                    elif aid == "reviewer":
                        qa_hint = pipeline_state.reviewer_context(
                            acceptance_context(), pipeline_state.qa
                        )
                        if visual_required:
                            visual_evidence = await capture_visual_evidence()
                            reviewer_screenshot_captured = bool(
                                visual_evidence and visual_evidence.get("captured")
                            )
                            agent_image_context = visual_evidence
                            if visual_evidence:
                                qa_hint += "\n\n" + visual_evidence.get("text", "")

                    if aid in ("pm", "dev"):
                        if aid == "pm" and not fresh_task:
                            agent_transcript = session.build_pm_transcript(user_msg)
                        else:
                            agent_transcript = f"使用者: {user_msg}"
                        if aid == "dev" and allow_workspace_tools:
                            agent_transcript += (
                                "\n\n[目前工作區程式碼]\n"
                                + workspace_snapshot(WORKSPACE_DIR)
                            )
                    else:
                        # QA/Reviewer receive explicit state bundles below, never chat history.
                        agent_transcript = ""

                    res = await run_and_stream(
                        emit, agent, agent_transcript,
                        session.session_id, round_no=round_no, version=1, phase=None,
                        artifacts=artifacts, stop_flag=stop_flag,
                        extra_context=qa_hint, task_skills=task_skills,
                        is_refine=False,
                        allow_workspace_tools=allow_workspace_tools,
                        image_context=agent_image_context,
                    )
                    if stop_flag.get("stop"):
                        return
                    if res.get("error"):
                        raise AgentGenerationError(res["error"])

                    if aid == "dev":
                        await _handle_dev_output(res, is_first_round=True)
                    if aid == "reviewer":
                        reviewer_screenshot_captured = bool(
                            visual_evidence and visual_evidence.get("captured")
                        ) if visual_required else False
                    if aid != "qa":
                        collect_tests(aid, res)

                    responses[aid] = res["content"]
                    if aid == "pm":
                        pm_contract = parse_agent_contract(res["content"], "pm")
                        if pm_contract.get("requirements"):
                            pipeline_state.requirement = pm_contract
                        if ((pm_contract.get("verdict") == "blocked" or pm_contract.get("route_to") == "human")
                                and PM_BLOCK_ON_UNRESOLVABLE):
                            pipeline_state.qa = {
                                "verdict": "blocked",
                                "route_to": "human",
                                "summary": pm_contract.get("summary") or "PM 需要使用者補充需求。",
                            }
                            pipeline_state.route_to = "human"
                            pipeline_state.transition("blocked", "PM 需要澄清需求", route_to="human")
                            await emit({
                                "type": "system",
                                "content": f"⏸️ 需求需要澄清：{pipeline_state.qa['summary']}",
                            })
                        elif pm_contract.get("verdict") == "blocked" or pm_contract.get("route_to") == "human":
                            pipeline_state.requirement.setdefault("assumptions", []).append(
                                "PM 阻塞策略已停用；使用目前可行的預設繼續。"
                            )
                            await emit({
                                "type": "system",
                                "content": "PM 提出的歧義已依設定採合理預設，流程繼續。",
                            })
                    elif aid == "dev":
                        pipeline_state.record_artifacts(artifact_files_from_result(res))
                    elif aid == "qa":
                        pipeline_state.qa = parse_agent_contract(res["content"], "qa")
                    elif aid == "reviewer":
                        pipeline_state.reviewer = parse_agent_contract(res["content"], "reviewer")
                        attach_scene_assertion_checks()
                        if (pipeline_state.reviewer.get("unlock_test")
                                or UNLOCK_RE.search(res["content"])):
                            frozen_tests = {}
                            await emit({"type": "system", "content": "🔓 Reviewer 指出測試本身有誤，已解除測試凍結"})
                    all_messages.append({"agent_id": aid, "content": res["content"], "phase": None})
                    transcript += f"\n\n[{agent.name}]:\n{res['content']}"
                    if aid == "pm" and pipeline_state.route_to == "human":
                        break

            dev_output = responses.get("dev", "")
            qa_output = responses.get("qa", "")
            reviewer_output = responses.get("reviewer", "")
            pipeline_state.tests = list(collected_tests)
            if qa_output:
                pipeline_state.qa = parse_agent_contract(qa_output, "qa")
            if reviewer_output:
                pipeline_state.reviewer = parse_agent_contract(reviewer_output, "reviewer")

            if not has_reviewer and "qa" not in agents_to_run:
                if pipeline_state.phase != "done":
                    pipeline_state.transition("done", "使用者明確指定只執行部分 Agent", route_to="done")
                summary = build_final_summary(user_msg, responses, all_messages, 0, artifacts, "DONE", None)
                await emit({"type": "final_delivery", "verdict": "DONE", "rounds": 0, "stop_reason": None, "summary": summary})
            elif (not has_reviewer
                    and pipeline_state.qa.get("verdict") == "approved"
                    and pipeline_state.qa.get("route_to") == "done"):
                if pipeline_state.phase != "done":
                    pipeline_state.transition("done", "QA 已核准", route_to="done")
                summary = build_final_summary(user_msg, responses, all_messages, 0, artifacts, "PASS", None)
                await emit({"type": "final_delivery", "verdict": "PASS", "rounds": 0, "stop_reason": None, "summary": summary})
            else:
                if has_reviewer:
                    final_verdict, visual_guard_reason = guard_visual_verdict(
                        reviewer_output,
                        "frontend" if visual_required else session.meta.get("task_type", "complex"),
                        reviewer_screenshot_captured,
                        pipeline_state.requirement,
                        visual_evidence.get("metadata") if visual_evidence else None,
                    )
                else:
                    final_verdict = "PASS" if pipeline_state.qa.get("verdict") == "approved" else "REVISE"
                    visual_guard_reason = None

                if visual_guard_reason:
                    pipeline_state.reviewer = {
                        **pipeline_state.reviewer,
                        "verdict": "changes_requested",
                        "route_to": "dev",
                        "summary": visual_guard_reason,
                    }
                    await emit({"type": "system", "content": f"🛡️ {visual_guard_reason}"})

                next_route, route_reason = choose_next_route(
                    pipeline_state,
                    visual_required=(visual_required and has_reviewer),
                )
                logger.info("pipeline_route_decision", route=next_route, reason=route_reason)
                if next_route in ("human", "pm"):
                    await emit({"type": "system", "content": f"⏸️ 流程暫停：{route_reason}"})
                if next_route != "done":
                    final_verdict = "REVISE"
                pipeline_state.route_to = next_route
                pipeline_state.transition(
                    "done" if next_route == "done" else "implementing" if next_route == "dev" else "blocked",
                    route_reason,
                    route_to=next_route,
                )
                
                if final_verdict == "PASS":
                    tests_ok, tests_reason = _all_tests_passed(collected_tests)
                    if not tests_ok:
                        pipeline_state.qa = {
                            **pipeline_state.qa,
                            "verdict": "changes_requested",
                            "route_to": "dev",
                            "summary": tests_reason,
                        }
                        logger.warn("verdict_override_by_backend", original="PASS", forced="REVISE", reason=tests_reason, phase="initial")
                        await emit({"type": "system", "content": f"⚠️ 後端否決 PASS：{tests_reason}。強制進入修正。"})
                        final_verdict = "REVISE"

                await emit({"type": "verdict", "round": 0, "verdict": final_verdict})

                soft_limit = min(
                    MAX_REFINE_ROUNDS,
                    MAX_REFINE_HARD_LIMIT,
                    MAX_QUALITY_ROUNDS if visual_required else MAX_REFINE_HARD_LIMIT,
                )
                prev_reviewer: str | None = reviewer_output
                prev_dev: str | None = dev_output
                prev_passed = extract_test_counts(qa_output)[0]
                prev_issue_count = len(pipeline_state.reviewer.get("issues", []))
                prev_quality = pipeline_state.reviewer.get("visual_quality")
                no_progress_count = 0
                stop_reason: str | None = None

                refine_round = 0
                refine_pipeline = [a for a in ["dev", "qa", "reviewer"] if a in agents_to_run]
                initial_route_reason = route_reason

                while (refine_round < soft_limit and final_verdict == "REVISE"
                       and pipeline_state.route_to in ("dev", "pm")):
                    if stop_flag.get("stop"):
                        return
                    refine_round += 1
                    phase_label = f"第 {refine_round} 輪改進"
                    await emit({"type": "phase", "label": phase_label, "round": refine_round})

                    with logger.span(trace_id, f"phase:refine:{refine_round}", phase=phase_label):
                        pipeline_state.iteration = refine_round
                        active_pipeline = (["pm"] + refine_pipeline
                                           if pipeline_state.route_to == "pm"
                                           else refine_pipeline)
                        if active_pipeline[0] == "pm":
                            if pipeline_state.phase != "blocked":
                                pipeline_state.transition("blocked", "下游要求 PM 重新釐清", route_to="pm")
                            pipeline_state.transition("planning", "PM 重新評估需求", route_to="pm")

                        pm_replan_blocked = False
                        for aid in active_pipeline:
                            if aid == "pm":
                                pm_context = (
                                    f"[原始需求]\n{user_msg}\n\n"
                                    f"[目前驗收規格]\n{acceptance_context()}\n\n"
                                    f"[QA 結果]\n{pipeline_state.qa.get('summary', '')}\n"
                                    f"[Reviewer 結果]\n{pipeline_state.reviewer.get('summary', '')}\n"
                                    "請根據阻塞問題更新規格。若仍需使用者提供資訊，verdict=blocked、route_to=human。"
                                )
                                pm_res = await run_and_stream(
                                    emit, AGENTS["pm"], "", session.session_id,
                                    round_no=round_no, version=refine_round + 1,
                                    phase=phase_label, artifacts=artifacts,
                                    stop_flag=stop_flag, extra_context=pm_context,
                                    task_skills=task_skills, is_refine=True,
                                    allow_workspace_tools=False,
                                )
                                if pm_res.get("error"):
                                    raise AgentGenerationError(pm_res["error"])
                                pm_contract = parse_agent_contract(pm_res["content"], "pm")
                                all_messages.append({"agent_id": "pm", "content": pm_res["content"], "phase": phase_label})
                                responses["pm"] = pm_res["content"]
                                if ((pm_contract.get("verdict") != "approved" or not pm_contract.get("requirements"))
                                        and PM_BLOCK_ON_UNRESOLVABLE):
                                    pipeline_state.route_to = "human"
                                    pipeline_state.transition("blocked", "PM 判定需求仍需人工澄清", route_to="human")
                                    stop_reason = pm_contract.get("summary") or "PM 需要使用者澄清需求。"
                                    final_verdict = "REVISE"
                                    pm_replan_blocked = True
                                    break
                                pipeline_state.requirement = pm_contract
                                pipeline_state.transition("implementing", "PM 更新驗收規格後交回 Dev", route_to="dev")
                                continue

                            next_phase = {"dev": "implementing", "qa": "testing", "reviewer": "reviewing"}.get(aid)
                            if next_phase and next_phase != pipeline_state.phase:
                                pipeline_state.transition(
                                    next_phase, f"第 {refine_round} 輪開始 {aid} 階段",
                                    route_to=aid,
                                )
                            agent = AGENTS[aid]
                            qa_hint = None

                            if aid == "dev":
                                prev_code = pipeline_state.artifact_context()
                                if len(prev_code) > REFINE_PREVIOUS_CODE_CHARS:
                                    prev_code = (
                                        "[前一版較早內容已省略]\n"
                                        + prev_code[-REFINE_PREVIOUS_CODE_CHARS:]
                                    )
                                qa_summary = _extract_summary_block(qa_output)
                                rev_summary = _extract_summary_block(reviewer_output)
                                qa_issues = pipeline_state.qa.get("issues", [])
                                review_issues = pipeline_state.reviewer.get("issues", [])
                                failed_checks = [
                                    item.get("output", "")[-1800:]
                                    for item in collected_tests
                                    if re.search(r"failed|error|runtime|pageerror|console", item.get("output", ""), re.I)
                                ]

                                parts = [
                                    f"[原始使用者需求]\n{user_msg}",
                                    f"[目前驗收規格]\n{acceptance_context()}",
                                    f"[你上一版的程式碼（節選）]\n```\n{prev_code}\n```",
                                ]
                                if qa_summary:
                                    parts.append(f"[QA 測試摘要]\n{qa_summary}")
                                if qa_issues:
                                    parts.append("[QA issues]\n" + json.dumps(qa_issues, ensure_ascii=False)[:2400])
                                parts.append(f"[評審意見摘要]\n{rev_summary}")
                                if review_issues:
                                    parts.append("[Reviewer issues]\n" + json.dumps(review_issues, ensure_ascii=False)[:3000])
                                if failed_checks:
                                    parts.append("[失敗的自動 checks / 瀏覽器錯誤]\n" + "\n".join(failed_checks[-3:]))
                                if visual_evidence and visual_evidence.get("text"):
                                    parts.append("[上一版截圖診斷]\n" + visual_evidence["text"][:2400])
                                if visual_guard_reason:
                                    parts.append(f"[系統視覺驗收摘要]\n{visual_guard_reason}")
                                
                                if frozen_tests:
                                    parts.append("[系統提示] 測試檔已凍結，**只修改實作檔**。")
                                
                                if refine_round >= PIVOT_ROUND:
                                    parts.append("[系統強制指令 - 策略轉向]\n改進已超過 3 輪但仍未通過。請**停止修補**，退一步重新思考架構，從頭重寫一份全新的實作。")
                                elif pm_contract.get("verdict") != "approved" or not pm_contract.get("requirements"):
                                    pipeline_state.requirement.setdefault("assumptions", []).append(
                                        "PM 阻塞策略已停用；沿用既有驗收標準並繼續。"
                                    )
                                    pipeline_state.transition("implementing", "PM 阻塞已依設定轉為合理假設", route_to="dev")
                                else:
                                    parts.append("請根據上述 QA 摘要與評審意見修改實作。保留正確部分，只改錯誤。")
                                extra = "\n\n".join(parts)

                                dev_res = await run_and_stream(
                                    emit, agent, "", session.session_id,
                                    round_no=round_no, version=refine_round + 1, phase=phase_label,
                                    artifacts=artifacts, stop_flag=stop_flag, extra_context=extra,
                                    task_skills=task_skills, is_refine=True,
                                    image_context=visual_evidence_for_dev(visual_evidence) if visual_required else None,
                                )
                                if stop_flag.get("stop"):
                                    return
                                if dev_res.get("error"):
                                    raise AgentGenerationError(dev_res["error"])
                                dev_output = dev_res["content"]
                                pipeline_state.record_artifacts(artifact_files_from_result(dev_res))
                                await _handle_dev_output(dev_res, is_first_round=False)
                                if visual_required:
                                    visual_evidence = await capture_visual_evidence()
                                    reviewer_screenshot_captured = bool(
                                        visual_evidence and visual_evidence.get("captured")
                                    )
                                collect_tests("dev", dev_res)
                                all_messages.append({"agent_id": "dev", "content": dev_output, "phase": phase_label})
                                transcript += f"\n\n[{agent.name}({phase_label})]:\n{dev_output}"

                                if frozen_tests:
                                    write_frozen_tests(frozen_tests)

                            elif aid == "qa":
                                project_info = detect_project_kind()
                                auto_results = await _auto_run_tests(emit, project_info, task_skills, collected_tests)
                                qa_hint = build_qa_analysis_context(project_info, auto_results, task_skills)
                                qa_hint = pipeline_state.qa_context(acceptance_context(), qa_hint)
                                await push_test_verification(emit, {"tools_called": [{"name": r["name"], "state": "done", "result": r["result"]} for r in auto_results]})

                                qa_res = await run_and_stream(
                                    emit, agent, "", session.session_id,
                                    round_no=round_no, version=refine_round + 1, phase=phase_label,
                                    artifacts=artifacts, stop_flag=stop_flag, extra_context=qa_hint,
                                    task_skills=task_skills, is_refine=True,
                                )
                                if stop_flag.get("stop"):
                                    return
                                if qa_res.get("error"):
                                    raise AgentGenerationError(qa_res["error"])
                                qa_output = qa_res["content"]
                                pipeline_state.qa = parse_agent_contract(qa_output, "qa")
                                pipeline_state.tests = list(collected_tests)
                                all_messages.append({"agent_id": "qa", "content": qa_output, "phase": phase_label})
                                transcript += f"\n\n[{agent.name}({phase_label})]:\n{qa_output}"

                            elif aid == "reviewer":
                                review_context = pipeline_state.reviewer_context(
                                    acceptance_context(), pipeline_state.qa
                                )
                                if visual_required and visual_evidence:
                                    review_context += "\n\n" + visual_evidence.get("text", "")
                                rev_res = await run_and_stream(
                                    emit, agent, "", session.session_id,
                                    round_no=round_no, version=refine_round + 1, phase=phase_label,
                                    artifacts=artifacts, stop_flag=stop_flag, extra_context=review_context,
                                    task_skills=task_skills, is_refine=True,
                                    image_context=visual_evidence if visual_required else None,
                                )
                                if stop_flag.get("stop"):
                                    return
                                if rev_res.get("error"):
                                    raise AgentGenerationError(rev_res["error"])
                                reviewer_output = rev_res["content"]
                                pipeline_state.reviewer = parse_agent_contract(reviewer_output, "reviewer")
                                attach_scene_assertion_checks()
                                reviewer_screenshot_captured = bool(
                                    visual_evidence and visual_evidence.get("captured")
                                ) if visual_required else False

                                if (pipeline_state.reviewer.get("unlock_test")
                                        or UNLOCK_RE.search(reviewer_output)):
                                    frozen_tests = {}
                                    await emit({"type": "system", "content": "🔓 Reviewer 主動解鎖測試"})

                                all_messages.append({"agent_id": "reviewer", "content": reviewer_output, "phase": phase_label})
                                transcript += f"\n\n[{agent.name}({phase_label})]:\n{reviewer_output}"

                        if pm_replan_blocked:
                            await emit({"type": "verdict", "round": refine_round, "verdict": "REVISE"})
                            break

                        if has_reviewer:
                            final_verdict, visual_guard_reason = guard_visual_verdict(
                                reviewer_output,
                                "frontend" if visual_required else session.meta.get("task_type", "complex"),
                                reviewer_screenshot_captured,
                                pipeline_state.requirement,
                                visual_evidence.get("metadata") if visual_evidence else None,
                            )
                        else:
                            final_verdict = "PASS" if pipeline_state.qa.get("verdict") == "approved" else "REVISE"
                            visual_guard_reason = None
                        if visual_guard_reason:
                            pipeline_state.reviewer = {
                                **pipeline_state.reviewer,
                                "verdict": "changes_requested",
                                "route_to": "dev",
                                "summary": visual_guard_reason,
                            }
                            await emit({"type": "system", "content": f"🛡️ {visual_guard_reason}"})
                        
                        if final_verdict == "PASS":
                            tests_ok, tests_reason = _all_tests_passed(collected_tests)
                            if not tests_ok:
                                pipeline_state.qa = {
                                    **pipeline_state.qa,
                                    "verdict": "changes_requested",
                                    "route_to": "dev",
                                    "summary": tests_reason,
                                }
                                logger.warn("verdict_override_by_backend", original="PASS", forced="REVISE", reason=tests_reason, phase=f"refine:{refine_round}")
                                await emit({"type": "system", "content": f"⚠️ 後端否決 PASS：{tests_reason}。強制進入下一輪修正。"})
                                final_verdict = "REVISE"

                        next_route, route_reason = choose_next_route(
                            pipeline_state,
                            visual_required=(visual_required and has_reviewer),
                        )
                        logger.info("pipeline_route_decision", route=next_route,
                                    reason=route_reason, iteration=refine_round)
                        if next_route in ("human", "pm"):
                            await emit({"type": "system", "content": f"⏸️ 流程暫停：{route_reason}"})
                        if next_route != "done":
                            final_verdict = "REVISE"
                        pipeline_state.route_to = next_route
                        target_phase = (
                            "done" if next_route == "done"
                            else "implementing" if next_route == "dev"
                            else "blocked"
                        )
                        if target_phase != pipeline_state.phase:
                            pipeline_state.transition(target_phase, route_reason, route_to=next_route)

                        await emit({"type": "verdict", "round": refine_round, "verdict": final_verdict})

                    if final_verdict == "PASS":
                        break

                    cur_passed, _ = extract_test_counts(qa_output)
                    new_stop_reason, no_progress_count = check_refine_stop(
                        prev_reviewer=prev_reviewer, prev_dev=prev_dev, prev_passed=prev_passed,
                        no_progress_count=no_progress_count, cur_reviewer=reviewer_output,
                        cur_dev=dev_output, cur_passed=cur_passed,
                        visual_task=visual_required,
                        prev_issue_count=prev_issue_count,
                        cur_issue_count=len(pipeline_state.reviewer.get("issues", [])),
                        prev_quality=prev_quality,
                        cur_quality=pipeline_state.reviewer.get("visual_quality"),
                    )

                    if new_stop_reason:
                        stop_reason = new_stop_reason
                        pipeline_state.route_to = "human"
                        if pipeline_state.phase != "blocked":
                            pipeline_state.transition("blocked", new_stop_reason, route_to="human")
                        logger.info("refine_early_stop", round=refine_round, reason=new_stop_reason)
                        await emit({"type": "system", "content": f"⏹ 提前停止：{new_stop_reason}"})
                        break

                    prev_reviewer = reviewer_output
                    prev_dev = dev_output
                    prev_passed = cur_passed
                    prev_issue_count = len(pipeline_state.reviewer.get("issues", []))
                    prev_quality = pipeline_state.reviewer.get("visual_quality")

                if final_verdict == "REVISE" and not stop_reason:
                    if pipeline_state.route_to in ("pm", "human"):
                        stop_reason = f"需要人工／PM 釐清：{initial_route_reason}"
                    else:
                        stop_reason = f"已達改進輪數上限（{soft_limit} 輪）"
                        pipeline_state.route_to = "human"
                        logger.info("refine_limit_reached", limit=soft_limit,
                                    task_type=pipeline_state.task_type,
                                    verdict=final_verdict)
                        await emit({"type": "system", "content": f"⏹ 自動改進已達 {soft_limit} 輪上限；{stop_reason}"})
                        if pipeline_state.phase != "blocked":
                            pipeline_state.transition("blocked", stop_reason, route_to="human")

                summary = build_final_summary(user_msg, responses, all_messages, refine_round, artifacts, final_verdict, stop_reason)
                
                if final_verdict == "PASS":
                    is_high_risk = _has_high_risk_code(dev_output)
                    if is_high_risk:
                        logger.warn("high_risk_code_detected")
                        await emit({"type": "system", "content": "⚠️ 系統檢測到高風險代碼（如 eval, os, auth）。AI 批准不能頂替人，已標記為需人工審查。"})
                        summary += "\n\n**[SYSTEM] ⚠️ 高風險代碼警告：此專案涉及敏感操作，請人類開發者務必人工複核。**"
                        stop_reason = "高風險代碼，需人工複核"

                delivery_verdict = (
                    "BLOCKED" if pipeline_state.route_to in ("pm", "human")
                    else final_verdict
                )
                await emit({"type": "final_delivery", "verdict": delivery_verdict, "rounds": refine_round, "stop_reason": stop_reason, "summary": summary})

            for m in all_messages:
                aid = m["agent_id"]
                if aid not in responses:
                    responses[aid] = m["content"]
                elif m.get("phase"):
                    responses[aid] += "\n\n" + m["content"]

            pipeline_state.tests = list(collected_tests)
            session.add_round(
                user_msg, responses, artifacts, messages=all_messages,
                tests=collected_tests, shared_state=pipeline_state.as_dict(),
            )
            session.frozen_tests = frozen_tests
            session.save()

            await extract_facts(user_msg, responses, session.session_id)

            t = logger.finish_trace(trace_id)
            logger.info("round_done", session=session.session_id, agents=agents_to_run, task_skills=task_skills, verdict=locals().get("final_verdict", "DONE"), duration_ms=t["duration_ms"] if t else 0)
            await emit({"type": "round_done", "session": session.meta})

        except asyncio.CancelledError:
            logger.info("round_cancelled", session=session.session_id, completed_agents=list(responses.keys()))
            if session and (responses or all_messages or artifacts):
                try:
                    for m in all_messages:
                        aid = m["agent_id"]
                        if aid not in responses:
                            responses[aid] = m["content"]
                        elif m.get("phase"):
                            responses[aid] += "\n\n" + m["content"]

                    session.add_round(
                        user_msg, responses, artifacts, messages=all_messages,
                        tests=collected_tests, interrupted=True,
                        shared_state=(pipeline_state.as_dict()
                                      if "pipeline_state" in locals() else {}),
                    )
                    session.frozen_tests = frozen_tests
                    session.save()
                    logger.info("round_saved_on_cancel", session=session.session_id, agents=len(responses))
                except Exception as e:
                    logger.error("save_on_cancel_failed", error=str(e))
            try:
                await emit({"type": "round_cancelled"})
            except Exception:
                pass

        except AgentGenerationError as e:
            logger.error("agent_generation_failed", session=session.session_id, error=str(e))
            try:
                await emit({
                    "type": "final_delivery",
                    "verdict": "ERROR",
                    "rounds": 0,
                    "stop_reason": str(e),
                    "summary": f"未完成：{e}",
                })
            except Exception:
                pass
            logger.finish_trace(trace_id)

        except Exception as e:
            logger.error("run_chat_exception", error=str(e), exc_type=type(e).__name__)
            print("\n" + "=" * 60)
            print("  ✗ _run_chat 內發生異常")
            print("=" * 60)
            traceback.print_exc()
            print("=" * 60 + "\n")
            try:
                await emit({"type": "error", "message": f"後端錯誤: {type(e).__name__}: {e}"})
            except Exception:
                pass
            raise

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type", "chat")

            if msg_type == "stop":
                stop_flag["stop"] = True
                if current_task and not current_task.done():
                    current_task.cancel()
                continue

            if msg_type == "bind":
                sid = (data.get("session_id") or "").strip()
                if sid and (SESSIONS_DIR / f"{sid}.json").exists():
                    session = Session(sid)
                    await emit({"type": "bound", "session": session.meta, "history": session.history, "frozen_tests": list((session.frozen_tests or {}).keys())})
                else:
                    session = None
                    await emit({"type": "bound", "session": {"id": None, "title": "新對話", "created": 0, "updated": 0}, "history": [], "frozen_tests": []})
                continue

            if msg_type != "chat":
                continue

            try:
                user_msg = check_user_input(data.get("message") or "")
            except GuardrailError as e:
                await emit({"type": "error", "message": str(e)})
                continue

            if session is None:
                sid = uuid.uuid4().hex[:12]
                session = Session(sid)
                await emit({"type": "bound", "session": session.meta, "history": [], "frozen_tests": []})

            stop_flag["stop"] = False
            current_task = asyncio.create_task(_run_chat(user_msg))
            current_task.add_done_callback(_log_task_exception)

    except WebSocketDisconnect:
        logger.info("ws_disconnected", session=session.session_id if session else None)
        stop_flag["stop"] = True
        if current_task and not current_task.done():
            current_task.cancel()
            try:
                await asyncio.sleep(0.3)
            except Exception:
                pass
        return
    except Exception as e:
        logger.error("ws_error", error=str(e))
        try:
            await emit({"type": "error", "message": str(e)})
        except Exception:
            pass


if __name__ == "__main__":
    ensure_dirs()
    ensure_vendor()
    cleanup_empty_sessions()
    idx = rag.build_index()
    logger.info("rag_index_built", chunks=idx["built"])
    n_ctx = asyncio.run(get_n_ctx())
    logger.info("n_ctx_detected", n_ctx=n_ctx)
    uvicorn.run(app, host="127.0.0.1", port=8000)

