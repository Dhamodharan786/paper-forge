import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paper_forge import gap
from paper_forge.claude import extract_json
from paper_forge.config import load
from paper_forge.cost import CapExceeded, CostTracker
from paper_forge.research import validate_pattern

SAMPLE = json.loads((Path(__file__).parent / "sample_pattern.json").read_text(encoding="utf-8"))


def test_gap_marks_image_missing():
    rep = gap.gap_report(SAMPLE)
    assert [r["type"] for r in rep["missing"]] == ["MCQ_IMAGE"]
    assert rep["total_questions"] == 22 and rep["supported_questions"] == 17


def test_unknown_type_is_missing():
    p = {"company": "x", "role": "y", "sections": [{"name": "s", "type": "WEIRD", "questions": 3}]}
    assert gap.gap_report(p)["missing"][0]["supported"] is False


@pytest.mark.parametrize("n", [1, 2, 5, 7, 10, 20, 67])
@pytest.mark.parametrize("split", [(60, 30, 10), (30, 50, 20), (10, 40, 50), (33, 33, 34)])
def test_largest_remainder_adds_up(n, split):
    parts = gap.largest_remainder(n, *split)
    assert sum(parts) == n and min(parts) >= 0


def test_split_must_total_100():
    with pytest.raises(ValueError):
        gap.largest_remainder(10, 50, 50, 10)


def test_plan_matches_mockup_for_ten_questions():
    assert gap.largest_remainder(10, 30, 50, 20) == [3, 5, 2]


def test_validate_pattern():
    assert validate_pattern(SAMPLE) == []
    bad = {"sections": [{"name": "a", "type": "NOPE", "questions": 0}], "sources": []}
    assert len(validate_pattern(bad)) == 3


def test_extract_json_takes_last_fenced_block():
    text = 'thinking {"a":1}\n```json\n{"b": 2}\n```\n'
    assert extract_json(text) == {"b": 2}
    assert extract_json('prefix {"c": 3} suffix') == {"c": 3}


def test_cost_and_cap():
    cfg = load()
    t = CostTracker(cfg)
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=3))
    c = t.add("x", "claude-haiku-5-5", usage)
    assert c.usd == pytest.approx(0.10 + 3 * cfg.web_search_usd)
    big = SimpleNamespace(input_tokens=0, output_tokens=1_000_000, server_tool_use=None)
    t.add("y", "claude-opus-5-5", big)  # $20 output
    with pytest.raises(CapExceeded):
        t.check()
