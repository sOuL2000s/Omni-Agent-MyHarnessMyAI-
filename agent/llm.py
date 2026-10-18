# agent/llm.py
"""OpenAI-compatible chat client with automatic key rotation, model fallback,
provider failover, rich progress events and light self-healing.

Order of attempts for one request
---------------------------------
for each pool (service) in priority order:
    for each model in [chosen model, fallback models...]:
        for each usable key (scored, not blind round-robin):
            try -> success: return
                   failure: classify, cooldown the right thing, continue

Notable behaviours
------------------
* Rich progress events: `notify` receives short human strings the UI can render.
* Self-healing: 400s that mention `tools` are retried once without tools; 404s
  trigger a model-list refresh and one retry with the closest match.
* Cancel is checked between attempts AND inside the wait loop.
"""
import datetime as _dt
import difflib
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request

UA = "omni-agent/2.1"


class LLMError(Exception):
    pass


class Fail(Exception):
    def __init__(self, kind, msg, scope="key_model", cooldown=0, status=None):
        super().__init__(msg)
        self.kind, self.msg, self.scope, self.cooldown, self.status = kind, msg, scope, cooldown, status


MIN_RATE_COOLDOWN = 10
DEFAULT_RATE_COOLDOWN = 45
NO_TEMP = set()

# Cap the on-disk / in-memory model caches so a long-running server doesn't
# grow them without bound.
_MODEL_CACHE_MAX = 64
_NO_TEMP_MAX = 128


# ---------------------------------------------------------------- classification
def _next_pacific_midnight():
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Los_Angeles"))
        nxt = (now + _dt.timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        return max(60, int((nxt - now).total_seconds()))
    except Exception:
        return 6 * 3600


def _retry_after(headers, text):
    try:
        v = headers.get("Retry-After") if headers else None
        if v and re.fullmatch(r"\d+(\.\d+)?", v.strip()):
            return float(v)
    except Exception:
        pass
    m = re.search(r'retryDelay\W+(\d+(?:\.\d+)?)s', text)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:retry|try again)\s*(?:in|after)\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|seconds|m|min)?", text, re.I)
    if m:
        n, u = float(m.group(1)), (m.group(2) or "s").lower()
        return n / 1000 if u == "ms" else n * 60 if u.startswith("m") and u != "ms" else n
    return None


def _short(text):
    try:
        d = json.loads(text)
        if isinstance(d, list) and d:
            d = d[0]
        e = d.get("error", d) if isinstance(d, dict) else d
        if isinstance(e, dict):
            msg = e.get("message") or json.dumps(e)
            raw = (e.get("metadata") or {}).get("raw")
            return (msg + (" | " + str(raw) if raw else ""))[:300]
    except Exception:
        pass
    return re.sub(r"\s+", " ", text)[:300]


def classify(status, text, headers=None):
    low = text.lower()
    msg = _short(text)
    retry = _retry_after(headers, text)
    prefix = f"HTTP {status}: " if status else ""

    if status == 400 and "temperature" in low and re.search(r"unsupported|not support|only the default|invalid", low):
        return Fail("param", msg, "none", 0, status)

    # Tools-schema complaints -> the caller may retry without tools.
    if status == 400 and "tools" in low and re.search(r"unsupported|not support|invalid|schema|parameters", low):
        return Fail("tools", msg, "none", 0, status)

    if status == 429 or "resource_exhausted" in low:
        if re.search(r"per[ _-]?day|daily|requestsperday|PerDay", text, re.I):
            return Fail("quota", prefix + "daily quota used up: " + msg, "key_model", _next_pacific_midnight(), status)
        cd = (retry + 2) if retry is not None else DEFAULT_RATE_COOLDOWN
        return Fail("rate", prefix + "rate limited: " + msg, "key_model", min(max(cd, MIN_RATE_COOLDOWN), 900), status)

    if (status == 401 or "api_key_invalid" in low or "api key not valid" in low or "api key expired" in low
            or "invalid api key" in low or "incorrect api key" in low or "invalid_api_key" in low
            or "reported as leaked" in low or "api key was reported" in low):
        return Fail("auth", prefix + "bad/expired/revoked key: " + msg, "key", 6 * 3600, status)

    if status == 402 or (re.search(r"insufficient (credits|funds|balance)|billing|payment required|credit", low)
                         and status in (400, 402, 403)):
        return Fail("billing", prefix + "billing/credits problem: " + msg, "key", 3600, status)

    if status == 403:
        return Fail("auth", prefix + "access denied for this key: " + msg, "key", 1800, status)

    if status == 404 or (status in (400, 422) and re.search(
            r"(model|models/).{0,100}(not found|not exist|does not exist|is not valid|invalid model|not supported|unsupported|unknown|no endpoints)"
            r"|no endpoints found|invalid model", low)):
        return Fail("model", prefix + "model not available: " + msg, "pool_model", 600, status)

    if status in (408, 409, 425) or (status and status >= 500):
        return Fail("server", prefix + "server error: " + msg, "pool_model", 20, status)

    return Fail("fatal", prefix + msg, "none", 0, status)


# ---------------------------------------------------------------- HTTP
def _post_json(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            txt = ""
        finally:
            e.close()
        return e.code, e.headers, txt
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise Fail("network", f"network error: {reason}", "pool_model", 15)
    except Exception as e:
        raise Fail("network", f"network error: {e}", "pool_model", 15)


def _get_json(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _headers(p):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    if p.api_key:
        h["Authorization"] = f"Bearer {p.api_key}"
    return h


def _normalise(msg):
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        if not isinstance(fn.get("arguments", ""), str):
            fn["arguments"] = json.dumps(fn["arguments"])
        elif fn.get("arguments") is None:
            fn["arguments"] = "{}"
    return msg


def _attempt(cfg, p, model, base):
    """One HTTP call to one key. Retries once without temperature if the model rejects it."""
    drop_tools_next = False
    for _ in range(3):
        body = dict(base, model=model)
        if drop_tools_next:
            body.pop("tools", None)
            body.pop("tool_choice", None)
        if (p.pool, model) not in NO_TEMP:
            body["temperature"] = cfg.temperature
        status, headers, text = _post_json(p.base_url + "/chat/completions", _headers(p), body, cfg.timeout)
        if status == 200:
            try:
                data = json.loads(text)
            except ValueError:
                raise Fail("server", "gateway returned invalid JSON", "pool_model", 15)
            if isinstance(data, dict) and data.get("choices"):
                msg = data["choices"][0].get("message") or {}
                if msg.get("content") or msg.get("tool_calls"):
                    return _normalise(msg)
                raise Fail("empty", "model returned an empty reply", "none", 0)
            err = data.get("error") if isinstance(data, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            f = classify(code if isinstance(code, int) else 500, text, headers)
            raise f
        f = classify(status, text, headers)
        if f.kind == "param" and (p.pool, model) not in NO_TEMP:
            NO_TEMP.add((p.pool, model))
            if len(NO_TEMP) > _NO_TEMP_MAX:
                NO_TEMP.clear()
            continue
        if f.kind == "tools" and not drop_tools_next:
            drop_tools_next = True
            continue
        raise f
    raise Fail("fatal", "model keeps rejecting the request parameters", "none", 0)


# ---------------------------------------------------------------- public API
def ping(p, cfg, model=None):
    try:
        return _attempt(cfg, p, model or p.model,
                        {"messages": [{"role": "user", "content": "Reply with the word pong."}],
                         "max_tokens": 64})
    except Fail as f:
        if f.kind == "empty":
            return {"content": "(key works - model sent an empty reply to the tiny test)"}
        raise LLMError(f.msg)


def probe(cfg, p, model=None):
    pm, model = cfg.providers, (model or p.model)
    try:
        msg = _attempt(cfg, p, model,
                       {"messages": [{"role": "user", "content": "Reply with the word pong."}],
                        "max_tokens": 64})
        pm.health.clear(name=p.name)
        pm.health.ok(p)
        return True, "ok", str(msg.get("content") or "")[:60]
    except Fail as f:
        if f.kind == "empty":
            pm.health.clear(name=p.name)
            return True, "ok", "(key works)"
        pm.health.mark(p, model, f.scope, f.kind, f.cooldown, f.msg)
        return False, f.kind, f.msg


def _stderr(text):
    print("  ! " + text, file=sys.stderr)


def _progress_payload(pool, model, key_name, kind, remaining, log):
    """Turn the recent log into a compact progress string the UI can show."""
    recent = []
    for e in log[-5:]:
        try:
            recent.append(f"{e[2]} [{e[1]}] -> {e[3]}")
        except Exception:
            continue
    detail = f"{len(log)} attempt(s) so far, {remaining:.0f}s left before retry"
    return {"title": "All keys busy", "detail": detail, "recent": recent,
            "route": f"{pool} · {model}"}


def chat_ex(cfg, messages, tools=None, notify=None, cancel=None, on_progress=None):
    """Returns (assistant_message, route).

    route = {provider, pool, model, fallback, reason}
    on_progress(payload) is optional; the web server wires it to a UI event.
    """
    pm = cfg.providers
    notify = notify or _stderr
    progress = on_progress or (lambda payload: None)
    base = {"messages": messages}
    if tools:
        base["tools"] = tools
        base["tool_choice"] = "auto"
    deadline = time.time() + pm.max_wait
    waited_notice = False
    attempted_model_refresh = set()

    while True:
        if cancel is not None and cancel.is_set():
            raise LLMError("stopped by user")
        pools = pm.pools()
        if not pools:
            raise LLMError("No provider configured. Add keys in the UI or:  /add openrouter YOUR_KEY")
        log = []
        for pi, (pool, _) in enumerate(pools):
            chain = pm.model_chain(pool)
            for mi, model in enumerate(chain):
                for p in pm.pick_order(pool, model):
                    st = pm.health.state(p, model)
                    if st:
                        log.append((pool, model, p.name, st["kind"], st["reason"], st["remaining"]))
                        if st["scope"] == "pool_model":
                            break
                        continue
                    try:
                        msg = _attempt(cfg, p, model, base)
                    except Fail as f:
                        # ---- self-healing: 404 model -> refresh list, retry once
                        if f.kind == "model" and pool not in attempted_model_refresh:
                            attempted_model_refresh.add(pool)
                            try:
                                pm.refresh_models(p.name, force=True)
                                fresh = p.models or []
                                close = difflib.get_close_matches(model, fresh, n=1, cutoff=0.4)
                                if close and close[0] != model:
                                    notify(f"{model} not available on {pool}; retrying with {close[0]}")
                                    try:
                                        msg = _attempt(cfg, p, close[0], base)
                                        pm.health.ok(p)
                                        pm.note_used(pool, p.name)
                                        return msg, {"provider": p.name, "pool": pool,
                                                     "model": close[0], "fallback": True,
                                                     "reason": "model-swapped-after-404"}
                                    except Fail as f2:
                                        f = f2
                            except Exception:
                                pass

                        if f.kind == "fatal":
                            # Last chance: retry once with a minimal request
                            try:
                                minimal = {"messages": messages}
                                msg = _attempt(cfg, p, model, minimal)
                                pm.health.ok(p)
                                pm.note_used(pool, p.name)
                                return msg, {"provider": p.name, "pool": pool, "model": model,
                                             "fallback": True, "reason": "retried-without-tools"}
                            except Fail:
                                raise LLMError(f"{p.name} ({model}): {f.msg}")

                        pm.health.mark(p, model, f.scope, f.kind, f.cooldown, f.msg)
                        log.append((pool, model, p.name, f.kind, f.msg, f.cooldown))
                        notify(f"{p.name} [{model}] {f.kind}: {f.msg[:110]}")
                        if f.scope == "pool_model":
                            break
                        if f.scope == "none":
                            raise LLMError(f"{p.name} ({model}): {f.msg}")
                        continue
                    pm.health.ok(p)
                    pm.note_used(pool, p.name)
                    reason = "primary" if (pi == 0 and mi == 0) else \
                             ("model-fallback" if mi > 0 else "pool-fallback")
                    route = {"provider": p.name, "pool": pool, "model": model,
                             "fallback": (pi > 0 or mi > 0), "reason": reason}
                    if route["fallback"] and log:
                        notify(f"using fallback {p.name} / {model}")
                    return msg, route

        # nothing worked in this pass
        waits = [e[5] for e in log if e[3] in pm.health.TRANSIENT and isinstance(e[5], (int, float)) and e[5] > 0]
        if waits and time.time() + min(waits) <= deadline:
            w = min(waits) + 0.5
            if not waited_notice:
                notify(f"every key is busy - waiting {int(w)}s for the first one to free up")
                waited_notice = True
            last_pool = log[-1][0] if log else "?"
            last_model = log[-1][1] if log else "?"
            progress(_progress_payload(last_pool, last_model,
                                       log[-1][2] if log else "?",
                                       log[-1][3] if log else "?", w, log))
            slept = 0.0
            while slept < w:
                if cancel is not None and cancel.is_set():
                    raise LLMError("stopped by user")
                time.sleep(min(0.5, w - slept))
                slept += 0.5
                if int(slept * 2) % 6 == 0:
                    progress({"title": f"Waiting {int(w - slept)}s",
                              "detail": "checking keys again shortly",
                              "recent": [f"{e[2]} [{e[1]}] -> {e[3]}" for e in log[-4:]],
                              "route": f"{last_pool} · {last_model}"})
            continue
        raise LLMError(_summary(log))


def _summary(log):
    if not log:
        return "No usable key. Check the Keys panel (or /providers)."
    groups = {}
    for pool, model, name, kind, reason, _ in log:
        g = groups.setdefault((pool, model), {})
        g.setdefault(kind, [0, reason])[0] += 1
    hints = {"auth": "replace the invalid keys (UI -> Keys, or /key NAME NEWKEY)",
             "quota": "daily quota is used up - it resets by itself (Google: midnight Pacific); meanwhile pick another model or add keys from other Google projects",
             "rate": "too many requests per minute - it frees up in about a minute",
             "model": "this model name is not available for that service - choose another one (UI -> model list, or /models)",
             "billing": "add credits / check billing for that service",
             "network": "could not reach the service - check your internet / VPN / base_url",
             "server": "the service is having problems - try again shortly"}
    lines = ["All keys/providers failed:"]
    seen = set()
    for (pool, model), kinds in groups.items():
        parts = [f"{n}x {k}" for k, (n, _) in kinds.items()]
        lines.append(f"  - {pool} / {model}: " + ", ".join(parts))
        for k, (_, reason) in kinds.items():
            if k not in seen and k in hints:
                seen.add(k)
                lines.append(f"      {k}: {hints[k]}.  e.g. {reason[:150]}")
    return "\n".join(lines)


def chat(cfg, messages, tools=None, notify=None, cancel=None, on_progress=None):
    return chat_ex(cfg, messages, tools, notify, cancel, on_progress)[0]


# ---------------------------------------------------------------- model listing
_MODEL_CACHE = {}


def _cache_put(key, value):
    if len(_MODEL_CACHE) > _MODEL_CACHE_MAX:
        # Evict the oldest 16 instead of clearing everything, so we don't
        # trigger a thundering herd on the next request.
        for k in list(_MODEL_CACHE)[:16]:
            del _MODEL_CACHE[k]
    _MODEL_CACHE[key] = value


def list_models(cfg, pool, force=False):
    pm = cfg.providers
    hit = _MODEL_CACHE.get(pool)
    if hit and not force and time.time() - hit[0] < 600:
        return hit[1]
    ps = pm.providers_in(pool)
    if not ps:
        raise LLMError(f"no such pool: {pool}")
    last = None
    ordered = [p for p in ps if not pm.health.state(p, p.model)] + ps
    for p in ordered[:3]:
        try:
            d = _get_json(p.base_url + "/models", _headers(p))
            items = d.get("data") or d.get("models") or []
            ids = []
            for m in items:
                i = m.get("id") or m.get("name") if isinstance(m, dict) else m
                if isinstance(i, str):
                    i = i[7:] if i.startswith("models/") else i
                    if not re.search(r"embed|aqa|imagen|veo|tts|whisper|moderation|dall-e|image-gen", i, re.I):
                        ids.append(i)
            ids = sorted(set(ids))
            if ids:
                _cache_put(pool, (time.time(), ids))
                return ids
        except Exception as e:
            last = e
    raise LLMError(f"could not list models for {pool}: {last}")


def model_listed(cfg, pool, model):
    try:
        return model in list_models(cfg, pool)
    except Exception:
        return None


def list_models_for_provider(p, force=False):
    key = f"prov:{p.name}:{p.base_url}"
    hit = _MODEL_CACHE.get(key)
    if hit and not force and time.time() - hit[0] < 600:
        return hit[1]
    try:
        d = _get_json(p.base_url + "/models", _headers(p))
    except Exception as e:
        raise LLMError(f"could not list models for {p.name}: {e}")
    items = d.get("data") or d.get("models") or []
    ids = []
    for m in items:
        i = m.get("id") or m.get("name") if isinstance(m, dict) else m
        if isinstance(i, str):
            i = i[7:] if i.startswith("models/") else i
            if not re.search(r"embed|aqa|imagen|veo|tts|whisper|moderation|dall-e|image-gen",
                             i, re.I):
                ids.append(i)
    ids = sorted(set(ids))
    _cache_put(key, (time.time(), ids))
    return ids


def verify(cfg, messages, final_answer):
    """Strict-mode verifier. Returns [] on any failure."""
    if not final_answer or len(final_answer) < 80:
        return []
    prompt = (
        "You are a strict fact-checker reviewing a draft answer against the conversation that "
        "produced it. For each factual claim the draft makes about the user's files, commands, "
        "system, or external facts, decide whether the conversation contains a TOOL CALL that "
        "actually verified it (a read_file, list_dir, run_shell, web_fetch, search_files, etc. "
        "whose result supports the claim). If a claim was NOT backed by an observed tool result, "
        "mark verified=false.\n\n"
        "Reply with ONLY a JSON object of this exact shape, no prose:\n"
        '{"claims":[{"claim":"...","verified":true,"note":"..."}]}\n\n'
        "If the draft contains no factual claims at all, reply {\"claims\":[]}.\n\n"
        "DRAFT ANSWER:\n" + final_answer
    )
    msgs = list(messages) + [{"role": "user", "content": prompt}]
    try:
        msg, _ = chat_ex(cfg, msgs, tools=None)
        txt = (msg.get("content") or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return []
        data = json.loads(m.group(0))
        claims = data.get("claims", [])
        return [c for c in claims if isinstance(c, dict) and c.get("claim")]
    except Exception:
        return []