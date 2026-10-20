"""Structured contracts shared by pipeline stages."""
from __future__ import annotations

import json
import re
from typing import Any


CONTRACT_RE = re.compile(
    r"<agent_result>\s*([\s\S]*?)\s*</agent_result>", re.IGNORECASE
)
IMAGE_RE = re.compile(
    r"<image_analysis>\s*([\s\S]*?)\s*</image_analysis>", re.IGNORECASE
)
SUMMARY_RE = re.compile(r"<summary>\s*([\s\S]*?)\s*</summary>", re.IGNORECASE)
VERDICT_RE = re.compile(r"<verdict>\s*(PASS|REVISE)\s*</verdict>", re.IGNORECASE)


def _json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", candidate, re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", candidate):
        try:
            value, _ = decoder.raw_decode(candidate[match.start():])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


def parse_agent_contract(text: str, agent_id: str = "") -> dict[str, Any]:
    """Parse a JSON contract, with compatibility for the former tag format."""
    text = text or ""
    tagged = CONTRACT_RE.search(text)
    data = _json_object(tagged.group(1)) if tagged else _json_object(text)
    data = data or {}

    raw_verdict = str(data.get("verdict", "")).strip().lower()
    if raw_verdict in ("approved", "pass", "done", "ready"):
        verdict = "approved"
    elif raw_verdict in ("changes_requested", "revise", "rejected"):
        verdict = "changes_requested"
    elif raw_verdict in ("blocked", "needs_clarification"):
        verdict = "blocked"
    else:
        old_tag = VERDICT_RE.search(text)
        verdict = (
            "approved" if old_tag and old_tag.group(1).upper() == "PASS"
            else "changes_requested" if old_tag
            else "unknown"
        )

    issues = data.get("issues", [])
    if not isinstance(issues, list):
        issues = []
    issues = [item for item in issues if isinstance(item, dict)]

    checks = data.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    checks = [item for item in checks if isinstance(item, dict)]

    requirements = data.get("requirements", [])
    if not isinstance(requirements, list):
        requirements = []
    normalized_requirements = []
    for index, item in enumerate(requirements, start=1):
        if not isinstance(item, dict):
            continue
        requirement = dict(item)
        requirement.setdefault("id", f"R{index}")
        if not isinstance(requirement.get("assertions", []), list):
            requirement["assertions"] = []
        normalized_requirements.append(requirement)

    route = str(data.get("route_to", "")).strip().lower()
    allowed_routes = {"pm", "dev", "reviewer", "qa", "done", "human"}
    if route not in allowed_routes:
        if verdict == "blocked":
            route = "human" if agent_id == "pm" else "pm"
        elif verdict == "changes_requested":
            route = "dev"
        else:
            route = "done" if verdict == "approved" else "human"

    image = data.get("image_analysis", "")
    if not isinstance(image, str):
        image = ""
    if not image:
        match = IMAGE_RE.search(text)
        image = match.group(1).strip() if match else ""

    summary = data.get("summary", "")
    if not isinstance(summary, str):
        summary = ""
    if not summary:
        match = SUMMARY_RE.search(text)
        summary = match.group(1).strip() if match else ""
    if not summary and data:
        summary = str(data.get("rationale", ""))[:1000]

    return {
        "agent": agent_id,
        "verdict": verdict,
        "issues": issues,
        "checks": checks,
        "route_to": route,
        "summary": summary[:2000],
        "image_analysis": image[:1000],
        "visual_quality": data.get("visual_quality"),
        "requirements": normalized_requirements,
        "assertions": data.get("assertions", []) if isinstance(data.get("assertions", []), list) else [],
        "assumptions": data.get("assumptions", []) if isinstance(data.get("assumptions", []), list) else [],
        "questions": data.get("questions", []) if isinstance(data.get("questions", []), list) else [],
        "evidence": data.get("evidence", []) if isinstance(data.get("evidence", []), list) else [],
        "unlock_test": bool(data.get("unlock_test", False)) or "<unlock_test>" in text,
        "raw": text[:6000],
    }
