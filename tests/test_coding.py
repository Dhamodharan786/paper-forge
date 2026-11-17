import json
from dataclasses import replace
from pathlib import Path

import pytest

from paper_forge import coding
from paper_forge.config import load
from paper_forge.verify import web_check_text

REAL = load()

REF = """import sys
def main():
    d = sys.stdin.read().split()
    n, m = int(d[0]), int(d[1])
    cnt = [0] * m
    ans = 0
    for x in map(int, d[2:2 + n]):
        r = x % m
        ans += cnt[(m - r) % m]
        cnt[r] += 1
    print(ans)
main()
"""
BRUTE = """import sys
d = sys.stdin.read().split()
n, m = int(d[0]), int(d[1])
a = list(map(int, d[2:2 + n]))
ans = 0
for i in range(n):
    for j in range(i + 1, n):
        if (a[i] + a[j]) % m == 0:
            ans += 1
print(ans)
"""
GEN = """import random
def gen(i):
    rng = random.Random(i)
    n = 100000
    m = [7, 1000, 2][i]
    return f"{n} {m}\\n" + " ".join(str(rng.randint(0, 10**9)) for _ in range(n)) + "\\n"
"""


def design_text(key="pair-sums-divisible", ref=REF, hard_brute=True, title="Divisible Pair Tokens", statement=None):
    meta = {"problemKey": key, "title": title, "tags": ["ARRAY", "HASHING"],
            "inputFormat": "Line 1: n and m. Line 2: n integers.", "outputFormat": "One integer.",
            "constraintsText": "1 <= n <= 10^5, 1 <= m <= 10^3", "expectedTimeComplexity": "O(n + m)",
            "expectedSpaceComplexity": "O(m)", "timeLimitMs": 2000, "memoryLimitMb": 256,
            "examples": [{"input": "5 3\n1 2 3 4 5", "output": "4", "explanation": "four pairs"},
                         {"input": "4 2\n1 3 5 7", "output": "6", "explanation": "all pairs"}],
            "hints": ["Think about remainders.", "Count remainders as you scan."],
            "visibleInputs": ["5 3\n1 2 3 4 5", "4 2\n1 3 5 7", "1 5\n7"],
            "hiddenInputs": ["2 1\n5 5", "3 7\n7 14 21", "4 10\n1 2 3 4", "5 4\n0 0 0 0 0", "6 5\n5 10 15 1 4 9", "3 2\n1 2 3"]}
    files = {"statement.md": statement or "# Divisible Pair Tokens\n\nCount pairs whose sum is divisible by m.\n\n```\nInput:\n5 3\n```\n",
             "reference.py": ref, "brute.py": BRUTE, "generator.py": GEN,
             "starter.py": "# TODO: count the pairs\nprint(0)\n", "Main.java": "public class Main { public static void main(String[] a) { /* TODO */ } }\n",
             "solution.cpp": "int main() { /* TODO */ }\n", "solution.c": "int main() { /* TODO */ }\n"}
    out = "```json\n" + json.dumps(meta) + "\n```\n"
    for k, v in files.items():
        out += f"~~~file:{k}\n{v.rstrip()}\n~~~\n"
    return out


class Fake:
    def __init__(self, designs, label="Medium"):
        self.designs, self.label, self.calls = list(designs), label, []

    def ask(self, *, label, model, system, user, tools=None, max_tokens=16000):
        self.calls.append(label)
        if label == "coding-design":
            return self.designs.pop(0)
        if label == "coding-rate":
            return '```json\n{"label": "%s", "rulesHit": ["counting"], "intendedApproach": "remainders", "reason": "ok"}\n```' % self.label
        if label == "web-check":
            return '```json\n{"verdict": "no_match", "similarity": 3, "url": null, "note": ""}\n```'
        raise AssertionError(label)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(coding, "BRUTE_SECONDS", 1.0)


def cfg_for(tmp_path):
    repo = tmp_path / "repo"
    (repo / "problems" / "old-one").mkdir(parents=True)
    (repo / "problems" / "old-one" / "statement.md").write_text("# Old\n\nSomething about bananas and trees.", encoding="utf-8")
    (repo / "tags.json").write_text(json.dumps({"tags": ["ARRAY", "HASHING", "STRING"]}), encoding="utf-8")
    return replace(REAL, repo=repo, out_dir=tmp_path / "out")


def test_parse_design_keeps_code_fences_inside_files():
    d = coding.parse_design(design_text())
    assert d["meta"]["problemKey"] == "pair-sums-divisible" and "```" in d["files"]["statement.md"]
    assert set(d["files"]) >= {"reference.py", "brute.py", "generator.py", "starter.py"}


def test_happy_path_outputs_come_from_reference(tmp_path, fast):
    cfg = cfg_for(tmp_path)
    idx = coding.ProblemIndex(cfg.repo)
    res = coding.make_problem(cfg, Fake([design_text()]), idx, "MEDIUM", ["arrays"], [], True, coding.load_tags(cfg.repo), log=lambda *_: None)
    assert res["ok"] and res["flag"] is None, res["problems"]
    ev = res["evidence"]
    assert ev["bruteTimedOutAtMax"] and ev["bruteCrossChecked"] >= 8 and ev["tests"]["hidden"] == 9
    root = coding.write_problem_folder(cfg, res["_design"], "MEDIUM", res["_tests"], tmp_path / "probs")
    assert (root / "tests" / "visible" / "01.out").read_text() == "4\n"
    assert json.loads((root / "problem.json").read_text())["status"] == "REVIEW"
    assert json.loads((root / "problem.json").read_text())["marks"] == 100


def test_wrong_reference_is_caught_and_repaired(tmp_path, fast):
    cfg = cfg_for(tmp_path)
    bad_ref = REF.replace("ans += cnt[(m - r) % m]", "ans += cnt[(m - r)]") if False else REF.replace("print(ans)", "print(ans + 1)")
    fake = Fake([design_text(ref=bad_ref), design_text(key="pair-sums-fixed")])
    res = coding.make_problem(cfg, fake, coding.ProblemIndex(cfg.repo), "MEDIUM", [], [], False, coding.load_tags(cfg.repo), log=lambda *_: None)
    assert res["ok"] and res["attempts"] == 2 and res["key"] == "pair-sums-fixed"


def test_easy_brute_that_is_fast_is_fine_but_medium_needs_timeout(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(coding, "BRUTE_SECONDS", 120.0)       # brute will finish, so a MEDIUM problem must be rejected
    d = coding.parse_design(design_text())
    d["files"]["generator.py"] = GEN.replace("100000", "400")
    d["meta"]["visibleInputs"] = d["meta"]["visibleInputs"][:3]
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        bad, _, ev = coding.build_tests(cfg, d, "MEDIUM", Path(t))
    assert any("constraints do not force" in b for b in bad) or any("not near the constraint maximum" in b for b in bad)


def test_copy_of_existing_problem_rejected(tmp_path, fast):
    cfg = cfg_for(tmp_path)
    idx = coding.ProblemIndex(cfg.repo)
    d = coding.parse_design(design_text())
    idx.add("pair-sums-divisible", d["files"]["statement.md"], "Divisible Pair Tokens")
    fake = Fake([design_text()] * 3)
    res = coding.make_problem(cfg, fake, idx, "MEDIUM", [], [], False, coding.load_tags(cfg.repo), log=lambda *_: None)
    assert not res["ok"] and "needs a human" in res["flag"]


def test_difficulty_mismatch_on_last_attempt_is_kept_and_flagged(tmp_path, fast):
    cfg = replace(cfg_for(tmp_path), max_attempts=2)
    fake = Fake([design_text(key="a-one"), design_text(key="a-two")], label="Hard")
    res = coding.make_problem(cfg, fake, coding.ProblemIndex(cfg.repo), "MEDIUM", [], [], False, coding.load_tags(cfg.repo), log=lambda *_: None)
    assert res["ok"] and res["flag"].startswith("kept")


@pytest.mark.skipif(not (REAL.repo / "scripts" / "verify-solutions.py").is_file(), reason="real repo scripts not found")
def test_repo_scripts_accept_the_generated_folder(tmp_path, fast):
    cfg = replace(REAL, out_dir=tmp_path / "out")
    idx = coding.ProblemIndex(tmp_path)  # empty index, the real repo is not needed for this check
    res = coding.make_problem(cfg, Fake([design_text(key="pair-sums-divisible-zz")]), idx, "MEDIUM", [], [], False,
                              coding.load_tags(REAL.repo) | {"ARRAY", "HASHING"}, log=lambda *_: None)
    assert res["ok"], res["problems"]
    coding.write_problem_folder(cfg, res["_design"], "MEDIUM", res["_tests"], tmp_path / "probs")
    v = coding.validate_problem_draft(cfg, "pair-sums-divisible-zz", tmp_path / "probs")
    assert v["ran"] and v["ok"], v["output"]
