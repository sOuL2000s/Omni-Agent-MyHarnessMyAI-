# agent/tools/web.py
"""web_fetch / web_search.

By default this uses urllib (fast, no Chrome, no scraping).
Set AGENT_BROWSER=selenium in .env to opt in to a headless Chrome via Selenium,
which can render JavaScript pages. That path is slower and downloads a
ChromeDriver on first use; it also scrapes Google for web_search, which is
against their ToS and breaks whenever they change markup.

The README recommends web_fetch for known URLs and only opting into
AGENT_BROWSER=selenium when you actually need JS rendering.
"""
import os
import re
import time
import urllib.parse
import urllib.request

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


@tool("web_search", "Search the web using Google (requires AGENT_BROWSER=selenium).",
      {"query": {"type": "string"}}, ["query"])
def web_search(cfg, query):
    if not _use_browser():
        return ("web_search needs a browser. Set AGENT_BROWSER=selenium in .env "
                "and install selenium + webdriver-manager. "
                "Prefer web_fetch on a known URL.")

    options = Options()
    options.add_argument("--headless=new")
    # Do NOT pass --no-sandbox unless you know why.
    options.add_argument("--disable-gpu")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()),
        options=options,
    )
    try:
        driver.get(f"https://www.google.com/search?q={urllib.parse.quote(query)}")
        time.sleep(2)
        results = []
        for g in driver.find_elements(By.CSS_SELECTOR, "div.g"):
            try:
                anchors = g.find_elements(By.TAG_NAME, "a")
                if not anchors:
                    continue
                link = anchors[0].get_attribute("href")
                title_el = g.find_elements(By.TAG_NAME, "h3")
                title = title_el[0].text if title_el else "No title"

                snippet = ""
                for cls in ("VwiC3b", "yXM94b", "MUwY0b"):
                    s_el = g.find_elements(By.CLASS_NAME, cls)
                    if s_el:
                        snippet = s_el[0].text
                        break

                if link and title:
                    results.append(f"Title: {title}\nLink: {link}\nSnippet: {snippet}\n")
            except Exception:
                continue

        if not results:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            return "Search results (raw text):\n" + body_text[:2000]

        return "\n".join(results[:10])
    except Exception as e:
        return f"Error during search: {str(e)}"
    finally:
        driver.quit()