"""Providers (= API keys), pools (= keys of one service) and key health.

Data model
----------
* A **provider** is one API key: name, base_url, api_key, model.   (data/providers.json)
* A **pool** is every provider whose name is `<pool>-<number>` (gemini-1 ... gemini-37 -> pool "gemini").
  A provider without a number is its own pool.  The model is chosen per pool, so changing the model
  changes it for all keys at once.
* Settings (strategy, fallback models, max wait) live in data/settings.json.
* Health (which key/model is cooling down and why) lives in data/state.json, so a daily quota
  that was hit is still remembered after a restart.
"""
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, asdict

# Convenience defaults only - model names change often, choose yours with /model or in the UI.
PRESETS = {
    "openrouter": ("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-5.5"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "omniroute": ("http://localhost:20128/v1", "auto"),
    "openai": ("https://api.openai.com/v1", "gpt-4o"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5-coder:7b"),
}

STRATEGIES = ("round_robin", "failover")
DEFAULT_SETTINGS = {"strategy": "round_robin", "fallbacks": {}, "max_wait": 90}


def pool_of(name: str) -> str:
    """gemini-12 -> gemini, key_3 -> key, openrouter -> openrouter"""
    m = re.match(r"^(.*?)[-_ ]?\d+$", name)
    return m.group(1) if m and m.group(1) else name


def read_json(path, default):
    """Read JSON tolerant of a UTF-8 BOM (Windows PowerShell/Notepad write one).
    A corrupt file is copied aside (never silently dropped). Returns (data, error_or_None)."""
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
    """Atomic write, UTF-8 *without* BOM."""
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

    @property
    def pool(self):
        return pool_of(self.name)

    def masked(self):
        k = self.api_key
        return (k[:4] + "..." + k[-3:]) if len(k) > 10 else ("(none)" if not k else "***")


# ---------------------------------------------------------------- health
class Health:
    """Cooldowns with three scopes:
       key        - the key itself is bad (invalid / revoked / billing)         -> all models blocked for it
       key_model  - this key hit a rate/quota limit on this model              -> other models still usable
       pool_model - the model/server is failing for the whole service           -> skip all keys of the pool
    """
    TRANSIENT = ("rate", "server", "network")   # worth waiting for

    def __init__(self, path=None):
        self.path = path
        self.lock = threading.RLock()
        self.key, self.key_model, self.pool_model, self.stats = {}, {}, {}, {}
        if path:
            d, _ = read_json(path, {})
            self.key = d.get("key", {})
            self.key_model = d.get("key_model", {})
            self.pool_model = d.get("pool_model", {})
            self.stats = d.get("stats", {})
            self._expire()

    def _expire(self):
        now = time.time()
        for table in (self.key, self.key_model, self.pool_model):
            for k in [k for k, v in table.items() if v["until"] <= now]:
                del table[k]

    def save(self):
        if not self.path:
            return
        try:
            write_json(self.path, {"key": self.key, "key_model": self.key_model,
                                   "pool_model": self.pool_model, "stats": self.stats})
        except Exception:
            pass

    def state(self, p, model):
        """None if usable, else {kind, reason, remaining, scope}."""
        with self.lock:
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


# ---------------------------------------------------------------- manager
class Manager:
    """items order = priority. Pools are ordered by their first key; within a pool the strategy decides."""

    def __init__(self, path, default=None, state_path=None, settings_path=None):
        self.path = path
        self.lock = threading.RLock()
        self.load_error = None
        self.items = []
        self.rr = {}                      # pool -> name of the key used last (round robin)
        data, err = read_json(path, [])
        self.load_error = err
        try:
            self.items = [Provider(**{k: d[k] for k in ("name", "base_url", "api_key", "model")}) for d in data]
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
        """[(pool, [providers])] in priority order."""
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
        """Accepts a pool name or a key name."""
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

    def pick_order(self, pool, model):
        """Keys of the pool in the order they should be tried right now (usable ones only)."""
        with self.lock:
            ps = list(self.providers_in(pool))
            if self.strategy == "round_robin" and len(ps) > 1:
                last = self.rr.get(pool)
                names = [p.name for p in ps]
                if last in names:
                    i = names.index(last) + 1
                    ps = ps[i:] + ps[:i]
            return ps

    def note_used(self, pool, name):
        self.rr[pool] = name

    # ---- editing
    def add(self, name, key, model=None, base_url=None):
        pool = pool_of(name)
        pre = PRESETS.get(pool) or PRESETS.get(name.split("-")[0].split("_")[0])
        existing = self.providers_in(pool)
        base_url = base_url or (existing[0].base_url if existing else (pre[0] if pre else None))
        model = model or (existing[0].model if existing else (pre[1] if pre else None))
        if not base_url or not model:
            raise ValueError("Unknown preset: give model and base_url, e.g. /add mine KEY my-model https://host/v1")
        key = clean_key(key)
        with self.lock:
            new = Provider(name, base_url.rstrip("/"), key, model)
            for i, p in enumerate(self.items):          # replace in place keeps its priority
                if p.name == name:
                    self.items[i] = new
                    break
            else:
                self.items.append(new)
            self.health.clear(name=name)
            self.save()

    def add_keys(self, pool, keys, base_url=None, model=None):
        """Bulk add keys to a pool (auto-numbered). Returns (added_names, skipped_duplicates)."""
        pool = pool.strip()
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
        return added, dup

    def set_key(self, name, key):
        with self.lock:
            self.get(name).api_key = clean_key(key)
            self.health.clear(name=name)
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
        """Make a pool (or the pool of a key, and that key first) the primary."""
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

    order_by = order_pools   # backwards compatible name

    # ---- display helpers
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
        """JSON-able overview for the UI."""
        out = []
        for pool, ps in self.pools():
            model = self.model_of(pool)
            keys, counts = [], {"ready": 0, "cooling": 0, "invalid": 0}
            for p in ps:
                st = self.health.state(p, model)
                stats = self.health.stats.get(p.name, {})
                if not st:
                    counts["ready"] += 1
                elif st["kind"] in ("auth", "billing"):
                    counts["invalid"] += 1
                else:
                    counts["cooling"] += 1
                keys.append({"name": p.name, "masked": p.masked(), "status": st["kind"] if st else "ready",
                             "reason": st["reason"] if st else "", "remaining": st["remaining"] if st else 0,
                             "ok": stats.get("ok", 0), "fail": stats.get("fail", 0),
                             "last_error": stats.get("last_error", "")})
            out.append({"pool": pool, "model": model, "fallbacks": self.fallbacks(pool),
                        "base_url": ps[0].base_url, "counts": counts, "keys": keys})
        return out


def clean_key(k):
    return (k or "").strip().strip('"').strip("'").strip()
