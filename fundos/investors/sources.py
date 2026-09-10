"""Data source adapters.

Ported from the supplied scraper.py, with the anti-bot handling, retry policy
and circuit breaker preserved because they were correct and hard-won:

  * A realistic user agent, a 1920x1080 viewport, and removal of the
    `navigator.webdriver` flag.
  * Polite delays between article fetches.
  * Retry on transient network errors only. An HTTP 4xx/5xx is a definitive
    answer from the server and retrying it just burns IP reputation — the
    supplied code got this distinction right and it is preserved exactly.
  * A circuit breaker so a site outage stops the run instead of generating
    hundreds of failing requests.

TWO CHANGES
-----------
  * pyppeteer → Playwright. pyppeteer is unmaintained and pins an old Chromium.
    Everything above ports directly.
  * The adapter is registered by name and every operational setting comes from
    the DataSource row. Nothing about any particular publication is hardcoded,
    so a second source is a class plus a config row.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

_REGISTRY = {}


def register(key):
    def deco(cls):
        _REGISTRY[key] = cls
        return cls
    return deco


def get_adapter(key):
    cls = _REGISTRY.get(key)
    if cls is None:
        raise ValueError(
            f"No source adapter registered as {key!r}. "
            f"Available: {sorted(_REGISTRY)}")
    return cls()


# ---------------------------------------------------------------------------
# Circuit breaker — port of market_config.CircuitBreaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Thread-safe CLOSED → OPEN → HALF_OPEN breaker.

    Behaviour preserved from the original: after `failure_threshold`
    consecutive failures the circuit opens and blocks calls for
    `recovery_timeout` seconds, after which exactly one probe is allowed
    through before the circuit re-decides.
    """

    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(self, name, failure_threshold=3, recovery_timeout=300.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = self.CLOSED
        self._failures = 0
        self._last_failure = None
        self._lock = threading.Lock()

    def is_open(self):
        with self._lock:
            if self._state == self.OPEN:
                elapsed = time.monotonic() - (self._last_failure or 0)
                if elapsed >= self.recovery_timeout:
                    self._state = self.HALF_OPEN
                    logger.info("[breaker:%s] recovery elapsed — HALF_OPEN, "
                                "one probe allowed.", self.name)
                    return False
                return True
            return False

    def record_success(self):
        with self._lock:
            if self._state != self.CLOSED:
                logger.info("[breaker:%s] success — back to CLOSED.", self.name)
            self._state, self._failures, self._last_failure = self.CLOSED, 0, None

    def record_failure(self):
        with self._lock:
            self._failures += 1
            self._last_failure = time.monotonic()
            if self._failures >= self.failure_threshold:
                if self._state != self.OPEN:
                    logger.warning(
                        "[breaker:%s] %d consecutive failures — OPEN for %.0fs.",
                        self.name, self._failures, self.recovery_timeout)
                self._state = self.OPEN

    @property
    def state(self):
        with self._lock:
            return self._state


_BREAKERS = {}


def breaker_for(name, source):
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(
            name, failure_threshold=source.breaker_failure_threshold,
            recovery_timeout=float(source.breaker_recovery_seconds))
    return _BREAKERS[name]


def breaker_states():
    """For the diagnostics dashboard."""
    return {n: b.state for n, b in _BREAKERS.items()}


# ---------------------------------------------------------------------------
# Text sanitisation — port of market_config.DataValidator
# ---------------------------------------------------------------------------

def sanitize_text(value, max_length=500):
    if value is None:
        return ""
    s = str(value).replace("\x00", "").strip()
    s = " ".join(s.split())
    return s[:max_length]


def sanitize_url(url):
    """Reject non-http(s) schemes and control characters (log-injection guard)."""
    if not url:
        return ""
    s = str(url).strip()
    if "\r" in s or "\n" in s or "\x00" in s:
        return ""
    if not (s.startswith("http://") or s.startswith("https://")):
        return ""
    return s


# ---------------------------------------------------------------------------
# Base adapter
# ---------------------------------------------------------------------------

class SourceAdapter:
    """Returns a list of deal dicts. Keys match what pipeline.stage_explode
    expects: date, name, sector, subsector, business_model, amount,
    round_type, investors, lead_investor, source_url."""

    key = "base"

    def fetch(self, source, *, limit_articles=None, single_url=None):
        raise NotImplementedError


@register("inc42_funding_galore")
class FundingGaloreAdapter(SourceAdapter):
    """Weekly funding round-ups published as an HTML table per article.

    Table layout (8- or 9-column):
        0 Date  1 Company  2 Sector  3 Subsector  4 Business model
        5 Amount  6 Round type  7 Investors  8 Lead investor (9-col only)

    If the publisher changes the layout, change COLUMN_MAP — not the parser.
    """

    key = "inc42_funding_galore"
    COLUMN_MAP = {
        "date": 0, "name": 1, "sector": 2, "subsector": 3,
        "business_model": 4, "amount": 5, "round_type": 6,
        "investors": 7, "lead_investor": 8,
    }
    SKIP_MARKERS = ("source:", "note:", "only disclosed", "*including", "**mix")

    def fetch(self, source, *, limit_articles=None, single_url=None):
        urls = ([sanitize_url(single_url)] if single_url
                else self._discover(source, limit_articles))
        urls = [u for u in urls if u]
        if not urls:
            logger.warning("SOURCE %s: no article URLs resolved.", source.name)
            return []

        breaker = breaker_for(source.name, source)
        rows = []
        for i, url in enumerate(urls):
            if breaker.is_open():
                logger.warning("SOURCE %s: breaker OPEN — stopping after %d "
                               "articles.", source.name, i)
                break
            rows.extend(self._fetch_article(url, i, source, breaker))
            time.sleep(float(source.request_delay_seconds or 0.5))
        logger.info("SOURCE %s: %d rows from %d articles.",
                    source.name, len(rows), len(urls))
        return rows

    # -- discovery ---------------------------------------------------------

    def _discover(self, source, limit_articles):
        """Collect article URLs, manual overrides first.

        Manual URLs are prepended and de-duplicated against discovered ones —
        the supplied config.MANUAL_URL_LIST behaviour, which exists so a report
        published but not yet indexed can still be included.
        """
        limit = limit_articles or source.articles_per_run or 10
        manual = [sanitize_url(u) for u in (source.manual_urls or [])]
        manual = [u for u in manual if u]

        discovered = []
        try:
            discovered = self._discover_with_browser(source, limit)
        except Exception as e:
            logger.error("SOURCE %s: discovery failed: %s", source.name, e,
                         exc_info=True)

        merged = manual + [u for u in discovered if u not in manual]
        return merged[:limit]

    def _discover_with_browser(self, source, limit):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error(
                "SOURCE %s: Playwright is not installed. Install it and run "
                "`playwright install chromium`, or supply manual URLs on the "
                "data source.", source.name)
            return []

        urls = []
        with sync_playwright() as p:
            launch = {"headless": True,
                      "args": ["--no-sandbox", "--disable-gpu",
                               "--disable-infobars"]}
            if source.browser_path:
                launch["executable_path"] = source.browser_path
            browser = p.chromium.launch(**launch)
            try:
                page = browser.new_page(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/122.0.0.0 Safari/537.36"))
                page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',"
                    "{get:()=>undefined})")
                page.goto(source.base_url, timeout=45000)

                for _ in range(12):
                    found = page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)")
                    urls = list(dict.fromkeys(
                        u for u in found if self._looks_like_article(u)))
                    if len(urls) >= limit:
                        break
                    more = page.query_selector(
                        "text=/load more/i") or page.query_selector(
                        ".load-more, [class*=load-more]")
                    if not more:
                        break
                    more.click()
                    page.wait_for_timeout(1500)
            finally:
                browser.close()
        return urls[:limit]

    @staticmethod
    def _looks_like_article(url):
        u = (url or "").lower()
        return ("funding" in u and u.count("/") >= 4
                and not u.rstrip("/").endswith("funding-galore"))

    # -- article parsing ---------------------------------------------------

    def _fetch_article(self, url, index, source, breaker):
        import requests
        from bs4 import BeautifulSoup

        attempts = max(1, source.max_retries or 3)
        for attempt in range(attempts):
            try:
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                    timeout=20)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.content, "html.parser")
                table = soup.find("table")
                if not table:
                    # The page loaded; a missing table is a content problem,
                    # not a transport one. Do not trip the breaker for it.
                    breaker.record_success()
                    logger.warning("SOURCE: no table at %s", url)
                    return []
                rows = self._parse_table(table, url)
                breaker.record_success()
                return rows

            except requests.exceptions.HTTPError as e:
                breaker.record_failure()
                code = e.response.status_code if e.response is not None else "?"
                logger.error("SOURCE: HTTP %s for %s — not retrying.", code, url)
                return []
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as e:
                breaker.record_failure()
                logger.warning("SOURCE: attempt %d/%d transient error on %s: %s",
                               attempt + 1, attempts, url, e)
                if attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))
            except Exception as e:
                logger.error("SOURCE: parse error on %s: %s", url, e,
                             exc_info=True)
                return []

        logger.error("SOURCE: giving up on %s after %d attempts.", url, attempts)
        return []

    def _parse_table(self, table, url):
        rows_out = []
        trs = table.find_all("tr")
        start = 0
        if trs:
            first = trs[0]
            first_td = first.find("td")
            head_text = first_td.get_text(strip=True).lower() if first_td else ""
            if first.find("th") or any(
                    k in head_text for k in ("date", "week", "sl", "s.no",
                                             "sr.no", "no.")):
                start = 1

        cmap = self.COLUMN_MAP
        for tr in trs[start:]:
            tds = tr.find_all("td")
            if any(td.get("colspan") for td in tds):
                continue
            if len(tds) < 8:
                continue
            row_text = tr.get_text().lower()
            if any(m in row_text for m in self.SKIP_MARKERS):
                continue
            has_lead = len(tds) >= 9

            def cell(key, cap=200):
                idx = cmap[key]
                if idx >= len(tds):
                    return ""
                return sanitize_text(tds[idx].get_text(strip=True), cap)

            rows_out.append({
                "source": "inc42",
                "date": cell("date", 64),
                "name": cell("name", 200),
                "sector": cell("sector", 100),
                "subsector": cell("subsector", 100),
                "business_model": cell("business_model", 100),
                "amount": cell("amount", 100),
                "round_type": cell("round_type", 100),
                "investors": cell("investors", 1000),
                "lead_investor": cell("lead_investor", 200) if has_lead else "",
                "source_url": url,
            })
        return rows_out
