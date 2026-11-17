from __future__ import annotations

# What the repo, validator and student screen can carry today.
# Verified from companies/AUTHORING.md and the Capgemini mocks. Edit here when developers add a type.
SUPPORT: dict[str, tuple[bool, str]] = {
    "MCQ_TEXT":        (True,  "Text MCQ"),
    "MCQ_CODE_OUTPUT": (True,  "Text MCQ with a code block"),
    "CODING":          (True,  "problems/ folders with tests"),
    "AUDIO_LISTENING": (True,  "Audio, as in the Capgemini communication mocks"),
    "SPEAKING":        (True,  "VOICE_DRILL"),
    "MCQ_IMAGE":       (False, "No image field in the paper schema, validator or student screen"),
    "VIDEO":           (False, "No video field anywhere"),
    "WRITING":         (True,  "WRITING section, scored by the Writing Lab engine"),
    "OTHER":           (False, "Unknown type"),
}


def gap_report(pattern: dict) -> dict:
    rows, missing = [], []
    total = supported_q = 0
    for s in pattern["sections"]:
        ok, where = SUPPORT.get(s["type"], SUPPORT["OTHER"])
        q = s["questions"]
        total += q
        supported_q += q if ok else 0
        row = {"section": s["name"], "type": s["type"], "questions": q, "supported": ok,
               "where": where, "confidence": s.get("confidence")}
        rows.append(row)
        if not ok:
            missing.append(row)
    return {"company": pattern["company"], "role": pattern["role"], "rows": rows,
            "missing": missing, "total_questions": total, "supported_questions": supported_q}


def developer_task(report: dict, row: dict) -> str:
    return (
        f"Developer task: support '{row['type']}' ({row['section']}, {row['questions']} questions per paper)\n"
        f"Company/role that needs it: {report['company']}, {report['role']}\n"
        f"Today: {row['where']}.\n"
        "Needed: (1) field in schema + validate_placement_papers.py, (2) bet-retail-service reads it, "
        "(3) bet-apps student screen shows it, (4) tester signs off.\n"
        "Until then Paper Forge writes the questions as content and keeps the section marked pending."
    )


def largest_remainder(n: int, easy: int, medium: int, hard: int) -> list[int]:
    """Split n questions by percentages so the parts add up exactly."""
    if easy + medium + hard != 100:
        raise ValueError("Percentages must add up to 100")
    raw = [n * easy / 100, n * medium / 100, n * hard / 100]
    base = [int(x) for x in raw]
    order = sorted(range(3), key=lambda i: raw[i] - base[i], reverse=True)
    for i in range(n - sum(base)):
        base[order[i % 3]] += 1
    return base


def difficulty_plan(pattern: dict, easy: int, medium: int, hard: int) -> list[dict]:
    plan = []
    for s in pattern["sections"]:
        e, m, h = largest_remainder(s["questions"], easy, medium, hard)
        plan.append({"section": s["name"], "questions": s["questions"], "easy": e, "medium": m, "hard": h})
    return plan


def render(report: dict, plan: list[dict] | None = None) -> str:
    out = [f"Pattern for {report['company']}, {report['role']}", ""]
    out.append(f"{'Section':<34}{'Type':<18}{'Qs':>4}  Status")
    for r in report["rows"]:
        status = "Supported" if r["supported"] else "MISSING"
        out.append(f"{r['section'][:33]:<34}{r['type']:<18}{r['questions']:>4}  {status}  ({r['where']})")
    out.append("")
    out.append(f"{report['supported_questions']} of {report['total_questions']} questions can be generated today.")
    if report["missing"]:
        out.append("")
        out.append("Missing types, one decision each: developer task / use closest type / leave out")
        for r in report["missing"]:
            out.append(f"  - {r['section']} ({r['type']}, {r['questions']} Qs)")
    if plan:
        out.append("")
        out.append(f"{'Difficulty plan':<34}{'Qs':>4}{'Easy':>6}{'Med':>6}{'Hard':>6}")
        for p in plan:
            out.append(f"{p['section'][:33]:<34}{p['questions']:>4}{p['easy']:>6}{p['medium']:>6}{p['hard']:>6}")
        out.append(f"{'Paper total':<34}{sum(p['questions'] for p in plan):>4}"
                   f"{sum(p['easy'] for p in plan):>6}{sum(p['medium'] for p in plan):>6}{sum(p['hard'] for p in plan):>6}")
    return "\n".join(out)
