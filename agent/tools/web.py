# agent/tools/web.py
"""web_fetch / web_search.

web_fetch uses urllib by default (fast, no Chrome). Set AGENT_BROWSER=selenium
to opt into headless Chrome for JS-rendered pages (source installs only).

web_search tries a chain of independent backends so a block or rate-limit on
one does not stop the search:

    1. DuckDuckGo HTML endpoint   (structured, keyless, fast)
    2. Bing HTML scrape           (different infrastructure, keyless)
    3. DuckDuckGo Instant Answer  (separate JSON endpoint)
    4. curl subprocess -> Bing    (bypasses Python's HTTP stack entirely)

All parsing is stdlib-only, so this works in the shipped .exe with no extra
dependencies.
"""
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .registry import tool

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False


def _use_browser() -> bool:
    """Selenium is off unless AGENT_BROWSER=selenium is set."""
    return HAS_SELENIUM and os.environ.get("AGENT_BROWSER", "").lower() == "selenium"


# ---------------------------------------------------------------- web_fetch
@tool("web_fetch", "Fetch a URL and return readable text (HTML stripped).",
      {"url": {"type": "string"}}, ["url"])
def web_fetch(cfg, url):
    if _use_browser():
        options = Options()
        options.add_argument("--headless=new")
        # Do NOT pass --no-sandbox unless you know why.
        options.add_argument("--disable-gpu")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        driver = None
        try:
            driver = webdriver.Chrome(
                service=ChromeService(ChromeDriverManager().install()),
                options=options,
            )
            driver.get(url)
            time.sleep(1.5)  # give JS a moment
            return driver.find_element(By.TAG_NAME, "body").text.strip()
        except Exception:
            # Fall through to the urllib path below.
            pass
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    req = urllib.request.Request(url, headers={"User-Agent": "omni-agent/2.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(2_000_000).decode(errors="replace")
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- web_search backends
_DDG_URL = "https://html.duckduckgo.com/html/"
_DDG_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/119.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
}

_BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0.0.0 Safari/537.36")


class _DDGParser(HTMLParser):
    """Extract (title, url, snippet) triples from DuckDuckGo's HTML results."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._mode = None       # "title" | "snippet"
        self._buf = []
        self._current = None    # the result dict being built

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "")
        href = attrs_d.get("href", "")
        if "result__a" in cls and tag == "a":
            # DDG wraps links as //duckduckgo.com/l/?uddg=<encoded>
            m = re.search(r"[?&]uddg=([^&]+)", href)
            real_url = urllib.parse.unquote(m.group(1)) if m else href
            if not real_url.startswith(("http://", "https://")):
                return
            self._current = {"title": "", "url": real_url, "snippet": ""}
            self.results.append(self._current)
            self._mode = "title"
            self._buf = []
        elif "result__snippet" in cls and self._current is not None:
            self._mode = "snippet"
            self._buf = []

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._mode == "title" and tag == "a" and self._current is not None:
            self._current["title"] = "".join(self._buf).strip()
            self._mode = None
        elif self._mode == "snippet" and self._current is not None:
            self._current["snippet"] = " ".join("".join(self._buf).split())[:300]
            self._mode = None


def _search_ddg(query, max_results):
    """Layer 1: DuckDuckGo HTML endpoint."""
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(_DDG_URL, data=data, headers=_DDG_HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read(1_500_000).decode("utf-8", "replace")
    except Exception as e:
        raise RuntimeError(f"ddg request: {e}")
    # DDG's bot-detection page.
    if "anomaly-modal" in html or "captcha" in html.lower()[:2000]:
        raise RuntimeError("ddg blocked")
    parser = _DDGParser()
    parser.feed(html)
    hits = [r for r in parser.results if r["title"] and r["url"]][:max_results]
    if not hits:
        raise RuntimeError("ddg empty")
    return hits


def _parse_bing_html(html, max_results):
    """Shared Bing result-block parser used by layers 2 and 4."""
    hits = []
    for block in re.findall(r'<li[^>]*class="[^"]*\bb_algo\b[^"]*"[^>]*>.*?</li>', html, re.S):
        m = re.search(r'<h2>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url_val, title_raw = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snip_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = re.sub(r"<[^>]+>", "", snip_m.group(1)).strip()[:300] if snip_m else ""
        if title_raw and url_val.startswith("http"):
            hits.append({"title": title_raw, "url": url_val, "snippet": snippet})
        if len(hits) >= max_results:
            break
    return hits


def _search_bing(query, max_results):
    """Layer 2: Bing HTML scrape (stdlib only)."""
    url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _BING_UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read(1_500_000).decode("utf-8", "replace")
    except Exception as e:
        raise RuntimeError(f"bing request: {e}")
    hits = _parse_bing_html(html, max_results)
    if not hits:
        raise RuntimeError("bing empty")
    return hits


def _search_ddg_api(query, max_results):
    """Layer 3: DuckDuckGo Instant Answer API (separate JSON endpoint)."""
    url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        raise RuntimeError(f"ddg-api: {e}")

    hits = []
    if data.get("AbstractText") and data.get("AbstractURL"):
        hits.append({"title": data.get("Heading") or query,
                     "url": data["AbstractURL"],
                     "snippet": data["AbstractText"][:300]})
    for topic in (data.get("RelatedTopics") or []):
        if len(hits) >= max_results:
            break
        if isinstance(topic, dict) and topic.get("FirstURL"):
            text = re.sub(r"<[^>]+>", "", topic.get("Text", ""))
            hits.append({"title": text.split(" - ")[0][:120] or "result",
                         "url": topic["FirstURL"],
                         "snippet": text[:300]})
        elif isinstance(topic, dict) and topic.get("Topics"):
            # Grouped topics — flatten once.
            for sub in topic["Topics"]:
                if len(hits) >= max_results:
                    break
                if isinstance(sub, dict) and sub.get("FirstURL"):
                    text = re.sub(r"<[^>]+>", "", sub.get("Text", ""))
                    hits.append({"title": text.split(" - ")[0][:120] or "result",
                                 "url": sub["FirstURL"],
                                 "snippet": text[:300]})
    if not hits:
        raise RuntimeError("ddg-api empty")
    return hits[:max_results]


def _search_curl(query, max_results):
    """Layer 4: shell out to curl against Bing.

    Uses a completely separate HTTP stack from urllib, so it survives proxy
    misconfiguration, a broken Python CA bundle, or a TLS fingerprint that
    Bing's edge rejects.
    """
    url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", "15",
             "-A", _BING_UA,
             url],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl exit {result.returncode}")
        html = result.stdout
    except FileNotFoundError:
        raise RuntimeError("curl not installed")
    except subprocess.TimeoutExpired:
        raise RuntimeError("curl timeout")

    hits = _parse_bing_html(html, max_results)
    if not hits:
        raise RuntimeError("curl empty")
    return hits


@tool(
    "web_search",
    "Search the web. Tries several independent backends (DuckDuckGo HTML, "
    "Bing, DuckDuckGo Instant Answer, curl) so a block or rate-limit on one "
    "does not stop the search. Returns titles, URLs, and snippets.",
    {"query": {"type": "string"}, "max_results": {"type": "integer"}},
    ["query"],
)
def web_search(cfg, query, max_results=8):
    max_results = min(max(1, max_results or 8), 15)

    errors = []
    for label, fn in (
        ("duckduckgo", _search_ddg),
        ("bing", _search_bing),
        ("ddg-api", _search_ddg_api),
        ("curl", _search_curl),
    ):
        try:
            hits = fn(query, max_results)
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue

        lines = []
        for i, r in enumerate(hits, 1):
            lines.append(f"{i}. {r['title']}\n   {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
        # Tell the model which backend answered, so it knows the provenance
        # when a fallback layer produced the results.
        header = "" if label == "duckduckgo" else f"[results via {label}]\n"
        return header + "\n".join(lines)

    return "ERROR: all search backends failed:\n  " + "\n  ".join(errors)