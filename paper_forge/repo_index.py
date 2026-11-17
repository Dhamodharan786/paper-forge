"""Free originality checks against every question already in the repo (no tokens)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def normalize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def masked(text: str) -> list[str]:
    """Same text with every number replaced, so 'same story, new numbers' still matches."""
    return normalize(_NUM.sub(" N ", text))


def shingles(words: list[str], n: int = 4) -> set[str]:
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def fingerprint(text: str) -> str:
    return hashlib.sha1(" ".join(masked(text)).encode()).hexdigest()


def question_text(q: dict) -> str:
    opts = q.get("options") or {}
    return (q.get("question") or "") + " " + " ".join(str(v) for v in opts.values())


@dataclass
class Entry:
    qid: str
    where: str
    plain: set
    mask: set
    fp: str
    stem_mask: set
    stem_fp: str


def _walk(node, where, out):
    if isinstance(node, dict):
        if isinstance(node.get("question"), str) and len(node["question"]) > 15:
            out.append((node.get("questionKey") or node.get("id") or "?", where, node))
        if isinstance(node.get("audioTranscript"), str) and len(node["audioTranscript"]) > 15:
            out.append((node.get("questionKey") or "transcript", where, {"question": node["audioTranscript"]}))
        for v in node.values():
            _walk(v, where, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, where, out)


class RepoIndex:
    def __init__(self, repo: Path):
        self.entries: list[Entry] = []
        self.fps: dict[str, Entry] = {}
        found: list[tuple[str, str, dict]] = []
        for sub in ("companies", "question-banks"):
            base = repo / sub
            if not base.is_dir():
                continue
            for f in base.rglob("*.json"):
                if f.name in ("company.json", "company-tracks.json") or "_template" in f.parts:
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                _walk(data, str(f.relative_to(repo)).replace("\\", "/"), found)
        for qid, where, node in found:
            self._put(qid, where, node)

    def _put(self, qid: str, where: str, q: dict) -> None:
        text, stem = question_text(q), q.get("question") or ""
        e = Entry(qid, where, shingles(normalize(text)), shingles(masked(text)), fingerprint(text),
                  shingles(masked(stem)), fingerprint(stem))
        self.entries.append(e)
        self.fps.setdefault(e.stem_fp, e)

    def add(self, qid: str, where: str, q: dict) -> None:
        """Remember a question generated in this run, so sibling papers cannot repeat it either."""
        self._put(qid, where, q)

    def check(self, q: dict) -> dict:
        """q is a question dict. Compares the whole question and the stem alone, with and without numbers,
        so a copy with reworded options or new numbers is still caught."""
        text, stem = question_text(q), q.get("question") or ""
        plain, mask, smask = shingles(normalize(text)), shingles(masked(text)), shingles(masked(stem))
        best, best_e = 0.0, None
        for e in self.entries:
            s = max(jaccard(plain, e.plain), jaccard(mask, e.mask), jaccard(smask, e.stem_mask))
            if s > best:
                best, best_e = s, e
        hit = self.fps.get(fingerprint(stem))
        return {
            "maxRepoSimilarity": round(best, 3),
            "closest": f"{best_e.qid} ({best_e.where})" if best_e else None,
            "fingerprintCollision": bool(hit),
            "fingerprintWith": f"{hit.qid} ({hit.where})" if hit else None,
        }
