from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .claude import Claude, extract_json
from .config import Config

# Fixed vocabulary so the gap check is a plain lookup, not a guess.
TYPES = [
    "MCQ_TEXT",          # aptitude, logical, verbal, technical: text question with options
    "MCQ_CODE_OUTPUT",   # read code / pseudocode, pick the output
    "CODING",            # write a program, judged by tests
    "AUDIO_LISTENING",   # listen, then answer
    "SPEAKING",          # record a spoken answer
    "MCQ_IMAGE",         # question or options are images (figures, charts as pictures)
    "VIDEO",             # video based question
    "WRITING",           # essay / email / free text
    "OTHER",
]

SYSTEM = f"""You research public information about a company's campus online-test pattern.

Your job is the PATTERN only: sections, number of questions, time, marking, topics, question styles.
Never copy or quote question text. Do not output any actual question.

Use web search. Prefer several independent sources, and prefer the newest. If sources disagree, say so.
Be honest about confidence: "high" only when two or more sources agree, "low" when only one thin source.

Classify every section into exactly one of these types: {", ".join(TYPES)}.

Reply with ONE JSON object in a ```json fenced block and nothing after it. Shape:
{{
  "company": str, "role": str, "year_or_date_seen": str|null,
  "sections": [{{"name": str, "type": one of the types, "questions": int, "minutes": int|null,
                 "topics": [str], "difficulty_note": str|null, "confidence": "high"|"medium"|"low",
                 "mode": for SPEAKING one of "read-aloud"|"listen-and-repeat"|"extempore"; for WRITING one of "EMAIL"|"ESSAY"|"LETTER"|"MEMO"|"REPORT"|"MIXED"; else null,
                 "word_limit": for WRITING {{"min": int, "max": int}} if the exam states one, else null,
                 "marks_each": for WRITING marks per task if stated, else null}}],
  "total_minutes": int|null, "negative_marking": str|null,
  "sources": [{{"url": str, "title": str, "date": str|null}}],
  "conflicts": [str], "notes": str
}}"""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def cache_path(cfg: Config, company: str, role: str) -> Path:
    return cfg.cache_dir / f"{slug(company)}__{slug(role)}.json"


def validate_pattern(p: dict) -> list[str]:
    """Return a list of problems. Empty list means the pattern is usable."""
    problems = []
    secs = p.get("sections")
    if not isinstance(secs, list) or not secs:
        return ["No sections found"]
    for i, s in enumerate(secs, 1):
        name = s.get("name") or f"section {i}"
        if s.get("type") not in TYPES:
            problems.append(f"{name}: unknown type {s.get('type')!r}")
        if not isinstance(s.get("questions"), int) or s["questions"] <= 0:
            problems.append(f"{name}: question count missing")
    if not p.get("sources"):
        problems.append("No sources listed")
    return problems


def research(cfg: Config, claude: Claude, company: str, role: str, refresh: bool = False) -> dict:
    path = cache_path(cfg, company, role)
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))

    tools = [{"type": cfg.search_tool, "name": "web_search", "max_uses": cfg.search_max_uses}]
    user = (f"Company: {company}\nRole: {role}\n"
            "Find the latest online-test pattern for this company and role "
            "(campus / university drive). Return the JSON described.")
    text = claude.ask(label="research", model=cfg.research_model, system=SYSTEM, user=user, tools=tools)
    pattern = extract_json(text)
    pattern["company"] = pattern.get("company") or company
    pattern["role"] = pattern.get("role") or role
    pattern["_fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    pattern["_problems"] = validate_pattern(pattern)

    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pattern, indent=2, ensure_ascii=False), encoding="utf-8")
    return pattern
