"""Polite HTTP client: honest User-Agent, robots.txt, one request per host every few seconds."""
from __future__ import annotations

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
        self.log: list[dict] = []

    def _host(self, url: str) -> str:
        p = urllib.parse.urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _wait(self, host: str):
        wait = self.delay - (time.time() - self._last.get(host, 0))
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
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return bool(rp and rp.can_fetch(self.ua, url))

    def get(self, url: str, params: dict | None = None, respect_robots: bool = True, method: str = "GET"):
        """Returns a Response, or None when blocked by robots.txt or the request failed."""
        if respect_robots and not self.allowed(url):
            rp = self._robots.get(self._host(url))
            why = "robots.txt could not be read" if rp is None else "robots.txt disallows this URL for our bot"
            self.log.append({"url": url, "error": why})
            return None
        self._wait(self._host(url))
        try:
            r = self.s.request(method, url, params=params, timeout=self.timeout, allow_redirects=True)
            return r
        except requests.RequestException as e:
            self.log.append({"url": url, "error": f"{type(e).__name__}: {e}"[:300]})
            return None

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
            if "CERTIFICATE_VERIFY_FAILED" in why or "SSLCertVerificationError" in why:
                return None, "inconclusive: site's TLS certificate chain could not be verified by the scanner"
            return False, why
        return (r.status_code < 400), f"HTTP {r.status_code}"
