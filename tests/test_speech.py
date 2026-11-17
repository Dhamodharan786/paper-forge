"""Listening and speaking sections, the audio hand-off for the dev team, and the audio gate on publish."""
import io
import json
import random
import re

import pytest
from pypdf import PdfReader

from paper_forge import publish as pub, review, speech
from paper_forge.cost import CostTracker
from paper_forge.pdf import build_pdf
from paper_forge.pipeline import generate_papers
from test_m2 import FakeClaude
from test_publish import make_repo, sh

WORDS = ("river lantern ticket bridge morning schedule venue deadline library notice campus invoice parcel garden "
         "meeting budget printer window platform ledger sample signal harbour orchard lecture kitchen mirror pocket "
         "survey voucher station tunnel wizard canvas anchor dinner engine forest guitar helmet island jungle kettle").split()


def text_of(rng, n):
    return " ".join(rng.choice(WORDS) for _ in range(n))


class MediaFake(FakeClaude):
    def __init__(self):
        super().__init__()
        self.k = 0

    def ask(self, **kw):
        label, user, system = kw["label"], kw["user"], kw["system"]
        if label == "listening-design":
            self.k += 1
            rng = random.Random(self.k)
            n = int(re.search(r"exactly (\d+) question", user).group(1))
            lines = [f"{['Maya', 'Joseph'][i % 2]}: {text_of(rng, 17).capitalize()}." for i in range(8)]
            qs = [{"question": f"What did the speakers decide about the {text_of(rng, 3)}?",
                   "options": {"A": text_of(rng, 4), "B": text_of(rng, 4), "C": text_of(rng, 4), "D": text_of(rng, 4)},
                   "correctAnswer": "B", "explanation": "B is right because Joseph says so; the others were not agreed.",
                   "tags": ["detail"]} for _ in range(n)]
            return "```json\n" + json.dumps({"title": f"Recording {self.k}", "speakers": ["Maya", "Joseph"],
                                             "transcript": "\n".join(lines), "questions": qs}) + "\n```"
        if label == "speaking-design":
            mode = re.search(r"Mode: ([a-z-]+)", user).group(1)
            n = int(re.search(r"Write exactly (\d+) item", user).group(1))
            items = []
            for _ in range(n):
                self.k += 1
                rng = random.Random(100 + self.k)
                size = {"read-aloud": 70, "listen-and-repeat": 15, "extempore": 40}[mode]
                items.append({"title": f"{mode} {self.k}", "text": text_of(rng, size).capitalize() + ".",
                              "modelAnswer": text_of(rng, 120) if mode == "extempore" else "Pause after commas.", "tags": [mode]})
            return "```json\n" + json.dumps({"items": items}) + "\n```"
        if label == "rate" and ("listening-comprehension" in system.lower() or "spoken-communication" in system.lower()):
            n = len(re.findall(r"^\[\d+\]", user, re.M))
            r = [{"index": i, "label": "Medium", "rulesHit": ["x"], "expectedSeconds": 40, "reason": "r"} for i in range(n)]
            return "```json\n" + json.dumps({"ratings": r}) + "\n```"
        return super().ask(**kw)


PATTERN = {"company": "Acme", "role": "Software Engineer", "sources": [{"url": "u", "title": "t"}], "sections": [
    {"name": "Quantitative", "type": "MCQ_TEXT", "questions": 5, "minutes": 10, "topics": ["percentages"]},
    {"name": "Listening", "type": "AUDIO_LISTENING", "questions": 5, "minutes": 8, "topics": ["campus"]},
    {"name": "Read aloud", "type": "SPEAKING", "mode": "read-aloud", "questions": 1, "minutes": 2},
    {"name": "Listen and repeat", "type": "SPEAKING", "mode": "listen-and-repeat", "questions": 3, "minutes": 3},
    {"name": "Spoken response", "type": "SPEAKING", "questions": 2, "minutes": 6}]}


def test_split_recordings_and_modes():
    assert [len(x) for x in speech.split_recordings(["Medium"] * 5)] == [3, 2]
    assert [len(x) for x in speech.split_recordings(["Medium"] * 4)] == [2, 2]
    assert [len(x) for x in speech.split_recordings(["Medium"])] == [1]
    assert speech.section_mode({"name": "Read aloud", "type": "SPEAKING"}) == "read-aloud"
    assert speech.section_mode({"name": "Repeat the sentence", "type": "SPEAKING"}) == "listen-and-repeat"
    assert speech.section_mode({"name": "Answer", "type": "SPEAKING", "mode": "read-aloud"}) == "read-aloud"
    assert speech.section_mode({"name": "x", "type": "AUDIO_LISTENING"}) == "listening-comprehension"


def test_recording_problems_catch_bad_shapes():
    ok = {"transcript": "\n".join(f"Maya: {' '.join(['word'] * 20)}" for _ in range(6)),
          "questions": [{"question": "A long enough question?", "options": {"A": "a1", "B": "b2", "C": "c3", "D": "d4"},
                         "correctAnswer": "A", "explanation": "Because the speaker said so earlier in the talk."}]}
    assert speech.recording_problems(ok, ["Easy"]) == []
    assert any("exactly 2" in p for p in speech.recording_problems(ok, ["Easy", "Hard"]))
    assert any("Name: text" in p for p in speech.recording_problems(dict(ok, transcript="just some words"), ["Easy"]))


@pytest.fixture
def world(tmp_path):
    cfg, repo, origin = make_repo(tmp_path)
    res = generate_papers(cfg, MediaFake(), CostTracker(cfg), PATTERN, (0, 100, 0), 1, "dev", True, log=lambda *_: None,
                          coding=False)
    return cfg, repo, origin, res[0]


def test_paper_has_listening_and_speaking_sections(world):
    cfg, repo, origin, res = world
    p, rep = res["paper"], res["report"]
    names = [s["name"] for s in p["sections"]]
    assert names == ["Quantitative", "Listening Comprehension — Recording A", "Listening Comprehension — Recording B",
                     "Speaking — Read Aloud", "Speaking — Listen and Repeat", "Speaking — Spoken Response"]
    a = p["sections"][1]
    assert a["config"]["hasAudioPrompt"] and a["config"]["audioAsset"] is None and len(a["questions"]) == 3
    assert [len(s["questions"]) for s in p["sections"][3:]] == [1, 3, 2]
    ra, lr, vr = p["sections"][3:]
    assert ra["questions"][0]["referenceText"] and "audioAsset" in lr["questions"][0] and lr["questions"][0]["audioAsset"] is None
    assert lr["config"]["graderOnlyFields"] == ["audioTranscript", "referenceText", "modelAnswer"]
    assert p["config"]["requiresMicrophone"] and p["config"]["requiresAudioPlayback"]
    assert p["totalMarks"] == 5 + 5 + 5 + 15 + 20
    assert rep["needsHuman"] == 0 and rep["repoValidator"]["ran"] and rep["repoValidator"]["ok"], rep["repoValidator"]["output"]
    assert len(rep["audio"]) == 5 and {j["kind"] for j in rep["audio"]} == {"listening", "repeat"}


def test_handoff_for_dev_team_says_exactly_what_to_record(world):
    cfg, _, _, res = world
    key = res["paper"]["paperKey"]
    folder = cfg.out_dir / "audio" / key
    hand = (folder / "HANDOFF.md").read_text(encoding="utf-8")
    assert "dev team" in hand and "Claude cannot make audio" in hand and "paper_forge audio " + key in hand
    txt = (folder / "acme_001_listening_A.txt").read_text(encoding="utf-8")
    assert "TEXT TO SPEAK" in txt and "Maya:" in txt and "Do NOT say the name aloud" in txt
    assert "Wasabi" in txt and "File name : acme_001_listening_A.mp3" in txt
    assert json.loads((folder / "urls.json").read_text(encoding="utf-8")) == {j["file"]: None for j in res["report"]["audio"]}
    assert "--map urls.json" in hand and "null" in hand
    rep = [x for x in (folder / "acme_001_repeat_1.txt",)][0].read_text(encoding="utf-8")
    assert "single sentence once" in rep
    assert len(list(folder.glob("*.txt"))) == 5


def test_review_is_read_only_for_audio_and_speaking_and_pdf_works(world):
    cfg, _, _, res = world
    key = res["paper"]["paperKey"]
    v = review.view(cfg, key)
    assert [s.get("kind") for s in v["sections"]] == [None, None, None, "SPEAKING", "SPEAKING", "SPEAKING"]
    assert v["sections"][1]["editable"] is False and v["sections"][1]["audio"]["transcript"]
    assert len(v["audioFiles"]) == 5
    paper, rep = review.load(cfg, key)
    audio_q = paper["sections"][1]["questions"][0]["questionKey"]
    out = review.apply_changes(cfg, MediaFake(), CostTracker(cfg), key, {audio_q: "Hard"}, log=lambda *_: None)
    assert out["results"] == []                                        # not rewritable from the screen
    text = "\n".join(pg.extract_text() for pg in PdfReader(io.BytesIO(build_pdf(paper))).pages)
    assert "Audio recording" in text and "Marking guide" in text


def all_urls(rep, base="https://wasabi.example.com/audio/"):
    return {j["file"]: base + j["file"] for j in rep["audio"]}


def test_attach_replaces_null_with_dev_team_urls(world):
    cfg, _, _, res = world
    key = res["paper"]["paperKey"]
    paper, rep = review.load(cfg, key)
    out = speech.attach_urls(paper, rep, {"acme_001_listening_A.mp3": "https://w.example/a.mp3",
                                          "acme_001_repeat_2.mp3": "https://w.example/r2.mp3"})
    assert out["applied"] == ["acme_001_listening_A.mp3", "acme_001_repeat_2.mp3"] and len(out["pending"]) == 3
    assert paper["sections"][1]["config"]["audioAsset"] == "https://w.example/a.mp3"
    assert paper["sections"][4]["questions"][1]["audioAsset"] == "https://w.example/r2.mp3"
    assert paper["sections"][2]["config"]["audioAsset"] is None and paper["sections"][4]["questions"][0]["audioAsset"] is None
    bad = speech.attach_urls(paper, rep, {"nope.mp3": "https://x/y.mp3", "acme_001_repeat_1.mp3": "ftp://x"})
    assert bad["unknown"] == ["nope.mp3"] and bad["badUrl"] == ["acme_001_repeat_1.mp3"]


def test_parse_url_lines_accepts_json_and_plain_lines():
    assert speech.parse_url_lines('{"a.mp3": "https://x/a", "b.mp3": null}') == {"a.mp3": "https://x/a"}
    assert speech.parse_url_lines("a.mp3 https://x/a\nb.mp3 = https://x/b\n\n") == {"a.mp3": "https://x/a", "b.mp3": "https://x/b"}


def test_cli_audio_attach_saves_and_validates(world, monkeypatch, tmp_path, capsys):
    from paper_forge import cli
    cfg, _, _, res = world
    key = res["paper"]["paperKey"]
    monkeypatch.setattr(cli, "load", lambda: cfg)
    urls = tmp_path / "urls.json"
    paper, rep = review.load(cfg, key)
    urls.write_text(json.dumps(all_urls(rep)), encoding="utf-8")
    assert cli.main(["audio", key, "--map", str(urls)]) == 0
    assert "Filled in 5 URL(s)" in capsys.readouterr().out
    paper, rep = review.load(cfg, key)
    assert paper["sections"][2]["config"]["audioAsset"].startswith("https://wasabi.example.com/") and rep["repoValidator"]["ok"]
    assert cli.main(["audio", key, "--set", "wrong.mp3=https://x/y"]) == 2          # unknown file name: nothing saved


def test_publish_waits_for_urls_then_goes(world, monkeypatch):
    cfg, repo, _, res = world
    key = res["paper"]["paperKey"]
    with pytest.raises(pub.PublishError, match="still have no URL"):
        pub.publish(cfg, [key], log=lambda *_: None)
    assert sh(repo, "branch", "--show-current") == "my-feature"       # nothing was created
    paper, rep = review.load(cfg, key)
    speech.attach_urls(paper, rep, all_urls(rep))
    review._save(cfg, key, paper, rep)
    monkeypatch.setattr(speech, "check_urls", lambda urls, timeout=15.0: {u: "missing (404)" for u in urls})
    with pytest.raises(pub.PublishError, match="not uploaded yet"):
        pub.publish(cfg, [key], log=lambda *_: None)
    monkeypatch.setattr(speech, "check_urls", lambda urls, timeout=15.0: {u: "ok" for u in urls})
    out = pub.publish(cfg, [key], log=lambda *_: None)
    assert out["validator"]["ok"]
    on_branch = json.loads((repo / "companies" / "university-exams" / "acme" / f"{key}.json").read_text(encoding="utf-8"))
    assert on_branch["sections"][1]["config"]["audioAsset"].startswith("https://wasabi.example.com/")
    assert "uploaded to Wasabi" in open(out["prDraft"], encoding="utf-8").read()


def test_publish_can_override_missing_audio(world):
    cfg, _, _, res = world
    assert pub.publish(cfg, [res["paper"]["paperKey"]], allow_missing_audio=True, log=lambda *_: None)["branch"]
