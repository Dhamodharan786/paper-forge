import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from paper_forge import publish as pub
from paper_forge.config import load
from paper_forge.cost import CostTracker
from paper_forge.pipeline import generate_papers
from test_m2 import SAMPLE, FakeClaude

REAL_REPO = load().repo


def sh(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(tmp_path):
    if not (REAL_REPO / "companies" / "tools" / "validate_placement_papers.py").is_file():
        pytest.skip("real repo not available for the validator")
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    sh(repo, "config", "user.email", "t@example.com")
    sh(repo, "config", "user.name", "Tester")
    uni = repo / "companies" / "university-exams"
    (uni / "acme").mkdir(parents=True)
    (repo / "companies" / "tools").mkdir(parents=True)
    for f in (REAL_REPO / "companies" / "tools").glob("*.py"):
        shutil.copy(f, repo / "companies" / "tools" / f.name)
    shutil.copytree(REAL_REPO / "companies" / "_template", repo / "companies" / "_template")
    (uni / "acme" / "company.json").write_text(json.dumps({
        "schemaVersion": 1, "companySlug": "acme", "companyName": "Acme", "shortName": "Acme", "displayOrder": 1,
        "status": "ACTIVE", "hiringTracks": [],
        "rounds": [{"roundKey": "online-assessment", "number": 1, "name": "OA", "testType": "ONLINE_ASSESSMENT",
                    "covers": "x", "paperPool": []}]}, indent=2) + "\n", encoding="utf-8")
    (uni / "university-tracks.json").write_text(json.dumps({"companies": [{
        "companySlug": "acme", "companyName": "Acme", "shortName": "Acme", "displayOrder": 1, "status": "ACTIVE",
        "hiringTracks": [], "file": "acme/company.json"}]}, indent=2) + "\n", encoding="utf-8")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-m", "init")
    sh(repo, "remote", "add", "origin", str(origin))
    sh(repo, "push", "-u", "origin", "main")
    for sub in ("scripts", "schema"):
        shutil.copytree(REAL_REPO / sub, repo / sub, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    shutil.copy(REAL_REPO / "tags.json", repo / "tags.json")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-m", "tooling")
    sh(repo, "push", "origin", "main")
    sh(repo, "switch", "-c", "my-feature")           # the user's own branch, as in real life
    return replace(load(), repo=repo, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache"), repo, origin


@pytest.fixture
def world(tmp_path):
    cfg, repo, origin = make_repo(tmp_path)
    p = dict(SAMPLE, company="Acme")
    fake = FakeClaude()
    res = generate_papers(cfg, fake, CostTracker(cfg), p, (0, 100, 0), 1, "dev", False, log=lambda *_: None, coding=False)
    return cfg, repo, origin, res[0]["paper"]["paperKey"]


def test_add_to_pool_keeps_formatting():
    t = '{\n  "rounds": [\n    {\n      "roundKey": "a",\n      "paperPool": [\n        "x-001"\n      ]\n    },\n' \
        '    {\n      "roundKey": "b",\n      "paperPool": []\n    }\n  ]\n}\n'
    out = pub.add_to_pool(t, "b", "y-002")
    assert json.loads(out)["rounds"][1]["paperPool"] == ["y-002"] and json.loads(out)["rounds"][0]["paperPool"] == ["x-001"]
    assert pub.add_to_pool(t, "a", "x-002").count('"x-001",\n        "x-002"') == 1
    with pytest.raises(pub.PublishError):
        pub.add_to_pool(t, "a", "x-001")


def test_branch_name():
    assert pub.branch_name("virtusa", ["v-007", "v-009"], "university") == "feat/virtusa-university-papers-007-009"
    assert pub.branch_name("virtusa", ["v-007"], "placement") == "feat/virtusa-papers-007"


def test_publish_creates_local_branch_from_origin_main(world):
    cfg, repo, origin, key = world
    (repo / "scratch.txt").write_text("untracked user file", encoding="utf-8")   # untracked files are fine
    res = pub.publish(cfg, [key], fetch=True, log=lambda *_: None)
    assert res["branch"] == "feat/acme-university-papers-001" and res["previousBranch"] == "my-feature"
    assert sh(repo, "branch", "--show-current") == res["branch"]
    paper = repo / "companies" / "university-exams" / "acme" / f"{key}.json"
    assert paper.is_file()
    assert key in json.loads((paper.parent / "company.json").read_text(encoding="utf-8"))["rounds"][0]["paperPool"]
    staged = sh(repo, "diff", "--cached", "--name-only").splitlines()
    assert f"companies/university-exams/acme/{key}.json" in staged and any(s.endswith("image-support.md") for s in staged)
    assert res["validator"]["ran"] and res["validator"]["ok"]
    assert sh(repo, "rev-list", "--count", "main..HEAD") == "0"         # not committed
    assert sh(origin, "branch", "--list") .strip().lstrip("* ") == "main"  # nothing pushed
    assert "feat/acme" in Path(res["prDraft"]).name.replace("_", "/") and "Sign-off" in Path(res["prDraft"]).read_text(encoding="utf-8")
    assert "RESTART" in pub.preview_instructions(cfg, res)


def test_commit_flag_commits_locally_only(world):
    cfg, repo, origin, key = world
    res = pub.publish(cfg, [key], commit=True, log=lambda *_: None)
    assert res["committed"] and sh(repo, "rev-list", "--count", "main..HEAD") == "1"
    assert "feat/acme-university-papers-001" not in sh(origin, "branch", "--list")


def test_dirty_tracked_files_block_unless_worktree(world, tmp_path):
    cfg, repo, _, key = world
    f = repo / "companies" / "university-exams" / "acme" / "company.json"
    f.write_text(f.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(pub.PublishError, match="uncommitted"):
        pub.publish(cfg, [key], log=lambda *_: None)
    assert sh(repo, "branch", "--show-current") == "my-feature"           # nothing was touched
    wt = tmp_path / "wt"
    res = pub.publish(cfg, [key], worktree=wt, log=lambda *_: None)
    assert (wt / "companies" / "university-exams" / "acme" / f"{key}.json").is_file()
    assert sh(repo, "branch", "--show-current") == "my-feature"           # user's checkout untouched
    assert res["worktree"]


def test_flagged_draft_blocked(world):
    cfg, repo, _, key = world
    rp = cfg.out_dir / f"{key}.report.json"
    rep = json.loads(rp.read_text(encoding="utf-8"))
    rep["needsHuman"] = 2
    rp.write_text(json.dumps(rep), encoding="utf-8")
    with pytest.raises(pub.PublishError, match="need a human"):
        pub.publish(cfg, [key], log=lambda *_: None)
    assert pub.publish(cfg, [key], allow_flagged=True, log=lambda *_: None)["branch"]


def test_failure_rolls_everything_back(world):
    cfg, repo, _, key = world
    paper_file = repo / "companies" / "university-exams" / "acme" / f"{key}.json"
    sh(repo, "switch", "main")
    paper_file.write_text("{}", encoding="utf-8")                         # same file already on origin/main
    sh(repo, "add", "-A")
    sh(repo, "commit", "-m", "someone already added it")
    sh(repo, "push", "origin", "main")
    sh(repo, "switch", "my-feature")
    with pytest.raises(pub.PublishError, match="already exists"):
        pub.publish(cfg, [key], log=lambda *_: None)
    assert sh(repo, "branch", "--show-current") == "my-feature"
    assert "feat/acme-university-papers-001" not in sh(repo, "branch", "--list")
    assert sh(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_unknown_company_not_invented(world):
    cfg, repo, _, key = world
    paper = json.loads((cfg.out_dir / f"{key}.json").read_text(encoding="utf-8"))
    paper["companySlug"] = "nobody"
    (cfg.out_dir / f"{key}.json").write_text(json.dumps(paper), encoding="utf-8")
    with pytest.raises(pub.PublishError, match="not in the university tree"):
        pub.publish(cfg, [key], log=lambda *_: None)
    assert sh(repo, "branch", "--show-current") == "my-feature"
