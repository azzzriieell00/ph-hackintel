"""AI extraction: provider choice, Gemini model selection, request budget. No network: a fake session answers."""
import json

import pytest

from pipeline import config, extract


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, models, answer, status=200):
        self.models, self.answer, self.status = models, answer, status
        self.posts = []

    def get(self, url, **kw):
        assert kw["headers"]["x-goog-api-key"] == "test-key"
        return Resp(200, {"models": [{"name": n, "supportedGenerationMethods": ["generateContent"]} for n in self.models]})

    def post(self, url, **kw):
        self.posts.append(url)
        if self.status != 200:
            return Resp(self.status, {"error": "x"})
        return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(self.answer)}]}}]})


class FakeHttp:
    def __init__(self, session):
        self.s = session
        self.log = []


from datetime import datetime

Y = datetime.now().year + 1     # AI dates must be within two years of today, so the fixture follows the clock
PAGE = (f"Iloilo Regional CTF {Y} on Dec 5 at Test Hall, Iloilo City. Registration until November 30. "
        "Register: https://reg.example/form")
ANSWER = {"is_hackathon": True, "category": "ctf", "in_scope_philippines": True, "name": f"Iloilo Regional CTF {Y}",
          "mode": "onsite", "venue": "Test Hall", "city": "Iloilo City", "province": "Iloilo", "start": f"{Y}-12-05",
          "registration_deadline": f"{Y}-11-30", "registration_url": "https://reg.example/form", "themes": ["Web"]}
MODELS = ["models/gemini-3.1-flash-lite", "models/gemini-3.5-flash-lite", "models/gemini-3.5-flash-lite-preview-tts",
          "models/gemini-3.8-flash", "models/gemini-3.1-pro-preview", "models/text-embedding-9"]


@pytest.fixture()
def gemini(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "LLM_PROVIDER", "")
    monkeypatch.setattr(config, "GEMINI_MODEL", "")
    monkeypatch.setattr(config, "LLM_USAGE_FILE", tmp_path / "llm_usage.json")
    monkeypatch.setattr(config, "LLM_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(extract, "BUDGET", None)


def test_no_key_means_no_ai(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "LLM_PROVIDER", "")
    monkeypatch.setattr(extract, "BUDGET", None)
    assert config.llm_provider() == "" and extract.llm_available() is False


def test_picks_newest_stable_flash_lite(gemini):
    http = FakeHttp(FakeSession(MODELS, ANSWER))
    assert extract._pick_gemini_model(http) == "models/gemini-3.5-flash-lite"


def test_gemini_extraction_and_budget(gemini):
    s = FakeSession(MODELS, ANSWER)
    ev = extract.llm_extract(FakeHttp(s), PAGE, "https://news.example/a")
    assert ev["name"] == f"Iloilo Regional CTF {Y}" and ev["category"] == "ctf"
    assert ev["deadline"] == f"{Y}-11-30T23:59:00+08:00" and ev["registration_url"] == "https://reg.example/form"
    assert ev["start"].startswith(f"{Y}-12-05") and ev["ai_checks"] is None
    assert s.posts[0].endswith("models/gemini-3.5-flash-lite:generateContent")
    extract.BUDGET.save()
    saved = json.loads(config.LLM_USAGE_FILE.read_text())
    assert saved["count"] == 1


def test_run_cap_and_daily_cap(gemini, monkeypatch):
    monkeypatch.setattr(config, "LLM_RUN_CAP", 2)
    s = FakeSession(MODELS, ANSWER)
    for _ in range(4):
        extract.llm_extract(FakeHttp(s), PAGE, "https://news.example/a")
    assert len(s.posts) == 2 and "run cap" in extract.BUDGET.stopped

    monkeypatch.setattr(extract, "BUDGET", None)
    monkeypatch.setattr(config, "LLM_RUN_CAP", 50)
    monkeypatch.setattr(config, "LLM_DAILY_CAP", 3)   # 2 already used today (saved above? no: new file) -> set explicitly
    config.LLM_USAGE_FILE.write_text(json.dumps({"date": extract.LLMBudget().today, "count": 3}))
    s2 = FakeSession(MODELS, ANSWER)
    assert extract.llm_extract(FakeHttp(s2), PAGE, "https://news.example/a") is None
    assert not s2.posts and "daily cap" in extract.BUDGET.stopped


def test_rate_limit_stops_ai_for_the_run(gemini):
    s = FakeSession(MODELS, ANSWER, status=429)
    assert extract.llm_extract(FakeHttp(s), PAGE, "https://news.example/a") is None
    assert "429" in extract.BUDGET.stopped
    assert extract.llm_extract(FakeHttp(s), PAGE, "https://news.example/b") is None
    assert len(s.posts) == 1
    # the pause is saved, so the next scan does not knock on the door again right away
    extract.BUDGET.save()
    extract.BUDGET = None
    assert extract.llm_available() is False and "paused" in extract.BUDGET.stopped


def test_daily_quota_pauses_until_reset(gemini):
    b = extract.LLMBudget()
    b.quota_hit('{"error": {"message": "Quota exceeded for metric: GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}')
    until = datetime.fromisoformat(b.blocked_until.replace("Z", "+00:00"))
    assert until.hour == 8 and until.minute == 0


def test_answered_flag_and_several_events(gemini):
    page = PAGE + f" Also: Bacolod Hackathon {Y} on Dec 9 at Test Center, Bacolod."
    two = {"events": [ANSWER, {**ANSWER, "category": "hackathon", "name": f"Bacolod Hackathon {Y}", "city": "Bacolod",
                                "province": "Negros Occidental", "start": f"{Y}-12-09", "registration_deadline": None,
                                "registration_url": None}]}
    evs, answered = extract.llm_extract_ex(FakeHttp(FakeSession(MODELS, two)), page, "https://news.example/a")
    assert answered and [e["name"] for e in evs] == [f"Iloilo Regional CTF {Y}", f"Bacolod Hackathon {Y}"]
    assert evs[1]["deadline_is_estimate"] is True and evs[1]["registration_url"] == "https://news.example/a"
    evs, answered = extract.llm_extract_ex(FakeHttp(FakeSession(MODELS, two, status=500)), page, "https://news.example/a")
    assert evs == [] and answered is False
