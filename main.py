import asyncio
import json
import re
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import (
    AGENTS, PIPELINE, SESSIONS_DIR, MAX_REFINE_ROUNDS,
    ensure_dirs, get_n_ctx, SYSTEM_BUDGET, BASE_DIR,
)
from memory import Session, list_sessions, long_term
from guardrails import check_user_input, GuardrailError
from observability import logger
from agent import run_agent, extract_facts
from tools import execute as execute_tool, parse_verdict_tag
from token_budget import check_llama_health, count_tokens
import rag

app = FastAPI()

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

VERDICT_RE = re.compile(r"<verdict>\s*(PASS|REVISE)\s*</verdict>", re.IGNORECASE)

ALLOWED_ORIGINS = {
    "http://127.0.0.1:8000", "http://localhost:8000",
    "http://127.0.0.1:3000", "http://localhost:3000",
}


def parse_verdict(text: str) -> str:
    v = parse_verdict_tag(text)
    if v:
        return v
    tail = text[-400:]
    if any(k in tail for k in ("LGTM", "透過", "透過", "無需修改", "無需修改",
                                "沒有問題", "沒有問題", "Approved", "PASS")):
        return "PASS"
    return "REVISE"


# ---------- REST ----------
@app.get("/")
async def index():
    return HTMLResponse(
        (BASE_DIR / "index.html").read_text(encoding="utf-8")
    )


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
    return {"llama_server": ok, "detail": msg, "n_ctx": n_ctx}


# ---------- Agent 執行輔助 ----------
async def _run_and_stream(ws: WebSocket, agent, transcript: str,
                          session_id: str, round_no: int, version: int,
                          phase: str | None,
                          artifacts: dict[str, list[dict]],
                          stop_flag: dict,
                          extra_context: str | None = None) -> dict:
    event = {
        "type": "agent_start",
        "agent": {
            "id": agent.id, "name": agent.name,
            "avatar": agent.avatar, "color": agent.color,
        },
        "round_no": round_no,
        "version": version,
    }
    if phase:
        event["phase"] = phase
    await ws.send_json(event)

    full = ""
    metrics = None
    stopped = False
    tool_called = False
    tools_called: list[dict] = []

    async for ev in run_agent(
        agent, transcript, session_id,
        round_no=round_no, version=version,
        extra_context=extra_context,
        stop_check=lambda: stop_flag.get("stop", False),
    ):
        if ev["type"] == "token":
            full += ev["token"]
            await ws.send_json({
                "type": "token",
                "agent_id": agent.id,
                "token": ev["token"],
            })
        elif ev["type"] == "tool":
            tool_called = True
            tool_record = {
                "name": ev.get("name"),
                "args": ev.get("args"),
                "state": ev.get("state"),
                "result": ev.get("result"),
                "duration_ms": ev.get("duration_ms"),
            }
            tools_called.append(tool_record)
            await ws.send_json({
                "type": "tool",
                "agent_id": agent.id,
                **{k: ev.get(k) for k in
                   ("name", "args", "state", "result", "duration_ms")},
            })
        elif ev["type"] == "artifact":
            for f in ev["files"]:
                key = f.get("logical_name") or f["filename"]
                artifacts.setdefault(key, []).append(f)
            await ws.send_json({
                "type": "artifact",
                "agent_id": agent.id,
                "files": ev["files"],
            })
        elif ev["type"] == "done":
            metrics = {
                "tokens": ev.get("tokens"),
                "duration_ms": ev.get("duration_ms"),
                "tps": ev.get("tps"),
            }
        elif ev["type"] == "stopped":
            stopped = True
            metrics = {
                "duration_ms": ev.get("duration_ms"),
                "tps": 0,
                "tokens": 0,
            }

    await ws.send_json({
        "type": "agent_done",
        "agent_id": agent.id,
        "metrics": metrics,
    })
    return {
        "content": full,
        "metrics": metrics,
        "stopped": stopped,
        "tool_called": tool_called,
        "tools_called": tools_called,
    }


async def _auto_run_tests_if_qa_skipped(ws: WebSocket, aid: str, res: dict) -> dict:
    """QA 未呼叫 run_tests 時，系統自動補跑並推送工具事件。"""
    if aid != "qa" or res.get("tool_called"):
        return res

    logger.warn("qa_no_tool_call", action="auto_inject_run_tests")

    await ws.send_json({
        "type": "tool",
        "agent_id": "qa",
        "name": "run_tests",
        "args": {},
        "state": "start",
    })
    auto = execute_tool("run_tests", {})
    await ws.send_json({
        "type": "tool",
        "agent_id": "qa",
        "name": "run_tests",
        "args": {},
        "state": "done",
        "result": auto[:4000],
        "duration_ms": 0,
        "_auto": True,
    })
    res["content"] += f"\n\n[系統自動補跑 pytest]\n{auto[:1500]}"
    res.setdefault("tools_called", []).append({
        "name": "run_tests",
        "args": {},
        "state": "done",
        "result": auto,
        "duration_ms": 0,
    })
    res["tool_called"] = True
    return res


# ---------- WebSocket ----------
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

    async def _run_chat(user_msg: str):
        nonlocal session
        trace_id = logger.start_trace("chat_round", session=session.session_id)
        transcript = session.build_transcript(user_msg)
        responses: dict[str, str] = {}
        artifacts: dict[str, list[dict]] = {}
        all_messages: list[dict] = []
        collected_tests: list[dict] = []
        round_no = len(session.history) + 1

        def collect_tests(aid: str, res: dict):
            """把 res 裡 run_tests / run_python 的完成態工具結果存到 collected_tests。"""
            for t in res.get("tools_called", []):
                if t.get("state") != "done":
                    continue
                if t.get("name") not in ("run_tests", "run_python",
                                          "run_latest_artifact"):
                    continue
                result = (t.get("result") or "").strip()
                if not result:
                    continue
                collected_tests.append({
                    "agent_id": aid,
                    "name": t.get("name"),
                    "output": result,
                    "ts": int(time.time() * 1000),
                })

        try:
            transcript_tokens = await count_tokens(transcript)
            n_ctx = await get_n_ctx()
            ctx_pct = round(
                transcript_tokens / max(n_ctx - SYSTEM_BUDGET, 1) * 100, 1
            )
            await ws.send_json({
                "type": "metrics",
                "ctx_pct": min(100, ctx_pct),
                "transcript_tokens": transcript_tokens,
                "n_ctx": n_ctx,
            })
        except Exception:
            pass

        try:
            # ========== 首輪：PM → Dev → QA → Reviewer ==========
            with logger.span(trace_id, "phase:initial", phase="initial"):
                for aid in PIPELINE:
                    agent = AGENTS[aid]
                    res = await _run_and_stream(
                        ws, agent, transcript, session.session_id,
                        round_no=round_no, version=1, phase=None,
                        artifacts=artifacts, stop_flag=stop_flag,
                    )
                    if stop_flag.get("stop"):
                        return

                    # QA 兜底
                    res = await _auto_run_tests_if_qa_skipped(ws, aid, res)

                    # 收集測試結果
                    collect_tests(aid, res)

                    responses[aid] = res["content"]
                    all_messages.append({
                        "agent_id": aid,
                        "content": res["content"],
                        "phase": None,
                    })
                    transcript += f"\n\n[{agent.name}]:\n{res['content']}"

            dev_agent = AGENTS["dev"]
            qa_agent = AGENTS["qa"]
            reviewer_agent = AGENTS["reviewer"]

            dev_output = responses.get("dev", "")
            qa_output = responses.get("qa", "")
            reviewer_output = responses.get("reviewer", "")
            refine_round = 0
            final_verdict = parse_verdict(reviewer_output)

            await ws.send_json({
                "type": "verdict",
                "round": 0,
                "verdict": final_verdict,
            })

            # ========== 改進迴圈 ==========
            while refine_round < MAX_REFINE_ROUNDS and final_verdict == "REVISE":
                if stop_flag.get("stop"):
                    return
                refine_round += 1
                phase_label = f"第 {refine_round} 輪改進"
                await ws.send_json({
                    "type": "phase",
                    "label": phase_label,
                    "round": refine_round,
                })

                with logger.span(trace_id, f"phase:refine:{refine_round}",
                                 phase=phase_label):
                    # --- Dev 修改 ---
                    parts = [
                        f"[你上一版的程式碼]\n```python\n{dev_output}\n```"
                    ]
                    if qa_output:
                        parts.append(f"[QA 測試結果]\n{qa_output}")
                    parts.append(f"[評審意見]\n{reviewer_output}")
                    parts.append(
                        "請根據 QA 測試結果與評審意見修改程式碼。"
                        "保留正確部分，只改動被指出的問題。"
                    )
                    dev_extra = "\n\n".join(parts)

                    dev_res = await _run_and_stream(
                        ws, dev_agent, transcript, session.session_id,
                        round_no=round_no, version=refine_round + 1,
                        phase=phase_label, artifacts=artifacts,
                        stop_flag=stop_flag, extra_context=dev_extra,
                    )
                    if stop_flag.get("stop"):
                        return
                    dev_output = dev_res["content"]
                    collect_tests("dev", dev_res)
                    all_messages.append({
                        "agent_id": "dev",
                        "content": dev_output,
                        "phase": phase_label,
                    })
                    transcript += (
                        f"\n\n[{dev_agent.name}({phase_label})]:\n{dev_output}"
                    )

                    # --- QA 重測 ---
                    qa_res = await _run_and_stream(
                        ws, qa_agent, transcript, session.session_id,
                        round_no=round_no, version=refine_round + 1,
                        phase=phase_label, artifacts=artifacts,
                        stop_flag=stop_flag,
                    )
                    if stop_flag.get("stop"):
                        return
                    qa_res = await _auto_run_tests_if_qa_skipped(ws, "qa", qa_res)
                    qa_output = qa_res["content"]
                    collect_tests("qa", qa_res)
                    all_messages.append({
                        "agent_id": "qa",
                        "content": qa_output,
                        "phase": phase_label,
                    })
                    transcript += (
                        f"\n\n[{qa_agent.name}({phase_label})]:\n{qa_output}"
                    )

                    # --- Reviewer 複審 ---
                    rev_res = await _run_and_stream(
                        ws, reviewer_agent, transcript, session.session_id,
                        round_no=round_no, version=refine_round + 1,
                        phase=phase_label, artifacts=artifacts,
                        stop_flag=stop_flag,
                    )
                    if stop_flag.get("stop"):
                        return
                    reviewer_output = rev_res["content"]
                    all_messages.append({
                        "agent_id": "reviewer",
                        "content": reviewer_output,
                        "phase": phase_label,
                    })
                    transcript += (
                        f"\n\n[{reviewer_agent.name}({phase_label})]:\n{reviewer_output}"
                    )

                    final_verdict = parse_verdict(reviewer_output)
                    await ws.send_json({
                        "type": "verdict",
                        "round": refine_round,
                        "verdict": final_verdict,
                    })

            # ========== 交付摘要 ==========
            summary = _build_final_summary(
                user_msg, responses, all_messages,
                refine_round, artifacts, final_verdict,
            )
            await ws.send_json({
                "type": "final_delivery",
                "verdict": final_verdict,
                "rounds": refine_round,
                "summary": summary,
            })

            # ========== 儲存會話（含完整工件與測試） ==========
            responses["dev"] = "\n\n".join(
                m["content"] for m in all_messages if m["agent_id"] == "dev"
            )
            responses["qa"] = "\n\n".join(
                m["content"] for m in all_messages if m["agent_id"] == "qa"
            )
            responses["reviewer"] = "\n\n".join(
                m["content"] for m in all_messages if m["agent_id"] == "reviewer"
            )

            # 儲存完整 artifacts（含 content），而不是隻存檔名
            artifacts_serializable = {}
            for key, versions in artifacts.items():
                artifacts_serializable[key] = versions

            session.add_round(
                user_msg,
                responses,
                artifacts_serializable,
                messages=all_messages,
                tests=collected_tests,
            )
            await extract_facts(user_msg, responses, session.session_id)

            t = logger.finish_trace(trace_id)
            logger.info("round_done", session=session.session_id,
                        refine_rounds=refine_round,
                        verdict=final_verdict,
                        tests_count=len(collected_tests),
                        duration_ms=t["duration_ms"] if t else 0)
            await ws.send_json({"type": "round_done", "session": session.meta})

        except asyncio.CancelledError:
            logger.info("round_cancelled", session=session.session_id)
            try:
                await ws.send_json({"type": "round_cancelled"})
            except Exception:
                pass

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
                else:
                    sid = uuid.uuid4().hex[:12]
                    session = Session(sid)
                    session.save()
                await ws.send_json({
                    "type": "bound",
                    "session": session.meta,
                    "history": session.history,
                })
                continue

            if msg_type != "chat":
                continue

            try:
                user_msg = check_user_input(data.get("message") or "")
            except GuardrailError as e:
                await ws.send_json({"type": "error", "message": str(e)})
                continue

            if session is None:
                sid = uuid.uuid4().hex[:12]
                session = Session(sid)
                session.save()
                await ws.send_json({
                    "type": "bound",
                    "session": session.meta,
                    "history": [],
                })

            stop_flag["stop"] = False
            current_task = asyncio.create_task(_run_chat(user_msg))

    except WebSocketDisconnect:
        stop_flag["stop"] = True
        if current_task and not current_task.done():
            current_task.cancel()
        return
    except Exception as e:
        logger.error("ws_error", error=str(e))
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


def _build_final_summary(user_msg, responses, all_messages, refine_round,
                          artifacts, verdict):
    lines = [f"任務：{user_msg[:100]}"]
    lines.append(f"最終判定：{'透過 ✓' if verdict == 'PASS' else '仍有待改進'}")
    lines.append(f"改進輪數：{refine_round}")
    dev_versions = [m for m in all_messages if m["agent_id"] == "dev"]
    lines.append(f"工程師輸出：{len(dev_versions)} 個版本")
    if artifacts:
        file_list = []
        for logical_name, versions in artifacts.items():
            if versions:
                file_list.append(logical_name)
        if file_list:
            lines.append(f"程式碼工件：{', '.join(file_list)}")
    return "\n".join(lines)


if __name__ == "__main__":
    ensure_dirs()
    idx = rag.build_index()
    logger.info("rag_index_built", chunks=idx["built"])
    n_ctx = asyncio.run(get_n_ctx())
    logger.info("n_ctx_detected", n_ctx=n_ctx)
    uvicorn.run(app, host="127.0.0.1", port=8000)