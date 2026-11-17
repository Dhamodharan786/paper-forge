import json
from dataclasses import replace
from pathlib import Path

from paper_forge import assemble, generate
from paper_forge.config import load
from paper_forge.cost import CostTracker
from paper_forge.pipeline import generate_papers
from paper_forge.repo_index import RepoIndex

SAMPLE = json.loads((Path(__file__).parent / "sample_pattern.json").read_text(encoding="utf-8"))
OLD_Q = "A shop sells pens at 12 rupees each and gives 10 percent discount on 20 pens. What is the total cost paid?"


def tok(n):
    return "".join(chr(97 + int(d)) for d in str(n)) + "x"


def mcq(text, ans="A", expl="Step one, then step two, so the answer is the keyed option."):
    return {"question": text, "options": {"A": text[:8] + " one", "B": "two b", "C": "three c", "D": "four d"},
            "correctAnswer": ans, "explanation": expl, "category": "Arithmetic", "tags": ["t"]}


class FakeClaude:
    """Scripted stand-in for the API. First batch contains one copy of an existing repo question."""

    def __init__(self):
        self.calls = []
        self.n = 0

    def ask(self, *, label, model, system, user, tools=None, max_tokens=16000):
        self.calls.append(label)
        if label.startswith("generate"):
            want = user.count(", ") and int(user.split("Write ")[1].split(" ")[0])
            qs = []
            for i in range(want):
                self.n += 1
                qs.append(mcq(OLD_Q if (self.n == 1) else f"Unique question {tok(self.n)} about trains moving between {tok(self.n * 7)} stations", "B"))
            return "```json\n" + json.dumps({"questions": qs}) + "\n```"
        if label.startswith("rewrite"):
            items = json.loads(user.split("Rewrite these:\n")[1].split("\n")[0])
            qs = []
            for it in items:
                self.n += 1
                q = mcq(f"Rewritten item {tok(self.n)} about boats crossing {tok(self.n * 11)} rivers", "C")
                q["replaces"] = it["index"]
                qs.append(q)
            return "```json\n" + json.dumps({"questions": qs}) + "\n```"
        if label == "web-check":
            return '```json\n{"verdict":"no_match","similarity":5,"url":null,"note":"none"}\n```'
        if label == "rate":
            n = user.count("\n\n") + 1
            # blind rater: say Medium for everything
            r = [{"index": i, "label": "Medium", "rulesHit": ["2 steps"], "expectedSeconds": 60, "reason": "two steps"} for i in range(n)]
            return "```json\n" + json.dumps({"ratings": r}) + "\n```"
        raise AssertionError(label)


def make_cfg(tmp_path):
    repo = tmp_path / "repo"
    (repo / "companies" / "other").mkdir(parents=True)
    old = {"sections": [{"questions": [{"questionKey": "OLD_1", "question": OLD_Q,
                                        "options": {"A": "216", "B": "200", "C": "180", "D": "240"}}]}]}
    (repo / "companies" / "other" / "other-001.json").write_text(json.dumps(old), encoding="utf-8")
    return replace(load(), repo=repo, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache")


def test_repo_index_catches_same_story_new_numbers(tmp_path):
    cfg = make_cfg(tmp_path)
    idx = RepoIndex(cfg.repo)
    assert len(idx.entries) == 1
    changed = OLD_Q.replace("12", "15").replace("20", "30").replace("10", "25")
    assert idx.check({"question": changed, "options": {"A": "a"}})["fingerprintCollision"]
    assert idx.check({"question": "Completely different text about birds and trees in a forest today"})["maxRepoSimilarity"] < 0.2


def test_pipeline_rewrites_duplicate_and_matches_blueprint(tmp_path):
    cfg = make_cfg(tmp_path)
    fake = FakeClaude()
    res = generate_papers(cfg, fake, CostTracker(cfg), SAMPLE, (0, 100, 0), 1, "dev", True, log=lambda *_: None, coding=False)
    paper, report = res[0]["paper"], res[0]["report"]
    assert "rewrite:Quantitative" in fake.calls            # the copied question was caught and rewritten
    assert [s["name"] for s in paper["sections"]] == ["Quantitative aptitude", "Pseudocode"]
    assert paper["totalMarks"] == 15 and report["needsHuman"] == 0
    assert (cfg.out_dir / f"{paper['paperKey']}.pending.json").exists()   # image section kept out of the paper
    assert all(q["complexity"] == "Medium" for s in paper["sections"] for q in s["questions"])
    keys = [q["questionKey"] for s in paper["sections"] for q in s["questions"]]
    assert len(keys) == len(set(keys))


def test_rater_disagreement_flows_to_final_attempt_and_keeps_rating(tmp_path):
    cfg = replace(make_cfg(tmp_path), web_check=False, max_attempts=2)
    fake = FakeClaude()
    res = generate_papers(cfg, fake, CostTracker(cfg), SAMPLE, (100, 0, 0), 1, "drop", False, log=lambda *_: None, coding=False)
    rep = res[0]["report"]
    assert rep["requestedSplit"]["Easy"] == 15 and rep["deliveredSplit"]["Medium"] == 15   # drift is reported
    assert all(q["flag"] and q["flag"].startswith("kept") for q in rep["questions"])


def test_shuffle_spreads_answers_and_locks_cross_references():
    import random
    rng = random.Random(1)
    q = mcq("Some question text long enough", "A")
    letters = {assemble.shuffle_options(q, rng)["correctAnswer"] for _ in range(40)}
    assert len(letters) > 1
    locked = {"options": {"A": "x", "B": "y", "C": "z", "D": "All of the above"}, "correctAnswer": "D"}
    assert assemble.shuffle_options(locked, rng) == {"options": locked["options"], "correctAnswer": "D", "lockOptions": True}


def test_check_question_flags_bad_shapes():
    assert generate.check_question(mcq("A fine question with enough text")) == []
    bad = mcq("short")
    bad["options"]["B"] = bad["options"]["A"]
    assert len(generate.check_question(bad)) >= 2
