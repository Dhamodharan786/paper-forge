"""The Claude Code backend (uses a Claude plan login instead of an API key) and the choice between backends."""
import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from paper_forge import claude as cl
from paper_forge.config import load
from paper_forge.cost import CapExceeded, CostTracker


def ok_json(result="hello", **usage):
    u = {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 50,
         "server_tool_use": {"web_search_requests": 0}}
    u.update(usage)
    return json.dumps({"is_error": False, "result": result, "usage": u})


class FakeRun:
    def __init__(self, stdout, returncode=0, stderr="", raises=None):
        self.stdout, self.returncode, self.stderr, self.raises, self.calls = stdout, returncode, stderr, raises, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raises:
            raise self.raises
        sysfile = cmd[cmd.index("--system-prompt-file") + 1]
        self.calls[-1] += (open(sysfile, encoding="utf-8").read(),)         # the file must exist while the process runs
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr, returncode=self.returncode)


@pytest.fixture
def cc(tmp_path, monkeypatch):
    cfg = replace(load(), cache_dir=tmp_path / "cache", claude_code_models={}, claude_code_effort={})
    monkeypatch.setattr(cl.shutil, "which", lambda name: "C:/bin/claude.exe" if name == "claude" else None)
    return cl.ClaudeCode(cfg, CostTracker(cfg)), cfg


def test_command_is_minimal_and_prompt_goes_by_stdin(cc, monkeypatch):
    c, cfg = cc
    run = FakeRun(ok_json("hi"))
    monkeypatch.setattr(cl.subprocess, "run", run)
    assert c.ask(label="x", model="claude-haiku-5-5", system="SYS", user="THE QUESTION") == "hi"
    cmd, kw, sys_text = run.calls[0]
    assert cmd[:2] == ["C:/bin/claude.exe", "-p"] and cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project" and "--allowedTools" not in cmd
    assert kw["input"] == "THE QUESTION" and "THE QUESTION" not in " ".join(cmd)      # long prompts never hit the command line
    assert sys_text.startswith("SYS") and "Do not use any tools" in sys_text
    assert not any(p.suffix == ".md" for p in c.workdir.iterdir())                    # temp prompt file removed


def test_web_search_enables_only_that_tool_and_limits_uses(cc, monkeypatch):
    c, _ = cc
    run = FakeRun(ok_json("found", server_tool_use={"web_search_requests": 3}))
    monkeypatch.setattr(cl.subprocess, "run", run)
    c.ask(label="r", model="claude-haiku-5-5", system="S", user="u",
          tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}])
    cmd, _, sys_text = run.calls[0]
    assert cmd[cmd.index("--tools") + 1] == "WebSearch" and cmd[cmd.index("--allowedTools") + 1] == "WebSearch"
    assert "at most 8 time(s)" in sys_text
    assert c.tracker.calls[0].searches == 3


def test_usage_is_tracked_and_cap_is_enforced(cc, monkeypatch):
    c, cfg = cc
    monkeypatch.setattr(cl.subprocess, "run", FakeRun(ok_json(input_tokens=1_000_000, cache_creation_input_tokens=0, output_tokens=0)))
    c.ask(label="a", model="claude-haiku-5-5", system="S", user="u")
    assert c.tracker.total == pytest.approx(0.10)                                    # 1M tokens at the Haiku input price
    c.tracker.cfg = replace(cfg, spend_cap_usd=0.05)
    with pytest.raises(CapExceeded):
        c.ask(label="b", model="claude-haiku-5-5", system="S", user="u")


def test_model_override_for_plans_without_opus(cc, monkeypatch):
    c, cfg = cc
    c.cfg = replace(cfg, claude_code_models={"claude-opus-5-5": "sonnet"})
    run = FakeRun(ok_json())
    monkeypatch.setattr(cl.subprocess, "run", run)
    c.ask(label="g", model="claude-opus-5-5", system="S", user="u")
    assert run.calls[0][0][run.calls[0][0].index("--model") + 1] == "sonnet"
    assert c.tracker.calls[0].model == "claude-opus-5-5"                              # still priced as the model asked for


def test_effort_per_model(cc, monkeypatch):
    c, cfg = cc
    c.cfg = replace(cfg, claude_code_effort={"claude-sonnet-5-5": "high"})
    run = FakeRun(ok_json())
    monkeypatch.setattr(cl.subprocess, "run", run)
    c.ask(label="r", model="claude-sonnet-5-5", system="S", user="u")
    c.ask(label="c", model="claude-haiku-5-5", system="S", user="u")
    assert run.calls[0][0][run.calls[0][0].index("--effort") + 1] == "high"
    assert "--effort" not in run.calls[1][0]                                         # other models keep the default


@pytest.mark.parametrize("text,hint", [("Not logged in · Please run /login", "sign in"),
                                       ("Claude usage limit reached", "usage limit"),
                                       ("There's an issue with the selected model", "[claude_code.models]")])
def test_failures_explain_what_to_do(cc, monkeypatch, text, hint):
    c, _ = cc
    monkeypatch.setattr(cl.subprocess, "run", FakeRun(json.dumps({"is_error": True, "result": text}), returncode=1))
    with pytest.raises(RuntimeError, match=hint):
        c.ask(label="x", model="claude-haiku-5-5", system="S", user="u")


def test_unreadable_output_and_timeout(cc, monkeypatch):
    c, _ = cc
    monkeypatch.setattr(cl.subprocess, "run", FakeRun("garbage", returncode=1, stderr="boom"))
    with pytest.raises(RuntimeError, match="unreadable"):
        c.ask(label="x", model="claude-haiku-5-5", system="S", user="u")
    monkeypatch.setattr(cl.subprocess, "run", FakeRun("", raises=subprocess.TimeoutExpired("claude", 1)))
    with pytest.raises(RuntimeError, match="did not answer"):
        c.ask(label="x", model="claude-haiku-5-5", system="S", user="u")


def test_backend_choice(monkeypatch):
    cfg = load()
    have_cli = lambda name: "C:/bin/claude.exe" if name == "claude" else None   # noqa: E731
    monkeypatch.delenv("PAPER_FORGE_BACKEND", raising=False)
    monkeypatch.setattr(cl.shutil, "which", have_cli)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cl.effective_backend(replace(cfg, backend="auto")) == "claude-code"          # a Pro user with no key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert cl.effective_backend(replace(cfg, backend="auto")) == "api"
    monkeypatch.setenv("PAPER_FORGE_BACKEND", "claude-code")
    assert cl.effective_backend(replace(cfg, backend="api")) == "claude-code"           # the environment wins
    monkeypatch.setenv("PAPER_FORGE_BACKEND", "nonsense")
    with pytest.raises(ValueError):
        cl.effective_backend(cfg)
    monkeypatch.delenv("PAPER_FORGE_BACKEND")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(cl.shutil, "which", lambda name: None)
    assert cl.effective_backend(replace(cfg, backend="auto")) == "api"                  # nothing available: api, which will explain
    with pytest.raises(RuntimeError, match="was not found"):
        cl.ClaudeCode(cfg, CostTracker(cfg))
