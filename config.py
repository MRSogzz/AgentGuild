import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
from openai import AsyncOpenAI

# ============================================================
# 目錄
# ============================================================
BASE_DIR = Path(__file__).parent

SESSIONS_DIR = BASE_DIR / "sessions"
MEMORY_DIR = BASE_DIR / "memory"
DOCS_DIR = BASE_DIR / "docs"
LOGS_DIR = BASE_DIR / "logs"
WORKSPACE_DIR = BASE_DIR / "workspace"
ARTIFACT_DIR = WORKSPACE_DIR / "artifacts"

VENDOR_SRC_DIR = BASE_DIR / "static" / "vendor"
VENDOR_DST_DIR = WORKSPACE_DIR / "vendor"


def ensure_dirs():
    for d in (SESSIONS_DIR, MEMORY_DIR, DOCS_DIR, LOGS_DIR,
              WORKSPACE_DIR, ARTIFACT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ensure_vendor():
    if not VENDOR_SRC_DIR.exists():
        return
    VENDOR_DST_DIR.mkdir(parents=True, exist_ok=True)
    for src in VENDOR_SRC_DIR.iterdir():
        if not src.is_file():
            continue
        dst = VENDOR_DST_DIR / src.name
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            shutil.copy2(src, dst)
        except Exception:
            pass


# ============================================================
# LLM
# ============================================================
LLAMA_BASE_URL = os.getenv("LLAMA_BASE_URL", "http://127.0.0.1:18080/v1")
LLAMA_API_KEY = os.getenv("LLAMA_API_KEY", "sk-local")
MODEL_NAME = os.getenv("MODEL_NAME", "Qwen3.8-27B-Flash-MTP")

_llm_client: AsyncOpenAI | None = None


def get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            base_url=LLAMA_BASE_URL,
            api_key=LLAMA_API_KEY,
            timeout=httpx.Timeout(300.0, connect=5.0),
            max_retries=1,
        )
    return _llm_client


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ============================================================
# 上下文預算
# ============================================================
SYSTEM_BUDGET = 3000
SAFETY_MARGIN = 1000
OUTPUT_RESERVE_DEFAULT = 8192
MEMORY_INJECT_MAX_TOKENS = 800

_n_ctx_cache: int | None = None


async def get_n_ctx() -> int:
    global _n_ctx_cache
    if _n_ctx_cache is not None:
        return _n_ctx_cache

    base = LLAMA_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/props")
            if r.status_code == 200:
                data = r.json()
                n = (data.get("default_generation_settings", {}).get("n_ctx")
                     or data.get("n_ctx"))
                if n:
                    _n_ctx_cache = int(n)
                    return _n_ctx_cache
    except Exception:
        pass

    _n_ctx_cache = 32768
    return _n_ctx_cache


# ============================================================
# 生成 / 循環 / 審查策略
# ============================================================
MAX_CONTINUATIONS = 3
CONTINUATION_ANCHOR_CHARS = _env_int("CONTINUATION_ANCHOR_CHARS", 800)

MAX_REFINE_ROUNDS = _env_int("MAX_REFINE_ROUNDS", 3)
MAX_REFINE_HARD_LIMIT = _env_int("MAX_REFINE_HARD_LIMIT", 3)
MAX_QUALITY_ROUNDS = _env_int("MAX_QUALITY_ROUNDS", 3)
PIVOT_ROUND = _env_int("PIVOT_ROUND", 3)

REFINE_SIMILARITY_REVIEW = float(os.getenv("REFINE_SIMILARITY_REVIEW", "0.90"))
REFINE_SIMILARITY_DEV = float(os.getenv("REFINE_SIMILARITY_DEV", "0.95"))
REFINE_NO_PROGRESS_LIMIT = _env_int("REFINE_NO_PROGRESS_LIMIT", 2)
VISUAL_QUALITY_MIN = _env_int("VISUAL_QUALITY_MIN", 7)
VISUAL_QUALITY_DIMENSIONS = tuple(filter(None, os.getenv(
    "VISUAL_QUALITY_DIMENSIONS", "requirement_match,composition,readability,finish"
).split(",")))
VISUAL_QUALITY_NO_PROGRESS_LIMIT = _env_int("VISUAL_QUALITY_NO_PROGRESS_LIMIT", 2)
PM_BLOCK_ON_UNRESOLVABLE = _env_bool("PM_BLOCK_ON_UNRESOLVABLE", True)
JSON_CONTRACT_RETRIES = _env_int("JSON_CONTRACT_RETRIES", 1)
PATHOLOGICAL_REPETITION_RETRIES = _env_int("PATHOLOGICAL_REPETITION_RETRIES", 1)
PATHOLOGICAL_REPETITION_WINDOW = _env_int("PATHOLOGICAL_REPETITION_WINDOW", 200)
PATHOLOGICAL_REPEATED_CHAR_MIN = _env_int("PATHOLOGICAL_REPEATED_CHAR_MIN", 20)
PATHOLOGICAL_CHAR_RATIO = float(os.getenv("PATHOLOGICAL_CHAR_RATIO", "0.70"))
PATHOLOGICAL_RETRY_FREQUENCY_PENALTY = float(os.getenv("PATHOLOGICAL_RETRY_FREQUENCY_PENALTY", "0.8"))
LLM_STOP_SEQUENCES = tuple(filter(None, os.getenv("LLM_STOP_SEQUENCES", "").split("|")))
BROWSER_LAUNCH_ARGS = tuple(filter(None, os.getenv(
    "BROWSER_LAUNCH_ARGS", "--enable-webgl|--use-gl=angle|--use-angle=swiftshader"
).split("|")))
VISUAL_SCREENSHOT_WIDTH = _env_int("VISUAL_SCREENSHOT_WIDTH", 800)
VISUAL_SCREENSHOT_HEIGHT = _env_int("VISUAL_SCREENSHOT_HEIGHT", 600)
VISUAL_SCREENSHOT_JPEG_QUALITY = _env_int("VISUAL_SCREENSHOT_JPEG_QUALITY", 78)
VISUAL_SCREENSHOT_READY_TIMEOUT_MS = _env_int("VISUAL_SCREENSHOT_READY_TIMEOUT_MS", 8000)
VISUAL_SCREENSHOT_MAX_IMAGES = _env_int("VISUAL_SCREENSHOT_MAX_IMAGES", 4)
VISUAL_IMAGE_MAX_BYTES = _env_int("VISUAL_IMAGE_MAX_BYTES", 100_000)
VISUAL_IMAGE_TOTAL_MAX_BYTES = _env_int("VISUAL_IMAGE_TOTAL_MAX_BYTES", 420_000)
VISUAL_DEV_MAX_IMAGES = _env_int("VISUAL_DEV_MAX_IMAGES", 1)
REFINE_PREVIOUS_CODE_CHARS = _env_int("REFINE_PREVIOUS_CODE_CHARS", 3000)
VISION_IMAGE_TOKEN_RESERVE = _env_int("VISION_IMAGE_TOKEN_RESERVE", 700)
VISUAL_SCREENSHOT_WAIT_MS = _env_int("VISUAL_SCREENSHOT_WAIT_MS", 3000)
VISUAL_VIEW_SWITCH_WAIT_MS = _env_int("VISUAL_VIEW_SWITCH_WAIT_MS", 200)
BROWSER_PAGE_LOAD_TIMEOUT_MS = _env_int("BROWSER_PAGE_LOAD_TIMEOUT_MS", 15000)
FRONTEND_RUNTIME_WAIT_MS = _env_int("FRONTEND_RUNTIME_WAIT_MS", 1500)
BROWSER_READY_TIMEOUT_MS = _env_int("BROWSER_READY_TIMEOUT_MS", 5000)
FRONTEND_RUNTIME_TIMEOUT_SECONDS = _env_int("FRONTEND_RUNTIME_TIMEOUT_SECONDS", 45)
SCREENSHOT_SUBPROCESS_TIMEOUT_SECONDS = _env_int("SCREENSHOT_SUBPROCESS_TIMEOUT_SECONDS", 60)
VISUAL_EMPTY_PIXEL_RATIO = float(os.getenv("VISUAL_EMPTY_PIXEL_RATIO", "0.01"))
MAX_HISTORY_ROUNDS = _env_int("MAX_HISTORY_ROUNDS", 8)

HIGH_RISK_PATTERNS = [
    r"eval\(", r"exec\(", r"os\.system", r"subprocess",
    r"auth", r"login", r"password", r"token", r"secret",
    r"chmod", r"chown", r"sudo"
]


# ============================================================
# 記憶權重
# ============================================================
MEMORY_WEIGHT = {
    "user_explicit": 9,
    "user_preference": 7,
    "code_definition": 5,
    "general": 3,
}
MEMORY_HIGH_PRIORITY_THRESHOLD = 7
MEMORY_HIGH_PRIORITY_LIMIT = 5
MEMORY_SEARCH_TOP_K = 5


# ============================================================
# 測試驗證開關
# ============================================================
ENABLE_QA_SELF_TEST = _env_bool("ENABLE_QA_SELF_TEST", True)

# ============================================================
# Agent 定義
# ============================================================
@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    avatar: str
    color: str
    tools: tuple
    max_tokens: int = 2048
    temperature: float = 0.3
    max_tool_iterations: int = 0


AGENTS: dict[str, AgentSpec] = {
    a.id: a for a in [
        AgentSpec(
            "pm", "產品經理", "📋", "#8b5cf6",
            (),
            max_tokens=2048,
            temperature=0.5,
            max_tool_iterations=0,
        ),
        AgentSpec(
            "dev", "工程師", "💻", "#06b6d4",
            (),
            max_tokens=8192,
            temperature=0.2,
        ),
        AgentSpec(
            "qa", "QA 測試", "🧪", "#10b981",
            (),
            max_tokens=2048,
            temperature=0.1,
        ),
        AgentSpec(
            "reviewer", "程式評審員", "🔍", "#f59e0b",
            (),
            max_tokens=2048,
            temperature=0.1,
        ),
    ]
}

PIPELINE = ["pm", "dev", "qa", "reviewer"]
