"""Polite HTTP client: honest User-Agent, robots.txt, one request per host every few seconds."""
from __future__ import annotations

import re
import time
import urllib.parse
import urllib.robotparser

import requests

from . import config


class Http:
    def __init__(self, user_agent: str = config.USER_AGENT, delay: float = config.PER_HOST_DELAY,
                 timeout: float = config.HTTP_TIMEOUT, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Language": "en-PH,en;q=0.8,fil;q=0.5"})
        self.ua = user_agent
        self.delay = delay
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._wild: dict[str, list] = {}   # wildcard Disallow rules Python's robotparser does not understand
        self._host_delay: dict[str, float] = {}   # Crawl-delay from robots.txt (capped at 15 s)
        self.blocked: set[str] = set()            # platforms never fetched, even through a redirect
        self.log: list[dict] = []

    def _host(self, url: str) -> str:
        p = urllib.parse.urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _wait(self, host: str):
        wait = max(self.delay, self._host_delay.get(host, 0)) - (time.time() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.time()

    def allowed(self, url: str) -> bool:
        host = self._host(url)
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._wait(host)
                r = self.s.get(host + "/robots.txt", timeout=self.timeout)
                if r.status_code >= 500:
                    rp = None          # server trouble: treat as "try later", not as permission
                elif r.status_code >= 400:
                    rp.parse([])       # no robots.txt: everything allowed
                else:
                    lines = r.text.splitlines()
                    rp.parse(lines)
                    self._wild[host] = wildcard_disallows(lines, self.ua)
                    try:
                        cd = float(rp.crawl_delay(self.ua) or 0)
                    except (TypeError, ValueError):
                        cd = 0.0
                    if cd > 0:
                        self._host_delay[host] = min(cd, 15.0)
            except requests.RequestException:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        if not (rp and rp.can_fetch(self.ua, url)):
            return False
        p = urllib.parse.urlsplit(url)
        path = (p.path or "/") + ("?" + p.query if p.query else "")
        # Be stricter than required: a wildcard Disallow that matches wins, even over an Allow line.
        return not any(rx.match(path) for rx in self._wild.get(host, []))

    def is_blocked(self, url: str) -> bool:
        h = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
        return h in self.blocked or any(h.endswith("." + b) for b in self.blocked)

    def get(self, url: str, params: dict | None = None, respect_robots: bool = True, method: str = "GET"):
        """Returns a Response, or None when blocked (robots.txt, a blocked platform) or the request failed.

        Redirects are followed by hand, at most 5 hops, so every hop is checked against the blocked
        platforms and (when respect_robots) robots.txt. Query parameters are part of the robots check."""
        if params:
            url = requests.Request("GET", url, params=params).prepare().url
        first = url
        for _ in range(6):
            if self.is_blocked(url):
                self._fail(first, url, "redirects to a platform that does not allow automated access" if url != first
                           else "platform does not allow automated access")
                return None
            if respect_robots and not self.allowed(url):
                rp = self._robots.get(self._host(url))
                why = "robots.txt could not be read" if rp is None else "robots.txt disallows this URL for our bot"
                self._fail(first, url, why)
                return None
            self._wait(self._host(url))
            try:
                r = self.s.request(method, url, timeout=self.timeout, allow_redirects=False)
            except requests.RequestException as e:
                self._fail(first, url, f"{type(e).__name__}: {e}"[:300])
                return None
            loc = r.headers.get("Location") if r.is_redirect else None
            if not loc:
                return r
            nxt = urllib.parse.urljoin(url, loc)
            if urllib.parse.urlsplit(nxt).scheme not in ("http", "https"):
                return r
            if r.status_code == 303 or (r.status_code in (301, 302) and method == "POST"):
                method = "GET"
            url = nxt
        self._fail(first, url, "too many redirects")
        return None

    def _fail(self, first: str, url: str, why: str):
        if url != first:
            why = f"{why} ({url})"
        self.log.append({"url": first, "error": why})

    def why(self, url: str) -> str:
        """Last recorded failure reason for a URL."""
        for entry in reversed(self.log):
            if entry["url"] == url:
                return entry["error"]
        return "no response"

    def check_link(self, url: str) -> tuple[bool | None, str]:
        """Is a registration/official link alive? HEAD first, GET if HEAD is refused.

        Returns (True, status) alive, (False, reason) dead, (None, reason) inconclusive.
        Inconclusive: the server answered the TLS handshake but its certificate chain could not be
        verified here. Usually the server omits an intermediate certificate; browsers fetch it
        themselves (AIA), Python does not. We never turn verification off to "fix" this."""
        r = self.get(url, respect_robots=False, method="HEAD")  # a single link check, like a person clicking
        if r is None or r.status_code in (403, 405, 501):
            r = self.get(url, respect_robots=False)   # some servers refuse or drop HEAD requests
        if r is None:
            why = self.why(url)
            if "does not allow automated access" in why:
                return None, "inconclusive: " + why
            if "CERTIFICATE_VERIFY_FAILED" in why or "SSLCertVerificationError" in why:
                return None, "inconclusive: site's TLS certificate chain could not be verified by the scanner"
            return False, why
        return (r.status_code < 400), f"HTTP {r.status_code}"


def wildcard_disallows(lines: list[str], user_agent: str) -> list:
    """Disallow rules with * or $ (Google-style wildcards) for our bot, as compiled regexes.

    urllib.robotparser compares paths literally, so a rule like "Disallow: /*?s=" would not stop it.
    We apply the group that names our bot if there is one, else the "*" group."""
    token = user_agent.split("/")[0].strip().lower()        # "ph-hackintel"
    groups, agents, rules, in_rules = [], [], [], False
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                groups.append((agents, rules))
                agents, rules, in_rules = [], [], False
            agents.append(val.lower())
        elif key in ("allow", "disallow"):
            in_rules = True
            rules.append((key, val))
    if agents:
        groups.append((agents, rules))
    mine = [g for g in groups if any(a != "*" and a and a in token for a in g[0])]
    chosen = mine or [g for g in groups if "*" in g[0]]
    out = []
    for _, rs in chosen:
        for key, val in rs:
            if key == "disallow" and val and ("*" in val or val.endswith("$")):
                rx = re.escape(val).replace(r"\*", ".*")
                if rx.endswith(r"\$"):
                    rx = rx[:-2] + "$"
                out.append(re.compile(rx))
    return out
