from __future__ import annotations

import json
import random
import re
from pathlib import Path

from .layout import company_dir
from .research import slug

_LOCK = re.compile(r"all of the above|none of the above|both [a-d] and [a-d]|\b[a-d] and [a-d]\b", re.I)


def needs_lock(opts: dict) -> bool:
    return any(_LOCK.search(str(v)) for v in opts.values())


def shuffle_options(q: dict, rng: random.Random) -> dict:
    """Spread the correct letter around. Questions whose options refer to each other keep their order."""
    opts = q["options"]
    if needs_lock(opts):
        return {"options": dict(opts), "correctAnswer": q["correctAnswer"], "lockOptions": True}
    correct_text = opts[q["correctAnswer"]]
    texts = [opts[k] for k in "ABCD"]
    rng.shuffle(texts)
    new = dict(zip("ABCD", texts))
    ans = next(k for k, v in new.items() if v == correct_text)
    return {"options": new, "correctAnswer": ans, "lockOptions": False}


def pick_round(repo: Path, company_slug: str, wanted: str | None = None, tree: str = "university") -> tuple[str, str]:
    """(roundKey, testType) from the company's company.json. New companies get the standard round."""
    cdir = company_dir(repo, company_slug, tree)
    if cdir is not None:
        rounds = json.loads((cdir / "company.json").read_text(encoding="utf-8")).get("rounds", [])
        if wanted:
            for r in rounds:
                if r.get("roundKey") == wanted:
                    return r["roundKey"], r.get("testType", "ONLINE_ASSESSMENT")
            raise ValueError(f"Round {wanted!r} not found. Available: {[r.get('roundKey') for r in rounds]}")
        oa = [r for r in rounds if r.get("testType") == "ONLINE_ASSESSMENT"]
        if oa:
            return oa[0]["roundKey"], "ONLINE_ASSESSMENT"
    return "online-assessment", "ONLINE_ASSESSMENT"


def next_set_number(repo: Path, company_slug: str, key_prefix: str, out_dir: Path, tree: str = "university") -> int:
    """Next free NNN for papers named <key_prefix>-NNN, looking in the repo and in earlier drafts."""
    nums = [0]
    for d in (company_dir(repo, company_slug, tree), out_dir):
        if d is not None and d.is_dir():
            for f in d.glob(f"{key_prefix}-*.json"):
                m = re.fullmatch(rf"{re.escape(key_prefix)}-(\d{{3}})\.json", f.name)
                if m:
                    nums.append(int(m.group(1)))
    return max(nums) + 1


def difficulty_label(counts: dict) -> str:
    total = sum(counts.values()) or 1
    if counts.get("Hard", 0) / total >= 0.35:
        return "HARD"
    if counts.get("Easy", 0) / total >= 0.5:
        return "EASY"
    return "MODERATE"


def key_prefix(company: str, set_no: int) -> tuple[str, str, str]:
    """(short, num, question-key prefix) for a paper, e.g. ("virtusa", "007", "VIRT007")."""
    short = slug(company).split("-")[0]
    num = f"{set_no:03d}"
    return short, num, re.sub(r"[^A-Za-z0-9]", "", short).upper()[:4] + num


def build_paper(company: str, role: str, set_no: int, sections: list[dict], pattern: dict, seed: int,
                round_key: str = "online-assessment", test_type: str = "ONLINE_ASSESSMENT",
                coding_sections: list[dict] | None = None, media_sections: list[dict] | None = None) -> dict:
    """sections: [{"name","minutes","topics","questions":[final question dicts with complexity set]}]."""
    cs = slug(company)
    short = cs.split("-")[0]
    num = f"{set_no:03d}"
    paper_key = f"{short}-{round_key}-{num}"
    qprefix = re.sub(r"[^A-Za-z0-9]", "", short).upper()[:4] + num
    rng = random.Random(f"{paper_key}:{seed}")

    out_sections, total_marks, seconds = [], 0, 0
    counts = {"Easy": 0, "Medium": 0, "Hard": 0}
    for order, sec in enumerate(sections, 1):
        abbr = re.sub(r"[^A-Za-z]", "", sec["name"]).upper()[:3] or "SEC"
        qs = []
        for n, q in enumerate(sec["questions"], 1):
            sh = shuffle_options(q, rng)
            qs.append({
                "questionKey": f"{qprefix}_{abbr}_Q{n:02d}", "question": q["question"],
                "options": sh["options"], "correctAnswer": sh["correctAnswer"], "marks": 1, "negativeMarks": 0,
                "complexity": q["complexity"], "category": q.get("category") or sec["name"],
                "tags": q.get("tags") or [], "explanation": q["explanation"], "hint": None,
                "hintPenaltyPoints": None, "lockOptions": sh["lockOptions"],
            })
            counts[q["complexity"]] += 1
        total_marks += len(qs)
        seconds += (sec.get("minutes") or len(qs)) * 60
        neg = pattern.get("negative_marking") or "No negative marking."
        out_sections.append({
            "sectionKey": slug(sec["name"]), "name": sec["name"], "type": "MCQ", "displayOrder": order,
            "instructions": f"{len(qs)} questions, 1 mark each. {neg}", "tracks": None,
            "shuffleQuestions": True, "shuffleOptions": True, "config": {}, "questions": qs,
        })
    for msec in media_sections or []:
        total_marks += sum(q["marks"] for q in msec["questions"])
        seconds += msec.get("timeLimitSeconds") or 0
        out_sections.append(dict(msec, displayOrder=len(out_sections) + 1))
    for csec in coding_sections or []:
        probs = csec["problems"]
        if not probs:
            continue
        total_marks += sum(p["marks"] for p in probs)
        seconds += (csec.get("minutes") or 25 * len(probs)) * 60
        out_sections.append({
            "sectionKey": slug(csec["name"]), "name": csec["name"], "type": "CODING",
            "displayOrder": len(out_sections) + 1,
            "instructions": f"{len(probs)} problem(s). Solve them in the built-in IDE. Hidden test cases apply.",
            "tracks": None, "shuffleQuestions": False, "shuffleOptions": False,
            "config": {"problemSource": "fluentedge-coding-problems"}, "questions": [p["key"] for p in probs]})
    return {
        "schemaVersion": 1, "kind": "PLACEMENT_PAPER", "companySlug": cs, "paperKey": paper_key,
        "roundKey": round_key, "testType": test_type,
        "title": f"{company} — {role} — Set {set_no}",
        "description": f"Practice paper in the style of the {company} {role} online test. "
                       "All questions are newly written for this platform.",
        "timeLimitSeconds": seconds, "totalMarks": total_marks, "passingScore": round(total_marks * 0.4),
        "difficulty": difficulty_label(counts), "tags": [short, slug(role), "full-length"],
        "config": dict({"allowSectionRevisit": False, "allowQuestionRevisit": False},
                       **({"requiresMicrophone": True, "requiresAudioPlayback": True}
                          if any(m["type"] == "VOICE_DRILL" or m.get("config", {}).get("hasAudioPrompt")
                                 for m in media_sections or []) else {})),
        "sections": out_sections,
    }
