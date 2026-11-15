"""JSON files in the data directory: persona, memory, medication log, transcripts."""

import datetime as dt
import json
import logging
import os
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir

    def load(self, name: str, default: Any) -> Any:
        path = self.dir / name
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:  # never let one corrupt file break every future call
                log.error("corrupt %s, using default", path)
                return default
        bundled = resources.files("senior_voice") / "defaults" / name
        return json.loads(bundled.read_text()) if bundled.is_file() else default

    def save(self, name: str, obj: Any) -> None:
        """Atomic write: a crash mid-write must not leave a truncated memory.json behind."""
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2))
        os.replace(f.name, path)

    def snapshot(self, name: str) -> None:
        """Keep a dated copy before an LLM rewrites the file (memory history for review and rollback)."""
        if (self.dir / name).exists():
            stem = name.removesuffix(".json")
            self.save(f"history/{stem}-{dt.datetime.now():%Y%m%d-%H%M%S}.json", self.load(name, {}))

    def append(self, name: str, item: Any) -> None:
        items = self.load(name, [])
        items.append(item)
        self.save(name, items)

    def save_transcript(self, stem: str, text: str) -> Path:
        path = self.dir / "calls" / f"{stem}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path
