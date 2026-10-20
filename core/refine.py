"""改進循環的智能停止判定。"""
import re

from config import (
    REFINE_SIMILARITY_REVIEW, REFINE_SIMILARITY_DEV, REFINE_NO_PROGRESS_LIMIT,
    VISUAL_QUALITY_NO_PROGRESS_LIMIT,
)


CODE_FILE_RE = re.compile(
    r"```(?:[a-zA-Z0-9_+-]*):([^\n`]+?)\s*\n([\s\S]*?)```",
    re.MULTILINE,
)


def _reviewer_issue_descriptions(text: str) -> set[str]:
    try:
        from core.contracts import parse_agent_contract
        contract = parse_agent_contract(text, "reviewer")
        descriptions = {
            str(issue.get("desc", "")).strip().lower()
            for issue in contract.get("issues", [])
            if issue.get("desc")
        }
        if descriptions:
            return descriptions
    except Exception:
        pass
    return set()


def _source_lines(text: str) -> set[str]:
    files = CODE_FILE_RE.findall(text or "")
    source = "\n".join(code for _path, code in files) if files else (text or "")
    return {line.strip() for line in source.splitlines() if line.strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def text_similarity(a: str, b: str, *, kind: str = "dev") -> float:
    if not a or not b:
        return 0.0
    try:
        if kind == "reviewer":
            return _jaccard(_reviewer_issue_descriptions(a), _reviewer_issue_descriptions(b))
        return _jaccard(_source_lines(a), _source_lines(b))
    except Exception:
        return 0.0


def extract_test_counts(text: str) -> tuple[int, int]:
    """從任意文字提取 (passed, failed)，用於進度比較。"""
    if not text:
        return (0, 0)
    p_m = re.search(r"(\d+)\s+passed", text, re.IGNORECASE)
    f_m = re.search(r"(\d+)\s+failed", text, re.IGNORECASE)
    p = int(p_m.group(1)) if p_m else 0
    f = int(f_m.group(1)) if f_m else 0
    return (p, f)


def check_refine_stop(prev_reviewer, prev_dev, prev_passed,
                      no_progress_count, cur_reviewer, cur_dev, cur_passed,
                      visual_task: bool = False,
                      prev_issue_count: int | None = None,
                      cur_issue_count: int | None = None,
                      prev_quality: int | None = None,
                      cur_quality: int | None = None):
    """
    檢查是否該提前停止改進循環。
    返回 (stop_reason | None, new_no_progress_count)
    """
    if prev_reviewer and not visual_task:
        sim = text_similarity(prev_reviewer, cur_reviewer, kind="reviewer")
        if sim >= REFINE_SIMILARITY_REVIEW:
            return (
                f"評審意見連續兩輪高度重複（相似度 {sim:.0%}），"
                f"判定為無法進一步改進",
                no_progress_count,
            )
    if prev_dev and not visual_task:
        sim = text_similarity(prev_dev, cur_dev, kind="dev")
        if sim >= REFINE_SIMILARITY_DEV:
            return (
                f"工程師輸出連續兩輪幾乎相同（相似度 {sim:.0%}），"
                f"判定為無法修正",
                no_progress_count,
            )
    if visual_task:
        quality_improved = (
            prev_quality is not None and cur_quality is not None
            and cur_quality > prev_quality
        )
        issues_improved = (
            prev_issue_count is not None and cur_issue_count is not None
            and cur_issue_count < prev_issue_count
        )
        if quality_improved or issues_improved:
            no_progress_count = 0
        else:
            no_progress_count += 1
            if no_progress_count >= VISUAL_QUALITY_NO_PROGRESS_LIMIT:
                old_score = "?" if prev_quality is None else str(prev_quality)
                new_score = "?" if cur_quality is None else str(cur_quality)
                old_issues = "?" if prev_issue_count is None else str(prev_issue_count)
                new_issues = "?" if cur_issue_count is None else str(cur_issue_count)
                return (
                    "視覺品質連續未改善，"
                    f"issues {old_issues}→{new_issues}、分數 {old_score}→{new_score}；停止自動改進",
                    no_progress_count,
                )
    elif prev_passed is not None:
        if cur_passed <= prev_passed:
            no_progress_count += 1
            if no_progress_count >= REFINE_NO_PROGRESS_LIMIT:
                return (
                    f"測試通過數連續 {REFINE_NO_PROGRESS_LIMIT} 輪無增加"
                    f"（維持 {cur_passed} 通過），判定為無法進一步改善",
                    no_progress_count,
                )
        else:
            no_progress_count = 0
    return (None, no_progress_count)
