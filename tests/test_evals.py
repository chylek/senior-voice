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


def test_seeded_defects_and_kappa():
    import random

    from senior_voice.evals.crossjudge import DEFECTS, Transcript, analyse, cohen_kappa, seed_defects
    from senior_voice.evals.rubric import Verdict

    turns = [("Senior", "Haló?")] + [
        ("Asistentka", f"a{i}") if i % 2 == 0 else ("Senior", f"s{i}") for i in range(8)
    ]
    original = Transcript(SCENARIOS[0], "gpt", turns, "ctx")
    variants = seed_defects(original, random.Random(1))
    assert len(variants) == len(DEFECTS)
    for v, (criterion, level, line) in zip(variants, DEFECTS, strict=True):
        assert (v.defect, v.level) == (criterion, level)
        changed = [new for new, old in zip(v.turns, turns, strict=True) if new != old]
        assert len(changed) == 1 and changed[0][0] == "Asistentka" and changed[0][1].startswith(line)

    assert cohen_kappa(["pass", "fail"], ["pass", "fail"]) == 1.0
    assert cohen_kappa(["pass", "pass", "fail", "fail"], ["pass", "fail", "pass", "fail"]) == 0.0

    ok = {c: {"verdict": "pass"} for c in CRITERIA}
    for t in [original, *variants]:
        crit = dict(ok)
        if t.defect:
            crit[t.defect] = {"verdict": "fail"}
        t.verdicts = {"a": Verdict(crit, ""), "b": Verdict(dict(ok) if t.defect else crit, "")}
    report = analyse([original, *variants], ["a", "b"])
    assert set(report["pairs"]) == {"a~b"}
    assert all(v == 1.0 for v in report["detection"]["a"].values())
    assert all(v == 0.0 for v in report["detection"]["b"].values())
    assert report["false_fail_rate"] == {"a": 0.0, "b": 0.0}


def test_bootstrap_ci_brackets_the_difference():
    from senior_voice.evals.crossjudge import bootstrap_ci

    lo, hi = bootstrap_ci([1.0, 0.9, 0.8, 1.0], [0.5, 0.6, 0.4, 0.5])
    assert 0 < lo < 0.425 < hi
    assert bootstrap_ci([0.5] * 5, [0.5] * 5) == (0.0, 0.0)


def test_load_transcripts_round_trip():
    from senior_voice.evals.crossjudge import load_transcripts

    text = "Senior: Haló?\nAsistentka: Dobrý den,\npaní Marie.\nSenior: Ahoj"
    rows = [
        {"case": "lonely_sad", "generator": "claude", "defect": "", "transcript": text},
        {"case": "lonely_sad", "generator": "claude", "defect": "no_quizzing", "transcript": text},
        {"case": "_report"},
    ]
    (t,) = load_transcripts(rows)
    assert t.turns == [("Senior", "Haló?"), ("Asistentka", "Dobrý den,\npaní Marie."), ("Senior", "Ahoj")]
    assert t.generator == "claude" and "Atoris" in t.context


def test_cluster_bootstrap_is_wider_than_naive():
    from senior_voice.evals.crossjudge import bootstrap_ci

    # two clusters that disagree strongly: resampling clusters must widen the interval
    x = [1.0] * 10 + [0.0] * 10
    y = [0.5] * 20
    labels = (["a"] * 10 + ["b"] * 10, ["a"] * 10 + ["b"] * 10)
    naive = bootstrap_ci(x, y)
    clustered = bootstrap_ci(x, y, clusters=labels)
    assert clustered[1] - clustered[0] > naive[1] - naive[0]
