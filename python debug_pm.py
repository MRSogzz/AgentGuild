# debug_pm2.py   用法: python debug_pm2.py [tools|notools] [session_id]
import asyncio, re, sys
from config import AGENTS
from agent import run_agent
from memory import Session

BAD = re.compile(r"([^\s\w])\1{19,}")
MSG = "做一個旋轉的立方體"          # 換成實際出問題的需求

async def main():
    allow_tools = (sys.argv[1] if len(sys.argv) > 1 else "notools") == "tools"
    sid = sys.argv[2] if len(sys.argv) > 2 else None
    transcript = Session(sid).build_transcript(MSG) if sid else f"使用者: {MSG}"
    print(f"allow_tools={allow_tools} session={sid} transcript_chars={len(transcript)}")
    out = ""
    async for ev in run_agent(AGENTS["pm"], transcript, "dbg",
                              task_skills=["visual", "visual3d"],
                              allow_workspace_tools=allow_tools):
        t = ev["type"]
        if t == "token":
            out += ev["token"]
            if BAD.search(out[-80:]):
                print("!! 偵測到重複字元，停止"); break
        elif t == "tool":
            print("TOOL", ev["name"], ev["state"], (ev.get("result") or "")[:200])
        elif t in ("error", "done"):
            print(t, ev)
    print("OUTPUT:", repr(out[:500]))

asyncio.run(main())