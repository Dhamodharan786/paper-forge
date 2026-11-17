"""Sign-off and PR: nothing is committed, pushed or opened without both sign-offs and an explicit yes for each step."""
import json
from argparse import Namespace

import pytest

from paper_forge import cli, pr as prm, publish as pub
from test_publish import make_repo, sh, SAMPLE, FakeClaude, CostTracker, generate_papers   # noqa: F401


@pytest.fixture
def pubd(tmp_path):
    """A published (not committed) branch in a throwaway repo with a real bare origin."""
    cfg, repo, origin = make_repo(tmp_path)
    res = generate_papers(cfg, FakeClaude(), CostTracker(cfg), dict(SAMPLE, company="Acme"), (0, 100, 0), 1, "drop", False,
                          log=lambda *_: None, coding=False)
    out = pub.publish(cfg, [res[0]["paper"]["paperKey"]], log=lambda *_: None)
    return cfg, repo, origin, out


class FakeGh:
    def __init__(self, existing=None, auth_ok=True):
        self.calls, self.existing, self.auth_ok = [], existing or [], auth_ok

    def __call__(self, cwd, *args):
        self.calls.append(args)
        if args[:2] == ("auth", "status"):
            if not self.auth_ok:
                raise prm.PrError("gh auth status failed: not logged in")
            return "ok"
        if args[:2] == ("pr", "list"):
            return json.dumps(self.existing)
        if args[:2] == ("pr", "create"):
            return "https://github.com/example/papers/pull/999"
        raise AssertionError(args)

    def created(self):
        return [c for c in self.calls if c[:2] == ("pr", "create")]


class Yes:
    def __init__(self, answers=None):
        self.asked, self.answers = [], answers or {}

    def __call__(self, kind, what):
        self.asked.append(kind)
        return self.answers.get(kind, True)


def sign_both(cfg, ref="latest", dev="Asha Rao", qa="Vikram Shah"):
    rec = prm.load_record(cfg, ref)
    prm.add_signoff(cfg, rec, "developer", dev)
    prm.add_signoff(cfg, rec, "tester", qa)
    return prm.load_record(cfg, ref)


def origin_branches(origin):
    return sh(origin, "branch", "--list")


def test_publish_leaves_a_record_and_blocks_until_signed(pubd):
    cfg, _, _, out = pubd
    rec = prm.load_record(cfg, "latest")
    assert rec["branch"] == out["branch"] and rec["signoffs"] == {} and rec["keys"]
    assert prm.load_record(cfg, rec["keys"][0])["branch"] == rec["branch"]          # also findable by paper key
    assert prm.readiness(rec) == ["no developer sign-off yet", "no tester sign-off yet"]
    with pytest.raises(prm.PrError, match="Not ready"):
        prm.open_pr(cfg, "latest", Yes(), gh_fn=FakeGh())


def test_two_different_people_must_sign(pubd):
    cfg, *_ = pubd
    rec = prm.load_record(cfg, "latest")
    prm.add_signoff(cfg, rec, "developer", "Asha Rao")
    with pytest.raises(prm.PrError, match="two different people"):
        prm.add_signoff(cfg, rec, "tester", " asha rao ")
    with pytest.raises(prm.PrError):
        prm.add_signoff(cfg, rec, "manager", "Someone")
    with pytest.raises(prm.PrError):
        prm.add_signoff(cfg, rec, "tester", "x")
    prm.add_signoff(cfg, rec, "tester", "Vikram Shah")
    assert prm.readiness(prm.load_record(cfg, "latest")) == []


def test_changing_the_papers_makes_signoffs_stale(pubd):
    cfg, repo, _, out = pubd
    sign_both(cfg)
    paper = repo / out["paths"][0]
    paper.write_text(paper.read_text(encoding="utf-8") + "\n ", encoding="utf-8")
    left = prm.readiness(prm.load_record(cfg, "latest"))
    assert len(left) == 2 and all("stale" in x for x in left)
    with pytest.raises(prm.PrError, match="stale"):
        prm.open_pr(cfg, "latest", Yes(), gh_fn=FakeGh())


def test_full_flow_asks_before_each_step_and_pushes_a_fresh_branch(pubd):
    cfg, repo, origin, out = pubd
    sign_both(cfg)
    yes, gh = Yes(), FakeGh()
    done = prm.open_pr(cfg, "latest", yes, gh_fn=gh, log=lambda *_: None)
    assert yes.asked == ["commit", "push", "pr"]
    assert done == {"committed": True, "pushed": True, "prUrl": "https://github.com/example/papers/pull/999"}
    assert sh(repo, "rev-list", "--count", "main..HEAD") == "1" and out["branch"] in origin_branches(origin)
    assert sh(repo, "status", "--porcelain", "--untracked-files=no") == ""
    create = gh.created()[0]
    assert create[create.index("--base") + 1] == "main" and create[create.index("--head") + 1] == out["branch"]
    body = open(create[create.index("--body-file") + 1], encoding="utf-8").read()
    assert "- [x] Developer ran the backend" in body and "Asha Rao" in body and "Vikram Shah" in body
    assert "- [ ]" not in body and "Nothing is merged automatically" in body
    assert prm.load_record(cfg, "latest")["prUrl"].endswith("/999")


def test_signoffs_survive_the_commit_step(pubd):
    cfg, *_ = pubd
    rec = sign_both(cfg)
    with pytest.raises(prm.PrAborted):
        prm.open_pr(cfg, "latest", Yes({"push": False}), gh_fn=FakeGh(), log=lambda *_: None)
    assert prm.readiness(prm.load_record(cfg, "latest")) == []      # committing did not change the content


@pytest.mark.parametrize("refuse,committed,pushed,created", [("commit", 0, False, 0), ("push", 1, False, 0), ("pr", 1, True, 0)])
def test_a_no_stops_everything_after_it(pubd, refuse, committed, pushed, created):
    cfg, repo, origin, out = pubd
    sign_both(cfg)
    gh = FakeGh()
    with pytest.raises(prm.PrAborted):
        prm.open_pr(cfg, "latest", Yes({refuse: False}), gh_fn=gh, log=lambda *_: None)
    assert int(sh(repo, "rev-list", "--count", "main..HEAD")) == committed
    assert (out["branch"] in origin_branches(origin)) is pushed and len(gh.created()) == created


def test_never_pushes_to_a_merged_branch(pubd):
    cfg, repo, origin, out = pubd
    sign_both(cfg)
    yes = Yes()
    with pytest.raises(prm.PrError, match="already merged"):
        prm.open_pr(cfg, "latest", yes, gh_fn=FakeGh(existing=[{"number": 5, "state": "MERGED", "url": "u"}]), log=lambda *_: None)
    assert yes.asked == [] and out["branch"] not in origin_branches(origin)


def test_existing_open_pr_is_updated_not_duplicated(pubd):
    cfg, repo, origin, out = pubd
    sign_both(cfg)
    gh, yes = FakeGh(existing=[{"number": 7, "state": "OPEN", "url": "https://x/pull/7"}]), Yes()
    done = prm.open_pr(cfg, "latest", yes, gh_fn=gh, log=lambda *_: None)
    assert yes.asked == ["commit", "push"] and gh.created() == [] and done["prUrl"] == "https://x/pull/7"


def test_refuses_when_branch_not_checked_out_or_gh_not_logged_in(pubd):
    cfg, repo, *_ = pubd
    sign_both(cfg)
    with pytest.raises(prm.PrError, match="not logged in"):
        prm.open_pr(cfg, "latest", Yes(), gh_fn=FakeGh(auth_ok=False), log=lambda *_: None)
    sh(repo, "commit", "-m", "wip")
    sh(repo, "switch", "my-feature")
    with pytest.raises(prm.PrError, match="not on"):
        prm.open_pr(cfg, "latest", Yes(), gh_fn=FakeGh(), log=lambda *_: None)


def test_cli_confirm_needs_a_flag_when_not_interactive(capsys):
    confirm = cli._confirm_factory(Namespace(yes_commit=False, yes_push=True, yes_pr=False))
    assert confirm("push", "git push") is True
    assert confirm("commit", "commit things") is False and "--yes-commit" in capsys.readouterr().out


def test_cli_signoff_and_status(pubd, monkeypatch, capsys):
    cfg, *_ = pubd
    monkeypatch.setattr(cli, "load", lambda: cfg)
    assert cli.main(["signoff", "latest", "--role", "developer", "--name", "Asha Rao"]) == 2        # no --yes, not a terminal
    assert cli.main(["signoff", "latest", "--role", "developer", "--name", "Asha Rao", "--yes"]) == 0
    assert cli.main(["signoff", "latest", "--role", "tester", "--name", "Asha Rao", "--yes"]) == 1   # same person
    assert cli.main(["status"]) == 1 and "no tester sign-off" in capsys.readouterr().out
    assert cli.main(["signoff", "latest", "--role", "tester", "--name", "Vikram Shah", "--yes"]) == 0
    assert cli.main(["status"]) == 0 and "Ready for a PR." in capsys.readouterr().out
