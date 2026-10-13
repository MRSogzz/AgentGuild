import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from config import LOGS_DIR


class Logger:
    """簡易結構化日誌 + trace。寫到 logs/ 下按日期分檔案。"""

    def __init__(self):
        self.traces: dict[str, dict] = {}

    def _today_file(self) -> Path:
        return LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"

    def log(self, level: str, event: str, **fields):
        entry = {
            "ts": time.time(),
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "level": level,
            "event": event,
            **fields,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._today_file().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"[{level}] {event} {fields if fields else ''}")

    def info(self, event, **f):  self.log("INFO", event, **f)
    def warn(self, event, **f):  self.log("WARN", event, **f)
    def error(self, event, **f): self.log("ERROR", event, **f)

    # ---------- Trace ----------
    def start_trace(self, name: str, **meta) -> str:
        tid = uuid.uuid4().hex[:12]
        self.traces[tid] = {
            "id": tid,
            "name": name,
            "start": time.time(),
            "meta": meta,
            "spans": [],
        }
        return tid

    @contextmanager
    def span(self, trace_id: str, name: str, **meta):
        start = time.time()
        span = {"name": name, "start": start, "meta": meta}
        try:
            yield span
        finally:
            span["duration_ms"] = int((time.time() - start) * 1000)
            if trace_id in self.traces:
                self.traces[trace_id]["spans"].append(span)

    def finish_trace(self, trace_id: str):
        t = self.traces.pop(trace_id, None)
        if not t:
            return None
        t["duration_ms"] = int((time.time() - t["start"]) * 1000)
        path = LOGS_DIR / "traces.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
        return t

    def metrics(self) -> dict:
        path = LOGS_DIR / "traces.jsonl"
        if not path.exists():
            return {"traces": 0, "avg_duration_ms": 0}
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        n = len(rows)
        avg = int(sum(r.get("duration_ms", 0) for r in rows) / n) if n else 0
        return {"traces": n, "avg_duration_ms": avg}


logger = Logger()
