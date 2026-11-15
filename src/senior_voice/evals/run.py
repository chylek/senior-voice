"""CLI: `senior-voice-eval sim` (simulated scenarios) or `senior-voice-eval transcripts DIR` (real calls)."""

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path

from openai import AsyncOpenAI

from ..store import Store
from .rubric import CRITERIA, judge
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


async def run_sim(args, client: AsyncOpenAI) -> list[dict]:
    scenarios = [BY_ID[i] for i in args.scenario] if args.scenario else SCENARIOS
    store = Store(Path(args.data_dir))
    sims = await asyncio.gather(
        *(simulate(client, s, args.model, args.senior_model, store, args.turns) for s in scenarios)
    )
    results = []
    print(_header())
    for sim in sims:
        verdict = await judge(client, args.judge_model, sim.transcript, sim.scenario.focus, sim.context)
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
    sim.add_argument(
        "--data-dir", default="data", help="persona.json location (falls back to the bundled one)"
    )
    tr = sub.add_parser("transcripts", help="judge real call transcripts")
    tr.add_argument("dir")
    args = parser.parse_args(argv)

    client = AsyncOpenAI()
    runner = run_sim if args.command == "sim" else run_transcripts
    results = asyncio.run(runner(args, client))
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    path = out / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{args.command}.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    mean = sum(r["score"] for r in results) / len(results) if results else 0
    print(f"\nmean score {mean:.0%} over {len(results)} cases → {path}")
