"""Review a drafted paper: list it, change the difficulty of chosen questions, regenerate them, export."""
from __future__ import annotations

import json
import random
import re
import secrets
import time
from pathlib import Path

from .assemble import difficulty_label, shuffle_options
from .claude import Claude
from .config import Config
from .cost import CostTracker
from .generate import SectionPlan, rewrite
from .pipeline import new_records, refine
from .repo_index import RepoIndex
from .validate import validate_draft

LEVELS = ("Easy", "Medium", "Hard")
KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _paths(cfg: Config, key: str) -> tuple[Path, Path]:
    if not KEY_RE.fullmatch(key) or key.endswith((".report", ".pending")):
        raise ValueError("bad paper key")
    return cfg.out_dir / f"{key}.json", cfg.out_dir / f"{key}.report.json"


def list_drafts(cfg: Config) -> list[str]:
    if not cfg.out_dir.is_dir():
        return []
    names = [f.name[:-5] for f in cfg.out_dir.glob("*.json")
             if not f.name.endswith((".report.json", ".pending.json"))]
    return sorted(names)


def load(cfg: Config, key: str) -> tuple[dict, dict]:
    pp, rp = _paths(cfg, key)
    if not pp.is_file():
        raise FileNotFoundError(f"No draft named {key}")
    paper = json.loads(pp.read_text(encoding="utf-8"))
    report = json.loads(rp.read_text(encoding="utf-8")) if rp.is_file() else {"questions": []}
    return paper, report


def _save(cfg: Config, key: str, paper: dict, report: dict) -> None:
    pp, rp = _paths(cfg, key)
    pp.write_text(json.dumps(paper, indent=2, ensure_ascii=False), encoding="utf-8")
    rp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def _evidence(report: dict) -> dict[str, dict]:
    return {r["question"]: r for r in report.get("questions", []) if r.get("question")}


def _status(ev: dict | None) -> str:
    if not ev:
        return "Original"
    if ev.get("edited"):
        return "Edited"
    if ev.get("flag", "") and ev["flag"].startswith("kept"):
        return "Kept at rated level"
    return "Rewritten" if (ev.get("attempts") or 1) > 1 else "Original"


def coding_info(cfg: Config, paper: dict, report: dict | None = None) -> dict[str, dict]:
    """{slug: {title, difficulty, marks, statement, flag, evidence}} for every coding problem in the paper."""
    flags = {c.get("key"): c for c in (report or {}).get("coding", [])}
    out = {}
    for sec in paper["sections"]:
        if sec["type"] != "CODING":
            continue
        for slug in sec["questions"]:
            root = cfg.out_dir / "problems" / slug
            info = {"title": slug, "difficulty": "", "marks": 0, "statement": "(draft folder not found in out/problems)"}
            if (root / "problem.json").is_file():
                pj = json.loads((root / "problem.json").read_text(encoding="utf-8"))
                info.update(title=pj["title"], difficulty=pj["difficulty"], marks=pj["marks"],
                            statement=(root / "statement.md").read_text(encoding="utf-8"))
            c = flags.get(slug) or {}
            info.update(flag=c.get("flag"), evidence=c.get("evidence"))
            out[slug] = info
    return out


def view(cfg: Config, key: str) -> dict:
    paper, report = load(cfg, key)
    ev = _evidence(report)
    sections, n = [], 0
    info = coding_info(cfg, paper, report)
    for sec in paper["sections"]:
        if sec["type"] == "CODING":
            sections.append({"name": sec["name"], "kind": "CODING", "instructions": sec["instructions"],
                             "problems": [dict(key=k, **info[k]) for k in sec["questions"]]})
            continue
        if sec["type"] == "WRITING":
            sections.append({"name": sec["name"], "kind": "WRITING", "instructions": sec["instructions"],
                             "items": [{"type": q["type"], "prompt": q["question"], "wordLimit": q["wordLimit"],
                                        "marks": q["marks"], "complexity": q["complexity"],
                                        "criteria": q["evaluationCriteria"], "modelAnswer": q.get("modelAnswer")}
                                       for q in sec["questions"]]})
            continue
        if sec["type"] == "VOICE_DRILL":
            sections.append({"name": sec["name"], "kind": "SPEAKING", "instructions": sec["instructions"],
                             "mode": sec["config"].get("mode"),
                             "items": [{"title": q["title"], "prompt": q["question"], "marks": q["marks"],
                                        "complexity": q["complexity"], "modelAnswer": q.get("modelAnswer"),
                                        "audioAsset": q.get("audioAsset"), "hasAudio": "audioAsset" in q} for q in sec["questions"]]})
            continue
        qs = []
        for q in sec["questions"]:
            n += 1
            e = ev.get(q["question"])
            qs.append({"number": n, "questionKey": q["questionKey"], "question": q["question"], "options": q["options"],
                       "correctAnswer": q["correctAnswer"], "complexity": q["complexity"],
                       "explanation": q["explanation"], "status": _status(e),
                       "evidence": None if not e else {
                           "repo": e.get("repo"), "web": e.get("web"), "rating": e.get("rating"),
                           "attempts": e.get("attempts"), "flag": e.get("flag")}})
        audio = sec.get("config", {}).get("audioAsset")
        sections.append({"name": sec["name"], "instructions": sec["instructions"], "questions": qs,
                         **({"editable": False, "audio": {"url": audio, "transcript": sec["config"].get("audioTranscript")}}
                            if sec.get("config", {}).get("hasAudioPrompt") else {})})
    pending = (cfg.out_dir / f"{key}.pending.json").is_file()
    audio = report.get("audio") or []
    return {"key": key, "title": paper["title"], "audioFiles": [{"file": j["file"], "url": j["url"], "kind": j["kind"]} for j in audio], "totalMarks": paper["totalMarks"],
            "minutes": round(paper["timeLimitSeconds"] / 60), "difficulty": paper["difficulty"], "sections": sections,
            "requested": report.get("requestedSplit"), "delivered": report.get("deliveredSplit"),
            "needsHuman": report.get("needsHuman", 0), "pendingImageSection": pending,
            "validator": report.get("repoValidator"), "costUsd": report.get("costUsd"),
            "editsCostUsd": report.get("editsCostUsd", 0)}


def _company_role(paper: dict, report: dict) -> tuple[str, str]:
    if report.get("company") and report.get("role"):
        return report["company"], report["role"]
    parts = paper["title"].split(" — ")
    return parts[0], parts[1] if len(parts) > 1 else ""


def _index_for(cfg: Config, key: str, paper: dict, skip: set[str]) -> RepoIndex:
    index = RepoIndex(cfg.repo)
    for other in list_drafts(cfg):  # earlier drafts count as 'already existing' too
        p, _ = load(cfg, other)
        for s in p["sections"]:
            if s["type"] == "CODING":
                continue
            for q in s["questions"]:
                if other != key or q["questionKey"] not in skip:
                    index.add(q["questionKey"], f"draft {other}", q)
    return index


def apply_changes(cfg: Config, claude: Claude, tracker: CostTracker, key: str, changes: dict[str, str],
                  log=print) -> dict:
    """changes: {questionKey: "Easy"|"Medium"|"Hard"}. Rewrites each at the new level and re-checks it."""
    for lvl in changes.values():
        if lvl not in LEVELS:
            raise ValueError(f"bad level {lvl!r}")
    paper, report = load(cfg, key)
    company, role = _company_role(paper, report)
    start_cost, start_calls = tracker.total, len(tracker.calls)
    index = _index_for(cfg, key, paper, set(changes))
    ev = _evidence(report)
    results = []

    for sec in paper["sections"]:
        if sec["type"] in ("CODING", "VOICE_DRILL", "WRITING") or sec.get("config", {}).get("hasAudioPrompt"):
            continue   # these cannot be rewritten from here (a rewrite would lose the audio or the problem)
        todo = [(q, changes[q["questionKey"]]) for q in sec["questions"] if q["questionKey"] in changes]
        todo = [(q, lvl) for q, lvl in todo if lvl != q["complexity"]]
        if not todo:
            continue
        plan = SectionPlan(sec["name"], "MCQ_TEXT", [], None, [], False, False)
        items = [{"old": q, "target": lvl, "why": f"the reviewer wants this at {lvl} level (it is {q['complexity']})"}
                 for q, lvl in todo]
        recs = refine(cfg, claude, index, plan, new_records(rewrite(cfg, claude, items, plan, company, role)),
                      company, role, cfg.web_check, log)
        for (old, lvl), r in zip(todo, recs):
            if r["flag"] and r["flag"].startswith("needs"):
                results.append({"questionKey": old["questionKey"], "ok": False, "from": old["complexity"], "to": lvl,
                                "message": f"Could not produce a good {lvl} version. The old question is kept. "
                                           f"{r['flag']}"})
                continue
            new = r["q"]
            got = (r["rating"] or {}).get("label") or lvl
            sh = shuffle_options(new, random.Random(secrets.token_hex(8)))
            stem_before = old["question"]
            old.update({"question": new["question"], "options": sh["options"], "correctAnswer": sh["correctAnswer"],
                        "lockOptions": sh["lockOptions"], "complexity": got, "explanation": new["explanation"],
                        "category": new.get("category") or old.get("category"), "tags": new.get("tags") or old.get("tags")})
            entry = {"section": sec["name"], "pending": False, "question": new["question"], "requested": lvl,
                     "rated": got, "rating": r["rating"], "repo": r["repo"], "web": r["web"],
                     "attempts": r["attempts"], "flag": r["flag"], "edited": True}
            report["questions"] = [x for x in report["questions"] if x.get("question") != stem_before] + [entry]
            msg = f"Rewritten at {got} level." if got == lvl else f"Asked for {lvl}, rated {got} after {r['attempts']} tries, kept."
            results.append({"questionKey": old["questionKey"], "ok": True, "from": ev.get(stem_before, {}).get("rated", ""),
                            "to": got, "message": msg})

    live = [x for x in report["questions"] if not x.get("pending")]
    report["requestedSplit"] = {l: sum(1 for x in live if x["requested"] == l) for l in LEVELS}
    report["deliveredSplit"] = {l: sum(1 for x in live if x["rated"] == l) for l in LEVELS}
    report["needsHuman"] = sum(1 for x in live if (x.get("flag") or "").startswith("needs"))
    spent = tracker.total - start_cost
    report["editsCostUsd"] = round(report.get("editsCostUsd", 0) + spent, 4)
    report.setdefault("editLog", []).append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "changes": changes,
                                              "costUsd": round(spent, 4)})
    counts = {l: sum(1 for s in paper["sections"] if s["type"] != "CODING" for q in s["questions"] if q["complexity"] == l) for l in LEVELS}
    paper["difficulty"] = difficulty_label(counts)
    report["repoValidator"] = validate_draft(cfg, paper, report.get("tree", "university"))
    _save(cfg, key, paper, report)
    return {"results": results, "view": view(cfg, key), "spentUsd": round(spent, 4),
            "callsMade": len(tracker.calls) - start_calls}
