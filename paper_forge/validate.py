"""Run the repo's own validator on a draft paper, in a throwaway copy, so the real repo is untouched."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import Config
from .layout import company_dir


def validate_draft(cfg: Config, paper: dict, tree: str = "university") -> dict:
    slug = paper["companySlug"]
    validator = cfg.repo / "companies" / "tools" / "validate_placement_papers.py"
    if not validator.is_file():
        return {"ran": False, "ok": False, "output": f"validator not found at {validator}"}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        comp = root / "companies" / slug
        comp.mkdir(parents=True)
        src_dir = company_dir(cfg.repo, slug, tree)
        if src_dir is not None:
            company = json.loads((src_dir / "company.json").read_text(encoding="utf-8"))
        else:
            template = json.loads((cfg.repo / "companies" / "_template" / "company.json").read_text(encoding="utf-8"))
            company = template
            company["companySlug"] = slug
            company["companyName"] = paper["title"].split(" — ")[0]
            company["shortName"] = company["companyName"]
        rounds = company.get("rounds") or [{"roundKey": paper["roundKey"], "number": 1, "name": "Online Assessment",
                                           "testType": paper["testType"], "covers": "", "paperPool": []}]
        for r in rounds:  # other papers are not copied here, so only ours may be in a pool
            r["paperPool"] = []
        match = next((r for r in rounds if r.get("roundKey") == paper["roundKey"]), None)
        if match is None:
            match = {"roundKey": paper["roundKey"], "number": len(rounds) + 1, "name": "Online Assessment",
                     "testType": paper["testType"], "covers": "", "paperPool": []}
            rounds.append(match)
        match["paperPool"].append(paper["paperKey"])
        company["rounds"] = rounds
        (comp / "company.json").write_text(json.dumps(company), encoding="utf-8")
        (comp / f"{paper['paperKey']}.json").write_text(json.dumps(paper), encoding="utf-8")
        for sec in paper["sections"]:
            if sec["type"] == "CODING":
                for pslug in sec["questions"]:
                    src = cfg.out_dir / "problems" / pslug
                    if src.is_dir():
                        shutil.copytree(src, root / "problems" / pslug, dirs_exist_ok=True)
        for extra in ("manifest.json", "tags.json"):
            if (cfg.repo / extra).is_file():
                shutil.copy(cfg.repo / extra, root / extra)
        shutil.copytree(cfg.repo / "companies" / "_template", root / "companies" / "_template", dirs_exist_ok=True)
        r = subprocess.run([sys.executable, str(validator), "--repo-root", str(root)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (r.stdout + r.stderr).strip()
        return {"ran": True, "ok": r.returncode == 0, "output": out[-1500:]}
