"""Writing sections: email, essay, letter, memo, report tasks with a model answer and a rubric.

The platform scores the candidate's text with the Writing Lab engine, so there is no answer key (correctAnswer is null).
Scripts check that each model answer really fits its word limit; Claude checks difficulty and originality like elsewhere.
"""
from __future__ import annotations

import json

from . import prompts
from .claude import Claude, extract_json
from .config import Config
from .gap import largest_remainder
from .repo_index import RepoIndex
from .research import slug
from .speech import _rate, words
from .verify import web_check_text

LEVELS = ["Easy", "Medium", "Hard"]
TASKS = {"EMAIL": "Formal Email", "ESSAY": "Essay", "LETTER": "Formal Letter", "MEMO": "Formal Memo", "REPORT": "Short Report"}
DEFAULT_LIMITS = {"Easy": (60, 90), "Medium": (90, 120), "Hard": (150, 190)}
DEFAULT_MARKS = {"Easy": 5, "Medium": 10, "Hard": 15}
DEFAULT_MINUTES = {"Easy": 6, "Medium": 8, "Hard": 12}
BATCH = 4


def section_tasks(s: dict, n: int) -> list[str]:
    mode = (s.get("mode") or "").upper()
    if mode in TASKS:
        return [mode] * n
    name = s["name"].lower()
    for t in TASKS:
        if t.lower() in name:
            return [t] * n
    cycle = ["EMAIL", "ESSAY"]
    return [cycle[i % len(cycle)] for i in range(n)]


def build_writing_plan(pattern: dict, split: tuple[int, int, int]) -> list[dict]:
    plans = []
    for s in pattern["sections"]:
        if s["type"] != "WRITING":
            continue
        e, m, h = largest_remainder(s["questions"], *split)
        targets = ["Easy"] * e + ["Medium"] * m + ["Hard"] * h
        tasks = section_tasks(s, len(targets))
        wl = s.get("word_limit") or None
        items = []
        for t, task in zip(targets, tasks):
            lim = (int(wl["min"]), int(wl["max"])) if wl and wl.get("min") and wl.get("max") else DEFAULT_LIMITS[t]
            items.append({"target": t, "task": task, "limit": lim, "marks": int(s.get("marks_each") or DEFAULT_MARKS[t])})
        plans.append({"name": s["name"], "minutes": s.get("minutes"), "topics": s.get("topics") or [], "items": items})
    return plans


def _problems(it: dict, spec: dict) -> list[str]:
    bad = []
    q = (it.get("question") or "").strip()
    if len(q) < 60:
        bad.append("question is too short to be a full task")
    crit = it.get("evaluationCriteria") or []
    if len(crit) != 3 or not all(isinstance(c, str) and len(c.strip()) > 15 for c in crit):
        bad.append("evaluationCriteria must be exactly 3 concrete lines")
    ma = (it.get("modelAnswer") or "").strip()
    lo, hi = spec["limit"]
    if not ma:
        bad.append("modelAnswer missing")
    elif not lo <= words(ma) <= hi:
        bad.append(f"modelAnswer has {words(ma)} words but the limit is {lo} to {hi}. Rewrite it to fit")
    return bad


def make_writing_items(cfg: Config, claude: Claude, index: RepoIndex, plan: dict, web: bool, log=print) -> dict:
    """Returns {"items", "flags", "evidence", "attempts"}. Same repair loop as the other media sections."""
    specs = plan["items"]
    items: list[dict | None] = [None] * len(specs)
    evidence: list[dict] = [{} for _ in specs]
    why: dict[int, list[str]] = {i: ["not written yet"] for i in range(len(specs))}
    flags: dict[int, str] = {}
    attempts = 0
    for attempt in range(1, cfg.max_attempts + 1):
        todo = [i for i in range(len(specs)) if i not in flags and why.get(i)]
        if not todo:
            break
        attempts = attempt
        for start in range(0, len(todo), BATCH):
            chunk = todo[start:start + BATCH]
            lines = [f"{n + 1}. format {specs[i]['task']}, difficulty {specs[i]['target']}, "
                     f"word limit {specs[i]['limit'][0]} to {specs[i]['limit'][1]}, {specs[i]['marks']} marks"
                     for n, i in enumerate(chunk)]
            brief = (f"Section: {plan['name']}. Topic ideas: {', '.join(plan['topics']) or 'campus and workplace situations'}.\n"
                     "Write these items in order:\n" + "\n".join(lines))
            system = prompts.WRITING_SYSTEM + (prompts.MEDIA_REPAIR_ADDON if attempt > 1 else "")
            if attempt > 1:
                brief += "\n\nTHE EARLIER ITEMS AND WHAT WAS WRONG:\n" + json.dumps(
                    [{"question": (items[i] or {}).get("question"), "problems": why[i]} for i in chunk], ensure_ascii=False)
            try:
                got = extract_json(claude.ask(label="writing-design", model=cfg.generator_model, system=system,
                                              user=brief, max_tokens=12000)).get("items", [])
            except (ValueError, KeyError):
                got = []
            for k, i in enumerate(chunk):
                items[i] = got[k] if k < len(got) and isinstance(got[k], dict) else {}
                why[i] = _problems(items[i], specs[i])
                if not why[i]:
                    sim = index.check({"question": items[i]["question"]})
                    evidence[i]["repo"] = sim
                    if sim["fingerprintCollision"] or sim["maxRepoSimilarity"] >= cfg.repo_fail:
                        why[i].append(f"{int(sim['maxRepoSimilarity'] * 100)}% similar to {sim['closest']}")
                if not why[i] and web:
                    w = web_check_text(cfg, claude, prompts.WEB_CHECK_SYSTEM, items[i]["question"])
                    evidence[i]["web"] = w
                    if w["verdict"] == "match" or w["similarity"] >= 70:
                        why[i].append(f"already exists online: {w['url']}")
        ok = [i for i in todo if not why[i]]
        if ok:
            user = "\n\n".join(f"[{n}] ({specs[i]['task']}) {items[i]['question']}" for n, i in enumerate(ok))
            rated = {int(r["index"]): r for r in _rate(cfg, claude, prompts.WRITING_RATER_SYSTEM, user)}
            for n, i in enumerate(ok):
                rt = rated.get(n) or {}
                evidence[i]["rating"] = rt
                if rt.get("label") != specs[i]["target"]:
                    why[i].append(f"rated {rt.get('label')} but {specs[i]['target']} was wanted: {rt.get('reason')}")
        bad = [i for i in todo if why[i]]
        log(f"      writing pass {attempt}: {len(todo) - len(bad)} ok, {len(bad)} to redo")
        if attempt == cfg.max_attempts:
            for i in bad:
                only_diff = len(why[i]) == 1 and why[i][0].startswith("rated ")
                flags[i] = "kept; rated differently than wanted" if only_diff else "needs a human: " + "; ".join(why[i])
    return {"items": items, "flags": [flags.get(i) for i in range(len(specs))], "evidence": evidence, "attempts": attempts}


def build_writing_sections(qprefix: str, results: list[dict]) -> list[dict]:
    """results: [{"plan", "writing": make_writing_items(...)}]. One WRITING section per pattern section."""
    sections = []
    n = 0
    for r in results:
        plan, wr = r["plan"], r["writing"]
        qs = []
        for i, it in enumerate(wr["items"]):
            if not it or (wr["flags"][i] or "").startswith("needs"):
                continue
            spec = plan["items"][i]
            lab = ((wr["evidence"][i] or {}).get("rating") or {}).get("label")
            n += 1
            qs.append({"questionKey": f"{qprefix}_WR_Q{n:02d}", "question": it["question"], "type": spec["task"],
                       "wordLimit": {"min": spec["limit"][0], "max": spec["limit"][1]}, "marks": spec["marks"],
                       "complexity": lab if lab in LEVELS else spec["target"], "category": TASKS[spec["task"]],
                       "correctAnswer": None, "evaluationCriteria": it["evaluationCriteria"], "modelAnswer": it["modelAnswer"]})
        if not qs:
            continue
        minutes = plan.get("minutes")
        secs = round(minutes * 60) if minutes else sum(DEFAULT_MINUTES[q["complexity"]] for q in qs) * 60
        sections.append({
            "sectionKey": slug(plan["name"]), "name": plan["name"], "type": "WRITING", "timeLimitSeconds": secs,
            "instructions": (f"Complete {len(qs)} writing task(s). Use correct grammar, spelling and punctuation and stay within "
                             "the word limits shown. Backspace and editing are allowed."),
            "tracks": None, "shuffleQuestions": False, "shuffleOptions": False,
            "config": {"gradedBy": "MANUAL", "responseFormat": "FREE_TEXT"}, "questions": qs})
    return sections
