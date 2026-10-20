import json
import math
import re
import time
import uuid
from pathlib import Path

from config import (
    SESSIONS_DIR, MEMORY_DIR, AGENTS,
    MAX_HISTORY_ROUNDS,
    MEMORY_WEIGHT, MEMORY_HIGH_PRIORITY_THRESHOLD,
    MEMORY_HIGH_PRIORITY_LIMIT, MEMORY_SEARCH_TOP_K,
    PATHOLOGICAL_REPETITION_WINDOW, PATHOLOGICAL_REPEATED_CHAR_MIN,
    PATHOLOGICAL_CHAR_RATIO,
)


CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)


def _extract_code_blocks(text: str) -> list[str]:
    return CODE_BLOCK_RE.findall(text)


def _is_pathological_text(text: str) -> bool:
    tail = (text or "")[-PATHOLOGICAL_REPETITION_WINDOW:]
    if re.search(rf"([^\s])\1{{{max(1, PATHOLOGICAL_REPEATED_CHAR_MIN - 1)},}}", tail):
        return True
    ratio_window = min(100, PATHOLOGICAL_REPETITION_WINDOW)
    if len(tail) < ratio_window:
        return False
    sample = tail[-ratio_window:]
    counts = {char: sample.count(char) for char in set(sample)}
    return max(counts.values(), default=0) / len(sample) > PATHOLOGICAL_CHAR_RATIO and " " not in counts


def compress_agent_output(text: str, head: int = 400, tail: int = 300) -> str:
    if len(text) <= head + tail + 200:
        return text

    head_text = text[:head].rstrip()
    tail_text = text[-tail:].lstrip() if len(text) > tail else ""

    parts = [head_text, "...[中間說明已省略]..."]
    if tail_text and tail_text not in head_text:
        parts.append(tail_text)
    if _extract_code_blocks(text):
        parts.append("[程式碼區塊已省略；請從工作區工件讀取]")
    return "\n\n".join(parts)


class Session:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.path = SESSIONS_DIR / f"{session_id}.json"
        self.meta = {
            "id": session_id,
            "title": "新對話",
            "created": time.time(),
            "updated": time.time(),
        }
        self.history: list[dict] = []
        self.frozen_tests: dict[str, str] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.meta = data.get("meta", self.meta)
                self.history = data.get("history", [])
                self.frozen_tests = data.get("frozen_tests", {}) or {}
            except Exception:
                pass

    def save(self):
        self.meta["updated"] = time.time()
        self.path.write_text(
            json.dumps({
                "meta": self.meta,
                "history": self.history,
                "frozen_tests": self.frozen_tests,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add_round(self, user_msg: str,
                  responses: dict[str, str],
                  artifacts: dict | None = None,
                  messages: list[dict] | None = None,
                  tests: list[dict] | None = None,
                  interrupted: bool = False,
                  shared_state: dict | None = None):
        safe_responses = {
            key: "[退化輸出已從歷史中剔除]" if _is_pathological_text(value) else value
            for key, value in responses.items()
        }
        safe_messages = []
        for message in messages or []:
            item = dict(message)
            if _is_pathological_text(str(item.get("content", ""))):
                item["content"] = "[退化輸出已從歷史中剔除]"
            safe_messages.append(item)
        self.history.append({
            "round": len(self.history) + 1,
            "user": user_msg,
            "responses": safe_responses,
            "messages": safe_messages,
            "artifacts": artifacts or {},
            "tests": tests or [],
            "shared_state": shared_state or {},
            "interrupted": interrupted,
            "time": time.time(),
        })
        if self.meta["title"] == "新對話" and user_msg:
            title = user_msg.strip().splitlines()[0][:20]
            self.meta["title"] = title + ("…" if len(user_msg) > 20 else "")
        self.save()

    def build_transcript(self, current_user_msg: str) -> str:
        parts = []
        recent = self.history[-MAX_HISTORY_ROUNDS:]
        if len(self.history) > MAX_HISTORY_ROUNDS:
            parts.append(f"[之前還有 {len(self.history) - MAX_HISTORY_ROUNDS} 輪對話已省略]")

        for h in recent:
            parts.append(f"使用者: {h['user']}")
            msgs = h.get("messages") or self._fallback_messages(h)
            for m in msgs:
                agent = AGENTS.get(m["agent_id"])
                name = agent.name if agent else m["agent_id"]
                label = name + (f"({m['phase']})" if m.get("phase") else "")

                arts = (h.get("artifacts") or {}).get(m["agent_id"], [])
                file_names = []
                for a in arts:
                    if isinstance(a, str):
                        file_names.append(a)
                    elif isinstance(a, dict):
                        file_names.append(a.get("filename") or a.get("path") or "?")
                if not file_names and m["agent_id"] == "dev":
                    for logical_name, versions in (h.get("artifacts") or {}).items():
                        if isinstance(versions, list):
                            for v in versions:
                                if isinstance(v, dict):
                                    file_names.append(v.get("filename") or logical_name)

                if file_names and m["agent_id"] == "dev":
                    summary = compress_agent_output(m["content"], head=300, tail=200)
                    parts.append(
                        f"[{label}]:\n{summary}\n"
                        f"[程式碼已存為文件: {', '.join(file_names)}]"
                    )
                else:
                    parts.append(f"[{label}]:\n{compress_agent_output(m['content'])}")
            parts.append("---")

        parts.append(f"使用者: {current_user_msg}")
        return "\n\n".join(parts)

    def build_pm_transcript(self, current_user_msg: str) -> str:
        """Give PM recent user requests and requirement summaries, never source dumps."""
        parts = []
        for item in self.history[-3:]:
            if item.get("user"):
                parts.append(f"先前需求：{item['user'][:1200]}")
            requirement = (item.get("shared_state") or {}).get("requirement")
            if requirement:
                parts.append("先前驗收標準：" + json.dumps(requirement, ensure_ascii=False)[:2400])
        parts.append(f"目前需求：{current_user_msg}")
        return "\n\n".join(parts)

    def _fallback_messages(self, h: dict) -> list[dict]:
        msgs = []
        for aid, content in (h.get("responses") or {}).items():
            msgs.append({"agent_id": aid, "content": content, "phase": None})
        return msgs

    def to_dict(self):
        return {
            "meta": self.meta,
            "history": self.history,
            "frozen_tests": self.frozen_tests,
        }


def list_sessions():
    """列出所有會話。跳過沒有對話歷史的空會話。"""
    sessions = []
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not data.get("history"):
                continue
            sessions.append(data.get("meta", {"id": p.stem, "title": "未命名", "updated": 0}))
        except Exception:
            pass
    sessions.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return sessions


class LongTermMemory:
    def __init__(self):
        self.path = MEMORY_DIR / "facts.json"
        self.facts: list[dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.facts = []
                for f in data:
                    if "weight" not in f:
                        f["weight"] = MEMORY_WEIGHT["general"]
                        f["access_count"] = 0
                        f["last_accessed"] = f.get("created", time.time())
                    self.facts.append(f)
            except Exception:
                self.facts = []

    def _save(self):
        self.path.write_text(
            json.dumps(self.facts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, fact: str, tags: list[str] | None = None,
            weight: int | None = None, source_session: str = "",
            boost_if_exists: bool = True):
        fact = fact.strip()
        if not fact or len(fact) > 500:
            return

        if weight is None:
            if tags and "user_explicit" in tags:
                weight = MEMORY_WEIGHT["user_explicit"]
            elif tags and "user_preference" in tags:
                weight = MEMORY_WEIGHT["user_preference"]
            elif tags and "code" in tags:
                weight = MEMORY_WEIGHT["code_definition"]
            else:
                weight = MEMORY_WEIGHT["general"]

        for f in self.facts:
            if f["fact"] == fact:
                if boost_if_exists:
                    f["weight"] = min(10, f["weight"] + 1)
                    f["last_accessed"] = time.time()
                    self._save()
                return

        self.facts.append({
            "id": uuid.uuid4().hex[:8],
            "fact": fact,
            "tags": tags or [],
            "weight": max(0, min(10, weight)),
            "access_count": 0,
            "last_accessed": time.time(),
            "created": time.time(),
            "source_session": source_session,
        })
        self._save()

    def search(self, query: str, top_k: int = MEMORY_SEARCH_TOP_K) -> list[dict]:
        if not query.strip():
            return []
        tokens = [t for t in _tokenize(query) if len(t) > 1]
        if not tokens:
            return []

        now = time.time()
        scored = []
        for f in self.facts:
            text = (f["fact"] + " " + " ".join(f.get("tags", []))).lower()
            relevance = sum(1 for t in tokens if t in text)
            if relevance == 0:
                continue
            weight_factor = f.get("weight", 5) / 5.0
            age_days = (now - f.get("created", now)) / 86400
            recency_factor = 1.0 / (1.0 + age_days / 30)
            access_factor = 1.0 + math.log1p(f.get("access_count", 0)) * 0.3
            score = weight_factor * relevance * recency_factor * access_factor
            scored.append((score, f))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for _, f in scored[:top_k]:
            f["access_count"] = f.get("access_count", 0) + 1
            f["last_accessed"] = now
            results.append(f)
        if results:
            self._save()
        return results

    def high_priority(self, limit: int = MEMORY_HIGH_PRIORITY_LIMIT) -> list[dict]:
        high = [f for f in self.facts
                if f.get("weight", 0) >= MEMORY_HIGH_PRIORITY_THRESHOLD]
        high.sort(key=lambda f: (f["weight"], f.get("last_accessed", 0)), reverse=True)
        return high[:limit]

    def all(self):
        return list(self.facts)

    def stats(self) -> dict:
        by_weight = {}
        for f in self.facts:
            w = f.get("weight", 0)
            by_weight[w] = by_weight.get(w, 0) + 1
        return {
            "total": len(self.facts),
            "by_weight": by_weight,
            "high_priority": len(self.high_priority(limit=999)),
        }


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_]+", text)
    tokens += list(re.sub(r"[a-z0-9_\s]", "", text))
    return tokens


long_term = LongTermMemory()
