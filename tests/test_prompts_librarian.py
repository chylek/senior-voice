import datetime as dt
import json
from types import SimpleNamespace

from senior_voice import librarian, prompts

PERSONA = {"assistant_name": "Anička", "person": {"name": "Marie"}}
MEMORY = {"open_topics": ["zahrádka"], "live_notes": [{"text": f"n{i}"} for i in range(20)]}


def test_companion_prompt_has_context_and_care_rules():
    text = prompts.companion(PERSONA, MEMORY, dt.datetime(2026, 11, 15, 8, 24), "zataženo", "scheduled")
    assert "neděle 15. 11. 2026, 08:24" in text
    assert prompts.OPENINGS["scheduled"] in text
    assert '"zahrádka"' in text and "Marie" in text
    assert "n19" in text and "n4" not in text  # only the 15 most recent live notes
    assert "pamatujete si" in text and "155" in text and "umělá inteligence" in text


def test_inbound_opening():
    text = prompts.companion(PERSONA, {}, dt.datetime(2026, 11, 16, 9, 0), "?", "inbound")
    assert "pondělí" in text and prompts.OPENINGS["inbound"] in text


class FakeResponses:
    def __init__(self, payload: dict):
        self.payload, self.calls = payload, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(self.payload, ensure_ascii=False))


async def test_update_memory_pops_summary_and_keeps_live_notes():
    responses = FakeResponses({"open_topics": ["Gott"], "call_summary": "Byla smutná."})
    client = SimpleNamespace(responses=responses)
    memory, summary = await librarian.update_memory(
        client, "m", MEMORY, "Senior: smutně", dt.date(2026, 11, 15)
    )
    assert summary == "Byla smutná."
    assert memory["open_topics"] == ["Gott"] and "call_summary" not in memory
    assert memory["live_notes"] == MEMORY["live_notes"]
    request = responses.calls[0]
    assert "json" in request["input"]  # required by the json_object response format
    assert request["text"] == {"format": {"type": "json_object"}}


def test_report_lists_flags_and_topics():
    memory = {"flags": ["smutek"], "sensitive_topics": [{"date": "d", "topic": "t"}], "mood_trend": "smutná"}
    text = librarian.report("15. 11. 08:24, plánovaný, 117 s", "Shrnutí.", memory)
    assert text.startswith("📞 Hovor s Aničkou (15. 11. 08:24, plánovaný, 117 s)\n\nShrnutí.")
    assert "• smutek" in text and "• d – t" in text and "Otevřená témata" not in text
    assert text.endswith("Nálada: smutná")
