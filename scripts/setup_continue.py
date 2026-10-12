"""Writes ~/.continue/config.yaml from your .env so VS Code uses the same gateway."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import config

cfg = config.load()
tpl = (config.ROOT / "config" / "continue-config.yaml").read_text()
out = tpl.replace("__MODEL__", cfg.model).replace("__BASE_URL__", cfg.base_url).replace("__API_KEY__", cfg.api_key)
dest = Path.home() / ".continue" / "config.yaml"
dest.parent.mkdir(exist_ok=True)
if dest.exists():
    dest.with_suffix(".yaml.bak").write_text(dest.read_text())
    print("existing config backed up to config.yaml.bak")
dest.write_text(out)
print("wrote", dest)
