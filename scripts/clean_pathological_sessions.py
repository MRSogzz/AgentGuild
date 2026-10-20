"""Find and optionally redact degenerate agent output in saved sessions.

Run without arguments for a report. Pass --apply to create a .bak copy and
replace only contaminated response/message content with a marker.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from config import SESSIONS_DIR
from memory import _is_pathological_text


MARKER = "[退化輸出已從歷史中剔除]"


def scan(path: Path, apply: bool) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR {path.name}: {exc}")
        return 0

    count = 0
    for round_data in data.get("history", []):
        for key, value in (round_data.get("responses") or {}).items():
            if isinstance(value, str) and _is_pathological_text(value):
                count += 1
                if apply:
                    round_data["responses"][key] = MARKER
        for message in round_data.get("messages", []):
            value = message.get("content", "")
            if isinstance(value, str) and _is_pathological_text(value):
                count += 1
                if apply:
                    message["content"] = MARKER

    if count:
        print(f"{'REDACTED' if apply else 'FOUND'} {path.name}: {count} contaminated fields")
        if apply:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Back up and redact contaminated fields")
    args = parser.parse_args()
    total = sum(scan(path, args.apply) for path in sorted(SESSIONS_DIR.glob("*.json")))
    print(f"Total contaminated fields: {total}")
    if total and not args.apply:
        print("No files changed. Re-run with --apply to back up and redact matching fields.")


if __name__ == "__main__":
    main()
