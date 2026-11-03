"""Paths and settings shared by the pipeline. Values can be overridden with environment variables."""
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "state"

SOURCES_FILE = CONFIG_DIR / "sources.yaml"
MANUAL_FILE = CONFIG_DIR / "manual_events.yaml"
VENUES_FILE = DATA_DIR / "venues.json"
GEO_FILE = DATA_DIR / "geo.json"
EVENTS_FILE = DATA_DIR / "events.json"
REVIEW_FILE = DATA_DIR / "review.json"
GEOCODE_CACHE = STATE_DIR / "geocode_cache.json"
PAGE_STATE = STATE_DIR / "page_state.json"
LLM_USAGE_FILE = STATE_DIR / "llm_usage.json"

# Identify the bot honestly. Nominatim's usage policy requires a contact in the User-Agent.
CONTACT = os.environ.get("HACKINTEL_CONTACT", "set-HACKINTEL_CONTACT")
REPO_URL = os.environ.get("HACKINTEL_REPO_URL", "")
USER_AGENT = f"PH-HackIntel/1.0 (+{REPO_URL or 'no-repo-url'}; contact: {CONTACT})"

HTTP_TIMEOUT = float(os.environ.get("HACKINTEL_HTTP_TIMEOUT", "20"))
PER_HOST_DELAY = float(os.environ.get("HACKINTEL_PER_HOST_DELAY", "2.0"))  # seconds between requests to one host
MAX_FOLLOW_PER_SOURCE = int(os.environ.get("HACKINTEL_MAX_FOLLOW", "15"))

# Optional LLM extraction for pages without structured data.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("HACKINTEL_LLM_MODEL", "") or "claude-haiku-5-5"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Leave empty to pick the newest available Gemini Flash-Lite model automatically (free tier friendly).
GEMINI_MODEL = os.environ.get("HACKINTEL_GEMINI_MODEL", "")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# "gemini", "anthropic" or "" (auto: Gemini if its key is set, else Anthropic if its key is set, else off)
LLM_PROVIDER = os.environ.get("HACKINTEL_LLM_PROVIDER", "").strip().lower()
LLM_DAILY_CAP = int(os.environ.get("HACKINTEL_LLM_DAILY_CAP", "120"))   # requests per UTC day, all runs together
LLM_RUN_CAP = int(os.environ.get("HACKINTEL_LLM_RUN_CAP", "12"))        # requests per scan
LLM_MIN_INTERVAL_S = float(os.environ.get("HACKINTEL_LLM_MIN_INTERVAL", "6"))  # seconds between AI requests (per-minute limits)
AI_REFRESH_H = 6             # a page whose text changed is re-read by the AI at most this often

# Discovery limits: keep each hourly scan polite and well inside the workflow's time limit.
SCAN_TIME_BUDGET_S = float(os.environ.get("HACKINTEL_SCAN_BUDGET_S", "720"))   # stop opening new pages after this
MAX_PAGES_PER_RUN = int(os.environ.get("HACKINTEL_MAX_PAGES", "60"))           # announcement pages opened per scan
DAILY_REFRESH_PER_RUN = 8    # "site" sources whose homepage and search feeds are re-read in one scan (spreads the load)
FEED_MAX_AGE_D = 100         # feed items older than this are ignored (past events are kept about two months)
QUEUE_MAX_AGE_D = 14         # links waiting for the AI are dropped after this
# Searches run once a day on WordPress sites that allow them in robots.txt (their built-in search feed).
SEARCH_TERMS = ["hackathon", "capture the flag", "hackforgov"]


def llm_provider() -> str:
    """Which AI service extraction uses this run, or '' when none is configured."""
    if LLM_PROVIDER == "gemini" and GEMINI_API_KEY:
        return "gemini"
    if LLM_PROVIDER == "anthropic" and ANTHROPIC_API_KEY:
        return "anthropic"
    if not LLM_PROVIDER:
        return "gemini" if GEMINI_API_KEY else "anthropic" if ANTHROPIC_API_KEY else ""
    return ""

NOMINATIM_URL = os.environ.get("HACKINTEL_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
NOMINATIM_ENABLED = os.environ.get("HACKINTEL_NOMINATIM", "1") == "1"

# Publication rules
LINK_OK_WINDOW_H = 72        # a link check must have passed within this many hours
MANUAL_OK_WINDOW_D = 7       # a manual verification counts for this many days
MAX_LINK_FAILURES = 3        # consecutive failed checks before an event is hidden
KEEP_CLOSED_DAYS = 30        # (legacy) not used for finished events; see PAST_WINDOW_D
PAST_WINDOW_D = 62           # finished events stay on the map (as past pins) for about two months
NEW_WINDOW_H = 72            # "NEW" badge for events first seen within this window

KEYWORDS = ["hackathon", "hack-a-thon", "hackfest", "buildathon", "codefest", "code fest", "ideathon",
            "datathon", "hack4", "hack for", "hacks",
            # CTF and cybersecurity competitions
            "capture the flag", "capture-the-flag", "ctf", "hackforgov", "hack4gov", "cybersecurity competition",
            "cyber security competition", "cybersecurity challenge", "hacking competition", "cyber defense competition",
            "cyber olympiad", "cyber challenge"]

# Anything matching this is shown as a CTF / cybersecurity pin; everything else as a hackathon.
CTF_PATTERN = (r"\b(ctf|capture[- ]the[- ]flag|hackforgov|hack4gov|cyber ?security (competition|challenge|olympiad)|"
               r"hacking competition|cyber (defen[cs]e|challenge|olympiad|range)|red team|blue team|jeopardy)\b")
