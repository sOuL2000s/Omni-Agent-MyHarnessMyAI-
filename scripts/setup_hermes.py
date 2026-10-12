"""Writes ~/.hermes/config.yaml + .env (only if Hermes is installed/you want it)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import config

cfg = config.load()
d = Path.home() / ".hermes"
d.mkdir(exist_ok=True)
(d / "config.yaml").write_text(
    f'model:\n  provider: custom\n  base_url: "{cfg.base_url}"\n  default: "{cfg.model}"\n'
    'terminal:\n  backend: local\napprovals:\n  mode: manual\n')
env = d / ".env"
line = f"OMNIROUTE_API_KEY={cfg.api_key}\n"
env.write_text((env.read_text() if env.exists() else "") + line)
print("wrote", d / "config.yaml", "and key to", env)
print("If Hermes returns 401, check its docs for the exact env-var name it expects for custom endpoints.")
