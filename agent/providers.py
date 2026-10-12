"""Multiple providers/keys with priority order, stored in data/providers.json (chmod 600)."""
import json
import os
import time
from dataclasses import dataclass, asdict

# Convenience defaults only - model names change often, override with /model.
PRESETS = {
    "openrouter": ("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4.5"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "omniroute": ("http://localhost:20128/v1", "auto"),
    "openai": ("https://api.openai.com/v1", "gpt-4o"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5-coder:7b"),
}


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str

    def masked(self):
        k = self.api_key
        return (k[:4] + "..." + k[-3:]) if len(k) > 10 else ("(none)" if not k else "***")


class Manager:
    """List order = priority. items[0] is the primary; the rest are fallbacks."""

    def __init__(self, path, default=None):
        self.path = path
        self.items = []
        self.cooldown = {}
        if path.exists():
            try:
                self.items = [Provider(**d) for d in json.loads(path.read_text(encoding="utf-8"))]
            except Exception:
                self.items = []
        if not self.items and default and (default.api_key or default.base_url):
            self.items = [default]

    # ---- persistence
    def save(self):
        self.path.write_text(json.dumps([asdict(p) for p in self.items], indent=1), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    # ---- lookup
    def get(self, name):
        for p in self.items:
            if p.name == name:
                return p
        raise ValueError(f"No provider named '{name}'. See /providers")

    # ---- editing
    def add(self, name, key, model=None, base_url=None):
        pre = PRESETS.get(name.split("-")[0].split("_")[0])
        base_url = base_url or (pre[0] if pre else None)
        model = model or (pre[1] if pre else None)
        if not base_url or not model:
            raise ValueError("Unknown preset: give model and base_url, e.g. /add mine KEY my-model https://host/v1")
        self.items = [p for p in self.items if p.name != name] + [Provider(name, base_url.rstrip("/"), key, model)]
        self.save()

    def remove(self, name):
        self.get(name)
        self.items = [p for p in self.items if p.name != name]
        self.cooldown.pop(name, None)
        self.save()

    def use(self, name):
        p = self.get(name)
        self.items = [p] + [x for x in self.items if x is not p]
        self.cooldown.pop(name, None)
        self.save()

    def order_by(self, names):
        by = {p.name: p for p in self.items}
        for n in names:
            if n not in by:
                raise ValueError(f"No provider named '{n}'")
        rest = [p for p in self.items if p.name not in names]
        self.items = [by[n] for n in names] + rest
        self.save()

    # ---- failover support
    def mark_failed(self, name, seconds):
        self.cooldown[name] = time.time() + seconds

    def order(self):
        now = time.time()
        ready = [p for p in self.items if self.cooldown.get(p.name, 0) <= now]
        cooling = [p for p in self.items if self.cooldown.get(p.name, 0) > now]
        return ready + cooling  # cooling ones are a last resort, never skipped entirely

    def status(self, p):
        left = self.cooldown.get(p.name, 0) - time.time()
        return f"cooling down {int(left)}s" if left > 0 else "ready"
