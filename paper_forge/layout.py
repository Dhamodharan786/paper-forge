"""Where a company's papers live in fluentedge-coding-problems.

placement   -> companies/<slug>/
university  -> companies/university-exams/<folder>/   (folder comes from university-tracks.json, may differ from slug)
"""
from __future__ import annotations

import json
from pathlib import Path

TREES = ("university", "placement")
UNIVERSITY_ROOT = Path("companies") / "university-exams"
UNIVERSITY_INDEX = UNIVERSITY_ROOT / "university-tracks.json"


def university_folder(repo: Path, slug: str) -> str | None:
    idx = repo / UNIVERSITY_INDEX
    if not idx.is_file():
        return None
    for c in json.loads(idx.read_text(encoding="utf-8")).get("companies", []):
        if c.get("companySlug") == slug:
            return c["file"].rsplit("/", 1)[0]
    return None


def company_dir(repo: Path, slug: str, tree: str) -> Path | None:
    """Folder holding the company's company.json and papers, or None if the company is not in that tree."""
    if tree == "university":
        folder = university_folder(repo, slug)
        d = repo / UNIVERSITY_ROOT / folder if folder else None
    else:
        d = repo / "companies" / slug
    return d if d is not None and (d / "company.json").is_file() else None
