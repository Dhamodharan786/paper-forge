"""A rough cost estimate shown BEFORE a run. The real cost is measured during the run and written to each report.

The token counts below are my own guesses from the prompts' sizes, not measurements. They exist so the user sees an order of
magnitude and the spending cap before pressing the button. Replace them with measured averages after a few real runs.
"""
from __future__ import annotations

from .config import Config
from .generate import build_coding_plan, build_plan
from .research import cache_path
from .speech import build_media_plan, split_recordings
from .writing import build_writing_plan

# (model role, input tokens, output tokens, searches) per unit, BEFORE the retry factor
UNITS = {
    "mcq":       [("generator", 450, 700, 0), ("checker", 350, 60, 0)],
    "coding":    [("generator", 3500, 9000, 0), ("checker", 3000, 300, 0)],
    "recording": [("generator", 600, 2500, 0), ("checker", 1400, 300, 0)],
    "speaking":  [("generator", 300, 600, 0), ("checker", 200, 80, 0)],
    "writing":   [("generator", 400, 1200, 0), ("checker", 300, 80, 0)],
}
RETRY = {"mcq": 1.25, "coding": 1.5, "recording": 1.3, "speaking": 1.25, "writing": 1.3}
RESEARCH = ("research", 120_000, 6_000, 8)


def _usd(cfg: Config, role: str, tin: int, tout: int, searches: int) -> float:
    model = {"generator": cfg.generator_model, "checker": cfg.checker_model, "research": cfg.research_model}[role]
    p = cfg.prices[model]
    return (tin * p["input"] + tout * p["output"]) / 1e6 + searches * cfg.web_search_usd


def counts(pattern: dict, split: tuple[int, int, int], missing: str, coding: bool, media: bool, writing: bool) -> dict:
    plans, _ = build_plan(pattern, split, missing)
    c = {"mcq": sum(len(p.targets) for p in plans), "coding": 0, "recording": 0, "speaking": 0, "writing": 0}
    if coding:
        c["coding"] = sum(len(x["targets"]) for x in build_coding_plan(pattern, split))
    if media:
        for m in build_media_plan(pattern, split):
            if m["kind"] == "listening":
                c["recording"] += len(split_recordings(m["targets"]))
            else:
                c["speaking"] += len(m["targets"])
    if writing:
        c["writing"] = sum(len(w["items"]) for w in build_writing_plan(pattern, split))
    return c


def estimate(cfg: Config, pattern: dict, split: tuple[int, int, int], papers: int, *, missing: str = "dev",
             coding: bool = True, media: bool = True, writing: bool = True, web: bool = True) -> dict:
    c = counts(pattern, split, missing, coding, media, writing)
    per_paper, searches = 0.0, 0
    lines = []
    for unit, n in c.items():
        if not n:
            continue
        usd = sum(_usd(cfg, r, i, o, s) for r, i, o, s in UNITS[unit]) * RETRY[unit] * n
        # one web check per item; short repeat sentences are skipped, so speaking is counted at about 2/3
        srch = round(n * (0.67 if unit == "speaking" else 1)) if web else 0
        usd += srch * cfg.web_search_usd
        per_paper += usd
        searches += srch
        lines.append({"unit": unit, "count": n, "usd": round(usd, 2)})
    researched = cache_path(cfg, pattern["company"], pattern["role"]).exists()
    research_usd = 0.0 if researched else _usd(cfg, *RESEARCH)
    total = per_paper * papers + 0.0
    return {"counts": c, "perPaperUsd": round(per_paper, 2), "papers": papers, "totalUsd": round(total, 2),
            "searches": searches * papers, "researchUsdAlreadySpent": researched, "researchUsd": round(research_usd, 2),
            "lines": lines, "capUsd": cfg.spend_cap_usd, "overCap": total > cfg.spend_cap_usd,
            "note": "Rough estimate from guessed token counts. The real cost is measured during the run."}
