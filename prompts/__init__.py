"""Prompt 模組：角色 + 技能分層組裝。"""

# 只從 base 和 skills 導出，builder 單獨導入
from prompts.base import COMMON_PREFIX, ROLES
from prompts.skills import (
    SKILLS,
    detect_task_skills,
    list_all_skills,
    get_skill,
)

__all__ = [
    "COMMON_PREFIX",
    "ROLES",
    "SKILLS",
    "detect_task_skills",
    "list_all_skills",
    "get_skill",
]