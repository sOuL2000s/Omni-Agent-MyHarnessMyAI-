import re
import urllib.request
from .registry import tool


@tool("web_fetch", "Fetch a URL and return readable text (HTML stripped).", {"url": {"type": "string"}}, ["url"])
def web_fetch(cfg, url):
    req = urllib.request.Request(url, headers={"User-Agent": "omni-agent/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(2_000_000).decode(errors="replace")
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()
