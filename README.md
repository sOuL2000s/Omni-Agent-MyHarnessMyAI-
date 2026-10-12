# Omni Agent: your own AI harness (no Docker)

An agent you control. It talks to any OpenAI-compatible service (Gemini, OpenRouter, OpenAI, Groq, Ollama, OmniRoute...).
It can read and edit code, edit Word/Excel/PowerPoint files, run shell commands, fetch web pages and remember things.
It has a **web UI**, and it rotates API keys and falls back across models by itself. The core uses only the Python standard library.

## 1. Quick start (Windows)
```
install.bat          # once: creates .venv and installs python-docx / openpyxl / python-pptx
omni-ui.bat          # starts the web UI and opens your browser
```
(Linux/macOS/WSL: `./install.sh`, then `./omni-ui`.)
If you unzipped over your old folder, your `.venv` is kept and `omni-ui.bat` works straight away.

Keep the link printed in the terminal private: it contains a random token and unlocks the agent.

## 2. The web UI
* **Chat**: tool calls appear as collapsible cards, and key or model switches appear as notices. Shell commands and writes outside the workspace pop up an **Allow / Deny** dialog. **Stop** cancels a run.
* **Keys & models** (top right):
  * Paste many keys at once, one per line. Test one key or all of them. Each key shows a health dot.
  * Pick the model per service. **Apply** changes it for **all** keys of that service at once. **List...** loads the models the service really offers.
  * Set fallback models, the rotation strategy and the service priority (▲▼).
* **Files**: upload files into the workspace (📎 or drag and drop) and download what the agent made.
* Chats are saved in `data/sessions/`. The ☰ list on the left switches between them.

## 3. How keys, models and failover work
Keys are grouped into **pools**: `gemini-1 ... gemini-37` form the pool `gemini`. The model is chosen **per pool**.
For every request Omni tries, in this order:

1. each usable key of the first pool with the chosen model (*round robin* spreads calls over all keys; *failover* always starts with the first key),
2. the same keys with each **fallback model** (Gemini quotas are per model, so `gemini-2.5-flash-lite` still works when `gemini-2.5-flash` is used up),
3. the next pool (for example `openrouter`).

Every failure is classified, and only the right thing is put on cooldown:

| Problem | Meaning | Cooldown |
|---|---|---|
| 429 per minute | key too fast | the service's `retryDelay` (min 10 s) |
| 429 per day | daily quota used | until the quota resets (Google: midnight Pacific) |
| 401 / 400 `API_KEY_INVALID` / leaked key | key is dead | 6 h, shown as **INVALID KEY** |
| 402 or billing | no credits | 1 h |
| 404 or "model not found" | the model is wrong for that service | 10 min, **whole pool** skipped at once (not 37 wasted calls) |
| 5xx or network | service problem | 15-20 s, whole pool skipped |
| other 400 (bad request) | the request itself is wrong | **no cooldown**: the error is shown instead of burning all your keys |

If every key is only briefly rate-limited, Omni **waits** (up to `AGENT_MAX_WAIT`, default 90 s) instead of failing. Cooldowns are saved in `data/state.json`, so a used-up daily quota is remembered after a restart.

Terminal equivalents (`omni` chat or `omni provider ...`):
```
/providers   /keys [pool]   /addkeys gemini KEY1 KEY2 ...   /addkeys gemini @keys.txt
/model gemini-2.5-pro       /model openrouter anthropic/claude-sonnet-5.5     /models gemini flash
/fallback gemini gemini-2.5-flash-lite,gemini-2.0-flash      /strategy round_robin|failover
/use openrouter   /order gemini,openrouter   /test all   /revive   /remove NAME
```
`omni check` pings the primary pool and warns if its model is not in the service's list. `omni check --all` tests every key.

### Tips for many free Gemini keys
* Google's free quotas are per **project**, not per key. Keys created in the same Google Cloud project share one quota. Create each key in a separate project (AI Studio → "Create API key in new project") for rotation to actually add capacity.
* Set one or two fallback models (see above) so a used-up daily quota on one model does not stop you.
* Free keys can use your prompts for training. Do not send confidential data through them.

## 4. Configuration (`.env`)
`AGENT_WORKSPACE` (folder the agent may touch), `AGENT_ALLOW_OUTSIDE`, `AGENT_AUTO_APPROVE`, `AGENT_MAX_TURNS`, `AGENT_TEMPERATURE`, `AGENT_TIMEOUT`, `AGENT_STRATEGY`, `AGENT_MAX_WAIT`.
Keys live in `data/providers.json` (plain text, git-ignored). Treat that folder, `.env` and **this zip** as secrets.

## 5. Other commands
| Goal | Command |
|---|---|
| Web UI | `omni-ui.bat` or `omni.bat ui` (`--port 9000`, `--no-browser`) |
| Terminal chat | `omni.bat` |
| One-shot task | `omni.bat run "In report.docx replace 'Q2' with 'Q3'"` |
| Task from file | `omni.bat run -f workflows/review_code.md` |
| Repeat on a timer | `omni.bat schedule -f workflows/daily_notes_cleanup.md --every 86400` |

## 6. Hermes: what it is, and why it is not the UI here
[Hermes Agent](https://hermes-agent.nousresearch.com) is a **separate, full agent** with its own dashboard (`hermes dashboard`). That dashboard manages Hermes (its config, keys, sessions), not Omni. Its Chat tab embeds Hermes' terminal UI and needs Linux/macOS/WSL2. On native Windows only the admin pages work. So it cannot be a front end for Omni, and the Omni UI above replaces it for this project.
You can still run Hermes next to Omni (`config/hermes-config.yaml`, `scripts/setup_hermes.py`). Hermes keeps its own key pools (`hermes auth add ...`), separate from Omni's.

## 7. Safety
The UI listens on `127.0.0.1` only. It needs the per-start token and checks the `Host` header, and API keys are never sent to the browser. Only use `--host 0.0.0.0` on a network you trust, because anyone with the link can run commands as you. **auto-approve** (UI toggle or `AGENT_AUTO_APPROVE=1`) skips all confirmation dialogs, so use it only in a workspace you trust.

## 8. Extending
Add a function with `@tool(...)` in `agent/tools/` (see `files.py`) and import it in `agent/tools/__init__.py`. The model sees it automatically.

## 9. Tests (offline, mock gateways; they never touch your `data/`)
```
python tests/test_failover.py     # rotation, quotas, model fallback, waiting, persistence
python tests/test_agent.py        # tool loop, workspace guard, approvals
python tests/test_web.py          # UI server: auth, chat + approvals, key/model endpoints
```

## 10. Troubleshooting
* **"All keys/providers failed"**: the message says why per service (invalid, quota, model name...). Open Keys & models → *Test all*.
* **Model not available / 404**: click **List...** next to the model and pick one from the list. A ⚠ appears when the model is not in the service's list.
* **`providers.json` edited by hand on Windows**: Notepad/PowerShell add a BOM. Older versions silently ignored the whole file (all keys "missing"). It is now read correctly and rewritten without the BOM.
* **No browser opens**: copy the `http://localhost:8765/?token=...` link from the terminal.
