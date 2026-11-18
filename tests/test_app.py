"""The web app server: security, research and run jobs, gap report with estimate, publish, sign-off."""
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from paper_forge.config import load
from paper_forge.server import State, make_handler
from test_m2 import FakeClaude
from test_publish import make_repo, sh

PATTERN = {"company": "Acme", "role": "Software Engineer", "year_or_date_seen": "2026",
           "sections": [{"name": "Quantitative", "type": "MCQ_TEXT", "questions": 5, "minutes": 10, "topics": ["percentages"], "confidence": "high"},
                        {"name": "Figures", "type": "MCQ_IMAGE", "questions": 2, "minutes": 5, "topics": [], "confidence": "low"}],
           "total_minutes": 15, "negative_marking": None, "sources": [{"url": "https://example.com/a", "title": "A", "date": None}],
           "conflicts": ["Sources disagree on the number of aptitude questions"], "notes": ""}


class AppFake(FakeClaude):
    def __init__(self, delay=0.0):
        super().__init__()
        self.delay = delay

    def ask(self, **kw):
        if self.delay:
            time.sleep(self.delay)
        if kw["label"] == "research":
            return "```json\n" + json.dumps(PATTERN) + "\n```"
        return super().ask(**kw)


@pytest.fixture
def app(tmp_path):
    cfg, repo, origin = make_repo(tmp_path)
    fake = AppFake()
    state = State(cfg, claude_factory=lambda c, t: fake)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, headers=None):
        h = {"Content-Type": "application/json", "X-Paper-Forge": "1"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(), h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait(job, timeout=120):
        t0, since = time.time(), 0
        while time.time() - t0 < timeout:
            _, j = call(f"/api/job?id={job}&since={since}")
            if j["status"] != "running":
                return j
            time.sleep(0.2)
        raise AssertionError("job did not finish")

    yield {"cfg": cfg, "repo": repo, "state": state, "fake": fake, "call": call, "wait": wait, "base": base, "srv": srv}
    srv.shutdown()


def test_only_localhost_and_marked_posts_are_accepted(app):
    port = app["srv"].server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("GET", "/api/app", headers={"Host": "evil.example.com"})
    assert c.getresponse().status == 403
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("POST", "/api/cap", body=json.dumps({"usd": 20}), headers={"Content-Type": "application/json"})
    assert c.getresponse().status == 403                                   # no X-Paper-Forge header
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("POST", "/api/cap", body=json.dumps({"usd": 20}), headers={"Content-Type": "text/plain", "X-Paper-Forge": "1"})
    assert c.getresponse().status == 403                                   # not JSON
    assert app["call"]("/api/cap", {"usd": 20}) == (200, {"capUsd": 20.0})
    assert app["call"]("/api/cap", {"usd": 100000})[0] == 400


def test_status_and_pages(app, monkeypatch):
    monkeypatch.setenv("PAPER_FORGE_BACKEND", "api")                   # the API-key path, whatever forge.local.toml says
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-usr-test")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    code, a = app["call"]("/api/app")
    assert code == 200 and a["hasKey"] and a["needsWorkspaceId"] and a["models"]["generator"] == load().generator_model
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_x")
    assert app["call"]("/api/app")[1]["needsWorkspaceId"] is False
    html = urllib.request.urlopen(app["base"] + "/").read().decode()
    assert "Specialist Programmer" in html and "Start research" in html and "Lock plan and generate" in html


def test_research_then_gap_with_estimate(app):
    call, wait = app["call"], app["wait"]
    assert call("/api/gap?company=Acme&role=Software+Engineer")[0] == 400            # nothing researched yet
    code, r = call("/api/research", {"company": "Acme", "role": "Software Engineer"})
    j = wait(r["job"])
    assert code == 200 and j["status"] == "done" and j["result"]["sections"][1]["type"] == "MCQ_IMAGE"
    code, g = call("/api/gap?company=Acme&role=Software+Engineer&split=30/50/20&papers=3&missing=dev")
    assert code == 200
    assert [x["type"] for x in g["report"]["missing"]] == ["MCQ_IMAGE"] and g["pattern"]["conflicts"]
    assert g["counts"]["mcq"] == 7 and g["plan"][0]["easy"] + g["plan"][0]["medium"] + g["plan"][0]["hard"] == 5
    es = g["estimate"]
    assert es["papers"] == 3 and es["totalUsd"] == pytest.approx(es["perPaperUsd"] * 3, abs=0.02) and es["researchUsdAlreadySpent"]
    assert call("/api/gap?company=Acme&role=Software+Engineer&split=50/50/50")[0] == 400   # does not add to 100
    drop = call("/api/gap?company=Acme&role=Software+Engineer&missing=drop")[1]["counts"]["mcq"]
    assert drop == 5                                                                  # image section left out


def test_run_then_publish_and_signoff_flow(app):
    call, wait = app["call"], app["wait"]
    wait(call("/api/research", {"company": "Acme", "role": "Software Engineer"})[1]["job"])
    code, r = call("/api/run", {"company": "Acme", "role": "Software Engineer", "papers": 1, "split": "0/100/0", "missing": "drop",
                                "web": False, "coding": False, "media": False, "writing": False})
    assert code == 200
    j = wait(r["job"])
    assert j["status"] == "done", j["error"]
    key = j["result"]["keys"][0]
    assert any("saved out/" in line for line in call(f"/api/job?id={r['job']}")[1]["lines"])
    assert call("/api/drafts")[1]["drafts"] == [key] and call(f"/api/paper?key={key}")[1]["totalMarks"] == 5

    assert call("/api/publish-state")[1] == {"record": None}
    code, p = call("/api/publish", {"keys": [key]})
    assert code == 200 and p["result"]["branch"] == "feat/acme-university-papers-001" and "RESTART" in p["preview"]
    code, s = call("/api/publish-state")
    assert s["record"]["branch"] == p["result"]["branch"] and len(s["status"]["blockers"]) == 2

    assert call("/api/signoff", {"ref": "latest", "role": "developer", "name": "Asha Rao"})[0] == 400      # not confirmed
    assert call("/api/signoff", {"ref": "latest", "role": "developer", "name": "Asha Rao", "confirm": True})[0] == 200
    code, e = call("/api/signoff", {"ref": "latest", "role": "tester", "name": "asha rao", "confirm": True})
    assert code == 400 and "two different people" in e["error"]
    code, ok = call("/api/signoff", {"ref": "latest", "role": "tester", "name": "Vikram Shah", "confirm": True})
    assert code == 200 and ok["blockers"] == []
    # the PR step needs gh; with nothing allowed it asks permission for the commit and stops there
    code, pr = call("/api/pr", {"ref": "latest", "allow": []})
    assert code in (200, 400)
    if code == 200:
        assert pr.get("stopped") and sh(app["repo"], "rev-list", "--count", "main..HEAD") == "0"


def test_a_second_job_is_refused_while_one_runs(app):
    app["fake"].delay = 0.4
    call = app["call"]
    first = call("/api/research", {"company": "Acme", "role": "Software Engineer"})
    assert first[0] == 200
    second = call("/api/research", {"company": "Acme", "role": "Other"})
    assert second[0] == 409 and "still running" in second[1]["error"]
    app["wait"](first[1]["job"])
    assert call("/api/research", {"company": "Acme", "role": "Other"})[0] == 200


def test_run_requires_research_first(app):
    code, e = app["call"]("/api/run", {"company": "Nobody", "role": "None"})
    assert code == 400 and "Research" in e["error"]
