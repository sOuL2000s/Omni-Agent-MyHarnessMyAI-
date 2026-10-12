"""OpenAI-compatible chat client with automatic failover across providers (stdlib only)."""
import json
import sys
import time
import urllib.request
import urllib.error


class LLMError(Exception):
    pass


# seconds a failing provider is skipped before being tried again
COOLDOWN = {400: 120, 401: 600, 403: 600, 404: 300, 429: 60}


def _once(p, cfg, body):
    headers = {"Content-Type": "application/json"}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    data = json.dumps({**body, "model": p.model}).encode()
    req = urllib.request.Request(p.base_url + "/chat/completions", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]


def ping(p, cfg):
    return _once(p, cfg, {"messages": [{"role": "user", "content": "Reply with the word pong."}], "max_tokens": 10})


def chat(cfg, messages, tools=None):
    body = {"messages": messages, "temperature": cfg.temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    order = cfg.providers.order()
    if not order:
        raise LLMError("No provider configured. Use:  /add openrouter YOUR_KEY   (or edit .env)")
    errors = []
    for i, p in enumerate(order):
        attempts = 3 if len(order) == 1 else 1
        err, cd = "unknown", 30
        for a in range(attempts):
            try:
                return _once(p, cfg, body)
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
                cd = COOLDOWN.get(e.code, 30)
                if e.code in (400, 401, 403, 404):
                    break  # retrying the same provider won't help
            except Exception as e:  # network, timeout, bad JSON
                err, cd = str(e), 30
            if a < attempts - 1:
                time.sleep(2 ** a)
        cfg.providers.mark_failed(p.name, cd)
        errors.append(f"{p.name}: {err}")
        if i + 1 < len(order):
            print(f"  ! {p.name} failed ({err[:90]}) -> switching to {order[i + 1].name}", file=sys.stderr)
    raise LLMError("All providers failed:\n" + "\n".join(errors))
