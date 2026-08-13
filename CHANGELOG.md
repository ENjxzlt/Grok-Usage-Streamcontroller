# Changelog

## 1.0.1

- Fixed the ring/percentage silently not showing on accounts where `creditUsagePercent` is absent from the billing entry (observed on a fresh Free-tier account with zero usage so far, where xAI's API omits the field entirely instead of sending `0`). Now inferred as 0% specifically when `historyLen` is `0`; still falls back to a neutral state for any other case where the field is genuinely missing.

## 1.0.0

- Initial release: shows the current billing period's `creditUsagePercent` (as reported by the Grok Build CLI itself) as a progress ring, plus time remaining until the period resets — refreshed on an interval and on key press.
- Reads Grok Build's own local `~/.grok/logs/unified.jsonl` log for its most recent `"billing: fetched credits config"` entry — no API key, no network calls, no cost per refresh. Log format based on a redacted sample shared in [issue #6](https://github.com/ENjxzlt/Claude-Code-Usage-Streamcontroller/issues/6) by [@parkour86](https://github.com/parkour86); unofficial/reverse-engineered, not documented by xAI.
- Optional bottom-label mode reads the most recently active session's `updates.jsonl` for the last completed turn's token count or approximate USD cost.
- On Flatpak installs, log reads run via `flatpak-spawn --host` (same mechanism the Claude Code Usage plugin uses for `ccusage`), since the plugin's sandboxed filesystem view doesn't see `~/.grok` directly.
- Ring and label colors in xAI's monochrome brand palette, with hand-picked amber/red status accents since the brand kit itself has no traffic-light colors.
