# agent/llm.py
"""OpenAI-compatible chat client with automatic key rotation, model fallback,
provider failover, rich progress events and light self-healing.

Order of attempts for one request
---------------------------------
for each pool (service) in priority order:
    for each model in pm.model_chain(pool)   # auto mode ranks the list
        for each usable key (scored, not blind round-robin):
            try -> success: return
                   failure: classify, cooldown the right thing, continue

Never-fail behaviour
--------------------
`chat_ex` does NOT raise on a "all keys failed" cycle. It waits (up to
`max_wait`, then a growing backoff capped at 60s) and tries again. It
only raises `LLMError("stopped by user")` when the caller's cancel event
is set. A truly fatal 400 (bad request, no retry will help) still raises
so the user can fix the prompt — but the caller (loop) treats it as a
normal error event, not a crash.

Notable behaviours
------------------
* Rich progress events: `notify` receives short human strings the UI can render.
* Self-healing: 400s that mention `tools` are retried once without tools; 404s
  trigger a model-list refresh and one retry with the closest match.
* Self-healing: empty replies (content null + no tool_calls) get a nudge
  retry, then a "strip tools" retry, before cooling the key.
* Self-healing: 400s that carry an API_KEY_INVALID signal are classified as
  auth, not as a bad request.
* Cancel is checked between attempts AND inside the wait loop AND inside the
  HTTP call itself.
* `classify()` distinguishes the HTTP status from the *effective* error code
  in the body.
"""
import datetime as _dt
import difflib
import hashlib
import http.client
import json
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import logs

UA = "omni-agent/2.4"


class LLMError(Exception):
    pass


class Fail(Exception):
    def __init__(self, kind, msg, scope="key_model", cooldown=0, status=None):
        super().__init__(msg)
        self.kind, self.msg, self.scope, self.cooldown, self.status = kind, msg, scope, cooldown, status


MIN_RATE_COOLDOWN = 10
DEFAULT_RATE_COOLDOWN = 45
NO_TEMP = set()
_NO_TEMP_LOCK = threading.Lock()
_VERIFY_CACHE = {}

_MODEL_CACHE_MAX = 64
_NO_TEMP_MAX = 128
_VERIFY_CACHE_MAX = 64

# Backoff between full retry cycles when every key is cooling. Capped so a
# user who left the agent overnight doesn't wake to a 4-hour sleep.
BACKOFF_MIN = 2.0
BACKOFF_MAX = 60.0

# (removed) User-visible throttling was moved into the per-cycle notify.


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


def _heal_arguments(raw):
    """Return a JSON string (valid) for `raw`, or None if unrepairable."""
    if raw is None:
        return "{}"
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, list):
        return json.dumps(raw, ensure_ascii=False)
    if not isinstance(raw, str):
        return json.dumps(raw, ensure_ascii=False)
    s = raw.strip()
    if not s:
        return "{}"
    try:
        obj = json.loads(s)
        return json.dumps(obj, ensure_ascii=False) if isinstance(obj, (dict, list)) else s
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(s)):
            if s[i] == opener:
                depth += 1
            elif s[i] == closer:
                depth -= 1
                if depth == 0:
                    candidate = s[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                        return json.dumps(obj, ensure_ascii=False)
                    except ValueError:
                        break
    fixed = re.sub(r",\s*([}\]])", r"\1", s)
    if "'" in fixed and '"' not in fixed:
        fixed = fixed.replace("'", '"')
    try:
        obj = json.loads(fixed)
        return json.dumps(obj, ensure_ascii=False) if isinstance(obj, (dict, list)) else fixed
    except ValueError:
        return None


def _normalise(msg):
    calls = msg.get("tool_calls") or []
    for c in calls:
        fn = c.get("function") or {}
        raw = fn.get("arguments", "")
        healed = _heal_arguments(raw)
        if healed is None:
            fn["arguments"] = raw if isinstance(raw, str) else json.dumps(raw)
        else:
            fn["arguments"] = healed
    return msg


def classify(status, text, headers=None, effective_code=None):
    """Return a Fail for a failed HTTP response."""
    low = text.lower()
    msg = _short(text)
    retry = _retry_after(headers, text)
    wire = status
    code = effective_code if isinstance(effective_code, int) else status
    if effective_code is not None and effective_code != status:
        prefix = f"HTTP {status} (body says {effective_code}): "
    else:
        prefix = f"HTTP {status}: " if status else ""

    if (code in (400, 401, 403)
            and ("api_key_invalid" in low
                 or "api key not valid" in low
                 or "api key expired" in low
                 or "invalid api key" in low
                 or "incorrect api key" in low
                 or "invalid_api_key" in low
                 or "reported as leaked" in low
                 or "api key was reported" in low)):
        return Fail("auth", prefix + "bad/expired/revoked key: " + msg, "key", 6 * 3600, wire)

    if code == 401:
        return Fail("auth", prefix + "bad/expired/revoked key: " + msg, "key", 6 * 3600, wire)

    if code == 402 or (re.search(r"insufficient (credits|funds|balance)|billing|payment required|credit", low)
                       and code in (400, 402, 403)):
        return Fail("billing", prefix + "billing/credits problem: " + msg, "key", 3600, wire)

    if code == 403:
        return Fail("auth", prefix + "access denied for this key: " + msg, "key", 1800, wire)

    if code == 400 and "temperature" in low and re.search(r"unsupported|not support|only the default|invalid", low):
        return Fail("param", msg, "none", 0, wire)

    if code in (400, 422) and re.search(
            r"(function|tool_call|tool call|tool_choice|tools?)[^\n]{0,80}"
            r"(signature|missing|malformed|invalid|unsupported|not support|schema|parameters|argument)",
            low):
        return Fail("tools", prefix + "tool/function call rejected: " + msg, "none", 0, wire)

    if code in (400, 422) and re.search(
            r"invalid[ _]argument|malformed|parse error|invalid json|bad request|"
            r"missing (?:required )?(?:field|parameter|argument)",
            low):
        if "tool" in low or "function" in low:
            return Fail("tools", prefix + "malformed tool call: " + msg, "none", 0, wire)
        return Fail("empty_param", prefix + "malformed request: " + msg, "none", 0, wire)

    if code == 429 or "resource_exhausted" in low:
        if re.search(r"per[ _-]?day|daily|requestsperday|PerDay", text, re.I):
            return Fail("quota", prefix + "daily quota used up: " + msg, "key_model", _next_pacific_midnight(), wire)
        cd = (retry + 2) if retry is not None else DEFAULT_RATE_COOLDOWN
        return Fail("rate", prefix + "rate limited: " + msg, "key_model", min(max(cd, MIN_RATE_COOLDOWN), 900), wire)

    if code == 404 or (code in (400, 422) and re.search(
            r"(model|models/).{0,100}(not found|not exist|does not exist|is not valid|invalid model|not supported|unsupported|unknown|no endpoints)"
            r"|no endpoints found|invalid model", low)):
        return Fail("model", prefix + "model not available: " + msg, "pool_model", 600, wire)

    if code in (408, 409, 425) or (code and code >= 500):
        return Fail("server", prefix + "server error: " + msg, "pool_model", 20, wire)

    return Fail("fatal", prefix + msg, "none", 0, wire)


# ---------------------------------------------------------------- HTTP
class _AbortableSocket:
    def __init__(self, cancel_event):
        self.cancel = cancel_event
        self.sock = None
        self.conn = None
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:
                pass
            try:
                if self.sock is not None:
                    self.sock.close()
            except Exception:
                pass


def _parse_base_url(base_url):
    u = urllib.parse.urlparse(base_url)
    scheme = u.scheme or "http"
    host = u.hostname or "localhost"
    port = u.port or (443 if scheme == "https" else 80)
    path = (u.path or "").rstrip("/")
    return scheme, host, port, path


def _post_json_live(url, headers, body, timeout, cancel):
    scheme, host, port, path = _parse_base_url(url)
    if not path.endswith("/chat/completions") and "/chat/completions" not in path:
        _, _, _, full_path = _parse_base_url(url)
        path = full_path

    conn = None
    holder = _AbortableSocket(cancel)
    try:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        holder.conn = conn
        conn.connect()
        try:
            holder.sock = conn.sock
        except AttributeError:
            holder.sock = None

        body_bytes = json.dumps(body).encode()
        hdrs = dict(headers)
        hdrs["Content-Length"] = str(len(body_bytes))
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(body_bytes)

        resp = conn.getresponse()
        status = resp.status
        resp_headers = dict(resp.getheaders())
        data = resp.read()
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            text = ""
        return status, resp_headers, text
    except (socket.timeout, TimeoutError) as e:
        raise Fail("network", f"network error: timeout after {timeout}s ({e})", "pool_model", 15)
    except (ConnectionError, OSError) as e:
        raise Fail("network", f"network error: {e}", "pool_model", 15)
    except Exception as e:
        raise Fail("network", f"network error: {e}", "pool_model", 15)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _post_json_abortable(url, headers, body, timeout, cancel):
    if cancel is None:
        return _post_json_urllib(url, headers, body, timeout)

    box = {}

    def run():
        try:
            box["v"] = _post_json_live(url, headers, body, timeout, cancel)
        except Fail as e:
            box["fail"] = e
        except BaseException as e:
            box["e"] = e

    t = threading.Thread(target=run, daemon=True, name="llm-http")
    t.start()

    while t.is_alive():
        if cancel.is_set():
            for _ in range(40):
                if not t.is_alive():
                    break
                time.sleep(0.025)
            raise Fail("network", "stopped by user", "none", 0)
        t.join(0.1)

    if "fail" in box:
        raise box["fail"]
    if "e" in box:
        raise box["e"]
    return box["v"]


def _post_json_urllib(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            txt = ""
        finally:
            e.close()
        return e.code, dict(e.headers), txt
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


def _attempt(cfg, p, model, base, cancel=None):
    nudged = False
    stripped_tools = False
    for _ in range(5):
        body = dict(base, model=model)
        if stripped_tools:
            body.pop("tools", None)
            body.pop("tool_choice", None)
        with _NO_TEMP_LOCK:
            no_temp = (p.pool, model) in NO_TEMP
        if not no_temp:
            body["temperature"] = cfg.temperature

        if nudged:
            msgs = list(body.get("messages") or [])
            msgs.append({
                "role": "system",
                "content": (
                    "Your previous reply was empty. Respond now with either "
                    "a tool call or a non-empty assistant message. Do not "
                    "return an empty response."
                ),
            })
            body["messages"] = msgs

        status, headers, text = _post_json_abortable(
            p.base_url + "/chat/completions", _headers(p), body, cfg.timeout, cancel)
        if status == 200:
            try:
                data = json.loads(text)
            except ValueError:
                raise Fail("server", "gateway returned invalid JSON", "pool_model", 15)
            if isinstance(data, dict) and data.get("choices"):
                choice = data["choices"][0] or {}
                msg = choice.get("message") or {}
                finish = (choice.get("finish_reason") or "").lower()
                has_content = bool((msg.get("content") or "").strip())
                has_calls = bool(msg.get("tool_calls"))
                if has_content or has_calls:
                    return _normalise(msg)

                if not nudged and not stripped_tools:
                    nudged = True
                    continue
                if not stripped_tools:
                    stripped_tools = True
                    nudged = False
                    continue
                if finish == "length":
                    raise Fail("empty_param",
                               "model returned an empty reply (finish_reason=length); "
                               "increase max_tokens or use a different model",
                               "none", 0)
                raise Fail("empty", "model returned an empty reply", "none", 0)

            err = data.get("error") if isinstance(data, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            if isinstance(code, int):
                raise classify(status, text, headers, effective_code=code)
            raise classify(status, text, headers)

        f = classify(status, text, headers)
        if f.kind == "param":
            with _NO_TEMP_LOCK:
                if (p.pool, model) not in NO_TEMP:
                    NO_TEMP.add((p.pool, model))
                    if len(NO_TEMP) > _NO_TEMP_MAX:
                        oldest = next(iter(NO_TEMP))
                        NO_TEMP.discard(oldest)
            continue
        # Structural 'tools' errors are never retryable here. Every key
        # will 400 identically, and the caller (chat_ex) has its own
        # structural-error handling. Retrying without tools would
        # silently strip the model's ability to call tools — worse
        # than failing loudly.
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


def probe_model(cfg, p, model):
    """Like probe() but for an arbitrary model on a provider. Used by
    Manager.pick_auto_models to verify a candidate actually answers."""
    msg = _attempt(cfg, p, model,
                   {"messages": [{"role": "user", "content": "Reply with the word pong."}],
                    "max_tokens": 64})
    return msg


def _stderr(text):
    print("  ! " + text, file=sys.stderr)


def _progress_payload(pool, model, key_name, kind, remaining, log):
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

    Retries forever (with capped backoff) on transient failures (rate
    limits, quotas, network errors) until either the request succeeds or
    `cancel` is set. Structural failures — bad tool schemas, invalid
    parameters, malformed responses — raise LLMError immediately so the
    caller can surface them as a clear error event instead of looping.
    """
    pm = cfg.providers
    notify = notify or _stderr
    progress = on_progress or (lambda payload: None)
    base = {"messages": messages}
    if tools:
        base["tools"] = tools
        base["tool_choice"] = "auto"

    backoff = BACKOFF_MIN
    waited_notice = False
    attempt_round = 0

    # Structural errors set this and break out of BOTH loops.
    structural_error: str | None = None

    while structural_error is None:
        if cancel is not None and cancel.is_set():
            raise LLMError("stopped by user")
        attempt_round += 1
        pools = pm.pools()
        if not pools:
            # Nothing configured. This is not retryable — the user has to add
            # a key. Raise a clear error (the loop renders it as a note).
            raise LLMError("No provider configured. Add keys in the UI or:  /add openrouter YOUR_KEY")
        log = []
        attempted_model_refresh = set()

        # Set by structural-error branches below to break out of the
        # nested for-loops cleanly. `None` = keep trying.
        broke_out = False

        for pi, (pool, _) in enumerate(pools):
            if broke_out:
                break
            chain = pm.model_chain(pool)
            for mi, model in enumerate(chain):
                if broke_out:
                    break
                for p in pm.pick_order(pool, model):
                    if broke_out:
                        break
                    if cancel is not None and cancel.is_set():
                        raise LLMError("stopped by user")
                    st = pm.health.state(p, model)
                    if st:
                        log.append((pool, model, p.name, st["kind"], st["reason"], st["remaining"]))
                        if st["scope"] == "pool_model":
                            break
                        continue
                    try:
                        msg = _attempt(cfg, p, model, base, cancel=cancel)
                    except Fail as f:
                        # --- Structural errors that no retry will fix ---
                        # Set structural_error; the outer while loop checks
                        # it and breaks. Do NOT raise inside the loop — an
                        # LLMError raised here would be caught by nothing
                        # and the outer `while True` would just start a new
                        # cycle. That was the "stuck forever" bug.
                        if f.kind == "tools":
                            logs.error(
                                f"structural tool-schema error from {p.name} "
                                f"({model}): {f.msg}"
                            )
                            structural_error = (
                                "The model rejected the request shape "
                                "(tool definitions are invalid for this "
                                "provider). The request was NOT retried."
                            )
                            broke_out = True
                            break

                        if f.kind == "fatal":
                            logs.error(f"fatal request error on {p.name}/{model}: {f.msg}")
                            structural_error = (
                                "The model rejected the request. This usually means "
                                "the prompt is too long or contains an unsupported "
                                "field. Try shortening it or switching models."
                            )
                            broke_out = True
                            break

                        if f.kind == "empty_param":
                            logs.error(f"empty_param from {p.name}/{model}: {f.msg}")
                            structural_error = (
                                "The model returned a malformed response. "
                                "Try a different model or shorten the prompt."
                            )
                            broke_out = True
                            break

                        # --- Transient errors: try a model swap, then
                        # cooldown this key+model and try the next key ---
                        if f.kind == "model" and pool not in attempted_model_refresh:
                            attempted_model_refresh.add(pool)
                            try:
                                pm.refresh_models(p.name, force=True)
                                fresh = p.models or []
                                close = difflib.get_close_matches(model, fresh, n=1, cutoff=0.4)
                                if close and close[0] != model:
                                    notify(f"{model} not available on {pool}; retrying with {close[0]}")
                                    try:
                                        msg = _attempt(cfg, p, close[0], base, cancel=cancel)
                                        pm.health.ok(p)
                                        pm.note_used(pool, p.name)
                                        return msg, {"provider": p.name, "pool": pool,
                                                     "model": close[0], "fallback": True,
                                                     "reason": "model-swapped-after-404"}
                                    except Fail as f2:
                                        f = f2
                            except Exception:
                                pass

                        if f.kind in ("empty", "empty_param"):
                            try:
                                healed_msgs = list(messages) + [{
                                    "role": "system",
                                    "content": (
                                        "Your previous attempt produced an empty or "
                                        "malformed reply. Please respond again with "
                                        "either a valid tool call or a non-empty "
                                        "assistant message."
                                    ),
                                }]
                                healed_base = {"messages": healed_msgs}
                                if tools:
                                    healed_base["tools"] = tools
                                    healed_base["tool_choice"] = "auto"
                                msg = _attempt(cfg, p, model, healed_base, cancel=cancel)
                                pm.health.ok(p)
                                pm.note_used(pool, p.name)
                                return msg, {"provider": p.name, "pool": pool,
                                             "model": model, "fallback": True,
                                             "reason": "retried-after-empty"}
                            except Fail as f2:
                                f = f2

                        pm.health.mark(p, model, f.scope, f.kind, f.cooldown, f.msg)
                        log.append((pool, model, p.name, f.kind, f.msg, f.cooldown))
                        # Backend only. The chat never sees key names or raw errors.
                        logs.warn(f"{p.name} [{model}] {f.kind}: {f.msg[:180]}")
                        if f.scope == "pool_model":
                            break  # this model is bad for the whole pool; next model
                        if f.scope == "none":
                            continue  # parameter error on this key/model; next key
                        # ---- same-key model swap (rate/quota on this key
                        # but a different model on the same key may work) ----
                        if f.scope == "key_model" and f.kind in ("rate", "quota"):
                            for alt in pm.fallbacks(pool):
                                if alt == model:
                                    continue
                                st_alt = pm.health.state(p, alt)
                                if st_alt:
                                    continue
                                try:
                                    logs.notice(f"{p.name}: trying fallback '{alt}' on same key…")
                                    msg = _attempt(cfg, p, alt, base, cancel=cancel)
                                    pm.health.ok(p)
                                    pm.note_used(pool, p.name)
                                    notify("Using an alternate model on the same key…")
                                    return msg, {"provider": p.name, "pool": pool,
                                                 "model": alt, "fallback": True,
                                                 "reason": "same-key-model-swap"}
                                except Fail as f2:
                                    logs.warn(f"{p.name} [{alt}] swap failed: {f2.kind}: {f2.msg[:120]}")
                                    pm.health.mark(p, alt, f2.scope, f2.kind, f2.cooldown, f2.msg)
                                    continue
                        if broke_out:
                            break
                        continue
                    # Also catch broke_out on the success path (only hit
                    # when the previous iteration set it but the next
                    # model/key succeeded — we still want to bail because
                    # a structural error is unrecoverable).
                    if broke_out:
                        break
                    pm.health.ok(p)
                    pm.note_used(pool, p.name)
                    reason = "primary" if (pi == 0 and mi == 0) else \
                             ("model-fallback" if mi > 0 else "pool-fallback")
                    route = {"provider": p.name, "pool": pool, "model": model,
                             "fallback": (pi > 0 or mi > 0), "reason": reason}
                    if route["fallback"] and log:
                        notify(f"using fallback {p.name} / {model}")
                    return msg, route

        if broke_out:
            # A structural error was detected. Skip the wait-and-retry
            # block — we're about to raise it.
            break

        # ---- nothing worked this round. Wait and try again (forever).
        waits = [e[5] for e in log if e[3] in pm.health.TRANSIENT and isinstance(e[5], (int, float)) and e[5] > 0]
        wait_s = min(waits) + 0.5 if waits else min(backoff, BACKOFF_MAX)
        wait_s = max(1.0, wait_s)

        # Backend log gets the details. The UI gets a single soft line
        # via notify() at most once every SOFT_NOTIFY_INTERVAL seconds.
        logs.warn(f"chat_ex cycle {attempt_round}: all keys busy, "
                  f"waiting {int(wait_s)}s. Recent: "
                  + "; ".join(f"{e[2]}[{e[1]}]={e[3]}" for e in log[-3:]))
        # Always emit a short, human-readable status. The UI uses this to
        # show "Trying an alternative route…" in the live pill; the
        # terminal REPL prints it as a dim status line. If you need to
        # suppress the noise, lower the log level — do NOT gate this on
        # _LAST_SOFT_NOTIFY. Tests and the diagnostics panel rely on
        # seeing at least one notify per cycle.
        try:
            notify(f"waiting {int(wait_s)}s for an available key")
        except Exception:
            pass

        last_pool = log[-1][0] if log else "?"
        last_model = log[-1][1] if log else "?"
        progress(_progress_payload(last_pool, last_model,
                                   log[-1][2] if log else "?",
                                   log[-1][3] if log else "?", wait_s, log))

        slept = 0.0
        while slept < wait_s:
            if cancel is not None and cancel.is_set():
                raise LLMError("stopped by user")
            time.sleep(min(0.5, wait_s - slept))
            slept += 0.5
            if int(slept * 2) % 6 == 0:
                progress({"title": f"Waiting {int(wait_s - slept)}s",
                          "detail": f"cycle {attempt_round}; checking keys again shortly",
                          "recent": [f"{e[2]} [{e[1]}] -> {e[3]}" for e in log[-4:]],
                          "route": f"{last_pool} · {last_model}"})

        # Grow the backoff for the next all-fail cycle.
        backoff = min(backoff * 1.6, BACKOFF_MAX)
        waited_notice = False

    # A structural error was set inside the loop — raise it now that we've
    # broken out. This is the ONLY place chat_ex raises for a structural
    # failure, so the loop (which calls chat_ex) sees it as a normal error.
    if structural_error is not None:
        raise LLMError(structural_error)


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
             "server": "the service is having problems - try again shortly",
             "empty": "the model returned an empty reply - try a different model or retry",
             "empty_param": "the model returned an empty or malformed reply - try a different model or retry"}
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
        for k in list(_MODEL_CACHE)[:16]:
            del _MODEL_CACHE[k]
    _MODEL_CACHE[key] = value


def _cache_key_for(p):
    return f"prov:{p.name}:{p.base_url}"


def _fetch_models_for(p, force=False):
    key = _cache_key_for(p)
    hit = _MODEL_CACHE.get(key)
    if hit and not force and time.time() - hit[0] < 600:
        return hit[1]
    d = _get_json(p.base_url + "/models", _headers(p))
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


def list_models(cfg, pool, force=False):
    pm = cfg.providers
    ps = pm.providers_in(pool)
    if not ps:
        raise LLMError(f"no such pool: {pool}")
    seen = set()
    last = None
    ordered = [p for p in ps if not pm.health.state(p, p.model)] + ps
    for p in ordered[:3]:
        try:
            ids = _fetch_models_for(p, force=force)
        except Exception as e:
            last = e
            continue
        for i in ids:
            seen.add(i)
        if seen:
            return sorted(seen)
    raise LLMError(f"could not list models for {pool}: {last}")


def model_listed(cfg, pool, model):
    try:
        return model in list_models(cfg, pool)
    except Exception:
        return None


def list_models_for_provider(p, force=False):
    try:
        return _fetch_models_for(p, force=force)
    except Exception as e:
        raise LLMError(f"could not list models for {p.name}: {e}")


def _verify_cache_key(final_answer, tool_results):
    h = hashlib.sha256()
    h.update(final_answer.encode("utf-8", "replace"))
    h.update(b"|")
    h.update(tool_results.encode("utf-8", "replace"))
    return h.hexdigest()


def _verify_cache_get(key):
    with _NO_TEMP_LOCK:
        return _VERIFY_CACHE.get(key)


def _verify_cache_put(key, claims):
    with _NO_TEMP_LOCK:
        if len(_VERIFY_CACHE) >= _VERIFY_CACHE_MAX:
            for k in list(_VERIFY_CACHE)[:16]:
                _VERIFY_CACHE.pop(k, None)
        _VERIFY_CACHE[key] = claims


def verify(cfg, messages, final_answer):
    """Strict-mode verifier. Returns [] on any failure."""
    if not final_answer or len(final_answer) < 80:
        return []
    trimmed = []
    turn_starts = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            turn_starts.append(i)
    if len(turn_starts) >= 2:
        trimmed = messages[turn_starts[-2]:]
    else:
        trimmed = list(messages)

    tool_blob = "\n".join(
        str(m.get("content") or "") for m in trimmed if m.get("role") == "tool"
    )
    cache_key = _verify_cache_key(final_answer, tool_blob)
    cached = _verify_cache_get(cache_key)
    if cached is not None:
        return cached

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
    msgs = list(trimmed) + [{"role": "user", "content": prompt}]
    try:
        msg, _ = chat_ex(cfg, msgs, tools=None)
        txt = (msg.get("content") or "").strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return []
        data = json.loads(m.group(0))
        claims = data.get("claims", [])
        result = [c for c in claims if isinstance(c, dict) and c.get("claim")]
        _verify_cache_put(cache_key, result)
        return result
    except Exception:
        return []