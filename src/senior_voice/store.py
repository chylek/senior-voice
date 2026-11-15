"""JSON files in the data directory: persona, memory, medication log, transcripts."""

import json
from importlib import resources
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir

    def load(self, name: str, default: Any) -> Any:
        path = self.dir / name
        if path.exists():
            return json.loads(path.read_text())
        bundled = resources.files("senior_voice") / "defaults" / name
        return json.loads(bundled.read_text()) if bundled.is_file() else default

    def save(self, name: str, obj: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2))

    def append(self, name: str, item: Any) -> None:
        items = self.load(name, [])
        items.append(item)
        self.save(name, items)

    def save_transcript(self, stem: str, text: str) -> Path:
        path = self.dir / "calls" / f"{stem}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path
