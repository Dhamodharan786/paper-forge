import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from pypdf import PdfReader

from paper_forge import review
from paper_forge.cost import CostTracker
from paper_forge.pdf import build_pdf
from paper_forge.pipeline import generate_papers
from paper_forge.server import State, make_handler
from test_m2 import SAMPLE, FakeClaude, make_cfg


class LevelFake(FakeClaude):
    """Rater says whatever level the test set; everything else as in FakeClaude."""
    level = "Medium"

    def ask(self, **kw):
        if kw["label"] == "rate":
            n = kw["user"].count("\n\n") + 1
            r = [{"index": i, "label": self.level, "rulesHit": ["x"], "expectedSeconds": 50, "reason": "r"} for i in range(n)]
            return "```json\n" + json.dumps({"ratings": r}) + "\n```"
        return super().ask(**kw)


@pytest.fixture
def draft(tmp_path):
    cfg = make_cfg(tmp_path)
    fake = LevelFake()
    res = generate_papers(cfg, fake, CostTracker(cfg), SAMPLE, (0, 100, 0), 1, "drop", True, log=lambda *_: None, coding=False)
    return cfg, fake, res[0]["paper"]["paperKey"]


def test_list_and_view(draft):
    cfg, _, key = draft
    assert review.list_drafts(cfg) == [key]
    v = review.view(cfg, key)
    assert sum(len(s["questions"]) for s in v["sections"]) == 15
    assert v["requested"]["Medium"] == 15 and v["sections"][0]["questions"][0]["status"] in ("Original", "Rewritten")


def test_apply_changes_rewrites_checks_and_saves(draft):
    cfg, fake, key = draft
    paper, _ = review.load(cfg, key)
    qk = paper["sections"][0]["questions"][2]["questionKey"]
    before = paper["sections"][0]["questions"][2]["question"]
    fake.level = "Hard"
    out = review.apply_changes(cfg, fake, CostTracker(cfg), key, {qk: "Hard"}, log=lambda *_: None)
    assert out["results"][0]["ok"] and out["results"][0]["to"] == "Hard"
    after, report = review.load(cfg, key)
    q = after["sections"][0]["questions"][2]
    assert q["questionKey"] == qk and q["question"] != before and q["complexity"] == "Hard"
    assert report["requestedSplit"] == {"Easy": 0, "Medium": 14, "Hard": 1}
    assert report["deliveredSplit"]["Hard"] == 1 and report["editLog"][0]["changes"] == {qk: "Hard"}
    assert out["view"]["sections"][0]["questions"][2]["status"] == "Edited"


def test_same_level_is_a_no_op_and_bad_level_rejected(draft):
    cfg, fake, key = draft
    paper, _ = review.load(cfg, key)
    qk = paper["sections"][0]["questions"][0]["questionKey"]
    out = review.apply_changes(cfg, fake, CostTracker(cfg), key, {qk: "Medium"}, log=lambda *_: None)
    assert out["results"] == []
    with pytest.raises(ValueError):
        review.apply_changes(cfg, fake, CostTracker(cfg), key, {qk: "Impossible"})


def test_path_traversal_rejected(draft):
    cfg, _, _ = draft
    with pytest.raises(ValueError):
        review.load(cfg, "../secret")


def test_pdf_has_questions_and_answer_key(draft):
    cfg, _, key = draft
    paper, _ = review.load(cfg, key)
    paper["sections"][0]["questions"][0]["question"] = "Price is ₹66 per kg & 5 < 6.\n    indented line"
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(build_pdf(paper))).pages)
    assert paper["title"] in text and "Answer key and working" in text and "indented line" in text
    text2 = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(build_pdf(paper, with_answers=False))).pages)
    assert "Answer key" not in text2


def test_http_flow(draft):
    cfg, fake, key = draft
    fake.level = "Easy"
    state = State(cfg, claude_factory=lambda c, t: fake)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert b"Paper Forge review" in urllib.request.urlopen(base + "/").read()
        assert json.load(urllib.request.urlopen(base + "/api/drafts"))["drafts"] == [key]
        v = json.load(urllib.request.urlopen(f"{base}/api/paper?key={key}"))
        qk = v["sections"][0]["questions"][0]["questionKey"]
        req = urllib.request.Request(base + "/api/generate", json.dumps({"key": key, "changes": {qk: "Easy"}}).encode(),
                                     {"Content-Type": "application/json"})
        out = json.load(urllib.request.urlopen(req))
        assert out["results"][0]["ok"] and out["view"]["delivered"]["Easy"] == 1
        pdf = urllib.request.urlopen(f"{base}/api/pdf?key={key}")
        assert pdf.headers["Content-Type"] == "application/pdf" and pdf.read(4) == b"%PDF"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/api/paper?key=../x")
        assert e.value.code == 400
    finally:
        srv.shutdown()
