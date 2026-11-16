"""Guard eval: precision, recall and latency of each detector on labelled utterances."""

import asyncio
import statistics

from ..guard import ACTIONS, Guard
from .guard_cases import CASES, INJECTION_CASES

CATEGORIES = list(ACTIONS)


def score(predicted: list[set[str]], gold: list[frozenset[str]]) -> dict[str, dict[str, float]]:
    """Per category: precision, recall and counts. Precision is 1.0 when nothing was predicted."""
    out = {}
    for c in CATEGORIES:
        tp = sum(c in p and c in g for p, g in zip(predicted, gold, strict=True))
        fp = sum(c in p and c not in g for p, g in zip(predicted, gold, strict=True))
        fn = sum(c not in p and c in g for p, g in zip(predicted, gold, strict=True))
        out[c] = {
            "precision": tp / (tp + fp) if tp + fp else 1.0,
            "recall": tp / (tp + fn) if tp + fn else 1.0,
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }
    return out


async def run(guard: Guard, cases: list[tuple[str, frozenset[str]]] = CASES) -> dict:
    checks = await asyncio.gather(*(guard.check_turn(text, f"Senior: {text}") for text, _ in cases))
    gold = [g for _, g in cases]
    by_source = {
        src: [{s.category for s in c.signals if s.source == src} for c in checks]
        for src in ("jev", "moderation")
    }
    by_source["combined"] = [c.categories for c in checks]
    latency = {
        src: sorted(c.latency_ms[src] for c in checks if src in c.latency_ms) for src in ("jev", "moderation")
    }
    injections = await asyncio.gather(*(guard.is_injection(t) for t, _ in INJECTION_CASES))
    misses = [
        (text, sorted(gold_c), sorted(c.categories))
        for (text, gold_c), c in zip(cases, checks, strict=True)
        if set(gold_c) != c.categories
    ]
    return {
        "n": len(cases),
        "scores": {src: score(pred, gold) for src, pred in by_source.items()},
        "latency_ms": {
            src: {"p50": statistics.median(v), "p95": v[int(0.95 * (len(v) - 1))]}
            for src, v in latency.items()
            if v
        },
        "injection": {
            "correct": sum(p == g for p, (_, g) in zip(injections, INJECTION_CASES, strict=True)),
            "n": len(INJECTION_CASES),
        },
        "misses": misses,
    }


def print_report(report: dict) -> None:
    print(f"{report['n']} labelled utterances\n")
    print(f"{'category':<10}" + "".join(f"{src:>22}" for src in report["scores"]))
    for c in CATEGORIES:
        cells = [
            f"P {s[c]['precision']:.0%} R {s[c]['recall']:.0%}".rjust(22) for s in report["scores"].values()
        ]
        print(f"{c:<10}" + "".join(cells))
    for src, lat in report["latency_ms"].items():
        print(f"latency {src:<11} p50 {lat['p50']:.0f} ms  p95 {lat['p95']:.0f} ms")
    inj = report["injection"]
    print(f"injection screen: {inj['correct']}/{inj['n']} tool outputs classified correctly")
    if report["misses"]:
        print("\nmisses (expected → got):")
        for text, gold, got in report["misses"]:
            print(f"  {gold or '-'} → {got or '-'}: {text}")
