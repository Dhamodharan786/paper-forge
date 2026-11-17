"""Listening and speaking sections.

Claude writes the scripts, questions and prompts. It cannot make audio. The DEV TEAM produces the audio files, uploads
them to Wasabi and sends back the URLs. Until then every audio field in the paper is null. Each recording gets a plain
hand-off (exact text to speak, speakers, file name) in out/audio/<paperKey>/, and `paper-forge audio <paperKey> --map ...`
writes the URLs into the paper in place of the nulls. `publish` waits until none are left.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import prompts
from .assemble import shuffle_options
from .claude import Claude, extract_json
from .config import Config
from .gap import largest_remainder
from .generate import check_question
from .repo_index import RepoIndex
from .verify import web_check_text

LEVELS = ["Easy", "Medium", "Hard"]
WORDS_PER_SECOND = 2.4          # natural speech, for the estimated length of a recording
MAX_Q_PER_RECORDING = 3
MODES = ("read-aloud", "listen-and-repeat", "extempore")
SPEAK_DEFAULTS = {  # marks, expected seconds, section time seconds per item, evaluation criteria
    "read-aloud": (5, 75, 120, ["Accurate pronunciation and word recognition", "Natural phrasing and stress",
                                "Clarity and intelligibility", "Pace and minimal omissions"]),
    "listen-and-repeat": (5, 20, 60, ["Accuracy of words repeated", "Pronunciation", "Fluency", "Intonation"]),
    "extempore": (10, 90, 180, ["Reasoned, relevant recommendation supported by evidence",
                                "Logical organisation, qualification and alternatives",
                                "Fluency and intelligibility", "Grammar and vocabulary"]),
}
TITLES = {"read-aloud": "Speaking — Read Aloud", "listen-and-repeat": "Speaking — Listen and Repeat",
          "extempore": "Speaking — Spoken Response"}
SECTION_KEYS = {"read-aloud": "speaking-read-aloud", "listen-and-repeat": "speaking-listen-and-repeat",
                "extempore": "speaking-spoken-response"}
ABBR = {"read-aloud": "RA", "listen-and-repeat": "LR", "extempore": "VR"}


def section_mode(s: dict) -> str:
    """Mode of a SPEAKING or AUDIO_LISTENING pattern section. Falls back to the section name for older saved research."""
    m = (s.get("mode") or "").lower().strip()
    if s["type"] == "AUDIO_LISTENING":
        return "listening-comprehension"
    if m in MODES:
        return m
    n = s["name"].lower()
    if "read" in n:
        return "read-aloud"
    if "repeat" in n:
        return "listen-and-repeat"
    return "extempore"


def build_media_plan(pattern: dict, split: tuple[int, int, int]) -> list[dict]:
    plans = []
    for s in pattern["sections"]:
        if s["type"] not in ("AUDIO_LISTENING", "SPEAKING"):
            continue
        e, m, h = largest_remainder(s["questions"], *split)
        targets = ["Easy"] * e + ["Medium"] * m + ["Hard"] * h
        plans.append({"kind": "listening" if s["type"] == "AUDIO_LISTENING" else "speaking",
                      "mode": section_mode(s), "name": s["name"], "minutes": s.get("minutes"),
                      "topics": s.get("topics") or [], "targets": targets})
    return plans


def split_recordings(targets: list[str]) -> list[list[str]]:
    """Up to three questions per recording, keeping the difficulty order."""
    n = len(targets)
    parts = max(1, -(-n // MAX_Q_PER_RECORDING))
    size, extra = divmod(n, parts)
    out, i = [], 0
    for k in range(parts):
        take = size + (1 if k < extra else 0)
        out.append(targets[i:i + take])
        i += take
    return out


def words(s: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", s))


# ------------------------------------------------------------------------------------------------ listening
def recording_problems(d: dict, targets: list[str]) -> list[str]:
    bad = []
    tr = d.get("transcript") or ""
    lines = [l for l in tr.split("\n") if l.strip()]
    if len(lines) < 3 or not all(re.match(r"^[A-Z][\w .'-]{0,30}:\s+\S", l) for l in lines):
        bad.append('transcript must be at least 3 lines, each "Name: text"')
    if not 90 <= words(tr) <= 260:
        bad.append(f"transcript has {words(tr)} words; it must be 110 to 220")
    qs = d.get("questions") or []
    if len(qs) != len(targets):
        bad.append(f"need exactly {len(targets)} questions, got {len(qs)}")
    for i, q in enumerate(qs, 1):
        for p in check_question(q):
            bad.append(f"question {i}: {p}")
    return bad


def _rate(cfg: Config, claude: Claude, system: str, user: str) -> list[dict]:
    text = claude.ask(label="rate", model=cfg.checker_model, system=system, user=user, max_tokens=8000)
    try:
        return extract_json(text).get("ratings", [])
    except ValueError:
        return []


def _stamp(d: dict, targets: list[str], ev: dict) -> None:
    """Each question carries the level it was RATED at, falling back to the level asked for."""
    for i, q in enumerate(d["questions"]):
        lab = ((ev.get("ratings") or [None] * len(targets))[i] or {}).get("label")
        q["complexity"] = lab if lab in LEVELS else targets[i]


def make_recording(cfg: Config, claude: Claude, index: RepoIndex, targets: list[str], topics: list[str],
                   avoid: list[str], web: bool, log=print) -> dict:
    """One recording plus its questions. Returns {"ok","data","attempts","problems","flag","evidence"}."""
    brief = (f"Write one recording with exactly {len(targets)} question(s). Difficulty targets in order: {', '.join(targets)}.\n"
             f"Topic ideas: {', '.join(topics) or 'campus or workplace situations'}.\n"
             f"Situations already used in this paper, do not repeat: {', '.join(avoid) or 'none'}.")
    previous, problems = None, []
    res = {"ok": False, "data": None, "attempts": 0, "problems": [], "flag": None, "evidence": {}}
    for attempt in range(1, cfg.max_attempts + 1):
        res["attempts"] = attempt
        system = prompts.LISTENING_SYSTEM + (prompts.MEDIA_REPAIR_ADDON if previous else "")
        user = brief + (("\n\nYOUR EARLIER WORK:\n" + json.dumps(previous, ensure_ascii=False)[:7000]
                         + "\n\nPROBLEMS FOUND:\n- " + "\n- ".join(problems)) if previous else "")
        try:
            d = extract_json(claude.ask(label="listening-design", model=cfg.generator_model, system=system, user=user,
                                        max_tokens=12000))
            problems = recording_problems(d, targets)
        except (ValueError, KeyError) as e:
            problems, d = [f"your reply could not be read: {e}"], previous
        ev: dict = {}
        if not problems:
            sim = index.check({"question": d["transcript"]})
            ev["repo"] = sim
            if sim["fingerprintCollision"] or sim["maxRepoSimilarity"] >= cfg.repo_fail:
                problems.append(f"transcript is {int(sim['maxRepoSimilarity'] * 100)}% similar to {sim['closest']}")
        if not problems and web:
            w = web_check_text(cfg, claude, prompts.WEB_CHECK_SYSTEM, d["transcript"])
            ev["web"] = w
            if w["verdict"] == "match" or w["similarity"] >= 70:
                problems.append(f"this recording already exists online: {w['url']}")
        if not problems:
            qtxt = "\n\n".join(f"[{i}] {q['question']}\n" + " | ".join(f"{k}. {v}" for k, v in q["options"].items())
                               for i, q in enumerate(d["questions"]))
            rated = {int(r["index"]): r for r in _rate(cfg, claude, prompts.LISTENING_RATER_SYSTEM,
                                                       f"RECORDING SCRIPT:\n{d['transcript']}\n\nQUESTIONS:\n{qtxt}")}
            ev["ratings"] = [rated.get(i) for i in range(len(targets))]
            for i, t in enumerate(targets):
                lab = (rated.get(i) or {}).get("label")
                if lab != t:
                    problems.append(f"question {i + 1} is rated {lab} but {t} was wanted: {(rated.get(i) or {}).get('reason')}")
        res["evidence"] = ev
        previous = d if d else previous
        if not problems:
            _stamp(d, targets, ev)
            res.update(ok=True, data=d, problems=[])
            return res
        res["problems"] = problems
        log(f"      attempt {attempt}: {len(problems)} problem(s): {problems[0][:100]}")
        only_diff = all(p.startswith("question") and "is rated" in p for p in problems)
        if attempt == cfg.max_attempts and only_diff and d:
            _stamp(d, targets, ev)
            res.update(ok=True, data=d, flag="kept; some questions rated differently than wanted")
            return res
    res["flag"] = "needs a human: " + "; ".join(problems)
    return res


# ------------------------------------------------------------------------------------------------ speaking
def make_speaking_items(cfg: Config, claude: Claude, index: RepoIndex, mode: str, targets: list[str], topics: list[str],
                        avoid: list[str], web: bool, log=print) -> dict:
    """Items for one speaking section. Returns {"items": [...], "flags": [...], "attempts": n, "evidence": [...]}."""
    rules = prompts.SPEAKING_RULES[mode]
    items: list[dict | None] = [None] * len(targets)
    evidence: list[dict] = [{} for _ in targets]
    why: dict[int, list[str]] = {i: ["not written yet"] for i in range(len(targets))}
    flags: dict[int, str] = {}
    attempts = 0
    for attempt in range(1, cfg.max_attempts + 1):
        todo = [i for i in range(len(targets)) if i not in flags and why.get(i)]
        if not todo:
            break
        attempts = attempt
        brief = (f"Mode: {mode}.\nDifficulty rules for this mode:\n{rules}\n"
                 f"Topic ideas: {', '.join(topics) or 'professional and campus themes'}.\n"
                 f"Already used, do not repeat: {', '.join(avoid) or 'none'}.\n"
                 f"Write exactly {len(todo)} item(s). Difficulty targets in order: {', '.join(targets[i] for i in todo)}.")
        system = prompts.SPEAKING_SYSTEM + (prompts.MEDIA_REPAIR_ADDON if attempt > 1 else "")
        if attempt > 1:
            brief += "\n\nTHE EARLIER ITEMS AND WHAT WAS WRONG:\n" + json.dumps(
                [{"title": (items[i] or {}).get("title"), "text": (items[i] or {}).get("text"), "problems": why[i]} for i in todo],
                ensure_ascii=False)
        try:
            got = extract_json(claude.ask(label="speaking-design", model=cfg.generator_model, system=system, user=brief,
                                          max_tokens=12000)).get("items", [])
        except (ValueError, KeyError):
            got = []
        for k, i in enumerate(todo):
            items[i] = got[k] if k < len(got) and isinstance(got[k], dict) else {}
            why[i] = _speaking_problems(items[i], mode)
            if not why[i]:
                sim = index.check({"question": items[i]["text"]})
                evidence[i]["repo"] = sim
                if sim["fingerprintCollision"] or sim["maxRepoSimilarity"] >= cfg.repo_fail:
                    why[i].append(f"{int(sim['maxRepoSimilarity'] * 100)}% similar to {sim['closest']}")
            if not why[i] and web and mode != "listen-and-repeat":   # one short sentence is not worth a search
                w = web_check_text(cfg, claude, prompts.WEB_CHECK_SYSTEM, items[i]["text"])
                evidence[i]["web"] = w
                if w["verdict"] == "match" or w["similarity"] >= 70:
                    why[i].append(f"already exists online: {w['url']}")
        ok = [i for i in todo if not why[i]]
        if ok:
            user = f"Mode: {mode}\nRules:\n{rules}\n\n" + "\n\n".join(f"[{n}] {items[i]['text']}" for n, i in enumerate(ok))
            rated = {int(r["index"]): r for r in _rate(cfg, claude, prompts.SPEAKING_RATER_SYSTEM, user)}
            for n, i in enumerate(ok):
                rt = rated.get(n) or {}
                evidence[i]["rating"] = rt
                if rt.get("label") != targets[i]:
                    why[i].append(f"rated {rt.get('label')} but {targets[i]} was wanted: {rt.get('reason')}")
        bad = [i for i in todo if why[i]]
        log(f"      {mode} pass {attempt}: {len(todo) - len(bad)} ok, {len(bad)} to redo")
        if attempt == cfg.max_attempts:
            for i in bad:
                only_diff = len(why[i]) == 1 and why[i][0].startswith("rated ")
                flags[i] = "kept; rated differently than wanted" if only_diff else "needs a human: " + "; ".join(why[i])
    for i in range(len(targets)):
        if not why.get(i):
            flags.setdefault(i, None)
    return {"items": items, "flags": [flags.get(i) for i in range(len(targets))], "attempts": attempts, "evidence": evidence}


def _speaking_problems(it: dict, mode: str) -> list[str]:
    bad = []
    t = (it.get("text") or "").strip()
    if not t:
        return ["text missing"]
    n = words(t)
    lim = {"read-aloud": (50, 95), "listen-and-repeat": (7, 28), "extempore": (15, 120)}[mode]
    if not lim[0] <= n <= lim[1]:
        bad.append(f"text has {n} words; {mode} needs {lim[0]} to {lim[1]}")
    if not (it.get("modelAnswer") or "").strip():
        bad.append("modelAnswer missing")
    elif mode == "extempore" and words(it["modelAnswer"]) < 100:
        bad.append("modelAnswer needs at least 100 words")
    if not (it.get("title") or "").strip():
        bad.append("title missing")
    return bad


# ------------------------------------------------------------------------------------------------ building the paper parts
def build_media_sections(cfg: Config, qprefix: str, short: str, num: str, results: list[dict], rng) -> tuple[list[dict], list[dict]]:
    """results: [{"plan", "recordings":[{"data","flag"}]} | {"plan","speaking":{...}}]. Returns (sections, audio_jobs)."""
    sections, jobs = [], []
    rec_letter = iter("ABCDEFGHIJ")
    for r in results:
        plan = r["plan"]
        if plan["kind"] == "listening":
            secs = [rc for rc in r["recordings"] if rc["data"]]
            total_q = sum(len(rc["data"]["questions"]) for rc in secs) or 1
            minutes = plan.get("minutes")
            qn = 0
            for rc in secs:
                d = rc["data"]
                letter = next(rec_letter)
                file = f"{short}_{num}_listening_{letter}.mp3"
                qs = []
                for q in d["questions"]:
                    qn += 1
                    sh = shuffle_options(q, rng)
                    qs.append({"questionKey": f"{qprefix}_LC_Q{qn:02d}", "question": q["question"], "options": sh["options"],
                               "correctAnswer": sh["correctAnswer"], "marks": 1, "negativeMarks": 0,
                               "complexity": q["complexity"], "category": "Listening Comprehension",
                               "tags": q.get("tags") or ["listening"], "explanation": q["explanation"], "hint": None,
                               "hintPenaltyPoints": None, "lockOptions": sh["lockOptions"]})
                secs_time = round((minutes or 8) * 60 * len(qs) / total_q)
                sections.append({
                    "sectionKey": f"listening-comprehension-{letter.lower()}",
                    "name": f"Listening Comprehension — Recording {letter}", "type": "MCQ",
                    "timeLimitSeconds": secs_time,
                    "instructions": (f"Listen to Recording {letter} and answer {len(qs)} question(s). Each question carries 1 mark; "
                                     "no negative marking. The script is not shown during the test."),
                    "tracks": None, "shuffleQuestions": False, "shuffleOptions": True,
                    "config": {"mode": "listening-comprehension", "hasAudioPrompt": True, "gradedBy": "AUTO_KEY",
                               "audioAsset": None, "audioTranscript": d["transcript"]},
                    "questions": qs})
                jobs.append({"file": file, "url": None, "kind": "listening", "title": d.get("title"),
                             "sectionKey": f"listening-comprehension-{letter.lower()}", "questionKey": None,
                             "speakers": d.get("speakers") or [], "transcript": d["transcript"],
                             "estimatedSeconds": round(words(d["transcript"]) / WORDS_PER_SECOND)})
        else:
            mode, sp = plan["mode"], r["speaking"]
            marks, dur, per_item, crit = SPEAK_DEFAULTS[mode]
            qs = []
            for n, it in enumerate(sp["items"], 1):
                if not it or sp["flags"][n - 1] and sp["flags"][n - 1].startswith("needs"):
                    continue
                lab = ((sp["evidence"][n - 1] or {}).get("rating") or {}).get("label")
                label = lab if lab in LEVELS else plan["targets"][n - 1]
                q = {"questionKey": f"{qprefix}_{ABBR[mode]}_Q{len(qs) + 1:02d}", "title": it["title"], "marks": marks,
                     "complexity": label, "category": "Professional Communication", "tags": it.get("tags") or [mode],
                     "expectedDurationSeconds": dur, "evaluationCriteria": crit, "modelAnswer": it["modelAnswer"]}
                if mode == "read-aloud":
                    q["question"] = ("Read the following passage aloud, clearly and at a natural pace:\n\n\"" + it["text"] + "\"")
                    q["referenceText"] = it["text"]
                elif mode == "listen-and-repeat":
                    file = f"{short}_{num}_repeat_{len(qs) + 1}.mp3"
                    q["question"] = "Listen to the audio prompt and repeat the sentence."
                    q["modelAnswer"] = it["text"]
                    q.update(audioTranscript=it["text"], audioAsset=None, referenceText=it["text"])
                    jobs.append({"file": file, "url": None, "kind": "repeat", "title": it["title"],
                                 "sectionKey": SECTION_KEYS[mode], "questionKey": q["questionKey"],
                                 "speakers": [], "transcript": it["text"],
                                 "estimatedSeconds": round(words(it["text"]) / WORDS_PER_SECOND)})
                else:
                    q["question"] = it["text"]
                qs.append(q)
            if not qs:
                continue
            cfgd = {"mode": mode, "gradedBy": "AI_SPEECH_RUBRIC", "scoreOnSubmit": True,
                    "graderOnlyFields": {"read-aloud": ["referenceText", "modelAnswer"],
                                         "listen-and-repeat": ["audioTranscript", "referenceText", "modelAnswer"],
                                         "extempore": ["modelAnswer"]}[mode]}
            if mode == "listen-and-repeat":
                cfgd["hasAudioPrompt"] = True
            instr = {"read-aloud": "Read each passage aloud clearly and at a natural pace.",
                     "listen-and-repeat": "Play each sentence, listen carefully, then repeat it as accurately as possible.",
                     "extempore": "Answer each prompt aloud. Address the evidence, give your decision and state next steps."}[mode]
            sections.append({
                "sectionKey": SECTION_KEYS[mode], "name": TITLES[mode], "type": "VOICE_DRILL",
                "timeLimitSeconds": per_item * len(qs),
                "instructions": f"{instr} {marks} marks each. Your recordings are saved and scored when you submit the exam.",
                "tracks": None, "shuffleQuestions": False, "shuffleOptions": False, "config": cfgd, "questions": qs})
    return sections, jobs


# ------------------------------------------------------------------------------------------------ audio files
def check_urls(urls: list[str], timeout: float = 15.0) -> dict[str, str]:
    """HEAD each URL. Returns {url: "ok" | "missing (404)" | "error: ..."}. Read-only requests to the CDN."""
    out = {}
    for u in urls:
        try:
            req = urllib.request.Request(u, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out[u] = "ok" if r.status == 200 else f"status {r.status}"
        except urllib.error.HTTPError as e:
            out[u] = f"missing ({e.code})"
        except Exception as e:  # noqa: BLE001
            out[u] = f"error: {type(e).__name__}"
    return out


def write_audio_scripts(jobs: list[dict], folder, paper_key: str = "") -> None:
    """The hand-off for the DEV TEAM, who produce the audio files: one text file per recording that says, in plain words,
    exactly what to record and where to upload it, plus HANDOFF.md as the checklist."""
    folder.mkdir(parents=True, exist_ok=True)
    for j in jobs:
        spk = j["speakers"]
        kind = ("A conversation between " + ", ".join(spk) + ". Use a different voice for each person." if len(spk) > 1
                else "One voice, speaking alone." if j["kind"] == "listening"
                else "One voice. Speak this single sentence once, clearly, at a natural pace.")
        body = "\n".join([
            "AUDIO FILE TO PRODUCE (dev team)", "=" * 60,
            f"File name : {j['file']}",
            "Upload to : Wasabi (the usual audio location). Send back the public URL of the uploaded file.",
            f"What it is: {kind}",
            f"Length    : about {j['estimatedSeconds']} seconds at a natural pace",
            "", "TEXT TO SPEAK", "-" * 60, j["transcript"], "-" * 60, "",
            "Rules",
            "  - Speak exactly the words above. Do not add, remove or reword anything.",
            "  - Where a line starts with a name and a colon (for example \"Maya:\"), that name is the speaker. Do NOT say the name aloud.",
            "  - Natural pace, clear pronunciation, no music or sound effects.",
            "  - Save as MP3, the same format as the other audio files, with exactly the file name above.",
            ""])
        (folder / (j["file"].rsplit(".", 1)[0] + ".txt")).write_text(body, encoding="utf-8")
    rows = "\n".join(f"| {i} | `{j['file']}` | {j['kind']} | {len(j['speakers']) or 1} | ~{j['estimatedSeconds']}s | [ ] |"
                     for i, j in enumerate(jobs, 1))
    (folder / "HANDOFF.md").write_text("\n".join([
        f"# Audio to produce{(' for ' + paper_key) if paper_key else ''}", "",
        "Claude cannot make audio. The dev team produces these files from the text in each `.txt` file next to this one.",
        "Until the URLs come back, every audio field in the paper is `null`.", "",
        "| # | File to produce | Kind | Voices | Length | Done |", "|---|---|---|---|---|---|", rows, "",
        "## Steps",
        "1. Open each `.txt` file. The block under TEXT TO SPEAK is the exact wording to record.",
        "2. Produce one MP3 per file, named exactly as listed.",
        "3. Upload each MP3 to Wasabi and copy its public URL.",
        "4. Fill the URLs into `urls.json` in this folder (file name -> URL), or send them as lines of `file-name URL`.",
        f"5. Paper Forge replaces the nulls with those URLs: `python -m paper_forge audio {paper_key or '<paperKey>'} --map urls.json`.",
        "   `publish` will not continue while any audio URL is still null.", ""]), encoding="utf-8")
    (folder / "urls.json").write_text(json.dumps({j["file"]: None for j in jobs}, indent=2), encoding="utf-8")
    (folder / "index.json").write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


URL_RE = re.compile(r"^https?://\S+$")


def parse_url_lines(text: str) -> dict[str, str]:
    """Accepts a JSON object {file: url}, or plain lines of 'file-name URL' (also 'file-name = URL')."""
    text = text.strip()
    if text.startswith("{"):
        return {k: v for k, v in json.loads(text).items() if v}
    out = {}
    for line in text.splitlines():
        parts = re.split(r"[\s=,]+", line.strip(), maxsplit=1)
        if len(parts) == 2 and parts[0]:
            out[parts[0]] = parts[1].strip()
    return out


def attach_urls(paper: dict, report: dict, mapping: dict[str, str]) -> dict:
    """Write the dev team's URLs into the paper in place of null. Matches by MP3 file name.
    Returns {"applied": [...], "unknown": [...], "badUrl": [...], "pending": [...]}. Changes paper and report in place."""
    jobs = {j["file"]: j for j in report.get("audio") or []}
    res = {"applied": [], "unknown": [], "badUrl": [], "pending": []}
    for name, url in mapping.items():
        key = name.rsplit("/", 1)[-1]
        if key not in jobs:
            res["unknown"].append(name)
            continue
        if not URL_RE.match(url or ""):
            res["badUrl"].append(name)
            continue
        j = jobs[key]
        for sec in paper["sections"]:
            if sec["sectionKey"] != j["sectionKey"]:
                continue
            if j["questionKey"] is None:
                sec["config"]["audioAsset"] = url
            else:
                for q in sec["questions"]:
                    if q["questionKey"] == j["questionKey"]:
                        q["audioAsset"] = url
        j["url"] = url
        res["applied"].append(key)
    res["pending"] = [f for f, j in jobs.items() if not j["url"]]
    return res
