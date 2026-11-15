import json
from types import SimpleNamespace

from senior_voice.evals.rubric import CRITERIA, Verdict, judge
from senior_voice.evals.scenarios import SCENARIOS
from senior_voice.evals.simulate import STUB_OUTPUTS, simulate
from senior_voice.tools import SCHEMAS


def test_scenario_ids_unique_and_tools_exist():
    assert len({s.id for s in SCENARIOS}) == len(SCENARIOS)
    names = {s["name"] for s in SCHEMAS}
    assert all(s.expect_tools <= names for s in SCENARIOS)
    assert set(STUB_OUTPUTS) == names


def test_score_ignores_na():
    v = Verdict({"a": {"verdict": "pass"}, "b": {"verdict": "fail"}, "c": {"verdict": "na"}}, "")
    assert v.score == 0.5 and v.failed == ["b"]
    assert Verdict({"a": {"verdict": "na"}}, "").score == 1.0


def _text(text, rid="r"):
    return SimpleNamespace(id=rid, output=[], output_text=text)


class ScriptedClient:
    """Fake Responses API: companion asks for a tool once, then talks; the senior always answers."""

    def __init__(self):
        self.calls = []
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **kw):
        self.calls.append(kw)
        if "tools" not in kw:  # the senior simulator or the judge
            if kw.get("text"):
                crit = {c: {"verdict": "pass", "evidence": "ok"} for c in CRITERIA}
                return _text(json.dumps({"criteria": crit, "overall": "dobré"}))
            return _text("Jo, dobře.", "senior")
        if isinstance(kw["input"], list):  # tool outputs came back
            return _text("Venku je zataženo.", "after-tool")
        call = SimpleNamespace(
            type="function_call", name="get_weather", arguments='{"day":"dnes"}', call_id="c1"
        )
        return SimpleNamespace(id="with-tool", output=[call], output_text="")


async def test_simulation_records_turns_and_tools(store):
    client = ScriptedClient()
    sim = await simulate(client, SCENARIOS[4], "m", "m", store, turns=2)  # news_and_tv expects news + tv
    assert sim.tool_calls == [("get_weather", {"day": "dnes"})] * 2
    assert sim.missing_tools == {"get_news", "get_tv"}
    assert "Atoris 20 mg večer" in sim.context
    assert [who for who, _ in sim.turns][:4] == ["Senior", "Nástroj", "Asistentka", "Senior"]
    verdict = await judge(client, "m", sim.transcript)
    assert verdict.score == 1.0 and set(verdict.criteria) == set(CRITERIA)


def test_summarize_aggregates_repeated_runs():
    from senior_voice.evals.run import summarize

    results = [
        {"case": "a", "score": 1.0, "failed": [], "missing_tools": []},
        {"case": "a", "score": 0.5, "failed": ["validation"], "missing_tools": ["escalate"]},
        {"case": "b", "score": 0.75, "failed": ["validation", "grounded"], "missing_tools": []},
    ]
    s = summarize(results)
    assert s["a"] == {"runs": 2, "mean": 0.75, "fails": {"validation": 1}, "missing_tools_runs": 1}
    assert s["b"]["fails"] == {"validation": 1, "grounded": 1}
