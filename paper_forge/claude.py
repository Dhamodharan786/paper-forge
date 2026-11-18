from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anthropic

from .config import Config
from .cost import CostTracker

MAX_PAUSE_TURNS = 6


class Claude:
    """Thin wrapper: one place that calls the API, tracks cost and enforces the spend cap."""

    def __init__(self, cfg: Config, tracker: CostTracker):
        self.cfg = cfg
        self.tracker = tracker
        # A key made for a user (sk-ant-usr-...) is not tied to a workspace, so the API needs the workspace id as well.
        ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
        self.client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws} if ws else None)  # key: ANTHROPIC_API_KEY

    def ask(self, *, label: str, model: str, system: str, user: str,
            tools: list | None = None, max_tokens: int = 16000) -> str:
        messages = [{"role": "user", "content": user}]
        resp = None
        for _ in range(MAX_PAUSE_TURNS):
            self.tracker.check()
            kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
            if tools:
                kwargs["tools"] = tools
            resp = self.client.messages.create(**kwargs)
            self.tracker.add(label, model, resp.usage)
            if resp.stop_reason == "pause_turn":  # server-side search paused; send it back to continue
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        if resp is None:
            raise RuntimeError("No response")
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined this request: {getattr(resp, 'stop_details', None)}")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("Reply was cut off at max_tokens. Raise max_tokens for this step.")
        return "".join(b.text for b in resp.content if b.type == "text")


BACKENDS = ("api", "claude-code")


def effective_backend(cfg: Config) -> str:
    """Which way to reach Claude: the API (needs a key) or Claude Code (uses your Claude plan login)."""
    kind = (os.environ.get("PAPER_FORGE_BACKEND") or cfg.backend or "auto").strip().lower()
    if kind == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "api"
        return "claude-code" if shutil.which("claude") else "api"
    if kind not in BACKENDS:
        raise ValueError(f"backend must be auto, api or claude-code, got {kind!r}")
    return kind


class ClaudeCode:
    """Same interface as Claude, but runs the `claude` command (Claude Code) with your own Claude login.

    Use it when you have a Pro or Max plan and no API key. Calls are made with every extra switched off (no MCP servers, no
    skills, no tools except web search when asked) so each call carries about 1,000 tokens of overhead instead of about 30,000.
    Usage counts against your plan's limits. The dollar figures are the list-price equivalent reported by the tool, not a bill.
    """

    TIMEOUT = 900

    def __init__(self, cfg: Config, tracker: CostTracker):
        self.cfg, self.tracker = cfg, tracker
        self.exe = shutil.which("claude")
        if not self.exe:
            raise RuntimeError("The `claude` command was not found. Install Claude Code, run `claude` once and sign in, "
                               "or set ANTHROPIC_API_KEY and use the api backend.")
        self.workdir = Path(cfg.cache_dir).parent / ".claude-run"
        self.workdir.mkdir(exist_ok=True)
        (self.workdir / "empty-mcp.json").write_text('{"mcpServers": {}}', encoding="ascii")

    def ask(self, *, label: str, model: str, system: str, user: str,
            tools: list | None = None, max_tokens: int = 16000) -> str:
        self.tracker.check()
        web = [t for t in (tools or []) if t.get("name") == "web_search"]
        if web:
            uses = web[0].get("max_uses")
            note = f"\n\nYou may use the WebSearch tool{f' at most {uses} time(s)' if uses else ''}. Do not use any other tool."
        else:
            note = "\n\nDo not use any tools."
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, dir=self.workdir, encoding="utf-8") as f:
            f.write(system + note)
            sysfile = f.name
        cmd = [self.exe, "-p", "--model", (self.cfg.claude_code_models or {}).get(model, model), "--output-format", "json",
               "--no-session-persistence", "--tools", "WebSearch" if web else "", "--disable-slash-commands",
               "--strict-mcp-config", "--mcp-config", str(self.workdir / "empty-mcp.json"),
               "--setting-sources", "project", "--system-prompt-file", sysfile]
        if web:
            cmd += ["--allowedTools", "WebSearch"]
        try:
            r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=self.TIMEOUT, cwd=self.workdir)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Claude Code did not answer within {self.TIMEOUT // 60} minutes ({label}).") from e
        finally:
            Path(sysfile).unlink(missing_ok=True)
        try:
            j = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError(f"Claude Code returned something unreadable (exit {r.returncode}): "
                               f"{(r.stderr or r.stdout).strip()[:400]}") from None
        text = j.get("result") or ""
        if j.get("is_error") or r.returncode != 0:
            low = text.lower()
            hint = ""
            if "not logged in" in low or "/login" in low:
                hint = " Run `claude` once and sign in with /login."
            elif "limit" in low or "usage" in low:
                hint = " Your Claude plan's usage limit was reached. Wait for the reset, or use fewer papers or --no-web."
            elif "model" in low:
                hint = (" Your plan may not include this model. In forge.toml under [claude_code.models] map it to one you have, "
                        "for example \"claude-opus-5-5\" = \"sonnet\".")
            raise RuntimeError(f"Claude Code failed: {text.strip()[:300]}{hint}")
        u = j.get("usage") or {}
        stu = u.get("server_tool_use") or {}
        self.tracker.add(label, model, SimpleNamespace(
            input_tokens=u.get("input_tokens", 0), cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=u.get("cache_read_input_tokens", 0), output_tokens=u.get("output_tokens", 0),
            server_tool_use=SimpleNamespace(web_search_requests=stu.get("web_search_requests", 0))))
        return text


def make_claude(cfg: Config, tracker: CostTracker):
    return ClaudeCode(cfg, tracker) if effective_backend(cfg) == "claude-code" else Claude(cfg, tracker)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict:
    """Return the last JSON object in the reply (fenced block preferred, else outermost braces)."""
    blocks = _FENCE.findall(text)
    if blocks:
        return json.loads(blocks[-1])
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object found in the reply")
    return json.loads(text[start:end + 1])
