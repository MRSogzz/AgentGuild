import json
import re
from pathlib import Path

from config import DOCS_DIR

INDEX_FILE = DOCS_DIR / "_index.json"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80

_index_cache: dict | None = None


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_]+", text)
    tokens += list(re.sub(r"[a-z0-9_\s]", "", text))
    return [t for t in tokens if len(t) > 1]


def _chunk(text: str) -> list[str]:
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i:i + CHUNK_SIZE])
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def build_index() -> dict:
    docs = []
    for ext in ("*.md", "*.txt"):
        for p in DOCS_DIR.glob(ext):
            if p.name.startswith("_"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for idx, chunk in enumerate(_chunk(text)):
                docs.append({
                    "source": p.name,
                    "chunk": idx,
                    "text": chunk,
                    "tokens": _tokenize(chunk),
                })
    index = {"chunks": docs, "built": len(docs)}
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return index


def _load_index() -> dict:
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    if INDEX_FILE.exists():
        try:
            _index_cache = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            return _index_cache
        except Exception:
            pass
    _index_cache = build_index()
    return _index_cache


def rebuild():
    global _index_cache
    _index_cache = None
    return build_index()


def search(query: str, top_k: int = 3) -> list[dict]:
    idx = _load_index()
    chunks = idx.get("chunks", [])
    if not chunks:
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    scored = []
    for c in chunks:
        score = 0
        for t in q_tokens:
            score += c["tokens"].count(t)
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"source": c["source"], "text": c["text"], "score": float(s)}
        for s, c in scored[:top_k]
    ]
