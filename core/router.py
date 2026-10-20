"""任務路由：決定跑哪些 Agent。"""
import re

from config import (
    PIPELINE, AGENTS, WORKSPACE_DIR,
)
from observability import logger
from prompts.skills import detect_task_skills, TASK_ROUTING_RULES


def classify_task(user_msg: str) -> str:
    """Deterministic first-pass routing; explicit complex signals take priority."""
    text = (user_msg or "").lower()
    if (any(term in text for term in TASK_ROUTING_RULES["complex_terms"])
            or re.search(TASK_ROUTING_RULES["complex_word_pattern"], text)):
        return "complex"
    if any(term in text for term in TASK_ROUTING_RULES["database_terms"]):
        return "database"
    if any(term in text for term in TASK_ROUTING_RULES["backend_terms"]):
        return "backend"
    frontend_word = re.search(TASK_ROUTING_RULES["frontend_word_pattern"], text)
    if frontend_word or any(
        term in text for term in TASK_ROUTING_RULES["frontend_terms"]
        if term not in ("js", "ui")
    ):
        return "frontend"
    return "complex"


def classify_request(user_msg: str,
                     project_info: dict | None = None) -> tuple[str, list[str]]:
    """Return the task type and injected skills as one persisted decision."""
    task_type = classify_task(user_msg)
    skills = detect_task_skills(user_msg, project_info)
    if task_type in ("backend", "database"):
        skills = ["python"]
    elif task_type == "frontend" and not any(
        skill in skills for skill in ("frontend", "visual", "visual3d")
    ):
        skills = ["frontend"]
    elif task_type == "complex" and project_info:
        kind = project_info.get("kind")
        if kind == "frontend":
            skills = ["frontend"]
        elif kind == "python":
            skills = ["python"]
        elif kind == "mixed":
            skills = ["python", "frontend"]
    if task_type == "complex" and not skills:
        logger.warn("routing_fallback", task_type=task_type,
                    project_kind=(project_info or {}).get("kind", "unknown"),
                    reason="no matching task phrase or project files")
    return task_type, skills


def agents_for_task(task_type: str, include_pm: bool = True) -> list[str]:
    """Build the deterministic workflow; PM scopes fresh tasks by default."""
    if task_type == "frontend":
        workflow = ["dev", "qa", "reviewer"]
    elif task_type in ("backend", "database"):
        workflow = ["dev", "qa"]
    else:
        workflow = [agent for agent in PIPELINE if agent != "pm"]
    return (["pm"] if include_pm else []) + workflow


def has_test_files() -> bool:
    return any(WORKSPACE_DIR.glob("test_*.py"))


def has_code_files() -> bool:
    return any(WORKSPACE_DIR.glob("*.py"))


def _has_any_code_files() -> bool:
    """檢查工作區是否有任何程式碼檔（含前端）。"""
    patterns = ("*.py", "*.html", "*.js", "*.ts",
                "*.css", "*.jsx", "*.tsx", "*.vue", "*.svelte")
    for pat in patterns:
        if any(WORKSPACE_DIR.glob(pat)):
            return True
    return has_test_files()


def parse_agent_directive(user_msg: str) -> tuple[list[str] | None, str]:
    m = re.match(r"^\s*@(all|pm|dev|qa|reviewer|[\w,]+)\s+", user_msg, re.IGNORECASE)
    if not m:
        return None, user_msg

    tag = m.group(1).lower()
    cleaned = user_msg[m.end():].strip()

    if tag == "all":
        return list(PIPELINE), cleaned

    names = [x.strip().lower() for x in tag.split(",")]
    valid = [n for n in names if n in AGENTS]
    if not valid:
        return None, user_msg
    return valid, cleaned


def apply_agent_dependencies(agents: list[str], explicit: bool = False) -> list[str]:
    """根據依賴關係補全 agent 列表，並按標準順序排序。"""
    required = set(agents)

    if not explicit:
        needs_code = "qa" in required or "reviewer" in required
        if needs_code and not _has_any_code_files():
            required.add("dev")

    return [a for a in PIPELINE if a in required]


