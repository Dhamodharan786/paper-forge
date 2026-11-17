"""Coding problems: design with Claude, then PROVE everything with scripts.

Claude writes a statement, a reference solution, a brute force and a test generator. Test outputs are produced by
running the reference, never taken from the model's words. The brute force cross-checks the reference on small
inputs and shows (by timing out at maximum size) that the constraints force the intended technique.
"""
from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import prompts
from .claude import Claude, extract_json
from .config import Config
from .repo_index import jaccard, masked, shingles

MARKS = {"EASY": 50, "MEDIUM": 100, "HARD": 150}
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FILE_RE = re.compile(r"~~~file:(\S+)[^\n]*\n(.*?)\n~~~", re.S)
STARTERS = {"starter.py": "solution.py", "Main.java": "solution.java", "solution.cpp": "solution.cpp", "solution.c": "solution.c"}
REF_SECONDS_LIMIT = 6.0     # reference (Python) must finish each test within this
BRUTE_SECONDS = 6.0         # brute force at maximum size: timing out is the evidence we want
SMALL_INPUT_CHARS = 3000    # brute force cross-check only on inputs up to this size


# ---------------------------------------------------------------- running generated code (local, with timeouts)
def run_py(script: Path, stdin: str, timeout: float) -> tuple[str, str, float]:
    """Returns (status, stdout, seconds). status is 'ok', 'timeout' or 'error: ...'. Runs in the script's folder."""
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-I", str(script)], input=stdin, capture_output=True, text=True,
                           timeout=timeout, cwd=script.parent, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "timeout", "", time.time() - t0
    secs = time.time() - t0
    if r.returncode != 0:
        return f"error: {(r.stderr or '').strip()[-300:]}", r.stdout, secs
    return "ok", r.stdout, secs


_GEN_RUNNER = """import sys, importlib.util
spec = importlib.util.spec_from_file_location("generator", "generator.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for i in range(3):
    with open(f"gen_{i}.txt", "w", encoding="utf-8", newline="") as f:
        f.write(m.gen(i))
"""


def parse_design(text: str) -> dict:
    """Metadata JSON (first fenced json block) plus the tilde-fenced files."""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        raise ValueError("no metadata JSON block in the reply")
    meta = json.loads(m.group(1))
    files = {name: body for name, body in FILE_RE.findall(text)}
    return {"meta": meta, "files": files}


def load_tags(repo: Path) -> set[str]:
    f = repo / "tags.json"
    return set(json.loads(f.read_text(encoding="utf-8"))["tags"]) if f.is_file() else set()


# ---------------------------------------------------------------- the checks
def structural_problems(d: dict, difficulty: str, vocab: set[str], taken_keys: set[str]) -> list[str]:
    meta, files, bad = d["meta"], d["files"], []
    key = meta.get("problemKey", "")
    if not SLUG.fullmatch(key or ""):
        bad.append("problemKey must be lowercase-kebab")
    elif key in taken_keys:
        bad.append(f"problemKey {key!r} already exists. Choose a different title and key")
    tags = meta.get("tags") or []
    if not tags or (vocab and not set(tags) <= vocab):
        bad.append(f"tags must come from tags.json: {sorted(set(tags) - vocab) if vocab else ''} not allowed")
    if any(t.startswith(("SQL", "WEB")) for t in tags):
        bad.append("CODING problems must not use SQL or WEB tags")
    for k in ("title", "inputFormat", "outputFormat", "constraintsText", "expectedTimeComplexity", "expectedSpaceComplexity"):
        if not meta.get(k):
            bad.append(f"missing {k}")
    if not 2 <= len(meta.get("examples") or []) <= 4:
        bad.append("need 2 to 3 examples")
    if len(meta.get("hints") or []) < 2:
        bad.append("need 2 hints")
    if len(meta.get("visibleInputs") or []) < 3:
        bad.append("need at least 3 visibleInputs")
    if len(meta.get("hiddenInputs") or []) < 5:
        bad.append("need at least 5 hiddenInputs")
    for need in ("statement.md", "reference.py", "brute.py", "generator.py", *STARTERS):
        if not (files.get(need) or "").strip():
            bad.append(f"missing file {need}")
    for s in STARTERS:
        if files.get(s) and "TODO" not in files[s]:
            bad.append(f"{s} needs a TODO comment")
    if files.get("Main.java") and "class Main" not in files["Main.java"]:
        bad.append("Main.java must declare public class Main")
    return bad


def build_tests(cfg: Config, d: dict, difficulty: str, work: Path) -> tuple[list[str], dict, dict]:
    """Run everything. Returns (problems, tests, evidence). tests = {"visible": [(in, out)], "hidden": [...]}."""
    meta, files, bad = d["meta"], d["files"], []
    work.mkdir(parents=True, exist_ok=True)
    for name in ("reference.py", "brute.py", "generator.py"):
        (work / name).write_text(files[name], encoding="utf-8")
    (work / "gen_runner.py").write_text(_GEN_RUNNER, encoding="utf-8")
    ev: dict = {}

    st, _, _ = run_py(work / "gen_runner.py", "", 60)
    if st != "ok":
        return [f"generator.py failed: {st}"], {}, ev
    big = [(work / f"gen_{i}.txt").read_text(encoding="utf-8") for i in range(3)]
    small = list(meta["visibleInputs"]) + list(meta["hiddenInputs"])
    if max(len(b) for b in big) < max(200, 5 * max(len(s) for s in small)):
        bad.append("generator.py output is not near the constraint maximum; it must produce the largest allowed inputs")
    if any(len(b) > 3_000_000 for b in big):
        bad.append("a generated input is over 3 MB")
    if bad:
        return bad, {}, ev

    outputs: dict[str, str] = {}
    slowest = 0.0
    for label, inp in [(f"small{i}", s) for i, s in enumerate(small)] + [(f"big{i}", b) for i, b in enumerate(big)]:
        st, out, secs = run_py(work / "reference.py", inp if inp.endswith("\n") else inp + "\n", REF_SECONDS_LIMIT + 5)
        slowest = max(slowest, secs)
        if st != "ok":
            return [f"reference.py failed on {label} (input starts {inp[:80]!r}): {st}"], {}, ev
        if not out.strip():
            return [f"reference.py printed nothing for {label}"], {}, ev
        outputs[label] = out.strip()
    ev["referenceSlowestSeconds"] = round(slowest, 2)
    if slowest > REF_SECONDS_LIMIT:
        bad.append(f"reference.py takes {slowest:.1f}s on the largest test; the intended solution must be faster")
    if len(set(outputs.values())) < 2:
        bad.append("every test has the same expected output; the tests do not discriminate")

    for k, ex in enumerate(meta["examples"]):
        st, out, _ = run_py(work / "reference.py", ex["input"] + "\n", 10)
        if st != "ok" or out.strip() != str(ex["output"]).strip():
            bad.append(f"example {k + 1} says {str(ex['output']).strip()!r} but the reference prints {out.strip()!r}")

    mismatches = checked = 0
    for label, inp in [(f"small{i}", s) for i, s in enumerate(small)]:
        if len(inp) > SMALL_INPUT_CHARS:
            continue
        st, out, _ = run_py(work / "brute.py", inp + "\n", 15)
        checked += 1
        if st != "ok" or out.strip() != outputs[label]:
            mismatches += 1
            bad.append(f"brute force and reference disagree on input {inp[:120]!r}: brute says {out.strip()[:60]!r} "
                       f"({st if st != 'ok' else 'ok'}), reference says {outputs[label][:60]!r}. One of them is wrong.")
            break
    ev["bruteCrossChecked"] = checked

    timed_out = False
    for i, b in enumerate(big):
        st, out, _ = run_py(work / "brute.py", b, BRUTE_SECONDS)
        if st == "timeout":
            timed_out = True
        elif st == "ok" and out.strip() != outputs[f"big{i}"]:
            bad.append(f"brute force and reference disagree on maximum test {i}. One of them is wrong.")
    ev["bruteTimedOutAtMax"] = timed_out
    if difficulty in ("MEDIUM", "HARD") and not timed_out:
        bad.append("the brute force finishes at the maximum size, so the constraints do not force the intended technique. "
                   "Raise the limits or change the problem so only the intended approach is fast enough")

    def pair(label: str, inp: str) -> tuple[str, str]:
        return (inp if inp.endswith("\n") else inp + "\n", outputs[label] + "\n")

    tests = {"visible": [pair(f"small{i}", s) for i, s in enumerate(meta["visibleInputs"])],
             "hidden": [pair(f"small{len(meta['visibleInputs']) + i}", s) for i, s in enumerate(meta["hiddenInputs"])]
                       + [pair(f"big{i}", b) for i, b in enumerate(big)]}
    ev["tests"] = {"visible": len(tests["visible"]), "hidden": len(tests["hidden"]),
                   "largestInputChars": max(len(b) for b in big)}
    return bad, tests, ev


class ProblemIndex:
    """Statements of every problem already in the repo (and earlier in this run), for the free similarity check."""

    def __init__(self, repo: Path):
        self.items: list[tuple[str, set]] = []
        self.keys: set[str] = set()
        self.titles: set[str] = set()
        base = repo / "problems"
        if base.is_dir():
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                self.keys.add(d.name)
                st = d / "statement.md"
                if st.is_file():
                    self.items.append((d.name, shingles(masked(st.read_text(encoding="utf-8", errors="replace")))))
                pj = d / "problem.json"
                if pj.is_file():
                    try:
                        self.titles.add(json.loads(pj.read_text(encoding="utf-8")).get("title", "").strip().lower())
                    except ValueError:
                        pass

    def check(self, statement: str, title: str) -> dict:
        sh = shingles(masked(statement))
        best, who = 0.0, None
        for key, s in self.items:
            j = jaccard(sh, s)
            if j > best:
                best, who = j, key
        return {"maxStatementSimilarity": round(best, 3), "closest": who, "titleTaken": title.strip().lower() in self.titles}

    def add(self, key: str, statement: str, title: str) -> None:
        self.keys.add(key)
        self.titles.add(title.strip().lower())
        self.items.append((key, shingles(masked(statement))))


# ---------------------------------------------------------------- one problem, end to end
def _design(cfg: Config, claude: Claude, brief: str, previous: dict | None, problems: list[str]) -> dict:
    system = prompts.CODING_DESIGN_SYSTEM + (prompts.CODING_REPAIR_ADDON if previous else "")
    user = brief
    if previous:
        prev_files = "\n\n".join(f"--- {k} ---\n{v}" for k, v in previous["files"].items() if k in ("statement.md", "reference.py"))
        user += ("\n\nYOUR EARLIER DESIGN (metadata):\n" + json.dumps(previous["meta"], ensure_ascii=False)[:6000]
                 + "\n\n" + prev_files[:9000] + "\n\nPROBLEMS FOUND:\n- " + "\n- ".join(problems))
    text = claude.ask(label="coding-design", model=cfg.generator_model, system=system, user=user, max_tokens=20000)
    return parse_design(text)


def write_problem_folder(cfg: Config, d: dict, difficulty: str, tests: dict, dest_root: Path) -> Path:
    meta, files = d["meta"], d["files"]
    key = meta["problemKey"]
    root = dest_root / key
    if root.exists():
        shutil.rmtree(root)
    for sub in ("starter", "reference", "tests/visible", "tests/hidden"):
        (root / sub).mkdir(parents=True)
    (root / "statement.md").write_text(files["statement.md"].rstrip() + "\n", encoding="utf-8")
    (root / "reference" / "solution.py").write_text(files["reference.py"].rstrip() + "\n", encoding="utf-8")
    for src, dst in STARTERS.items():
        (root / "starter" / dst).write_text(files[src].rstrip() + "\n", encoding="utf-8")
    for kind in ("visible", "hidden"):
        for n, (i, o) in enumerate(tests[kind], 1):
            (root / "tests" / kind / f"{n:02d}.in").write_text(i, encoding="utf-8", newline="")
            (root / "tests" / kind / f"{n:02d}.out").write_text(o, encoding="utf-8", newline="")
    xp = [5, 10, 15, 20]
    problem = {
        "schemaVersion": 1, "problemKey": key, "title": meta["title"], "marks": MARKS[difficulty],
        "difficulty": difficulty, "tags": meta["tags"], "statementFile": "statement.md",
        "inputFormat": meta["inputFormat"], "outputFormat": meta["outputFormat"],
        "constraints": {"text": meta["constraintsText"], "expectedTimeComplexity": meta["expectedTimeComplexity"],
                        "expectedSpaceComplexity": meta["expectedSpaceComplexity"],
                        "timeLimitMs": int(meta.get("timeLimitMs") or 2000), "memoryLimitMb": int(meta.get("memoryLimitMb") or 256)},
        "examples": [{"input": e["input"], "output": str(e["output"]), "explanation": e.get("explanation", "")} for e in meta["examples"]],
        "hints": [{"seqNo": i + 1, "text": h, "xpCost": xp[min(i, 3)]} for i, h in enumerate(meta["hints"])],
        "scoring": {"mode": "PROPORTIONAL", "partialCredit": True},
        "status": "REVIEW",   # only a human reviewer flips a problem to ACTIVE
        "authorMeta": {"author": "paper-forge", "createdAt": datetime.date.today().isoformat(), "version": 1},
    }
    (root / "problem.json").write_text(json.dumps(problem, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return root


def make_problem(cfg: Config, claude: Claude, index: ProblemIndex, difficulty: str, topics: list[str], avoid: list[str],
                 web: bool, vocab: set[str], log=print) -> dict:
    """Returns {"ok", "key", "title", "difficulty", "attempts", "problems", "evidence", "flag"}."""
    from .verify import web_check_text  # local import: verify imports nothing from here
    brief = (f"Design one {difficulty} coding problem.\nTopic ideas (pick one, or a close relative): "
             f"{', '.join(topics) or 'standard campus-test DSA'}.\nAllowed tags: {', '.join(sorted(vocab))}.\n"
             f"Problems already in this paper, do not repeat their idea: {', '.join(avoid) or 'none'}.")
    previous, problems = None, []
    result = {"ok": False, "key": None, "title": None, "difficulty": difficulty, "attempts": 0, "problems": [], "evidence": {}, "flag": None}
    for attempt in range(1, cfg.max_attempts + 1):
        result["attempts"] = attempt
        try:
            d = _design(cfg, claude, brief, previous, problems)
        except (ValueError, KeyError) as e:
            problems, previous = [f"your reply could not be read: {e}"], previous
            log(f"    attempt {attempt}: unreadable reply")
            continue
        previous = d
        problems = structural_problems(d, difficulty, vocab, index.keys)
        ev: dict = {}
        tests: dict = {}
        if not problems:
            with tempfile.TemporaryDirectory() as tmp:
                problems, tests, ev = build_tests(cfg, d, difficulty, Path(tmp))
        if not problems:
            sim = index.check(d["files"]["statement.md"], d["meta"]["title"])
            ev["repo"] = sim
            if sim["titleTaken"]:
                problems.append("a problem with this title already exists")
            if sim["maxStatementSimilarity"] >= cfg.repo_fail:
                problems.append(f"{int(sim['maxStatementSimilarity'] * 100)}% similar to existing problem {sim['closest']}")
        if not problems and web:
            w = web_check_text(cfg, claude, prompts.CODING_WEB_CHECK_SYSTEM,
                               f"{d['meta']['title']}\n\n{d['files']['statement.md'][:3500]}")
            ev["web"] = w
            if w["verdict"] == "match" or w["similarity"] >= 70:
                problems.append(f"this problem already exists online: {w['url']}")
        if not problems:
            rt = _rate(cfg, claude, d)
            ev["rating"] = rt
            if (rt.get("label") or "").upper() != difficulty:
                problems.append(f"a reader rates this {rt.get('label')} but {difficulty} was wanted: {rt.get('reason')}")
        result["evidence"] = ev
        if not problems:
            result.update(ok=True, key=d["meta"]["problemKey"], title=d["meta"]["title"], problems=[])
            result["_design"], result["_tests"] = d, tests
            return result
        log(f"    attempt {attempt}: {len(problems)} problem(s): {problems[0][:110]}")
        result["problems"] = problems
        # last attempt that only disagrees on difficulty label: keep it, flag the drift
        if attempt == cfg.max_attempts and len(problems) == 1 and problems[0].startswith("a reader rates") and tests:
            result.update(ok=True, key=d["meta"]["problemKey"], title=d["meta"]["title"], flag="kept; rated differently than wanted")
            result["_design"], result["_tests"] = d, tests
            return result
    result["flag"] = "needs a human: " + "; ".join(problems)
    return result


def _rate(cfg: Config, claude: Claude, d: dict) -> dict:
    text = claude.ask(label="coding-rate", model=cfg.checker_model, system=prompts.CODING_RATER_SYSTEM,
                      user=d["files"]["statement.md"][:6000], max_tokens=4000)
    try:
        return extract_json(text)
    except ValueError:
        return {"label": None, "reason": "rating unreadable"}


# ---------------------------------------------------------------- run the repo's own scripts on a draft problem
def validate_problem_draft(cfg: Config, key: str, problems_root: Path) -> dict:
    """Copy scripts, schema, tags and this problem into a temp root and run verify-solutions and build-manifest there."""
    need = [cfg.repo / "scripts" / "verify-solutions.py", cfg.repo / "scripts" / "build-manifest.py", cfg.repo / "tags.json",
            cfg.repo / "schema" / "problem.schema.json"]
    if not all(p.is_file() for p in need):
        return {"ran": False, "ok": False, "output": "repo scripts not found"}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "schema").mkdir()
        for f in (cfg.repo / "scripts").glob("*.py"):
            shutil.copy(f, root / "scripts" / f.name)
        shutil.copy(cfg.repo / "tags.json", root / "tags.json")
        shutil.copy(cfg.repo / "schema" / "problem.schema.json", root / "schema" / "problem.schema.json")
        shutil.copytree(problems_root / key, root / "problems" / key)
        out = []
        ok = True
        for args in (["scripts/verify-solutions.py", "--problem", key], ["scripts/build-manifest.py"]):
            r = subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
            out.append(f"$ {' '.join(args)}\n{(r.stdout + r.stderr).strip()[-800:]}")
            ok = ok and r.returncode == 0
        return {"ran": True, "ok": ok, "output": "\n".join(out)}
