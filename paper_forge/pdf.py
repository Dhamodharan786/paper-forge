"""Question paper PDF: questions first, then the answer key with worked explanations."""
from __future__ import annotations

import io
import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer

_FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts"),
              Path("/System/Library/Fonts/Supplemental")]
_CANDIDATES = [("arial.ttf", "arialbd.ttf"), ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"), ("Arial.ttf", "Arial Bold.ttf")]
_registered: tuple[str, str, bool] | None = None


def _fonts() -> tuple[str, str, bool]:
    """(regular, bold, has_rupee). Falls back to Helvetica, which has no rupee sign."""
    global _registered
    if _registered:
        return _registered
    for reg, bold in _CANDIDATES:
        for d in _FONT_DIRS:
            if (d / reg).is_file() and (d / bold).is_file():
                pdfmetrics.registerFont(TTFont("PF-Regular", str(d / reg)))
                pdfmetrics.registerFont(TTFont("PF-Bold", str(d / bold)))
                _registered = ("PF-Regular", "PF-Bold", True)
                return _registered
    _registered = ("Helvetica", "Helvetica-Bold", False)
    return _registered


def _text(s: str, rupee_ok: bool) -> str:
    if not rupee_ok:
        s = s.replace("\u20b9", "Rs. ")
    s = escape(str(s))
    s = re.sub(r"^( +)", lambda m: "&nbsp;" * len(m.group(1)), s, flags=re.M)  # keep code indentation
    s = re.sub(r"(?<=\S)( {2,})", lambda m: "&nbsp;" * len(m.group(1)), s)
    return s.replace("\n", "<br/>")


def build_pdf(paper: dict, with_answers: bool = True, problems: dict | None = None) -> bytes:
    reg, bold, rupee = _fonts()
    base = ParagraphStyle("b", fontName=reg, fontSize=10, leading=14)
    st = {
        "title": ParagraphStyle("t", parent=base, fontName=bold, fontSize=17, leading=21, spaceAfter=4),
        "meta": ParagraphStyle("m", parent=base, textColor=colors.HexColor("#555555"), spaceAfter=10),
        "sec": ParagraphStyle("s", parent=base, fontName=bold, fontSize=12.5, leading=16, spaceBefore=12, spaceAfter=2),
        "ins": ParagraphStyle("i", parent=base, fontSize=9, textColor=colors.HexColor("#555555"), spaceAfter=6),
        "q": ParagraphStyle("q", parent=base, spaceBefore=6, spaceAfter=2, leftIndent=18, firstLineIndent=-18),
        "opt": ParagraphStyle("o", parent=base, leftIndent=30, firstLineIndent=-12),
        "ans": ParagraphStyle("a", parent=base, spaceBefore=5, leftIndent=18, firstLineIndent=-18),
        "why": ParagraphStyle("w", parent=base, fontSize=9, leftIndent=18, textColor=colors.HexColor("#333333")),
    }

    def P(txt, style):
        return Paragraph(_text(txt, rupee), style)

    mins = round(paper["timeLimitSeconds"] / 60)
    story = [P(paper["title"], st["title"]),
             P(f"{paper['totalMarks']} marks \u00b7 {mins} minutes \u00b7 {paper['paperKey']}", st["meta"])]
    n = 0
    numbered: list[tuple[int, dict]] = []
    for sec in paper["sections"]:
        story.append(P(sec["name"], st["sec"]))
        story.append(P(sec["instructions"], st["ins"]))
        if sec["type"] == "CODING":
            for slug in sec["questions"]:
                pr = (problems or {}).get(slug, {"title": slug, "statement": "", "marks": 0, "difficulty": ""})
                n += 1
                body = re.sub(r"^# .*\n", "", pr["statement"], count=1)
                story.append(Paragraph(f"<b>{n}. {escape(pr['title'])}</b>  "
                                       f"<font size=8 color='#777777'>[{pr['difficulty']}, {pr['marks']} marks]</font>", st["q"]))
                story.append(Paragraph(_text(body.replace("```", ""), rupee), st["opt"]))
            continue
        if sec.get("config", {}).get("hasAudioPrompt") and sec["type"] == "MCQ":
            story.append(P("(Audio recording. Listen first, then answer.)", st["ins"]))
        for q in sec["questions"]:
            n += 1
            numbered.append((n, q))
            if sec["type"] in ("VOICE_DRILL", "WRITING"):
                note = (f"({q['marks']} marks, {q['wordLimit']['min']} to {q['wordLimit']['max']} words)" if sec["type"] == "WRITING"
                        else f"({q['marks']} marks, about {q['expectedDurationSeconds']} seconds)")
                story.append(KeepTogether([Paragraph(f"<b>{n}.</b> " + _text(q["question"], rupee), st["q"]), P(note, st["ins"])]))
                continue
            block = [Paragraph(f"<b>{n}.</b> " + _text(q["question"], rupee), st["q"])]
            for letter in "ABCD":
                block.append(Paragraph(f"{letter}. " + _text(q["options"][letter], rupee), st["opt"]))
            story.append(KeepTogether(block))

    if with_answers:
        story += [PageBreak(), P("Answer key and working", st["title"]), Spacer(1, 4)]
        if any(sec["type"] == "CODING" for sec in paper["sections"]):
            story.append(P("Coding problems are graded by hidden test cases. Reference solutions are kept in the repo, not printed here.", st["ins"]))
        for num, q in numbered:
            if "options" not in q:
                story.append(KeepTogether([
                    Paragraph(f"<b>{num}.</b> " + ("Writing task. Marking criteria and model answer:" if "wordLimit" in q
                                                  else "Spoken answer. Marking guide:"), st["ans"]),
                    *([P("- " + c, st["why"]) for c in q.get("evaluationCriteria", [])] if "wordLimit" in q else []),
                    P(q.get("modelAnswer") or "", st["why"])]))
                continue
            ans = q["correctAnswer"]
            story.append(KeepTogether([
                Paragraph(f"<b>{num}.</b> {ans}. " + _text(q["options"][ans], rupee) +
                          f"  <font size=8 color='#777777'>[{q['complexity']}]</font>", st["ans"]),
                P(q["explanation"], st["why"])]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=paper["title"], author="Paper Forge")

    def footer(canvas, d):
        canvas.setFont(reg, 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawCentredString(A4[0] / 2, 9 * mm, f"{paper['title']} \u00b7 page {d.page}")

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
