"""Prompt 組裝：Common + Role + Skills。

注意：此檔案**不透過** prompts/__init__.py 導入，
而是直接導入同目錄的 base 和 skills，
避免循環導入問題。
"""

# 直接用相對導入，不走套件
from .base import COMMON_PREFIX, ROLES
from .skills import SKILLS, SKILL_MODIFY_VISUAL


def build_system_prompt(agent_id: str,
                        task_skills: list[str] | None = None,
                        is_refine: bool = False,
                        memory_hint: str = "") -> str:
    """
    組裝單一 Agent 的 system prompt。

    Args:
        agent_id: Agent 識別碼（pm / dev / qa / reviewer / router）
        task_skills: 任務技能列表（如 ["visual", "visual3d"]），
                     只有 dev 會用到
        is_refine: 是否為改進輪次（決定是否注入 SKILL_MODIFY）
        memory_hint: 長期記憶片段（由呼叫方組裝）

    Returns:
        完整的 system prompt 字串
    """
    parts = [COMMON_PREFIX]

    # 角色定義
    is_frontend = task_skills and any(
        skill in task_skills for skill in ("frontend", "visual", "visual3d")
    )
    if agent_id == "qa":
        if is_frontend:
            role = (
                "你是前端 QA。只根據原始需求、檔案產出物與系統提供的 HTML/CSS/JS 檢查結果驗收。"
                "語法通過不代表視覺或互動通過；不得對畫面做推測，畫面交由 Reviewer 截圖驗收。"
                "不呼叫工具。最後只輸出 JSON："
                '{"verdict":"approved","issues":[],'
                '"route_to":"done","summary":"...","evidence":[]} '
                "verdict 僅能是 approved / changes_requested / blocked；"
                "route_to 僅能是 done / dev / pm / human。"
            )
        else:
            role = (
                "你是 QA。只根據驗收標準、程式碼產出物及系統自動測試結果判定；"
                "不可相信 Dev 的自述，不可自行補跑工具。測試失敗或未執行時不得 approved。"
                "需求不明確回報 blocked 並 route_to=pm；實作或測試失敗回報 changes_requested 並 route_to=dev。"
                "最後只輸出 JSON："
                '{"verdict":"approved","issues":[{"severity":"high","file":"...","desc":"..."}],'
                '"route_to":"done","summary":"...","evidence":[]} '
                "verdict 僅能是 approved / changes_requested / blocked；"
                "route_to 僅能是 done / dev / pm / human。"
            )
    else:
        role = ROLES.get(agent_id, "")
    if role:
        parts.append(role)

    # 任務技能（只給 dev）
    if agent_id == "dev" and task_skills:
        for skill_name in task_skills:
            # 3D 視覺規範已包含完整的視覺呈現要求；再注入 2D 規範會
            # 造成「用 2D/CSS 排版」與「實作 three.js 場景」互相衝突。
            if skill_name == "visual" and "visual3d" in task_skills:
                continue
            skill_text = SKILLS.get(skill_name)
            if skill_text:
                parts.append(skill_text)

    # 改進輪次加修改原則
    if agent_id == "dev" and is_refine:
        if task_skills and any(
            skill in task_skills for skill in ("frontend", "visual", "visual3d")
        ):
            parts.append(SKILL_MODIFY_VISUAL)
        else:
            parts.append(SKILLS["modify"])

    # 長期記憶
    if memory_hint:
        parts.append(memory_hint)

    return "\n\n".join(parts)
