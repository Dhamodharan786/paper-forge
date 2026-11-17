from __future__ import annotations

import json
import re

import anthropic

from .config import Config
from .cost import CostTracker

MAX_PAUSE_TURNS = 6


class Claude:
    """Thin wrapper: one place that calls the API, tracks cost and enforces the spend cap."""

    def __init__(self, cfg: Config, tracker: CostTracker):
        self.cfg = cfg
        self.tracker = tracker
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

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
