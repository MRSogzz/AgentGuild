"""臨時診斷腳本，跑完可刪。"""
import inspect

# ============================================================
# 1. 檢查 skills.py 版本
# ============================================================
print("=" * 60)
print("  [1] prompts/skills.py 版本檢查")
print("=" * 60)

try:
    from prompts.builder import build_system_prompt
    sp = build_system_prompt(
        agent_id="dev",
        task_skills=["visual", "visual3d"],
        is_refine=False,
        memory_hint="",
    )
    print(f"System prompt 長度：{len(sp)} 字")
    print()

    keywords = [
        'div id="scene"',
        "數量必須嚴格照 PM",
        "DodecahedronGeometry",
        "全黑",
        "CanvasTexture",
    ]
    for kw in keywords:
        found = kw in sp
        print(f"  {'✓' if found else '✗'} {kw!r}")
    print()
    print("前 400 字：")
    print("-" * 60)
    print(sp[:400])
    print("-" * 60)

except Exception as e:
    print(f"[錯誤] 導入失敗：{e}")


# ============================================================
# 2. 檢查 agent.py 版本
# ============================================================
print()
print("=" * 60)
print("  [2] agent.py 版本檢查")
print("=" * 60)

try:
    from agent import run_agent
    src = inspect.getsource(run_agent)

    keywords = [
        "is_refine",
        "task_skills",
        "立即修改上一版的程式碼",
        "PM 已經給了實作規劃",
        "dev_starter_injected",
        "DodecahedronGeometry",
    ]
    for kw in keywords:
        found = kw in src
        print(f"  {'✓' if found else '✗'} {kw!r}")

    print()
    sig = inspect.signature(run_agent)
    print(f"run_agent 簽名：{sig}")

except Exception as e:
    print(f"[錯誤] 導入失敗：{e}")


# ============================================================
# 3. 檢查 core/runner.py 的參數透傳
# ============================================================
print()
print("=" * 60)
print("  [3] core/runner.py 版本檢查")
print("=" * 60)

try:
    from core.runner import run_and_stream
    src = inspect.getsource(run_and_stream)

    keywords = [
        "task_skills",
        "is_refine",
    ]
    for kw in keywords:
        found = kw in src
        print(f"  {'✓' if found else '✗'} {kw!r}")

    print()
    sig = inspect.signature(run_and_stream)
    print(f"run_and_stream 簽名：{sig}")

except Exception as e:
    print(f"[錯誤] 導入失敗：{e}")


# ============================================================
# 4. 檢查 workspace 現況
# ============================================================
print()
print("=" * 60)
print("  [4] workspace 現況")
print("=" * 60)

try:
    from config import WORKSPACE_DIR
    import os
    for f in sorted(WORKSPACE_DIR.iterdir()):
        if f.name in ("vendor", "artifacts"):
            continue
        size = f.stat().st_size if f.is_file() else 0
        tag = "DIR " if f.is_dir() else "FILE"
        print(f"  {tag} {f.name} ({size}B)")
except Exception as e:
    print(f"[錯誤] {e}")

print()
print("=" * 60)
print("  診斷完成，把上面的輸出全部貼給我")
print("=" * 60)