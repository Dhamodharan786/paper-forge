from __future__ import annotations

from . import prompts
from .claude import Claude, extract_json
from .config import Config
from .repo_index import question_text


def web_check(cfg: Config, claude: Claude, q: dict) -> dict:
    """One web search for one question. Returns {"verdict","similarity","url","note"}."""
    tools = [{"type": cfg.search_tool, "name": "web_search", "max_uses": 1}]
    user = "Question to check (do not answer it):\n" + question_text(q)
    try:
        text = claude.ask(label="web-check", model=cfg.checker_model, system=prompts.WEB_CHECK_SYSTEM,
                          user=user, tools=tools, max_tokens=4000)
        r = extract_json(text)
        return {"verdict": r.get("verdict", "no_match"), "similarity": int(r.get("similarity", 0)),
                "url": r.get("url"), "note": r.get("note", "")}
    except (ValueError, KeyError) as e:  # unreadable reply: treat as unchecked, never as a pass
        return {"verdict": "unchecked", "similarity": 0, "url": None, "note": f"web check unreadable: {e}"}


def rate(cfg: Config, claude: Claude, qs: list[dict], check_key: bool = False) -> list[dict]:
    """Blind difficulty rating in batches. Returns one rating dict per question (same order)."""
    out: list[dict] = []
    for i in range(0, len(qs), 20):
        batch = qs[i:i + 20]
        lines = []
        for k, q in enumerate(batch):
            opts = " | ".join(f"{a}. {b}" for a, b in q["options"].items())
            key = f"\nKeyed answer: {q['correctAnswer']}" if check_key else ""
            lines.append(f"[{k}] {q['question']}\n{opts}{key}")
        text = claude.ask(label="rate", model=cfg.checker_model, system=prompts.rater_system(check_key),
                          user="\n\n".join(lines), max_tokens=12000)
        got = {int(r["index"]): r for r in extract_json(text).get("ratings", [])}
        for k in range(len(batch)):
            r = got.get(k) or {"label": None, "rulesHit": [], "expectedSeconds": None, "reason": "not rated"}
            out.append(r)
    return out


def web_check_text(cfg: Config, claude: Claude, system: str, text: str) -> dict:
    """Same one-search check as web_check, for any text (used for coding problem statements)."""
    tools = [{"type": cfg.search_tool, "name": "web_search", "max_uses": 1}]
    try:
        reply = claude.ask(label="web-check", model=cfg.checker_model, system=system, user=text, tools=tools, max_tokens=4000)
        r = extract_json(reply)
        return {"verdict": r.get("verdict", "no_match"), "similarity": int(r.get("similarity", 0)),
                "url": r.get("url"), "note": r.get("note", "")}
    except (ValueError, KeyError) as e:
        return {"verdict": "unchecked", "similarity": 0, "url": None, "note": f"web check unreadable: {e}"}
