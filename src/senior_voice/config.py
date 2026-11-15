"""Runtime settings, read once from the environment."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    live_model: str = "gpt-live-1"
    backend_model: str = "gpt-6-luna"  # model the Live session delegates tool work to
    librarian_model: str = "gpt-6-luna"
    voice: str = "marin"
    openai_api_key: str = ""
    webhook_secret: str = ""
    telegram_token: str = ""
    telegram_chat: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        backend = env("BACKEND_MODEL", cls.backend_model)
        return cls(
            data_dir=Path(env("DATA_DIR", str(cls.data_dir))),
            live_model=env("LIVE_MODEL", cls.live_model),
            backend_model=backend,
            librarian_model=env("LIBRARIAN_MODEL", backend),
            voice=env("VOICE", cls.voice),
            openai_api_key=env("OPENAI_API_KEY", ""),
            webhook_secret=env("OPENAI_WEBHOOK_SECRET", ""),
            telegram_token=env("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat=env("TELEGRAM_CHAT_ID", ""),
        )
