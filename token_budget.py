"""
Token 預算管理。優先使用 llama-server 的 /tokenize 端點，失敗回退啟發式估算。
"""
import httpx

from config import LLAMA_BASE_URL


CJK_TOKEN_PER_CHAR = 1.4
ASCII_TOKEN_PER_CHAR = 0.3


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cjk
    return int(cjk * CJK_TOKEN_PER_CHAR + other * ASCII_TOKEN_PER_CHAR)


async def count_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                f"{LLAMA_BASE_URL}/tokenize",
                json={"content": text},
            )
            r.raise_for_status()
            data = r.json()
            tokens = data.get("tokens", [])
            if isinstance(tokens, list):
                return len(tokens)
    except Exception:
        pass
    return estimate_tokens(text)


async def check_llama_health() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{LLAMA_BASE_URL}/models")
            if r.status_code == 200:
                return True, "ok"
            return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)


def truncate_to_token_budget(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if estimate_tokens(text[-mid:]) >= max_tokens:
            hi = mid
        else:
            lo = mid + 1
    keep = max(0, lo - 1)
    return "[...更早內容已省略...]\n\n" + text[-keep:] if keep else text[-500:]