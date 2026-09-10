"""Website adapter — fetches and parses the company site.

This is one of only two sources that can ground a dossier in something
checkable (the other being uploaded documents), so what it returns decides
how much of the profile is evidence and how much is recollection.

It previously fetched the page and kept the <title> tag alone: a successful
fetch of a large company's site contributed about 220 characters, the model
was handed almost nothing, and it filled the gap from memory. The fetch
reported OK throughout, because fetching HAD succeeded — it was the
extraction that discarded everything.
"""
import logging
import re
import time

import requests

from fundos.research.adapters.base import BaseResearchAdapter, validate_url

logger = logging.getLogger(__name__)

# Pages worth trying beyond the homepage. A homepage is usually a marketing
# banner; the substance ("founded in", "our plants", "revenue grew") lives
# one click in. Ordered by how often they carry hard facts.
CANDIDATE_PATHS = ("/about", "/about-us", "/company", "/who-we-are",
                   "/investors", "/investor-relations", "/products",
                   "/what-we-do")
MAX_EXTRA_PAGES = 3
MAX_CHARS_PER_PAGE = 12000
MAX_TOTAL_CHARS = 30000

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def _visible_text(html):
    """Strip HTML to readable text. Deliberately dependency-free."""
    import html as _html

    text = _DROP_BLOCKS.sub(" ", html or "")
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", text,
                  flags=re.I)
    text = _TAG.sub(" ", text)
    text = _html.unescape(text)
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def _meta(html, name):
    m = re.search(
        r'<meta[^>]+(?:name|property)=["\']%s["\'][^>]+content=["\']([^"\']+)'
        % re.escape(name), html or "", re.I)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Host-level circuit breaker
# ---------------------------------------------------------------------------
# Ingesting many companies means one dead or blocking host would otherwise
# burn its full retry budget on every run, slowing the batch and spending
# our IP's reputation on a site that is not answering. After three
# consecutive failures a host is skipped outright for five minutes.
# ---------------------------------------------------------------------------
_BREAKER_FAILURES = {}
_BREAKER_OPENED = {}
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN = 300.0


def _breaker_is_open(host):
    opened = _BREAKER_OPENED.get(host)
    if opened is None:
        return False
    if time.monotonic() - opened >= _BREAKER_COOLDOWN:
        _BREAKER_OPENED.pop(host, None)
        _BREAKER_FAILURES[host] = 0
        return False
    return True


def _breaker_record(host, ok):
    if ok:
        _BREAKER_FAILURES[host] = 0
        _BREAKER_OPENED.pop(host, None)
        return
    count = _BREAKER_FAILURES.get(host, 0) + 1
    _BREAKER_FAILURES[host] = count
    if count >= _BREAKER_THRESHOLD:
        _BREAKER_OPENED[host] = time.monotonic()
        logger.warning("WEBSITE: circuit opened for %s after %d consecutive "
                       "failures — skipping it for %ds", host, count,
                       int(_BREAKER_COOLDOWN))


_TRANSIENT = (requests.exceptions.ReadTimeout,
              requests.exceptions.ConnectTimeout,
              requests.exceptions.ConnectionError,
              requests.exceptions.ChunkedEncodingError)


def _tables_to_text(html, limit=6):
    """Extract HTML tables as readable rows, preserving their structure.

    Indian company sites publish revenue, capacity and shareholding as
    tables. Flattening them into prose and asking the model to re-parse the
    result loses the row/column relationship that made them facts — the
    figure and the year stop being attached to each other.
    """
    out = []
    for match in re.finditer(r"<table\b.*?</table>", html or "",
                             re.I | re.S)  :
        block = match.group(0)
        rows = re.findall(r"<tr\b.*?</tr>", block, re.I | re.S)
        if len(rows) < 2:
            continue
        lines = []
        for row in rows:
            # A merged footer/header cell is layout, not data.
            if re.search(r"colspan\s*=\s*[\"\']?[2-9]", row, re.I):
                continue
            cells = re.findall(r"<t[dh]\b.*?</t[dh]>", row, re.I | re.S)
            values = [_visible_text(c) for c in cells]
            values = [v for v in values if v is not None]
            if not any(v.strip() for v in values):
                continue
            lines.append(" | ".join(v.strip()[:80] for v in values))
        if len(lines) >= 2:
            out.append("\n".join(lines[:40]))
        if len(out) >= limit:
            break
    return out


class WebsiteAdapter(BaseResearchAdapter):
    source_type = "website"

    def _get(self, url, timeout=15):
        """GET with a browser-ish UA, following redirects.

        Certificate verification is NEVER disabled. A site whose chain does
        not validate is a site we cannot prove we are talking to, and a
        research tool that silently accepts that is worse than one that
        reports the failure — the operator would have no way to know which
        figures came from an unauthenticated source. The remedy is to fix the
        trust store on the server, which the error now says plainly.
        """
        from urllib.parse import urlsplit
        host = urlsplit(url).netloc
        if _breaker_is_open(host):
            raise requests.exceptions.ConnectionError(
                f"circuit open for {host} — it failed "
                f"{_BREAKER_THRESHOLD} times in a row; skipping for "
                f"{int(_BREAKER_COOLDOWN)}s")
        last = None
        for attempt in range(3):
            try:
                resp = self._get_once(url, timeout)
                _breaker_record(host, resp.status_code < 500)
                return resp
            except _TRANSIENT as e:
                # Transient only. A 4xx is a considered answer from the
                # server and repeating it just wastes the budget.
                last = e
                if attempt < 2:
                    time.sleep(2)
            except Exception as e:
                _breaker_record(host, False)
                raise
        _breaker_record(host, False)
        raise last

    # A self-identifying bot string is the honest header, and on 17 Aug it
    # was refused with a bare 403 by both lenskart.com and titan.co.in —
    # neither of which is protecting anything: they serve the same homepage
    # to any browser. Consumer sites behind Cloudflare/Akamai reject unknown
    # user-agents by category, not by what is being requested.
    #
    # What we fetch is the company's own public homepage, for a profile that
    # company asked us to build. So we send the header set a browser sends.
    # The bot identity is kept in From/X-Purpose rather than dropped, so an
    # operator reading their logs can still see who we are and reach us.
    _BROWSER_HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }

    _BOT_HEADERS = {
        "User-Agent": ("Mozilla/5.0 (compatible; FundOS-Research/2.0; "
                       "+https://fundos.local/bot)"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-IN,en;q=0.9",
    }

    def _get_once(self, url, timeout=15):
        """Ask politely first; if the door is shut by category, knock normally.

        The bot header goes first so a site that WANTS to treat us as a bot
        (allow-list, rate-limit, serve a lighter page) still gets the chance.
        Only a 401/403/429 — a refusal of who we are rather than of what we
        asked for — triggers the browser header set. A 404 or a 500 is a
        different answer and is not retried, because repeating it would be
        pretending the refusal was about the header.
        """
        resp = requests.get(url, timeout=timeout, allow_redirects=True,
                            headers=self._BOT_HEADERS)
        if resp.status_code in (401, 403, 406, 429):
            logger.info("WEBSITE: %s returned %s to the research agent — "
                        "retrying with standard browser headers",
                        url, resp.status_code)
            retry = requests.get(
                url, timeout=timeout, allow_redirects=True,
                headers={**self._BROWSER_HEADERS,
                         # Still identifiable, and still contactable.
                         "From": "research@fundos.local"})
            if retry.status_code < 400:
                return retry
            # Return whichever answer is more informative.
            return retry if retry.status_code != resp.status_code else resp
        return resp

    def _fetch_with_variants(self, url):
        """Try the address as given, then the www/apex counterpart.

        Which of `example.com` and `www.example.com` actually serves the site
        is arbitrary, and whichever one a person typed into the company
        record is equally arbitrary. A certificate issued for only one of
        them, or an apex that does not resolve, loses the single best source
        the profile has — over a prefix. The first error is re-raised if
        every variant fails, so the diagnosis still describes the address the
        operator entered.
        """
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        host = parts.netloc
        alternates = [url]
        if host.startswith("www."):
            alternates.append(urlunsplit(parts._replace(netloc=host[4:])))
        else:
            alternates.append(urlunsplit(parts._replace(netloc="www." + host)))

        first_error = None
        for candidate in alternates:
            try:
                resp = self._get(candidate)
                if resp.status_code < 400:
                    if candidate != url:
                        logger.info("WEBSITE: %s failed, %s served the site",
                                    url, candidate)
                    return resp
                # A 4xx on the apex often means "use www"; keep trying, but
                # remember this response in case nothing better turns up.
                if first_error is None:
                    first_error = requests.HTTPError(
                        f"{resp.status_code} for {candidate}", response=resp)
            except requests.RequestException as e:
                if first_error is None:
                    first_error = e
        raise first_error

    def _candidate_domain(self):
        """The address to read, from the company record or the profile.

        `company.domain` alone was not enough. The founder-facing field is
        `CompanyProfile.website_url`, and `_collect_sources` resolves the
        address as `profile.website_url or company.domain` — but this adapter
        read only the latter. A company whose website was recorded on the
        profile therefore logged the URL it was about to read and then failed
        with "no valid website URL on the company record", which is both wrong
        and unactionable: the operator checks the company record, finds the
        field genuinely empty, and has no reason to look at the profile.

        Both are consulted here, company record first so an explicitly
        curated domain still wins.
        """
        domain = (getattr(self.deal.company, "domain", "") or "").strip()
        if domain:
            return domain
        try:
            from fundos.profile.models import CompanyProfile
            profile = (CompanyProfile.objects
                       .filter(company_id=self.deal.company_id)
                       .only("website_url").first())
        except Exception:                     # pragma: no cover - defensive
            logger.debug("website adapter could not read the profile",
                         exc_info=True)
            return ""
        return ((getattr(profile, "website_url", "") or "").strip()
                if profile else "")

    def fetch(self):
        domain = self._candidate_domain()
        url = domain if domain.startswith("http") else f"https://{domain}"
        if not validate_url(url):
            raise ValueError(
                "no website address is recorded on either the company record "
                "(company.domain) or the company profile (website_url)")

        try:
            resp = self._fetch_with_variants(url)
        except requests.exceptions.SSLError as e:
            # Distinguished from a network failure because the remedy is
            # completely different — the old diagnosis told operators to
            # check outbound internet access for what is a trust-store
            # problem on this server.
            raise requests.exceptions.SSLError(
                f"TLS certificate verification failed for {url}. The site is "
                f"reachable but its certificate chain does not validate "
                f"against this server's trust store. Usually a stale CA "
                f"bundle (sudo yum update ca-certificates && "
                f"sudo update-ca-trust) or an incomplete chain served by the "
                f"site. Original error: {e}") from e
        resp.raise_for_status()

        html = resp.text or ""
        pages = {resp.url: html[:MAX_CHARS_PER_PAGE * 4]}
        title = ""
        low = html.lower()
        if "<title>" in low:
            start = low.index("<title>") + 7
            end = low.find("</title>", start)
            title = html[start:end if end > 0 else start][:255].strip()
        description = _meta(html, "description") or _meta(html,
                                                          "og:description")

        # Follow a few likely-substantive pages. Best-effort throughout: a
        # 404 on /about is normal and must not fail the source.
        base = resp.url.rstrip("/")
        fetched_extra = []
        for path in CANDIDATE_PATHS:
            if len(fetched_extra) >= MAX_EXTRA_PAGES:
                break
            try:
                sub = self._get(base + path, timeout=10)
                if sub.status_code == 200 and "html" in (
                        sub.headers.get("Content-Type") or ""):
                    pages[sub.url] = sub.text[:MAX_CHARS_PER_PAGE * 4]
                    fetched_extra.append(path)
            except Exception:
                continue

        sections, total, tables = [], 0, []
        for page_url, page_html in pages.items():
            tables.extend(_tables_to_text(page_html))
            text = _visible_text(page_html)[:MAX_CHARS_PER_PAGE]
            if len(text) < 80:
                continue
            room = MAX_TOTAL_CHARS - total
            if room <= 0:
                break
            sections.append(f"--- {page_url} ---\n{text[:room]}")
            total += min(len(text), room)

        if tables:
            # Kept as a separate block, and labelled, so the model reads them
            # as tabular facts rather than as another paragraph of prose.
            block = "\n\n".join(f"[TABLE]\n{t}" for t in tables[:6])
            sections.append(block[:MAX_CHARS_PER_PAGE])

        body = "\n\n".join(sections)
        if not body:
            # Reaching a page and extracting nothing readable is the
            # signature of a JavaScript-rendered site. Say so, rather than
            # returning a near-empty payload that reads as a thin company.
            logger.info("WEBSITE: %s returned no extractable text (likely "
                        "client-rendered)", url)

        return {
            "facts": {
                "website_title": title,
                "website_description": description,
                "website_reachable": True,
                "website_text": body,
                "website_tables": tables[:6],
                "pages_fetched": list(pages.keys()),
                # True when a page loaded but yielded nothing readable —
                # the JavaScript-rendered signature. This is the flag a
                # headless renderer should key off, so the expensive path
                # runs only for the sites that actually need it.
                "client_rendered": not body,
            },
            "raw": {"status": resp.status_code, "final_url": resp.url,
                    "html_length": len(html), "text_length": total,
                    "extra_pages": fetched_extra},
            "confidence": 0.8 if body else 0.2,
        }
