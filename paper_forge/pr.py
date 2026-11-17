"""Sign-off and pull request.

The only code in Paper Forge that can commit, push or open a PR, and it never does so without an explicit yes for EACH
action. It refuses until a developer AND a tester (two different people) have signed off, and a sign-off goes stale if the
papers change afterwards. It never pushes to a branch whose PR has already been merged.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from .config import Config

ROLES = ("developer", "tester")
STATEMENTS = {
    "developer": "I ran the backend and frontend on this branch and confirmed the papers load and work.",
    "tester": "I tested these papers and confirm them.",
}
DRAFT_LINES = {"developer": r"^- \[ \] Developer[^\n]*$", "tester": r"^- \[ \] Tester[^\n]*$"}


class PrError(RuntimeError):
    pass


class PrAborted(RuntimeError):
    """The user said no. Nothing after this point was done."""


# ------------------------------------------------------------------------------------------------ records
def records_dir(cfg: Config) -> Path:
    return cfg.out_dir / "publish"


def save_record(cfg: Config, rec: dict) -> Path:
    d = records_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    p = d / (rec["branch"].replace("/", "_") + ".json")
    p.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def load_record(cfg: Config, ref: str) -> dict:
    """ref = branch name, paper key, or 'latest'."""
    d = records_dir(cfg)
    files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime) if d.is_dir() else []
    if not files:
        raise PrError("Nothing has been published yet. Run `publish` first.")
    recs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    if ref == "latest":
        return recs[-1]
    for r in reversed(recs):
        if r["branch"] == ref or ref in r.get("keys", []):
            return r
    raise PrError(f"No published branch matches {ref!r}. Known: " + ", ".join(r["branch"] for r in recs))


def content_hash(target: Path, paths: list[str]) -> str:
    """Hash of every file the branch adds (directories included), so any later change is detected."""
    h = hashlib.sha256()
    for rel in sorted(paths):
        p = target / rel
        files = sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p]
        for f in files:
            h.update(str(f.relative_to(target)).replace("\\", "/").encode())
            h.update(f.read_bytes() if f.is_file() else b"<missing>")
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ sign-off
def add_signoff(cfg: Config, rec: dict, role: str, name: str, note: str = "") -> dict:
    if role not in ROLES:
        raise PrError(f"role must be one of {ROLES}")
    name = name.strip()
    if len(name) < 2:
        raise PrError("Give the person's real name.")
    other = next(r for r in ROLES if r != role)
    prev = rec.get("signoffs", {}).get(other)
    if prev and prev["name"].strip().lower() == name.lower():
        raise PrError(f"The developer and the tester must be two different people ({name!r} already signed as {other}).")
    rec.setdefault("signoffs", {})[role] = {
        "name": name, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "statement": STATEMENTS[role], "note": note,
        "contentHash": content_hash(Path(rec["target"]), rec["paths"])}
    save_record(cfg, rec)
    return rec["signoffs"][role]


def readiness(rec: dict) -> list[str]:
    """What still blocks the PR. Empty list = ready."""
    bad = []
    cur = None
    for role in ROLES:
        s = rec.get("signoffs", {}).get(role)
        if not s:
            bad.append(f"no {role} sign-off yet")
            continue
        cur = cur or content_hash(Path(rec["target"]), rec["paths"])
        if s["contentHash"] != cur:
            bad.append(f"the {role} sign-off is stale: the papers changed after {s['name']} signed. Sign off again")
    return bad


def pr_body(rec: dict) -> str:
    draft = Path(rec["prDraft"]).read_text(encoding="utf-8") if Path(rec["prDraft"]).is_file() else f"## {rec['slug']}: new papers\n"
    for role, pattern in DRAFT_LINES.items():
        s = rec["signoffs"][role]
        draft, n = re.subn(pattern, lambda m: m.group(0).replace("- [ ]", "- [x]", 1) + f" ({s['name']}, {s['at']})",
                           draft, count=1, flags=re.M)
        if n != 1:   # the draft was edited by hand: still say who signed
            draft += f"\n- [x] {role.capitalize()} sign-off: {s['name']}, {s['at']}"
    return draft.rstrip() + "\n\nOpened with Paper Forge after both sign-offs. Nothing is merged automatically.\n"


# ------------------------------------------------------------------------------------------------ git / gh
def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise PrError(f"{cmd[0]} is not installed or not on PATH") from e


def git(cwd: Path, *args: str) -> str:
    r = _run(["git", *args], cwd)
    if r.returncode != 0:
        raise PrError(f"git {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def gh(cwd: Path, *args: str) -> str:
    r = _run(["gh", *args], cwd)
    if r.returncode != 0:
        raise PrError(f"gh {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


Confirm = Callable[[str, str], bool]   # (kind: "commit"|"push"|"pr", what will happen) -> yes/no


def status(rec: dict) -> dict:
    target = Path(rec["target"])
    out = {"branch": rec["branch"], "signoffs": {r: (rec.get("signoffs", {}).get(r) or {}).get("name") for r in ROLES},
           "blockers": readiness(rec), "pr": rec.get("prUrl")}
    try:
        out["checkedOut"] = git(target, "branch", "--show-current")
        out["committed"] = int(git(target, "rev-list", "--count", "origin/main..HEAD")) > 0
    except PrError as e:
        out["error"] = str(e)
    return out


def open_pr(cfg: Config, ref: str, confirm: Confirm, base: str = "main", log=print,
            gh_fn=gh, git_fn=git) -> dict:
    rec = load_record(cfg, ref)
    branch, target = rec["branch"], Path(rec["target"])
    if git_fn(target, "branch", "--show-current") != branch:
        raise PrError(f"{target} is not on {branch}. Switch to it first; Paper Forge will not guess.")
    blockers = readiness(rec)
    if blockers:
        raise PrError("Not ready for a PR:\n  - " + "\n  - ".join(blockers))
    gh_fn(target, "auth", "status")                                   # fail now, not after the push

    existing = json.loads(gh_fn(target, "pr", "list", "--head", branch, "--state", "all", "--json", "number,state,url") or "[]")
    if any(p["state"] == "MERGED" for p in existing):
        raise PrError(f"A PR for {branch} was already merged. Never push to it again; run `publish` for a fresh branch.")
    open_prs = [p for p in existing if p["state"] == "OPEN"]

    pending = git_fn(target, "status", "--porcelain", "--untracked-files=no", "--", *rec["paths"]).splitlines()
    unstaged_new = git_fn(target, "ls-files", "--others", "--exclude-standard", "--", *rec["paths"]).splitlines()
    need_commit = bool(pending or unstaged_new)
    title = f"feat({rec['slug']}): add {len(rec['keys'])} new {'university ' if rec['tree'] == 'university' else ''}paper(s)"
    log(f"Plan for {branch}:")
    log(f"  1. commit the papers locally" if need_commit else "  1. (already committed)")
    log(f"  2. git push -u origin {branch}" + ("   (updates the OPEN PR #%s)" % open_prs[0]["number"] if open_prs else ""))
    log("  3. open a PR against " + base if not open_prs else f"  3. (PR already open: {open_prs[0]['url']})")

    done = {"committed": False, "pushed": False, "prUrl": open_prs[0]["url"] if open_prs else None}
    if need_commit:
        if not confirm("commit", f"commit {len(rec['paths'])} path(s) on {branch}"):
            raise PrAborted("Not committed. Nothing was pushed.")
        git_fn(target, "add", "--", *rec["paths"])
        git_fn(target, "commit", "-m", f"{title}\n\n{', '.join(rec['keys'])}\n\nGenerated by Paper Forge. Developer and tester sign-offs "
                                       f"recorded: {rec['signoffs']['developer']['name']}, {rec['signoffs']['tester']['name']}.")
        done["committed"] = True
    if not confirm("push", f"git push -u origin {branch}"):
        raise PrAborted("Not pushed. The commit stays local.")
    git_fn(target, "push", "-u", "origin", branch)
    done["pushed"] = True
    if not open_prs:
        if not confirm("pr", f'open a PR "{title}" into {base}'):
            raise PrAborted(f"Pushed, but no PR was opened. Open it yourself or run `pr` again.")
        body_file = cfg.out_dir / "publish" / (branch.replace("/", "_") + ".pr-final.md")
        body_file.write_text(pr_body(rec), encoding="utf-8")
        url = gh_fn(target, "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", str(body_file))
        done["prUrl"] = url.strip().splitlines()[-1]
    rec["prUrl"] = done["prUrl"]
    save_record(cfg, rec)
    return done
