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


PAGE = "Iloilo Regional CTF 2099 on Dec 5 at Test Hall, Iloilo City. Register: https://reg.example/form"
ANSWER = {"is_hackathon": True, "category": "ctf", "in_scope_philippines": True, "name": "Iloilo Regional CTF 2099",
          "mode": "onsite", "venue": "Test Hall", "city": "Iloilo City", "province": "Iloilo", "start": "2099-12-05",
          "registration_deadline": "2099-11-30", "registration_url": "https://reg.example/form", "themes": ["Web"]}
MODELS = ["models/gemini-3.1-flash-lite", "models/gemini-3.5-flash-lite", "models/gemini-3.5-flash-lite-preview-tts",
          "models/gemini-3.8-flash", "models/gemini-3.1-pro-preview", "models/text-embedding-9"]


@pytest.fixture()
def gemini(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "LLM_PROVIDER", "")
    monkeypatch.setattr(config, "GEMINI_MODEL", "")
    monkeypatch.setattr(config, "LLM_USAGE_FILE", tmp_path / "llm_usage.json")
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
    assert ev["name"] == "Iloilo Regional CTF 2099" and ev["category"] == "ctf"
    assert ev["deadline"] == "2099-11-30T23:59:00+08:00" and ev["registration_url"] == "https://reg.example/form"
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
