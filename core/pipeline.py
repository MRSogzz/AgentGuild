"""Shared task state and deterministic workflow transitions."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from config import VISUAL_QUALITY_MIN


MAX_ARTIFACT_CONTEXT_CHARS = 24_000
SNAPSHOT_SUFFIXES = {".py", ".html", ".htm", ".css", ".js", ".ts", ".json", ".md", ".yaml", ".yml"}


def workspace_snapshot(root: Path,
                       max_chars: int = MAX_ARTIFACT_CONTEXT_CHARS) -> str:
    """Read a bounded set of project sources for explicit continuation tasks."""
    parts: list[str] = []
    remaining = max_chars
    try:
        paths = sorted(
            path for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SNAPSHOT_SUFFIXES
            and not ({"vendor", "artifacts", ".git", "node_modules"} & set(path.parts))
        )
    except OSError:
        return "(無法讀取工作區)"
    for path in paths:
        if remaining <= 0:
            parts.append("[其餘檔案省略]")
            break
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        snippet = content[:remaining]
        parts.append(f"### {path.relative_to(root).as_posix()}\n```\n{snippet}\n```")
        remaining -= len(snippet)
    return "\n\n".join(parts) or "(工作區沒有可讀取的專案檔案)"


@dataclass
class PipelineState:
    task_id: str
    user_request: str
    task_type: str
    agents: list[str]
    task_skills: list[str] = field(default_factory=list)
    phase: str = "planning"
    iteration: int = 0
    requirement: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, str]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    qa: dict[str, Any] = field(default_factory=dict)
    reviewer: dict[str, Any] = field(default_factory=dict)
    route_to: str = "pm"
    transitions: list[dict[str, Any]] = field(default_factory=list)
    human_checkpoint: dict[str, Any] = field(default_factory=lambda: {
        "required": False, "status": "not_required", "reason": "",
    })

    def transition(self, phase: str, reason: str, route_to: str | None = None) -> None:
        allowed = {
            "planning": {"implementing", "blocked"},
            "implementing": {"reviewing", "testing", "blocked"},
            "reviewing": {"testing", "implementing", "done", "blocked"},
            "testing": {"reviewing", "implementing", "done", "blocked"},
            "blocked": {"planning", "done"},
            "done": set(),
        }
        if phase != self.phase and phase not in allowed.get(self.phase, set()):
            raise ValueError(f"Invalid workflow transition: {self.phase} -> {phase}")
        previous = self.phase
        self.phase = phase
        if route_to:
            self.route_to = route_to
        if self.route_to == "human":
            self.human_checkpoint = {
                "required": True,
                "status": "pending",
                "reason": reason[:500],
            }
        self.transitions.append({
            "from": previous,
            "to": phase,
            "route_to": self.route_to,
            "reason": reason[:500],
            "iteration": self.iteration,
        })

    def record_artifacts(self, files: list[dict[str, Any]]) -> None:
        self.artifacts = [
            {"path": str(f.get("path") or f.get("filename") or ""),
             "content": str(f.get("content") or "")}
            for f in files
        ]

    def artifact_context(self) -> str:
        parts = []
        remaining = MAX_ARTIFACT_CONTEXT_CHARS
        for file in self.artifacts:
            path, content = file["path"], file["content"]
            if remaining <= 0:
                parts.append("[其餘檔案省略]")
                break
            snippet = content[:remaining]
            parts.append(f"### {path}\n```\n{snippet}\n```")
            remaining -= len(snippet)
        return "\n\n".join(parts) or "(沒有產生程式碼工件)"

    def qa_context(self, acceptance: str, test_context: str) -> str:
        return (
            "[驗收依據]\n"
            f"原始需求：{self.user_request}\n"
            f"需求規格：{acceptance or '(PM 未參與；依原始需求驗收)'}\n\n"
            "[實際產出物（僅包含程式碼，不含 Dev 解說或推理）]\n"
            f"{self.artifact_context()}\n\n"
            f"[自動測試結果]\n{test_context}"
        )

    def reviewer_context(self, acceptance: str, qa_result: dict[str, Any]) -> str:
        return (
            "[驗收依據]\n"
            f"原始需求：{self.user_request}\n"
            f"需求規格：{acceptance or '(PM 未參與；依原始需求驗收)'}\n\n"
            "[實際產出物（程式碼／截圖，不含 Dev 推理）]\n"
            f"{self.artifact_context()}\n\n"
            "[QA 結果]\n"
            f"verdict={qa_result.get('verdict', 'unknown')}\n"
            f"summary={qa_result.get('summary', '')}\n"
            f"issues={qa_result.get('issues', [])}"
        )

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["artifacts"] = [
            {"path": item["path"], "chars": len(item.get("content", ""))}
            for item in self.artifacts
        ]
        data["tests"] = [
            {"name": item.get("name", ""),
             "output": str(item.get("output", ""))[-1200:]}
            for item in self.tests
        ]
        return data


def evaluate_scene_assertions(requirement: dict[str, Any],
                               scene_info: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Evaluate optional machine-checkable object count assertions."""
    scene_info = scene_info if isinstance(scene_info, dict) else {}
    results: list[dict[str, Any]] = []
    for req in requirement.get("requirements", []):
        if not isinstance(req, dict):
            continue
        criterion_id = str(req.get("id", ""))
        assertions = req.get("assertions", [])
        if not isinstance(assertions, list):
            continue
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            name = str(assertion.get("object_name", ""))
            actual = scene_info.get(name)
            passed = isinstance(actual, (int, float))
            if passed and "exact_count" in assertion:
                passed = actual == assertion["exact_count"]
            if passed and "min_count" in assertion:
                passed = actual >= assertion["min_count"]
            if passed and "max_count" in assertion:
                passed = actual <= assertion["max_count"]
            expected = {
                key: assertion[key]
                for key in ("exact_count", "min_count", "max_count")
                if key in assertion
            }
            results.append({
                "criterion_id": criterion_id,
                "criterion": str(assertion.get("description") or name),
                "pass": bool(passed),
                "observation": (
                    f"scene_info[{name!r}]={actual!r}; expected {expected}"
                ),
            })
    return results

def choose_next_route(task_state: PipelineState,
                      visual_required: bool) -> tuple[str, str]:
    """Apply hard gates around model-proposed routes."""
    qa_verdict = task_state.qa.get("verdict", "unknown")
    review_verdict = task_state.reviewer.get("verdict", "unknown")
    routes = {
        task_state.qa.get("route_to"),
        task_state.reviewer.get("route_to"),
    }
    unknown_agents = [
        agent for agent, result in (
            ("QA", task_state.qa), ("Reviewer", task_state.reviewer)
        )
        if result and result.get("verdict") == "unknown"
    ]
    if unknown_agents:
        return "human", (
            f"{', '.join(unknown_agents)} 在格式重試後仍未提供可解析的結構化結果；"
            "這是輸出格式失敗，不代表需求需要澄清"
        )
    if "human" in routes or "blocked" in (qa_verdict, review_verdict):
        return "human", "Agent 回報需求阻塞或需要人工釐清"
    if "pm" in routes:
        return "pm", "Agent 判定需求需 PM 重新釐清"
    if visual_required:
        quality = task_state.reviewer.get("visual_quality")
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            quality = 0
        checks = task_state.reviewer.get("checks", [])
        if quality < VISUAL_QUALITY_MIN:
            return "dev", f"視覺品質分數 {quality}/10 未達 {VISUAL_QUALITY_MIN} 分門檻"
        if not checks or any(
            not isinstance(check.get("pass"), bool) or not check.get("pass")
            for check in checks
        ):
            return "dev", "Reviewer 驗收項目未全部通過或缺少結構化 checks"
        if any(
            str(issue.get("severity", "")).lower() in ("blocker", "critical")
            for issue in task_state.reviewer.get("issues", [])
        ):
            return "dev", "Reviewer 回報 BLOCKER / CRITICAL issue"
    if qa_verdict != "approved":
        return "dev", "QA 未核准，回到 Dev 修正"
    if visual_required and review_verdict != "approved":
        return "dev", "視覺 Reviewer 未核准，回到 Dev 修正"
    if "dev" in routes or "changes_requested" in (qa_verdict, review_verdict):
        return "dev", "Agent 指出需修改的實作問題"
    return "done", "所有必要驗收節點均已核准"
