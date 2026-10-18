# agent/providers.py
"""Providers (= API keys), pools (= keys of one service) and key health.

Design notes:
  * score-based key selection (health-weighted, not blind round-robin),
  * last_error_at tracking so recently-failed keys deprioritize briefly,
  * `Health.save()` prunes expired cooldowns so state.json stays small,
  * `refresh_models()` only auto-picks a default when the key has no model yet
    (so a flaky /models response can't silently change the user's choice).
"""
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, asdict, field

PRESETS = {
    "openrouter": ("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4.5"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "omniroute": ("http://localhost:20128/v1", "auto"),
    "openai": ("https://api.openai.com/v1", "gpt-4o"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-5"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5-coder:7b"),
}

STRATEGIES = ("round_robin", "failover")
DEFAULT_SETTINGS = {"strategy": "round_robin", "fallbacks": {}, "max_wait": 90}

PREFERRED_MODEL_HINTS = {
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro"],
    "openrouter": ["anthropic/claude-sonnet-4.5", "anthropic/claude-3.5-sonnet",
                   "openai/gpt-4o", "google/gemini-2.5-flash",
                   "meta-llama/llama-3.3-70b-instruct"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "mixtral-8x7b-32768"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "anthropic": ["claude-sonnet-4-5", "claude-3-5-sonnet-latest", "claude-3-5-haiku-latest"],
    "ollama": ["qwen2.5-coder:7b", "llama3.1:8b", "deepseek-coder-v2"],
}

KEY_PATTERNS = [
    (r"^AIza[0-9A-Za-z_\-]{30,}$", "gemini"),
    (r"^sk-or-v1-[0-9a-f]{40,}$", "openrouter"),
    (r"^sk-ant-[A-Za-z0-9_\-]{40,}$", "anthropic"),
    (r"^sk-proj-[A-Za-z0-9_\-]{30,}$", "openai"),
    (r"^sk-[A-Za-z0-9]{20,}$", "openai"),
    (r"^gsk_[A-Za-z0-9]{40,}$", "groq"),
    (r"^sk-[a-f0-9]{32}$", "deepseek"),
]


def detect_provider(key: str):
    key = (key or "").strip()
    for pattern, name in KEY_PATTERNS:
        if re.match(pattern, key):
            return name
    return None


def pool_of(name: str) -> str:
    m = re.match(r"^(.*?)[-_ ]?\d+$", name)
    return m.group(1) if m and m.group(1) else name


def read_json(path, default):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default, None
    try:
        return json.loads(raw.decode("utf-8-sig")), None
    except Exception as e:
        bad = path.with_name(path.name + f".corrupt-{int(time.time())}")
        try:
            shutil.copyfile(path, bad)
        except Exception:
            pass
        return default, f"{path.name} could not be read ({e}); a copy was kept as {bad.name}"


def write_json(path, data, private=False):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
    os.replace(tmp, path)


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    models: list = field(default_factory=list)
    models_fetched: float = 0.0

    @property
    def pool(self):
        return pool_of(self.name)

    def masked(self):
        k = self.api_key
        return (k[:4] + "..." + k[-3:]) if len(k) > 10 else ("(none)" if not k else "***")


class Health:
    TRANSIENT = ("rate", "server", "network")
    EXPIRE_INTERVAL = 5.0  # seconds between automatic prune passes

    def __init__(self, path=None):
        self.path = path
        self.lock = threading.RLock()
        self.key, self.key_model, self.pool_model, self.stats = {}, {}, {}, {}
        self._last_expire = 0.0
        if path:
            d, _ = read_json(path, {})
            self.key = d.get("key", {})
            self.key_model = d.get("key_model", {})
            self.pool_model = d.get("pool_model", {})
            self.stats = d.get("stats", {})
            self._expire(force=True)

    def _expire(self, force=False):
        now = time.time()
        if not force and now - self._last_expire < self.EXPIRE_INTERVAL:
            return
        self._last_expire = now
        for table in (self.key, self.key_model, self.pool_model):
            for k in [k for k, v in table.items() if v["until"] <= now]:
                del table[k]

    def save(self):
        if not self.path:
            return
        with self.lock:
            self._expire(force=True)  # drop dead entries before persisting
            try:
                write_json(self.path, {"key": self.key, "key_model": self.key_model,
                                       "pool_model": self.pool_model, "stats": self.stats})
            except Exception:
                pass

    def state(self, p, model):
        with self.lock:
            self._expire()
            now = time.time()
            for scope, table, k in (("key", self.key, p.name),
                                    ("key_model", self.key_model, f"{p.name}|{model}"),
                                    ("pool_model", self.pool_model, f"{p.pool}|{model}")):
                e = table.get(k)
                if e and e["until"] > now:
                    return {"kind": e["kind"], "reason": e["reason"], "scope": scope,
                            "remaining": int(e["until"] - now) + 1, "until": e["until"]}
            return None

    def mark(self, p, model, scope, kind, seconds, reason):
        if seconds <= 0:
            return
        with self.lock:
            entry = {"until": time.time() + seconds, "kind": kind, "reason": reason[:200]}
            if scope == "key":
                self.key[p.name] = entry
            elif scope == "key_model":
                self.key_model[f"{p.name}|{model}"] = entry
            else:
                self.pool_model[f"{p.pool}|{model}"] = entry
            s = self.stats.setdefault(p.name, {"ok": 0, "fail": 0})
            s["fail"] += 1
            s["last_error"] = f"{kind}: {reason[:120]}"
            s["last_error_at"] = time.time()
            self.save()

    def ok(self, p):
        with self.lock:
            s = self.stats.setdefault(p.name, {"ok": 0, "fail": 0})
            s["ok"] += 1
            s["last_ok"] = int(time.time())
            if s["ok"] % 5 == 1:
                self.save()

    def clear(self, name=None, pool=None):
        with self.lock:
            for table in (self.key, self.key_model, self.pool_model):
                for k in list(table):
                    base = k.split("|")[0]
                    if name is None and pool is None:
                        del table[k]
                    elif name and base == name:
                        del table[k]
                    elif pool and (base == pool or pool_of(base) == pool):
                        del table[k]
            self.save()

    def snapshot(self):
        """Deep-ish copy of the stats table for consistent scoring."""
        with self.lock:
            return {k: dict(v) for k, v in self.stats.items()}


class Manager:
    def __init__(self, path, default=None, state_path=None, settings_path=None):
        self.path = path
        self.lock = threading.RLock()
        self.load_error = None
        self.items = []
        self.rr = {}
        data, err = read_json(path, [])
        self.load_error = err
        try:
            self.items = [Provider(**{k: d[k] for k in ("name", "base_url", "api_key", "model")},
                                   models=d.get("models", []),
                                   models_fetched=d.get("models_fetched", 0.0))
                          for d in data]
        except Exception as e:
            self.items = []
            self.load_error = f"{path.name} has an unexpected format ({e})"
        if not self.items and default and (default.api_key or default.base_url):
            self.items = [default]
        self.settings_path = settings_path
        self.settings = dict(DEFAULT_SETTINGS, fallbacks={})
        if settings_path:
            s, _ = read_json(settings_path, {})
            if isinstance(s, dict):
                self.settings.update({k: s[k] for k in DEFAULT_SETTINGS if k in s})
        self.health = Health(state_path)

    # ---- persistence
    def save(self):
        with self.lock:
            write_json(self.path, [asdict(p) for p in self.items], private=True)

    def save_settings(self):
        if self.settings_path:
            write_json(self.settings_path, self.settings)

    # ---- lookup
    def get(self, name):
        for p in self.items:
            if p.name == name:
                return p
        raise ValueError(f"No key named '{name}'. See /keys")

    def pools(self):
        with self.lock:
            out = {}
            for p in self.items:
                out.setdefault(p.pool, []).append(p)
            return list(out.items())

    def pool_names(self):
        return [n for n, _ in self.pools()]

    def providers_in(self, pool):
        return [p for p in self.items if p.pool == pool]

    def resolve_pool(self, ident):
        if ident in self.pool_names():
            return ident
        return self.get(ident).pool

    # ---- models
    def model_of(self, pool):
        ps = self.providers_in(pool)
        return ps[0].model if ps else None

    def set_model(self, pool, model):
        pool = self.resolve_pool(pool)
        model = model.strip()
        if not model:
            raise ValueError("model name is empty")
        with self.lock:
            for p in self.providers_in(pool):
                p.model = model
            self.save()
        return pool

    def fallbacks(self, pool):
        return list(self.settings["fallbacks"].get(pool, []))

    def set_fallbacks(self, pool, models):
        pool = self.resolve_pool(pool)
        main = self.model_of(pool)
        models = [m.strip() for m in models if m.strip() and m.strip() != main]
        if models:
            self.settings["fallbacks"][pool] = list(dict.fromkeys(models))
        else:
            self.settings["fallbacks"].pop(pool, None)
        self.save_settings()

    def model_chain(self, pool):
        return [self.model_of(pool)] + self.fallbacks(pool)

    # ---- strategy
    @property
    def strategy(self):
        return self.settings.get("strategy", "round_robin")

    def set_strategy(self, s):
        if s not in STRATEGIES:
            raise ValueError("strategy must be one of: " + ", ".join(STRATEGIES))
        self.settings["strategy"] = s
        self.save_settings()

    @property
    def max_wait(self):
        return int(self.settings.get("max_wait", 90))

    # ---- smart key picking -------------------------------------------------
    def _score(self, p, now=None, stats=None):
        """Higher = better. Health-weighted with a mild recency penalty."""
        now = now or time.time()
        if stats is None:
            stats = self.health.snapshot().get(p.name, {})
        else:
            stats = stats.get(p.name, {})
        ok = stats.get("ok", 0)
        fail = stats.get("fail", 0)
        total = ok + fail
        # Bayesian success rate with 1 ok / 1 fail priors so new keys aren't frozen out.
        rate = (ok + 1) / (total + 2)
        recent_fail = 1.0 if (now - stats.get("last_error_at", 0)) < 60 else 0.0
        # small penalty for lifetime fail count, capped so it never dominates
        fail_pen = min(0.3, 0.02 * fail)
        return rate - 0.25 * recent_fail - fail_pen

    def pick_order(self, pool, model):
        with self.lock:
            ps = list(self.providers_in(pool))
            if not ps:
                return ps
            now = time.time()
            stats = self.health.snapshot()
            # Sort once by score (failover = strictly best-first).
            ps.sort(key=lambda p: self._score(p, now, stats), reverse=True)
            if self.strategy == "round_robin" and len(ps) > 1:
                # Rotate the top 3 to spread quota across them.
                top, rest = ps[:3], ps[3:]
                names = [p.name for p in top]
                last = self.rr.get(pool)
                if last in names:
                    i = names.index(last) + 1
                    top = top[i:] + top[:i]
                return top + rest
            return ps

    def note_used(self, pool, name):
        with self.lock:
            self.rr[pool] = name

    def reset_rr(self):
        with self.lock:
            self.rr.clear()

    # ---- editing
    def add(self, name, key, model=None, base_url=None):
        pool = pool_of(name)
        detected = detect_provider(key)
        if detected and (pool in ("default", "new", "custom") or not name):
            name = f"{detected}-{len(self.providers_in(detected)) + 1}"
            pool = detected
        pre = PRESETS.get(pool) or PRESETS.get(name.split("-")[0].split("_")[0])
        existing = self.providers_in(pool)
        base_url = base_url or (existing[0].base_url if existing else (pre[0] if pre else None))
        model = model or (existing[0].model if existing else (pre[1] if pre else None))
        if not base_url or not model:
            raise ValueError("Unknown preset: give model and base_url, e.g. /add mine KEY my-model https://host/v1")
        key = clean_key(key)
        with self.lock:
            new = Provider(name, base_url.rstrip("/"), key, model)
            for i, p in enumerate(self.items):
                if p.name == name:
                    self.items[i] = new
                    break
            else:
                self.items.append(new)
            self.health.clear(name=name)
            self.save()
        try:
            self.refresh_models(name)
        except Exception:
            pass

    def add_keys(self, pool, keys, base_url=None, model=None):
        pool = pool.strip()
        if pool not in PRESETS and keys:
            det = detect_provider(keys[0])
            if det:
                pool = det
        existing = self.providers_in(pool)
        pre = PRESETS.get(pool)
        base_url = base_url or (existing[0].base_url if existing else (pre[0] if pre else None))
        model = model or (existing[0].model if existing else (pre[1] if pre else None))
        if not base_url or not model:
            raise ValueError(f"'{pool}' is not a known preset - give base_url and model for a custom service.")
        base_url = base_url.rstrip("/")
        added, dup = [], 0
        with self.lock:
            have = {(p.base_url, p.api_key) for p in self.items}
            used = {p.name for p in self.items}
            n = 0
            for raw in keys:
                k = clean_key(raw)
                if not k or k.startswith("#"):
                    continue
                if (base_url, k) in have:
                    dup += 1
                    continue
                while True:
                    n += 1
                    name = f"{pool}-{n}"
                    if name not in used:
                        break
                used.add(name)
                self.items.append(Provider(name, base_url, k, model))
                have.add((base_url, k))
                added.append(name)
            if added:
                self.save()
        for nm in added:
            threading.Thread(target=self._safe_refresh, args=(nm,), daemon=True).start()
        return added, dup

    def _safe_refresh(self, name):
        try:
            self.refresh_models(name)
        except Exception:
            pass

    def refresh_models(self, name, force=True):
        if os.environ.get("OMNI_NO_REFRESH"):
            return []
        from . import llm
        p = self.get(name)
        ids = llm.list_models_for_provider(p, force=force)
        with self.lock:
            p.models = ids
            p.models_fetched = time.time()
            # Only auto-pick when the key has NO model yet, or the current
            # model is empty. Never silently replace a model the user chose —
            # a flaky /models response would otherwise overwrite their setting.
            if ids and not p.model:
                preferred = _pick_default_model(p.pool, ids)
                if preferred:
                    p.model = preferred
            self.save()
        return ids

    def refresh_all_models(self):
        from . import llm
        out = {}
        for p in list(self.items):
            try:
                ids = self.refresh_models(p.name)
                out[p.name] = ids
                llm._MODEL_CACHE[p.pool] = (time.time(), ids)
            except Exception as e:
                out[p.name] = f"error: {e}"
        return out

    def set_key(self, name, key):
        with self.lock:
            self.get(name).api_key = clean_key(key)
            self.health.clear(name=name)
            self.save()

    def set_model_for_key(self, name, model):
        with self.lock:
            p = self.get(name)
            p.model = model.strip()
            self.save()

    def remove(self, name):
        with self.lock:
            self.get(name)
            self.items = [p for p in self.items if p.name != name]
            self.health.clear(name=name)
            self.save()

    def remove_pool(self, pool):
        with self.lock:
            self.items = [p for p in self.items if p.pool != pool]
            self.settings["fallbacks"].pop(pool, None)
            self.save()
            self.save_settings()

    def use(self, ident):
        with self.lock:
            pool = self.resolve_pool(ident)
            first = [p for p in self.items if p.pool == pool]
            if ident != pool:
                chosen = self.get(ident)
                first = [chosen] + [p for p in first if p is not chosen]
            self.items = first + [p for p in self.items if p.pool != pool]
            self.health.clear(pool=pool)
            self.save()

    def order_pools(self, names):
        with self.lock:
            pools = []
            for n in names:
                pn = self.resolve_pool(n)
                if pn not in pools:
                    pools.append(pn)
            rest = [n for n in self.pool_names() if n not in pools]
            self.items = [p for n in pools + rest for p in self.providers_in(n)]
            self.save()

    order_by = order_pools

    def status(self, p, model=None):
        st = self.health.state(p, model or p.model)
        if not st:
            return "ready"
        if st["kind"] == "auth":
            return "INVALID KEY"
        if st["kind"] == "quota":
            return "daily quota used"
        return f"cooling {st['remaining']}s ({st['kind']})"

    def summary(self):
        out = []
        # Take one snapshot of stats so we don't re-acquire the lock per key.
        stats_all = self.health.snapshot()
        now = time.time()
        for pool, ps in self.pools():
            model = self.model_of(pool)
            keys, counts = [], {"ready": 0, "cooling": 0, "invalid": 0}
            for p in ps:
                st = self.health.state(p, model)
                stats = stats_all.get(p.name, {})
                if not st:
                    counts["ready"] += 1
                elif st["kind"] in ("auth", "billing"):
                    counts["invalid"] += 1
                else:
                    counts["cooling"] += 1
                keys.append({
                    "name": p.name, "masked": p.masked(),
                    "status": st["kind"] if st else "ready",
                    "reason": st["reason"] if st else "",
                    "remaining": st["remaining"] if st else 0,
                    "ok": stats.get("ok", 0), "fail": stats.get("fail", 0),
                    "last_error": stats.get("last_error", ""),
                    "model": p.model,
                    "models": p.models,
                    "models_fetched": p.models_fetched,
                    "score": round(self._score(p, now, stats_all), 3),
                })
            out.append({"pool": pool, "model": model, "fallbacks": self.fallbacks(pool),
                        "base_url": ps[0].base_url, "counts": counts, "keys": keys})
        return out


def _pick_default_model(pool, ids):
    for hint in PREFERRED_MODEL_HINTS.get(pool, []):
        for mid in ids:
            if mid == hint or mid.endswith("/" + hint) or hint in mid:
                return mid
    for mid in ids:
        low = mid.lower()
        if any(x in low for x in ("embed", "whisper", "tts", "dall-e", "moderation", "image")):
            continue
        return mid
    return ids[0] if ids else None


def clean_key(k):
    return (k or "").strip().strip('"').strip("'").strip()