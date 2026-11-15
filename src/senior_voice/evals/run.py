"""CLI: `senior-voice-eval sim` (simulated scenarios) or `senior-voice-eval transcripts DIR` (real calls)."""

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import statistics
from collections import defaultdict
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from ..store import Store
from . import crossjudge
from .jev import JevJudge
from .rubric import CRITERIA, Verdict, judge
from .scenarios import BY_ID, SCENARIOS
from .simulate import simulate


def _row(name: str, verdict, missing: set[str] = frozenset()) -> str:
    marks = "".join(
        f"{ {'pass': '✓', 'fail': '✗'}.get(verdict.criteria[c].get('verdict'), '·'):<3}" for c in CRITERIA
    )
    tools = f"  missing tools: {', '.join(sorted(missing))}" if missing else ""
    return f"{name:<28} {verdict.score:>5.0%}  {marks}{tools}"


def _header() -> str:
    return (
        f"{'case':<28} {'score':>5}  "
        + " ".join(c[:2] for c in CRITERIA)
        + f"\nlegend: {', '.join(CRITERIA)}"
    )


def summarize(results: list[dict]) -> dict[str, dict]:
    """Per case over repeated runs: mean score, how often each criterion failed, runs missing tools."""
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_case[r["case"]].append(r)
    summary = {}
    for case, runs in by_case.items():
        fails: dict[str, int] = defaultdict(int)
        for r in runs:
            for criterion in r["failed"]:
                fails[criterion] += 1
        summary[case] = {
            "runs": len(runs),
            "mean": sum(r["score"] for r in runs) / len(runs),
            "fails": dict(fails),
            "missing_tools_runs": sum(bool(r.get("missing_tools")) for r in runs),
        }
    return summary


async def run_sim(args, client: AsyncOpenAI) -> list[dict]:
    scenarios = [BY_ID[i] for i in args.scenario] if args.scenario else SCENARIOS
    store = Store(Path(args.data_dir))
    sims = await asyncio.gather(
        *(
            simulate(client, s, args.model, args.senior_model, store, args.turns)
            for s in scenarios
            for _ in range(args.repeat)
        )
    )
    verdicts = await asyncio.gather(
        *(judge(client, args.judge_model, sim.transcript, sim.scenario.focus, sim.context) for sim in sims)
    )
    results = []
    print(_header())
    for sim, verdict in zip(sims, verdicts, strict=True):
        print(_row(sim.scenario.id, verdict, sim.missing_tools))
        results.append(
            {
                "case": sim.scenario.id,
                "score": verdict.score,
                "failed": verdict.failed,
                "missing_tools": sorted(sim.missing_tools),
                "criteria": verdict.criteria,
                "overall": verdict.overall,
                "transcript": sim.transcript,
            }
        )
    if args.repeat > 1:
        print("\nper case over", args.repeat, "runs:")
        for case, agg in summarize(results).items():
            fails = ", ".join(f"{k}×{v}" for k, v in agg["fails"].items()) or "-"
            tools = (
                f"  missing tools in {agg['missing_tools_runs']} runs" if agg["missing_tools_runs"] else ""
            )
            print(f"{case:<28} {agg['mean']:>5.0%}  fails: {fails}{tools}")
    return results


async def run_transcripts(args, client: AsyncOpenAI) -> list[dict]:
    files = await asyncio.to_thread(
        lambda: sorted(p for p in Path(args.dir).glob("*.txt") if p.stat().st_size)
    )
    results = []
    print(_header())
    for path in files:
        context = "aktuální datum, čas a počasí; profil seniora včetně rozpisu léků"
        verdict = await judge(
            client, args.judge_model, await asyncio.to_thread(path.read_text), context=context
        )
        print(_row(path.stem, verdict))
        results.append({"case": path.stem, "score": verdict.score, "failed": verdict.failed, **vars(verdict)})
    return results


class GptJudge:
    name = "gpt"

    def __init__(self, client: AsyncOpenAI, model: str):
        self.client, self.model = client, model

    async def __call__(self, transcript: str, focus: str = "", context: str = "") -> Verdict:
        return await judge(self.client, self.model, transcript, focus, context)


async def run_crossjudge(args, client: AsyncOpenAI) -> list[dict]:
    scenarios = [BY_ID[i] for i in args.scenario] if args.scenario else SCENARIOS
    persona = Store(Path(args.data_dir)).load("persona.json", {})
    senior = crossjudge.GptGenerator(client, args.senior_model)
    generators = [crossjudge.GptGenerator(client, args.model), crossjudge.ClaudeGenerator(args.claude_model)]
    originals = await asyncio.gather(
        *(
            crossjudge.converse(g, senior, s, persona, args.turns)
            for g in generators
            for s in scenarios
            for _ in range(args.reps)
        )
    )
    rng = random.Random(args.seed)
    transcripts = [
        v for t in originals for v in (t, *(crossjudge.seed_defects(t, rng) if args.defects else ()))
    ]
    async with httpx.AsyncClient(timeout=60) as http:
        judges = [GptJudge(client, args.judge_model), JevJudge(http, os.environ["JEV_API_KEY"])]
        limit = asyncio.Semaphore(32)

        async def score(t, j):
            async with limit:
                t.verdicts[j.name] = await j(t.text, t.scenario.focus, t.context)

        await asyncio.gather(*(score(t, j) for t in transcripts for j in judges))
    report = crossjudge.analyse(transcripts, [j.name for j in judges])
    print_report(report, len(originals), len(transcripts) - len(originals))
    return [
        {
            "case": t.scenario.id,
            "generator": t.generator,
            "defect": t.defect,
            "transcript": t.text,
            "verdicts": {j: v.criteria for j, v in t.verdicts.items()},
            "score": statistics.mean(v.score for v in t.verdicts.values()),
        }
        for t in transcripts
    ] + [{"case": "_report", "score": 0, **report}]


def print_report(report: dict, n_orig: int, n_seeded: int) -> None:
    print(f"{n_orig} original transcripts, {n_seeded} with one seeded defect\n")
    print("mean rubric score on originals (judge × generator):")
    for j, by_gen in report["scores"].items():
        print(f"  {j:<6}" + "  ".join(f"{g} {v:.0%}" for g, v in by_gen.items()))
    if sp := report.get("self_preference"):
        a, b = sp["order"]
        print(f"  {a} − {b} gap: " + ", ".join(f"{j} {v:+.1%}" for j, v in sp["gap"].items()))
        lo, hi = sp["ci95"]
        print(f"  difference of gaps {sp['gap_minus']:+.1%} (95% bootstrap CI {lo:+.1%} … {hi:+.1%})")
    judges = list(report["detection"])
    if report["detection"][judges[0]]:
        print("\nseeded defect detected (judge said fail on that criterion):")
        print(f"  {'defect':<26}" + "".join(f"{j:>8}" for j in judges))
        for key in report["detection"][judges[0]]:
            print(f"  {key:<26}" + "".join(f"{report['detection'][j][key]:>8.0%}" for j in judges))
    print(
        "false fail rate on originals (same criteria): "
        + ", ".join(f"{j} {v:.0%}" for j, v in report["false_fail_rate"].items())
    )
    if report["agreement"]:
        print("\njudge agreement per criterion (all transcripts):")
        for c, a in report["agreement"].items():
            print(f"  {c:<18} agree {a['agree']:.0%}  kappa {a['kappa']:+.2f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="senior-voice-eval", description=__doc__)
    parser.add_argument("--judge-model", default="gpt-6-luna")
    parser.add_argument("--out", default="eval-results", help="directory for JSONL results")
    sub = parser.add_subparsers(dest="command", required=True)
    sim = sub.add_parser("sim", help="simulate scenarios with a text-model proxy and judge them")
    sim.add_argument("--scenario", action="append", choices=list(BY_ID), help="repeatable; default: all")
    sim.add_argument("--model", default="gpt-6-luna", help="companion proxy model")
    sim.add_argument("--senior-model", default="gpt-6-luna")
    sim.add_argument("--turns", type=int, default=8)
    sim.add_argument("--repeat", type=int, default=1, help="runs per scenario (LLM sims are noisy)")
    sim.add_argument(
        "--data-dir", default="data", help="persona.json location (falls back to the bundled one)"
    )
    cj = sub.add_parser(
        "crossjudge", help="GPT vs Jev judge on GPT- and Claude-written calls + seeded defects"
    )
    cj.add_argument("--scenario", action="append", choices=list(BY_ID))
    cj.add_argument("--reps", type=int, default=2)
    cj.add_argument("--turns", type=int, default=6)
    cj.add_argument("--model", default="gpt-6-luna", help="GPT companion")
    cj.add_argument("--claude-model", default="sonnet", help="Claude companion (via the claude CLI)")
    cj.add_argument("--senior-model", default="gpt-6-luna")
    cj.add_argument("--seed", type=int, default=7)
    cj.add_argument("--no-defects", dest="defects", action="store_false", help="judge originals only")
    cj.add_argument("--data-dir", default="data")
    tr = sub.add_parser("transcripts", help="judge real call transcripts")
    tr.add_argument("dir")
    args = parser.parse_args(argv)

    client = AsyncOpenAI()
    runner = {"sim": run_sim, "transcripts": run_transcripts, "crossjudge": run_crossjudge}[args.command]
    results = asyncio.run(runner(args, client))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{args.command}.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    scored = [r for r in results if not r["case"].startswith("_")]
    mean = sum(r["score"] for r in scored) / len(scored) if scored else 0
    print(f"\nmean score {mean:.0%} over {len(scored)} cases → {path}")
