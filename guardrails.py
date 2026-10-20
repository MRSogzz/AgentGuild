import ast
from pathlib import Path

from config import WORKSPACE_DIR


class GuardrailError(Exception):
    pass


MAX_INPUT_CHARS = 8000


def check_user_input(text: str) -> str:
    if not text or not text.strip():
        raise GuardrailError("輸入為空")
    text = text.strip()
    if len(text) > MAX_INPUT_CHARS:
        raise GuardrailError(f"輸入過長（>{MAX_INPUT_CHARS} 字）")
    return text


def safe_path(raw: str) -> Path:
    if not raw:
        raise GuardrailError("路徑為空")
    p = (WORKSPACE_DIR / raw).resolve()
    root = WORKSPACE_DIR.resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise GuardrailError(f"路徑越界: {raw}")
    return p


MAX_FILE_SIZE = 500_000
MAX_FILES_IN_WORKSPACE = 500

# ★ 新增前端擴展名
ALLOWED_EXT = {
    # 後端
    ".py", ".txt", ".md", ".json", ".yaml", ".yml",
    ".csv", ".log", ".ini", ".toml",
    # 前端
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".js", ".mjs", ".cjs", ".jsx",
    ".ts", ".tsx", ".vue", ".svelte",
    ".svg", ".xml",
}


def check_write_target(p: Path):
    if p.exists():
        return
    ext = p.suffix.lower()
    if ext not in ALLOWED_EXT:
        raise GuardrailError(f"不允許的檔案類型: {ext or '(無後綴)'}")
    count = sum(1 for _ in WORKSPACE_DIR.rglob("*") if _.is_file())
    if count >= MAX_FILES_IN_WORKSPACE:
        raise GuardrailError(f"工作區檔案數已達上限（{MAX_FILES_IN_WORKSPACE}）")


def check_file_content_size(content: str):
    size = len(content.encode("utf-8"))
    if size > MAX_FILE_SIZE:
        raise GuardrailError(f"內容過大: {size} 位元組 > {MAX_FILE_SIZE}")


FORBIDDEN_IMPORTS = {
    "os", "sys", "subprocess", "shutil", "socket",
    "urllib", "requests", "pathlib", "ctypes",
    "multiprocessing", "threading", "pickle", "marshal",
    "importlib", "pkgutil", "platform", "pty", "tty",
}

FORBIDDEN_BUILTINS = {
    "eval", "exec", "compile", "__import__", "open",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "breakpoint",
}

FORBIDDEN_PATTERNS = (
    "__subclasses__", "__bases__", "__mro__", "__globals__",
    "__code__", "__closure__", "__builtins__", "__loader__",
)

MAX_SCRIPT_CHARS = 30000


def check_python_ast(code: str) -> str:
    if len(code) > MAX_SCRIPT_CHARS:
        raise GuardrailError(
            f"程式碼過長（>{MAX_SCRIPT_CHARS} 字元，實際 {len(code)}）"
        )

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise GuardrailError(f"語法錯誤: {e}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                root = name.name.split(".")[0]
                if root in FORBIDDEN_IMPORTS:
                    raise GuardrailError(f"禁止 import {root}")
        if isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in FORBIDDEN_IMPORTS:
                    raise GuardrailError(f"禁止 from {root} import")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_BUILTINS:
                raise GuardrailError(f"禁止調用 {node.func.id}()")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise GuardrailError(f"禁止訪問 dunder 屬性 {node.attr}")

    for pat in FORBIDDEN_PATTERNS:
        if pat in code:
            raise GuardrailError(f"禁止的模式: {pat}")

    return code


def truncate_output(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[截斷，原長 {len(text)}]"