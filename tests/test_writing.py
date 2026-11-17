"""Writing sections: plan, word-limit check, repair, paper format, review, PDF, publish."""
import io
import json
import random
import re

import pytest
from pypdf import PdfReader

from paper_forge import publish as pub, review, writing
from paper_forge.cost import CostTracker
from paper_forge.pdf import build_pdf
from paper_forge.pipeline import generate_papers
from test_m2 import FakeClaude
from test_publish import make_repo, sh
from test_speech import WORDS, text_of


class WritingFake(FakeClaude):
    def __init__(self):
        super().__init__()
        self.k = 0
        self.designs = 0

    def ask(self, **kw):
        label, user, system = kw["label"], kw["user"], kw["system"]
        if label == "writing-design":
            self.designs += 1
            items = []
            for m in re.finditer(r"(\d+)\. format (\w+), difficulty (\w+), word limit (\d+) to (\d+), (\d+) marks", user):
                self.k += 1
                lo, hi = int(m.group(4)), int(m.group(5))
                size = (lo + hi) // 2
                if self.designs == 1 and m.group(1) == "1":
                    size = hi + 80                       # the first answer is too long on purpose
                rng = random.Random(self.k)
                items.append({"question": f"Write a {m.group(2).lower()} about the {text_of(rng, 12)} to the {text_of(rng, 6)} team, "
                                          f"covering the {text_of(rng, 5)}.",
                              "evaluationCriteria": ["Required content: the three points are covered clearly",
                                                     "Structure, tone and clarity suit the format",
                                                     "Grammar and mechanics within the word limit"],
                              "modelAnswer": text_of(rng, size), "tags": ["x"]})
            return "```json\n" + json.dumps({"items": items}) + "\n```"
        if label == "rate" and "typed writing tasks" in system:
            n = len(re.findall(r"^\[\d+\]", user, re.M))
            r = [{"index": i, "label": "Medium", "rulesHit": ["x"], "expectedSeconds": 300, "reason": "r"} for i in range(n)]
            return "```json\n" + json.dumps({"ratings": r}) + "\n```"
        return super().ask(**kw)


PATTERN = {"company": "Acme", "role": "Software Engineer", "sources": [{"url": "u", "title": "t"}], "sections": [
    {"name": "Quantitative", "type": "MCQ_TEXT", "questions": 5, "minutes": 10, "topics": ["percentages"]},
    {"name": "English Writing", "type": "WRITING", "mode": "MIXED", "questions": 3, "minutes": 20, "topics": ["campus"]}]}


def test_plan_picks_formats_limits_and_marks():
    p = writing.build_writing_plan(PATTERN, (0, 100, 0))[0]
    assert [i["task"] for i in p["items"]] == ["EMAIL", "ESSAY", "EMAIL"]
    assert all(i["limit"] == (90, 120) and i["marks"] == 10 for i in p["items"])
    pat = {"sections": [{"name": "Email writing", "type": "WRITING", "questions": 2, "word_limit": {"min": 150, "max": 200}, "marks_each": 5}]}
    q = writing.build_writing_plan(pat, (50, 50, 0))[0]
    assert [i["task"] for i in q["items"]] == ["EMAIL", "EMAIL"]
    assert all(i["limit"] == (150, 200) and i["marks"] == 5 for i in q["items"])
    assert writing.section_tasks({"name": "Essay", "type": "WRITING"}, 2) == ["ESSAY", "ESSAY"]
    assert writing.section_tasks({"name": "x", "type": "WRITING", "mode": "memo"}, 1) == ["MEMO"]


def test_problems_enforce_word_limit_and_rubric():
    spec = {"limit": (90, 120)}
    ok = {"question": "x" * 80, "modelAnswer": " ".join(["word"] * 100),
          "evaluationCriteria": ["A concrete rubric line one", "A concrete rubric line two", "A concrete rubric line three"]}
    assert writing._problems(ok, spec) == []
    assert any("100 words" not in p and "limit is 90 to 120" in p for p in writing._problems(dict(ok, modelAnswer="short one"), spec))
    assert any("3 concrete" in p for p in writing._problems(dict(ok, evaluationCriteria=["only one line here ok"]), spec))


@pytest.fixture
def world(tmp_path):
    cfg, repo, origin = make_repo(tmp_path)
    fake = WritingFake()
    res = generate_papers(cfg, fake, CostTracker(cfg), PATTERN, (0, 100, 0), 1, "dev", True, log=lambda *_: None, coding=False)
    return cfg, repo, origin, res[0], fake


def test_paper_has_valid_writing_section_after_one_repair(world):
    cfg, _, _, res, fake = world
    p, rep = res["paper"], res["report"]
    sec = p["sections"][1]
    assert sec["type"] == "WRITING" and sec["config"] == {"gradedBy": "MANUAL", "responseFormat": "FREE_TEXT"}
    assert sec["timeLimitSeconds"] == 1200 and len(sec["questions"]) == 3
    q = sec["questions"][0]
    assert q["correctAnswer"] is None and q["wordLimit"] == {"min": 90, "max": 120} and q["marks"] == 10
    assert q["type"] == "EMAIL" and q["category"] == "Formal Email" and len(q["evaluationCriteria"]) == 3
    assert 90 <= len(q["modelAnswer"].split()) <= 120                      # the too-long first answer was repaired
    assert fake.designs == 2
    assert p["totalMarks"] == 5 + 30 and "requiresMicrophone" not in p["config"]
    assert rep["needsHuman"] == 0 and rep["repoValidator"]["ran"] and rep["repoValidator"]["ok"], rep["repoValidator"]["output"]


def test_review_pdf_and_publish_with_writing(world):
    cfg, repo, _, res, _ = world
    key = res["paper"]["paperKey"]
    v = review.view(cfg, key)
    w = [s for s in v["sections"] if s.get("kind") == "WRITING"][0]
    assert len(w["items"]) == 3 and w["items"][0]["wordLimit"] == {"min": 90, "max": 120}
    paper, rep = review.load(cfg, key)
    qk = paper["sections"][1]["questions"][0]["questionKey"]
    assert review.apply_changes(cfg, WritingFake(), CostTracker(cfg), key, {qk: "Hard"}, log=lambda *_: None)["results"] == []
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(build_pdf(paper))).pages)
    assert "90 to 120 words" in text and "Writing task. Marking criteria and model answer" in text
    out = pub.publish(cfg, [key], log=lambda *_: None)                      # no audio, so nothing to wait for
    assert out["validator"]["ok"] and sh(repo, "rev-list", "--count", "main..HEAD") == "0"
