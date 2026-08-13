"""Client HTTP minimal : rate-limit par seau a jetons + backoff sur 429.

Sans dependance externe, pour que le projet tourne sur un Python nu.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass

log = logging.getLogger(__name__)


class RateLimited(Exception):
    """L'API a repondu 429 apres epuisement des tentatives."""


@dataclass(slots=True)
class RateLimiter:
    """Autorise au plus `max_calls` appels par `period` secondes."""

    max_calls: int
    period: float = 60.0
    _calls: deque[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._calls = deque()

    def acquire(self) -> float:
        """Bloque si necessaire. Renvoie le temps d'attente consommé."""
        waited = 0.0
        while True:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return waited
            sleep_for = self.period - (now - self._calls[0]) + 0.01
            log.debug("Rate-limit : attente de %.1f s", sleep_for)
            time.sleep(sleep_for)
            waited += sleep_for


class HttpClient:
    """GET JSON avec rate-limit, retries et backoff exponentiel."""

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
        user_agent: str = "cs-tradeup-algo/0.1",
        timeout: float = 20.0,
        max_retries: int = 4,
        backoff_base: float = 5.0,
        headers: dict[str, str] | None = None,
    ):
        self.limiter = rate_limiter
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if headers:
            self.headers.update(headers)

    def get_text(self, url: str, params: dict[str, object] | None = None) -> str | None:
        """Recupere une page brute (HTML). Meme rate-limit que get_json."""
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        for attempt in range(self.max_retries + 1):
            if self.limiter:
                self.limiter.acquire()
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code in (429, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(self._retry_delay(exc, attempt))
                    continue
                if exc.code == 429:
                    raise RateLimited(f"429 persistant sur {url}") from exc
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base * (2**attempt))
                    continue
                raise
        return None

    def get_json(self, url: str, params: dict[str, object] | None = None) -> dict | list | None:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        for attempt in range(self.max_retries + 1):
            if self.limiter:
                self.limiter.acquire()
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if body.strip() else None
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                if exc.code in (429, 502, 503, 504) and attempt < self.max_retries:
                    delay = self._retry_delay(exc, attempt)
                    log.warning(
                        "HTTP %s sur %s ; nouvelle tentative dans %.0f s (%d/%d)",
                        exc.code, url, delay, attempt + 1, self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                if exc.code == 429:
                    raise RateLimited(
                        f"429 persistant sur {url}\n"
                        "Le quota de l'API est epuise sur une fenetre longue. "
                        "Reessayer immediatement ne sert a rien : attends "
                        "quelques minutes, et baisse --rate."
                    ) from exc
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < self.max_retries:
                    delay = self.backoff_base * (2**attempt)
                    log.warning("Erreur reseau (%s) ; retry dans %.0f s", exc, delay)
                    time.sleep(delay)
                    continue
                raise
        return None

    def _retry_delay(self, exc: urllib.error.HTTPError, attempt: int) -> float:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self.backoff_base * (2**attempt)
