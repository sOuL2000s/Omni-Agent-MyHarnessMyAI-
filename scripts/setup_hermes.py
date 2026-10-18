"""Writes ~/.hermes/config.yaml + .env using the *primary pool* from
data/providers.json (not the env fallback).

Usage:
    python scripts/setup_hermes.py

Only useful if you actually use Hermes. The generated config points it at
the same provider, model and key as Omni's primary pool.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import config  # noqa: E402


def main() -> int:
    cfg = config.load()
    pm = cfg.providers
    pools = pm.summary()
    if not pools:
        print("No keys configured yet.", file=sys.stderr)
        print("Add one in the web UI (Keys & models) or from the terminal:  /add AIza...",
              file=sys.stderr)
        return 1

    primary = pools[0]
    pool_name = primary["pool"]
    keys = pm.providers_in(pool_name)
    if not keys:
        print(f"Pool '{pool_name}' has no keys.", file=sys.stderr)
        return 1
    key = keys[0]

    d = Path.home() / ".hermes"
    d.mkdir(exist_ok=True)

    cfg_yaml = (
        'model:\n'
        '  provider: custom\n'
        f'  base_url: "{primary["base_url"]}"\n'
        f'  default: "{primary["model"]}"\n'
        'terminal:\n'
        '  backend: local\n'
        'approvals:\n'
        '  mode: manual\n'
    )
    (d / "config.yaml").write_text(cfg_yaml, encoding="utf-8")

    env = d / ".env"
    line = f"OMNIROUTE_API_KEY={key.api_key}\n"
    existing = env.read_text(encoding="utf-8") if env.exists() else ""
    if "OMNIROUTE_API_KEY=" not in existing:
        env.write_text(existing + line, encoding="utf-8")
    else:
        print(f"{env.name} already defines OMNIROUTE_API_KEY; leaving it unchanged.")

    print(f"wrote {d / 'config.yaml'} (pool '{pool_name}' -> {primary['model']})")
    print(f"key written to {env}")
    print("If Hermes returns 401, check its docs for the exact env-var name it expects "
          "for custom endpoints.")
    return 0


if __name__ == "__main__":
    sys.exit(main())