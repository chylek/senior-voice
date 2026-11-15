"""After each call: merge the transcript into long-term memory and write a family report."""

import datetime as dt
import json
import logging

from openai import AsyncOpenAI

from . import prompts

log = logging.getLogger(__name__)


async def update_memory(
    client: AsyncOpenAI, model: str, memory: dict, transcript: str, today: dt.date
) -> tuple[dict, str]:
    """Return (new memory, call summary). Recent `live_notes` written by tools during calls are kept.

    `call_flags` (signals from this call only) is kept next to the durable `flags`, so the family report
    doesn't repeat last week's chest pain as if it were new.
    """
    response = await client.responses.create(
        model=model,
        instructions=prompts.LIBRARIAN,
        text={"format": {"type": "json_object"}},
        input=(
            f"Vrať aktualizovanou paměť jako json. Dnes je {today}.\n"
            f"PAMĚŤ:\n{json.dumps(memory, ensure_ascii=False)}\n\nPŘEPIS:\n{transcript}"
        ),
    )
    new = json.loads(response.output_text)
    if not isinstance(new, dict) or not isinstance(new.get("open_topics", []), list):
        raise ValueError("librarian returned an unexpected shape")
    summary = new.pop("call_summary", "")
    new["call_flags"] = new.get("call_flags") or []
    new["live_notes"] = memory.get("live_notes", [])[-30:]
    return new, summary


def _bullets(memory: dict, key: str, title: str) -> str:
    items = [i if isinstance(i, str) else " – ".join(map(str, i.values())) for i in memory.get(key) or []]
    return f"\n{title}:\n" + "\n".join(f"• {i}" for i in items) if items else ""


def report(call_info: str, summary: str, memory: dict) -> str:
    """Telegram-ready post-call report for the family."""
    return (
        f"📞 Hovor s Aničkou ({call_info})\n\n{summary}\n"
        + _bullets(memory, "call_flags", "⚠️ Signály v tomto hovoru")
        + _bullets(memory, "open_topics", "Otevřená témata")
        + _bullets(memory, "sensitive_topics", "Citlivá témata")
        + f"\n\nNálada: {memory.get('mood_trend', '?')}"
    )
