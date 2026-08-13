# Grok Usage — StreamController Plugin

A [StreamController](https://github.com/StreamController/StreamController) plugin that shows your **Grok (xAI) API** rate-limit usage on a Stream Deck key: a progress ring for how much of your current requests- or tokens-per-window budget you've used, plus the time left until that window resets.

It works by making a tiny chat completion request to the [xAI API](https://docs.x.ai/) (`api.x.ai`) with your own API key and reading the `x-ratelimit-*` response headers xAI includes on every inference call — the same mechanism OpenAI-compatible APIs use to report request/token budgets. Unlike [Claude Code Usage](https://github.com/ENjxzlt/Claude-Code-Usage-Streamcontroller) (which reads free local log files via `ccusage`), **xAI has no local usage log and no free read-only usage endpoint**, so every refresh here is one small, real, billed API call.

![Ring preview at 35%, 78%, 96% and 100%](docs/preview.png)

> **Disclaimer:** This plugin — code, README, and assets — was written by [Claude Code](https://claude.com/claude-code), Anthropic's AI coding agent, based on prompts from the repo owner. It has not been verified against a live xAI account yet; review it yourself before trusting it, especially the parts that send requests and handle your API key. Issues and PRs are welcome.

## What it shows

- **Progress ring:** a circular gauge drawn around the key, filling clockwise as you use up the current rate-limit window, in xAI's own monochrome brand style — near-white under 70%, amber 70–90%, red ≥ 90% (the amber/red aren't from xAI's brand kit, which is deliberately black-and-white only; they're hand-picked for legibility).
- **Top label:** `Grok`
- **Center label:** the **percentage** used of whichever metric you pick in settings — **requests** per window or **tokens** per window (xAI rate-limits per model on both dimensions independently)
- **Bottom label:** time remaining until that window resets (e.g. `1m left`), or the raw remaining count (e.g. `482 reqs left`) if you enable that option
- If no API key is set yet, the key shows a prompt to add one instead of erroring
- Pressing the key forces an immediate refresh (still a real, billed request)

> **Note on accuracy:** this reflects xAI's own rate-limit accounting for the *model you configure*, not your account's overall spend, credit balance, or weekly/monthly caps — xAI doesn't expose those over the API. Rate-limit windows are typically short (per-minute), so treat this as a live "how close am I to getting throttled right now" gauge rather than a spend tracker.

## Requirements

- StreamController (Flatpak or native install both work)
- An [xAI API key](https://console.x.ai/) with a payment method on file (required by xAI before any key will work)
- Outbound network access to `api.x.ai` from wherever StreamController runs

No extra Python packages beyond what StreamController already ships (Pillow) — the HTTP call uses the standard library, no `requests`/Node/npm needed.

## Installation

### From the StreamController Store

Search for **Grok Usage** in StreamController's built-in store and install it from there.

### Manually

1. Clone or copy this folder somewhere on your machine.
2. Run the installer, which symlinks it into StreamController's plugin directory:
   ```bash
   ./install.sh
   ```
   This detects both the Flatpak install (`~/.var/app/com.core447.StreamController/data/plugins/`) and a native install (`~/.local/share/StreamController/plugins/`). If your setup differs, symlink the folder into your plugins directory manually.
3. Restart StreamController.
4. Open a key's action picker, find **Grok Usage**, and add it to a key.
5. Open the key's settings and paste in your xAI API key.

## Configuration

Click the key's action in StreamController to open its settings:

| Setting | Default | Description |
| --- | --- | --- |
| xAI API key | *(empty)* | From [console.x.ai](https://console.x.ai/) → API Keys. Required — the key shows a "Set API key" prompt until one is entered. |
| API base URL | `https://api.x.ai/v1` | Only change this if you're routing through a proxy in front of the xAI API. |
| Model to probe | `grok-4-fast` | Rate limits are per model — set this to whichever model you actually use, so the numbers reflect your real budget. |
| Ring metric | Requests | Whether the ring/percentage tracks your requests-per-window or tokens-per-window budget. |
| Refresh interval | `60` seconds | How often the key updates in the background (minimum 30s). **Every refresh is a real, billed request** — don't set this lower than you're comfortable paying for. |
| Show remaining count instead of reset time | off | Swap the bottom label to the raw remaining requests/tokens instead of the countdown to reset. |

## How it works

Every refresh interval (and once immediately on key press, as long as an API key is set), a background thread sends:

```
POST <base URL>/chat/completions
{"model": "<model>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
```

and reads the response's `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`, `x-ratelimit-reset-requests` headers (and the `-tokens` equivalents), which is the cheapest request that reliably gets xAI to populate them. The reset headers come back either as a plain second count or a compound duration string (`1h2m3s`, `6m0s`) — both are parsed. The ring is drawn with Pillow (already a StreamController dependency) at 4x resolution and downsampled for smooth edges, then set as the key's media. The UI update is marshalled back onto the main thread via `GLib.idle_add`, so a slow API response never blocks StreamController.

No `flatpak-spawn`/host-shell dance is needed here (unlike the Claude Code plugin) — this talks to the xAI API directly over HTTPS from within the plugin process, so it works the same way under Flatpak as natively, as long as the sandbox has network access (which StreamController's already does).

## Troubleshooting

Check the log for errors, filtering to just this plugin:

```bash
# Flatpak
grep -E "GrokUsage" ~/.var/app/com.core447.StreamController/data/logs/logs.log
# native
grep -E "GrokUsage" ~/.local/share/StreamController/logs/logs.log
```

- **Key shows "Set API key":** open the key's settings and paste in a key from [console.x.ai](https://console.x.ai/) → API Keys.
- **Key shows "!" / "xAI API error":** the `[GrokUsage] ...` line right above it in the log has the actual failure. Common causes: no payment method on the xAI account, a typo'd model name, or an invalid/revoked key. Test by hand: `curl https://api.x.ai/v1/chat/completions -H "Authorization: Bearer $XAI_API_KEY" -H "Content-Type: application/json" -d '{"model":"grok-4-fast","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' -i` and check the response headers/body.
- **Percentage looks stuck at the same value:** rate-limit windows reset frequently (often every minute) — check the bottom label's countdown, and remember the ring reflects usage *at the moment of the last refresh*, not live.

## Contributing

Issues and PRs welcome. This repo already ships everything the [store submission process](https://streamcontroller.github.io/docs/latest/plugin_dev/intro/) expects — `manifest.json` (with a `github` field), `about.json`, `attribution.json`, `requirements.txt`, the `store/` thumbnail, and `.github/workflows/notify-store.yml` (needs a `STORE_AUTOMATION_TOKEN` once accepted). What's still open, and has to come from whoever submits it (it's a personal attestation to Core447's store terms, tied to your own GitHub account):

1. Fork [StreamController-Store](https://github.com/StreamController/StreamController-Store).
2. Add an entry to `Plugins.json`:
   ```json
   {
       "url": "https://github.com/ENjxzlt/Grok-Usage-Streamcontroller",
       "commits": {
           "1.5.0-beta": "<commit hash of the release you tested against>"
       }
   }
   ```
3. Open a PR against the Store repo and wait for approval (usually a couple of hours).

## License

[GPL-3.0](LICENSE), matching StreamController itself. See [`attribution.json`](attribution.json) and the `Attribution.txt` files under `assets/` and `store/` for asset credits.
