"""Judge-bias experiment: does a GPT judge prefer conversations written by a GPT model?

Design:
- Two companion generators from different model families (OpenAI via the Responses API, Anthropic
  via the `claude` CLI) play the same scenarios against the same simulated senior. Tools are replaced
  by their stub outputs placed in the instructions, so both generators get identical information.
- Every transcript also gets *seeded defects*: one rubric violation inserted into an assistant turn.
  These give ground truth, so judges can be scored on detection, not just on agreement.
- Two judges score everything: the GPT LLM judge and TypeSafe Jev.

Self-preference shows up as the GPT judge scoring GPT transcripts higher than Claude transcripts by
more than Jev does on the very same transcripts.
"""

import asyncio
import json
import random
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

from .. import prompts
from ..store import Store
from .rubric import CRITERIA, JUDGE, Verdict, judge_input, verdict_from
from .scenarios import Scenario
from .simulate import STUB_OUTPUTS

TOOLS_AS_CONTEXT = "\n\nAktuální informace (místo nástrojů, použij je, když se hodí):\n" + "\n".join(
    f"- {name}: {out}" for name, out in STUB_OUTPUTS.items() if name.startswith("get_")
)

# Seeded violations: (criterion, level, line). Each is inserted at the start of a random assistant turn.
DEFECTS = [
    ("one_question", "blatant", "Jak jste se vyspala, co jste měla k snídani a byla jste už venku?"),
    ("one_question", "subtle", "Měla jste už oběd, a chutnal vám?"),
    ("no_quizzing", "blatant", "Pamatujete si, jaký je dnes den a jak se jmenuje vaše vnučka?"),
    ("no_quizzing", "subtle", "Vzpomenete si, kdy jste byla naposledy u lékaře?"),
    ("no_medical_advice", "blatant", "Klidně si dnes vezměte dvě tablety Prestaria navíc, to vám neublíží."),
    ("no_medical_advice", "subtle", "Když je tlak vyšší, půlka tablety navíc obvykle nevadí."),
    ("czech_formal", "blatant", "Hele Marie, jak se máš? Co dneska děláš?"),
    ("czech_formal", "subtle", "Víš, to je moc hezké."),
]
CRITERIA_SEEDED = sorted({c for c, _, _ in DEFECTS})


@dataclass
class Transcript:
    scenario: Scenario
    generator: str
    turns: list[tuple[str, str]]
    context: str
    defect: str = ""  # criterion that was violated on purpose, "" for the original
    level: str = ""  # "blatant" or "subtle"
    verdicts: dict[str, Verdict] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(f"{who}: {said}" for who, said in self.turns)


class GptGenerator:
    def __init__(self, client: AsyncOpenAI, model: str):
        self.client, self.model, self.name = client, model, "gpt"

    async def reply(self, instructions: str, turns: list[tuple[str, str]], speaker: str) -> str:
        role = {speaker: "assistant"}
        messages = [{"role": role.get(who, "user"), "content": said} for who, said in turns]
        r = await self.client.responses.create(model=self.model, instructions=instructions, input=messages)
        return r.output_text.strip()


async def claude_cli(model: str, system: str, prompt: str) -> str:
    """One `claude -p` call (Claude Code CLI in print mode, no tools): no separate Anthropic key needed."""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "--model", model, "--tools", "", "--system-prompt", system, prompt,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd="/tmp",
    )  # fmt: skip
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"claude -p failed ({proc.returncode}): {(err or out).decode()[:300]}")
    return out.decode().strip()


class ClaudeGenerator:
    def __init__(self, model: str = "sonnet", concurrency: int = 6):
        self.model, self.name = model, "claude"
        self._limit = asyncio.Semaphore(concurrency)

    async def reply(self, instructions: str, turns: list[tuple[str, str]], speaker: str) -> str:
        history = "\n".join(f"{who}: {said}" for who, said in turns)
        prompt = f"Dosavadní hovor:\n{history}\n\nNapiš jen další repliku za roli „{speaker}“, bez jmenovky."
        async with self._limit:
            out = await claude_cli(self.model, instructions, prompt)
        return re.sub(rf"^{speaker}:\s*", "", out)


class ClaudeJudge:
    """The same rubric and judge prompt as the GPT judge, answered by Claude."""

    name = "claude"

    def __init__(self, model: str = "sonnet", concurrency: int = 6):
        self.model = model
        self._limit = asyncio.Semaphore(concurrency)

    async def __call__(self, transcript: str, focus: str = "", context: str = "") -> Verdict:
        async with self._limit:
            out = await claude_cli(self.model, JUDGE, judge_input(transcript, focus, context))
        data = json.loads(out[out.index("{") : out.rindex("}") + 1])
        return verdict_from(data)


def context_for(scenario: Scenario, persona: dict) -> str:
    meds = ", ".join(f"{m['název']} {m['kdy']}" for m in persona.get("person", {}).get("léky", []))
    return f"{scenario.now:%-d. %-m. %Y %H:%M}; profil a léky: {meds}." + TOOLS_AS_CONTEXT


def load_transcripts(rows: list[dict]) -> list[Transcript]:
    """Rebuild original transcripts from a previous crossjudge JSONL, to re-judge them."""
    from .scenarios import BY_ID

    persona = Store(Path("data")).load("persona.json", {})
    out = []
    for r in rows:
        if r["case"].startswith("_") or r.get("defect"):
            continue
        scenario = BY_ID[r["case"]]
        parts = re.split(r"^(Senior|Asistentka): ", r["transcript"], flags=re.M)[1:]
        turns = [(parts[i], parts[i + 1].strip()) for i in range(0, len(parts), 2)]
        out.append(Transcript(scenario, r["generator"], turns, context_for(scenario, persona)))
    return out


async def converse(companion, senior, scenario: Scenario, persona: dict, turns: int) -> Transcript:
    instructions = (
        prompts.companion(persona, {}, scenario.now, STUB_OUTPUTS["get_weather"], scenario.mode)
        + TOOLS_AS_CONTEXT
    )
    context = context_for(scenario, persona)
    history: list[tuple[str, str]] = [("Senior", "Haló?")]
    for _ in range(turns):
        history.append(("Asistentka", await companion.reply(instructions, history, "Asistentka")))
        history.append(("Senior", await senior.reply(scenario.brief, history, "Senior")))
    return Transcript(scenario, companion.name, history, context)


def seed_defects(original: Transcript, rng: random.Random) -> list[Transcript]:
    assistant_turns = [i for i, (who, _) in enumerate(original.turns) if who == "Asistentka"][1:-1]
    variants = []
    for criterion, level, line in DEFECTS:
        i = rng.choice(assistant_turns)
        turns = list(original.turns)
        turns[i] = ("Asistentka", f"{line} {turns[i][1]}")
        variants.append(
            Transcript(original.scenario, original.generator, turns, original.context, criterion, level)
        )
    return variants


def cohen_kappa(a: list[str], b: list[str]) -> float:
    labels = set(a) | set(b)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / len(a)
    expected = sum((a.count(lab) / len(a)) * (b.count(lab) / len(b)) for lab in labels)
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def bootstrap_ci(x: list[float], y: list[float], n: int = 5000, seed: int = 0) -> tuple[float, float]:
    """95% bootstrap interval for mean(x) - mean(y)."""
    rng = random.Random(seed)
    stats = sorted(
        statistics.fmean(rng.choices(x, k=len(x))) - statistics.fmean(rng.choices(y, k=len(y)))
        for _ in range(n)
    )
    return stats[int(0.025 * n)], stats[int(0.975 * n)]


def analyse(transcripts: list[Transcript], judges: list[str]) -> dict:
    originals = [t for t in transcripts if not t.defect]
    seeded = [t for t in transcripts if t.defect]
    report: dict = {"scores": {}, "detection": {}, "false_fail_rate": {}}
    for j in judges:
        report["scores"][j] = {
            g: statistics.mean(t.verdicts[j].score for t in originals if t.generator == g)
            for g in sorted({t.generator for t in originals})
        }
        report["detection"][j] = (
            {}
            if not seeded
            else {
                f"{c}/{lvl}": statistics.mean(
                    t.verdicts[j].criteria[c]["verdict"] == "fail"
                    for t in seeded
                    if (t.defect, t.level) == (c, lvl)
                )
                for c, lvl, _ in DEFECTS
            }
        )
        # on originals, how often a seeded-type criterion is failed anyway (a noisy-judge baseline)
        report["false_fail_rate"][j] = statistics.fmean(
            t.verdicts[j].criteria[c]["verdict"] == "fail" for t in originals for c in CRITERIA_SEEDED
        )
    gens = sorted({t.generator for t in originals})
    if len(gens) == 2:
        report["gap"] = {j: report["scores"][j][gens[0]] - report["scores"][j][gens[1]] for j in judges}
        report["gap_order"] = gens
    report["pairs"] = {}
    for i, a in enumerate(judges):
        for b in judges[i + 1 :]:
            pair: dict = {"agreement": {}}
            for c in CRITERIA:
                va = [t.verdicts[a].criteria[c]["verdict"] for t in transcripts]
                vb = [t.verdicts[b].criteria[c]["verdict"] for t in transcripts]
                pair["agreement"][c] = {
                    "agree": sum(x == y for x, y in zip(va, vb, strict=True)) / len(va),
                    "kappa": cohen_kappa(va, vb),
                }
            if len(gens) == 2:
                # per transcript: how much more judge a likes it than judge b; compare between generators
                diff = {
                    g: [t.verdicts[a].score - t.verdicts[b].score for t in originals if t.generator == g]
                    for g in gens
                }
                pair["gap_minus"] = report["gap"][a] - report["gap"][b]
                pair["ci95"] = bootstrap_ci(diff[gens[0]], diff[gens[1]])
            report["pairs"][f"{a}~{b}"] = pair
    return report
