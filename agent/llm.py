"""Minimal OpenAI-compatible chat client (stdlib only)."""
import json
import time
import urllib.request
import urllib.error


class LLMError(Exception):
    pass


def chat(cfg, messages, tools=None):
    body = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    last = None
    for attempt in range(4):
        req = urllib.request.Request(cfg.base_url + "/chat/completions", data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
                out = json.loads(r.read().decode())
            return out["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            last = f"HTTP {e.code}: {detail}"
            if e.code in (401, 403, 404, 400):
                raise LLMError(last)
        except Exception as e:  # network, timeout, bad JSON
            last = str(e)
        time.sleep(2 ** attempt)
    raise LLMError(f"LLM request failed after retries: {last}")
