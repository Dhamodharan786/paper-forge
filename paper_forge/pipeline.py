from __future__ import annotations

import json
import time
from pathlib import Path

from .assemble import build_paper, key_prefix, next_set_number, pick_round
from .claude import Claude
from .config import Config
from .cost import CostTracker
from .coding import MARKS, ProblemIndex, load_tags, make_problem, validate_problem_draft, write_problem_folder
from . import speech, writing
from .generate import SectionPlan, build_coding_plan, build_plan, check_question, generate_section, rewrite
from .repo_index import RepoIndex
from .research import slug
from .validate import validate_draft
from .verify import rate, web_check


def process_section(cfg: Config, claude: Claude, index: RepoIndex, sec: SectionPlan, company: str, role: str,
                    web: bool, log=print) -> list[dict]:
    """Generate, check and (if needed) rewrite until every question passes or attempts run out.
    Returns records: {"q", "attempts", "problems", "repo", "web", "rating", "flag"}."""
    recs = new_records(generate_section(cfg, claude, sec, company, role))
    log(f"  {sec.name}: wrote {len(recs)} questions")
    return refine(cfg, claude, index, sec, recs, company, role, web, log)


def new_records(qs: list[dict]) -> list[dict]:
    return [{"q": q, "attempts": 1, "problems": [], "repo": None, "web": None, "rating": None, "flag": None}
            for q in qs]


def refine(cfg: Config, claude: Claude, index: RepoIndex, sec: SectionPlan, recs: list[dict], company: str,
           role: str, web: bool, log=print) -> list[dict]:
    """Check every record and rewrite the failing ones, up to cfg.max_attempts."""
    for attempt in range(1, cfg.max_attempts + 1):
        todo = [r for r in recs if r["flag"] is None and not r.get("done")]
        if not todo:
            break
        # 1. free checks
        for r in todo:
            r["problems"], r["web"], r["rating"] = [], None, None
            r["problems"] += check_question(r["q"])
            if not r["problems"]:
                r["repo"] = index.check(r["q"])
                if r["repo"]["fingerprintCollision"]:
                    r["problems"].append(f"same structure as {r['repo']['fingerprintWith']}")
                elif r["repo"]["maxRepoSimilarity"] >= cfg.repo_fail:
                    r["problems"].append(f"{int(r['repo']['maxRepoSimilarity'] * 100)}% similar to {r['repo']['closest']}")
        # 2. one web search each, only for questions that passed the free checks
        if web:
            for r in todo:
                if not r["problems"]:
                    r["web"] = web_check(cfg, claude, r["q"])
                    if r["web"]["verdict"] == "match" or r["web"]["similarity"] >= 70:
                        r["problems"].append(f"found online: {r['web']['url']}")
        # 3. blind difficulty rating for the survivors
        ok = [r for r in todo if not r["problems"]]
        if ok:
            for r, rt in zip(ok, rate(cfg, claude, [r["q"] for r in ok])):
                r["rating"] = rt
                if rt.get("label") != r["q"]["targetDifficulty"]:
                    r["problems"].append(f"rated {rt.get('label')} but wanted {r['q']['targetDifficulty']}: {rt.get('reason')}")
        failing = [r for r in todo if r["problems"]]
        for r in todo:
            if not r["problems"]:
                r["done"] = True
        log(f"    pass {attempt}: {len(todo) - len(failing)} ok, {len(failing)} to rewrite")
        if not failing:
            break
        if attempt == cfg.max_attempts:
            for r in failing:
                only_difficulty = len(r["problems"]) == 1 and r["problems"][0].startswith("rated ")
                if only_difficulty:
                    r["done"], r["flag"] = True, "kept at its rated level; split drifts"
                else:
                    r["flag"] = "needs a human: " + "; ".join(r["problems"])
            break
        news = rewrite(cfg, claude, [{"old": r["q"], "target": r["q"]["targetDifficulty"],
                                      "why": "; ".join(r["problems"])} for r in failing], sec, company, role)
        for r, nq in zip(failing, news):
            r["q"], r["attempts"] = nq, r["attempts"] + 1
    return recs


def generate_papers(cfg: Config, claude: Claude, tracker: CostTracker, pattern: dict, split: tuple[int, int, int],
                    count: int, missing: str, web: bool, log=print, round_key: str | None = None,
                    tree: str = "university", coding: bool = True, media: bool = True,
                    writing_on: bool = True) -> list[dict]:
    company, role = pattern["company"], pattern["role"]
    plans, notes = build_plan(pattern, split, missing)
    for n in notes:
        log(f"  skipped: {n}")
    cplans = build_coding_plan(pattern, split) if coding else []
    if not coding and any(x["type"] == "CODING" for x in pattern["sections"]):
        notes.append("Coding sections: left out (--no-coding)")
    mplans = speech.build_media_plan(pattern, split) if media else []
    wplans = writing.build_writing_plan(pattern, split) if writing_on else []
    if not writing_on and any(x["type"] == "WRITING" for x in pattern["sections"]):
        notes.append("Writing sections: left out (--no-writing)")
    if not media and any(x["type"] in ("AUDIO_LISTENING", "SPEAKING") for x in pattern["sections"]):
        notes.append("Audio and speaking sections: left out (--no-audio)")
    pindex, vocab = (ProblemIndex(cfg.repo), load_tags(cfg.repo)) if cplans else (None, set())
    index = RepoIndex(cfg.repo)
    log(f"Repo index: {len(index.entries)} existing questions to compare against")
    cfg.out_dir.mkdir(exist_ok=True)
    round_key, test_type = pick_round(cfg.repo, slug(company), round_key, tree)
    prefix = f"{slug(company).split('-')[0]}-{round_key}"
    first = next_set_number(cfg.repo, slug(company), prefix, cfg.out_dir, tree)
    log(f"Round: {round_key}. New papers will be {prefix}-{first:03d} onward")
    results = []
    for k in range(count):
        set_no = first + k
        log(f"Paper {set_no}")
        sections, pending, report_q = [], [], []
        for sec in plans:
            recs = process_section(cfg, claude, index, sec, company, role, web, log)
            final = []
            for r in recs:
                q = dict(r["q"])
                q["complexity"] = (r["rating"] or {}).get("label") or q["targetDifficulty"]
                if r["flag"] is None or r["flag"].startswith("kept"):
                    final.append(q)
                    index.add(f"new-{set_no}", "this run", q)
                report_q.append({"section": sec.name, "pending": sec.pending, "question": q.get("question"),
                                 "requested": q["targetDifficulty"], "rated": q["complexity"],
                                 "rating": r["rating"], "repo": r["repo"], "web": r["web"],
                                 "attempts": r["attempts"], "flag": r["flag"]})
            if sec.pending:
                pending.append({"section": sec.name, "note": "Needs image support (developer task)", "questions": final})
            else:
                sections.append({"name": sec.name, "minutes": sec.minutes, "topics": sec.topics, "questions": final})
        media_results, media_report = [], []
        for mp in mplans:
            if mp["kind"] == "listening":
                recs, avoid = [], []
                for tg in speech.split_recordings(mp["targets"]):
                    log(f"  recording ({len(tg)} question(s)) for {mp['name']}")
                    r = speech.make_recording(cfg, claude, index, tg, mp["topics"], avoid, web, log)
                    recs.append({"data": r["data"], "flag": r["flag"]})
                    if r["data"]:
                        avoid.append(r["data"].get("title") or "")
                        for q in r["data"]["questions"]:
                            index.add("new-listening", "this run", {"question": r["data"]["transcript"]})
                            break
                    media_report.append({"section": mp["name"], "kind": "listening", "title": (r["data"] or {}).get("title"),
                                         "attempts": r["attempts"], "flag": r["flag"], "problems": r["problems"],
                                         "evidence": r["evidence"]})
                media_results.append({"plan": mp, "recordings": recs})
            else:
                log(f"  speaking ({mp['mode']}, {len(mp['targets'])}) for {mp['name']}")
                sp = speech.make_speaking_items(cfg, claude, index, mp["mode"], mp["targets"], mp["topics"], [], web, log)
                for i, it in enumerate(sp["items"]):
                    if it and not (sp["flags"][i] or "").startswith("needs"):
                        index.add("new-speaking", "this run", {"question": it["text"]})
                    media_report.append({"section": mp["name"], "kind": mp["mode"], "title": (it or {}).get("title"),
                                         "attempts": sp["attempts"], "flag": sp["flags"][i], "evidence": sp["evidence"][i]})
                media_results.append({"plan": mp, "speaking": sp})
        writing_results = []
        for wp in wplans:
            log(f"  writing ({len(wp['items'])} task(s)) for {wp['name']}")
            wr = writing.make_writing_items(cfg, claude, index, wp, web, log)
            for i, it in enumerate(wr["items"]):
                if it and not (wr["flags"][i] or "").startswith("needs"):
                    index.add("new-writing", "this run", {"question": it["question"]})
                media_report.append({"section": wp["name"], "kind": "writing", "title": ((it or {}).get("question") or "")[:70],
                                     "attempts": wr["attempts"], "flag": wr["flags"][i], "evidence": wr["evidence"][i]})
            writing_results.append({"plan": wp, "writing": wr})
        media_sections, audio_jobs = [], []
        short, num, qprefix = key_prefix(company, set_no)
        if media_results:
            import random
            media_sections, audio_jobs = speech.build_media_sections(cfg, qprefix, short, num, media_results,
                                                                     random.Random(f"{short}-{num}:media"))
        media_sections = media_sections + writing.build_writing_sections(qprefix, writing_results)
        coding_secs, coding_report = [], []
        for cp in cplans:
            probs, avoid = [], []
            for diff in cp["targets"]:
                log(f"  coding problem ({diff.lower()}) for {cp['name']}")
                res = make_problem(cfg, claude, pindex, diff, cp["topics"], avoid, web, vocab, log)
                entry = {"section": cp["name"], "requested": diff, "key": res["key"], "title": res["title"],
                         "attempts": res["attempts"], "flag": res["flag"], "problems": res["problems"],
                         "evidence": res["evidence"]}
                if res["ok"]:
                    write_problem_folder(cfg, res["_design"], diff, res["_tests"], cfg.out_dir / "problems")
                    entry["repoScripts"] = validate_problem_draft(cfg, res["key"], cfg.out_dir / "problems")
                    if entry["repoScripts"]["ran"] and not entry["repoScripts"]["ok"]:
                        entry["flag"] = "needs a human: repo scripts failed on this problem"
                    else:
                        pindex.add(res["key"], res["_design"]["files"]["statement.md"], res["title"])
                        avoid.append(res["title"])
                        probs.append({"key": res["key"], "marks": MARKS[diff]})
                coding_report.append(entry)
            coding_secs.append({"name": cp["name"], "minutes": cp["minutes"], "problems": probs})
        paper = build_paper(company, role, set_no, sections, pattern, seed=set_no, round_key=round_key,
                            test_type=test_type, coding_sections=coding_secs, media_sections=media_sections)
        key = paper["paperKey"]
        requested = {l: sum(1 for r in report_q if not r["pending"] and r["requested"] == l) for l in ("Easy", "Medium", "Hard")}
        delivered = {l: sum(1 for r in report_q if not r["pending"] and r["rated"] == l) for l in ("Easy", "Medium", "Hard")}
        flagged = [r for r in report_q if r["flag"] and r["flag"].startswith("needs")] +             [c for c in coding_report if (c["flag"] or "").startswith("needs")] + \
            [m for m in media_report if (m["flag"] or "").startswith("needs")]
        report = {"company": company, "role": role, "tree": tree, "paperKey": key, "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "models": {
                      "generator": cfg.generator_model, "checker": cfg.checker_model},
                  "requestedSplit": requested, "deliveredSplit": delivered, "needsHuman": len(flagged),
                  "skipped": notes, "questions": report_q, "coding": coding_report, "media": media_report, "audio": audio_jobs, "cost": [c.__dict__ for c in tracker.calls],
                  "costUsd": round(tracker.total, 4), "tokens": tracker.tokens}
        report["repoValidator"] = validate_draft(cfg, paper, tree)
        log(f"  repo validator: {'PASS' if report['repoValidator']['ok'] else 'FAIL'}")
        (cfg.out_dir / f"{key}.json").write_text(json.dumps(paper, indent=2, ensure_ascii=False), encoding="utf-8")
        (cfg.out_dir / f"{key}.report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        if audio_jobs:
            speech.write_audio_scripts(audio_jobs, cfg.out_dir / "audio" / key, key)
            log(f"  {len(audio_jobs)} audio file(s) for the dev team to produce: out/audio/{key}/HANDOFF.md")
        if pending:
            (cfg.out_dir / f"{key}.pending.json").write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"  saved out/{key}.json  requested {requested}  delivered {delivered}  needs human: {len(flagged)}")
        results.append({"paper": paper, "report": report})
    return results
