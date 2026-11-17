from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import prompts
from .claude import Claude, extract_json
from .config import Config
from .gap import largest_remainder

MCQ_TYPES = {"MCQ_TEXT", "MCQ_CODE_OUTPUT"}
LEVELS = ["Easy", "Medium", "Hard"]


@dataclass
class SectionPlan:
    name: str
    type: str                      # MCQ_TEXT or MCQ_CODE_OUTPUT
    topics: list[str]
    minutes: int | None
    targets: list[str]             # one target difficulty per question, in order
    pending: bool = False          # True = needs developer work, kept out of the paper
    image: bool = False            # questions need image briefs


def build_plan(pattern: dict, split: tuple[int, int, int], missing: str = "dev") -> tuple[list[SectionPlan], list[str]]:
    """Which sections can be generated now. Returns (plans, notes for sections skipped)."""
    plans, notes = [], []
    for s in pattern["sections"]:
        t = s["type"]
        if t in MCQ_TYPES:
            pending = image = False
            ty = t
        elif t == "MCQ_IMAGE" and missing == "dev":
            ty, pending, image = "MCQ_TEXT", True, True
        elif t == "MCQ_IMAGE" and missing == "replace":
            ty, pending, image = "MCQ_TEXT", False, False
        elif t == "MCQ_IMAGE":
            notes.append(f"{s['name']}: left out by decision")
            continue
        elif t in ("CODING", "AUDIO_LISTENING", "SPEAKING", "WRITING"):
            continue   # handled by the coding, media and writing steps
        else:
            notes.append(f"{s['name']} ({t}): not supported")
            continue
        e, m, h = largest_remainder(s["questions"], *split)
        targets = ["Easy"] * e + ["Medium"] * m + ["Hard"] * h
        plans.append(SectionPlan(s["name"], ty, s.get("topics") or [], s.get("minutes"), targets, pending, image))
    return plans, notes


def build_coding_plan(pattern: dict, split: tuple[int, int, int]) -> list[dict]:
    """Coding sections of the pattern: how many problems, at which difficulty each."""
    out = []
    for s in pattern["sections"]:
        if s["type"] == "CODING":
            e, m, h = largest_remainder(s["questions"], *split)
            out.append({"name": s["name"], "minutes": s.get("minutes"), "topics": s.get("topics") or [],
                        "targets": ["EASY"] * e + ["MEDIUM"] * m + ["HARD"] * h})
    return out


def check_question(q: dict) -> list[str]:
    """Structural problems with one generated question. Empty list = fine."""
    bad = []
    opts = q.get("options")
    if not isinstance(q.get("question"), str) or len(q["question"].strip()) < 10:
        bad.append("question text missing")
    if not isinstance(opts, dict) or sorted(opts) != ["A", "B", "C", "D"]:
        bad.append("options must be A, B, C, D")
    else:
        vals = [str(v).strip().lower() for v in opts.values()]
        if len(set(vals)) != 4 or any(not v for v in vals):
            bad.append("options not distinct")
    if q.get("correctAnswer") not in ("A", "B", "C", "D"):
        bad.append("correctAnswer invalid")
    if not isinstance(q.get("explanation"), str) or len(q["explanation"].strip()) < 25:
        bad.append("explanation missing or too short")
    return bad


def _section_brief(sec: SectionPlan, targets: list[str], company: str, role: str) -> str:
    counts = {l: targets.count(l) for l in LEVELS}
    style = "Each question is a code or pseudocode snippet with a question about its output or behaviour." \
        if sec.type == "MCQ_CODE_OUTPUT" else "Text questions."
    img = prompts.IMAGE_BRIEF_ADDON if sec.image else ""
    return (f"Company style: {company}, role {role}.\nSection: {sec.name}\nStyle: {style}\n"
            f"Topics to cover (spread them): {', '.join(sec.topics) or 'the usual topics of this section'}\n"
            f"Write {len(targets)} questions. Difficulty counts: Easy {counts['Easy']}, Medium {counts['Medium']}, "
            f"Hard {counts['Hard']}.\nTarget order, one per question: {', '.join(targets)}.{img}")


def generate_section(cfg: Config, claude: Claude, sec: SectionPlan, company: str, role: str) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(sec.targets), cfg.chunk):
        chunk = sec.targets[i:i + cfg.chunk]
        text = claude.ask(label=f"generate:{sec.name[:12]}", model=cfg.generator_model,
                          system=prompts.GENERATOR_SYSTEM, user=_section_brief(sec, chunk, company, role))
        qs = extract_json(text).get("questions", [])
        # Trust the order we asked for; never trust the model's own count or label.
        for k, tgt in enumerate(chunk):
            q = qs[k] if k < len(qs) else {}
            q["targetDifficulty"] = tgt
            out.append(q)
    return out


def rewrite(cfg: Config, claude: Claude, items: list[dict], sec: SectionPlan, company: str, role: str) -> list[dict]:
    """items: [{"old": q, "target": level, "why": str}]. Returns one new question per item."""
    payload = [{"index": i, "targetDifficulty": it["target"], "failedBecause": it["why"],
                "oldQuestion": it["old"].get("question", "")} for i, it in enumerate(items)]
    user = (f"Company style: {company}, role {role}. Section: {sec.name}. "
            f"Topics: {', '.join(sec.topics) or 'as before'}.\nRewrite these:\n{json.dumps(payload, ensure_ascii=False)}"
            + (prompts.IMAGE_BRIEF_ADDON if sec.image else ""))
    text = claude.ask(label=f"rewrite:{sec.name[:12]}", model=cfg.generator_model,
                      system=prompts.REWRITE_SYSTEM, user=user)
    got = extract_json(text).get("questions", [])
    by_idx = {int(g.get("replaces", k)): g for k, g in enumerate(got)}
    out = []
    for i, it in enumerate(items):
        q = by_idx.get(i, {})
        q["targetDifficulty"] = it["target"]
        out.append(q)
    return out
