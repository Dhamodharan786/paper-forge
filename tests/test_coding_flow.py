"""A whole paper with coding problems: generate, review, PDF, publish to a branch."""
import io
import json

import pytest
from pypdf import PdfReader

from paper_forge import coding, publish as pub, review
from paper_forge.cost import CostTracker
from paper_forge.pdf import build_pdf
from paper_forge.pipeline import generate_papers
from test_coding import Fake as CodeFake, design_text
from test_m2 import SAMPLE, FakeClaude
from test_publish import make_repo, sh


class Both(FakeClaude):
    """MCQ calls go to the MCQ fake, coding calls to the coding fake."""

    def __init__(self, designs):
        super().__init__()
        self.code = CodeFake(designs)

    def ask(self, **kw):
        if kw["label"].startswith("coding"):
            return self.code.ask(**kw)
        return super().ask(**kw)


@pytest.fixture
def paper(tmp_path, monkeypatch):
    monkeypatch.setattr(coding, "BRUTE_SECONDS", 1.0)
    cfg, repo, origin = make_repo(tmp_path)
    fake = Both([
        design_text(key="token-pairs-one", title="Token Pairs One",
                    statement="# Token Pairs One\n\nA vending machine stores tokens. Count remainders.\n\n```\nInput:\n5 3\n```\n"),
        design_text(key="token-pairs-two", title="Lantern Bridge Count",
                    statement="# Lantern Bridge Count\n\nLanterns hang on a long bridge at dusk, and villagers pair them.\n")])
    res = generate_papers(cfg, fake, CostTracker(cfg), dict(SAMPLE, company="Acme"), (0, 100, 0), 1, "drop", False,
                          log=lambda *_: None)
    return cfg, repo, origin, res[0]


def test_generate_includes_verified_coding_section(paper):
    cfg, repo, origin, res = paper
    p, rep = res["paper"], res["report"]
    coding_sec = [s for s in p["sections"] if s["type"] == "CODING"][0]
    assert coding_sec["questions"] == ["token-pairs-one", "token-pairs-two"]
    assert coding_sec["config"] == {"problemSource": "fluentedge-coding-problems"}
    assert p["totalMarks"] == 15 + 100 + 100
    assert [c["key"] for c in rep["coding"]] == ["token-pairs-one", "token-pairs-two"]
    assert all(c["repoScripts"]["ok"] for c in rep["coding"]) and rep["needsHuman"] == 0
    assert rep["repoValidator"]["ran"] and rep["repoValidator"]["ok"], rep["repoValidator"]["output"]
    assert (cfg.out_dir / "problems" / "token-pairs-one" / "tests" / "hidden" / "09.out").is_file()


def test_review_and_pdf_show_coding(paper):
    cfg, _, _, res = paper
    key = res["paper"]["paperKey"]
    v = review.view(cfg, key)
    c = [s for s in v["sections"] if s.get("kind") == "CODING"][0]
    assert [p["title"] for p in c["problems"]] == ["Token Pairs One", "Lantern Bridge Count"] and c["problems"][0]["marks"] == 100
    paper, rep = review.load(cfg, key)
    text = "\n".join(pg.extract_text() for pg in PdfReader(io.BytesIO(build_pdf(paper, problems=review.coding_info(cfg, paper, rep)))).pages)
    assert "Lantern Bridge Count" in text and "hidden test cases" in text


def test_publish_adds_problems_and_manifest(paper):
    cfg, repo, origin, res = paper
    key = res["paper"]["paperKey"]
    out = pub.publish(cfg, [key], log=lambda *_: None)
    assert (repo / "problems" / "token-pairs-one" / "problem.json").is_file()
    manifest = json.loads((repo / "manifest.json").read_text(encoding="utf-8"))
    assert "token-pairs-two" in json.dumps(manifest)
    staged = sh(repo, "diff", "--cached", "--name-only").splitlines()
    assert "manifest.json" in staged and any(s.startswith("problems/token-pairs-one/") for s in staged)
    assert out["validator"]["ok"] and sh(repo, "rev-list", "--count", "main..HEAD") == "0"
    assert "REVIEW" in open(out["prDraft"], encoding="utf-8").read()


def test_publish_rolls_back_problems_on_failure(paper):
    cfg, repo, origin, res = paper
    key = res["paper"]["paperKey"]
    (cfg.out_dir / "problems" / "token-pairs-two" / "tests" / "hidden" / "01.out").write_text("wrong\n", encoding="utf-8")
    with pytest.raises(pub.PublishError, match="verify-solutions failed"):
        pub.publish(cfg, [key], log=lambda *_: None)
    assert sh(repo, "branch", "--show-current") == "my-feature"
    assert not (repo / "problems" / "token-pairs-one").exists()
    assert sh(repo, "status", "--porcelain", "--untracked-files=no") == ""
