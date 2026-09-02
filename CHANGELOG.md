# Changelog

## 1.0.3

- Fixed the settings panel crashing on open (`TypeError: gobject 'AdwEntryRow' doesn't support property 'subtitle'`), reported by [@Core447](https://github.com/Core447) while reviewing the StreamController Store submission. `AdwEntryRow` has no `subtitle` property (unlike `AdwActionRow`/`AdwComboRow`, which do) — passing one as a construct argument crashed as soon as the log-path or sessions-directory rows were built. Moved that hint text to a tooltip on each row instead.

## 1.0.2

Real-world fixes from [@parkour86](https://github.com/parkour86) testing against actual Grok Build/SuperGrok installs ([#3](https://github.com/ENjxzlt/Grok-Usage-Streamcontroller/issues/3), [#4](https://github.com/ENjxzlt/Grok-Usage-Streamcontroller/pull/4), [#5](https://github.com/ENjxzlt/Grok-Usage-Streamcontroller/pull/5), [#6](https://github.com/ENjxzlt/Grok-Usage-Streamcontroller/pull/6)):

- **Fixed a wrong 0% inference introduced in 1.0.1.** `historyLen` is not a usage signal — on SuperGrok it's `0` even on billing lines carrying a real `creditUsagePercent` (confirmed against 448/448 entries in a real log). `_infer_percent` now returns `creditUsagePercent` as-is (or `None`); the key shows an empty ring + `0%` whenever a billing period is known but xAI hasn't reported a percent yet (which, per the same log evidence, means "under ~1%", not "no data") — same visual, correct reasoning.
- **Fixed the unused ring track being invisible on black Stream Deck keys.** It composited to near-black; switched from a dark slate track to a pale, translucent one so the full circle reads correctly, with the used arc still fully opaque so it stays visually brighter.
- **Shrunk the ring** (media size 0.97 → 0.72, stroke 90 → 72, inset 70 → 48) so it sits around the percentage instead of running underneath the top/bottom labels, with a tiered center font size (20/18/15pt) so 100% still fits.

## 1.0.1

- Fixed the ring/percentage silently not showing on accounts where `creditUsagePercent` is absent from the billing entry (observed on a fresh Free-tier account with zero usage so far, where xAI's API omits the field entirely instead of sending `0`). Now inferred as 0% specifically when `historyLen` is `0`; still falls back to a neutral state for any other case where the field is genuinely missing.

## 1.0.0

- Initial release: shows the current billing period's `creditUsagePercent` (as reported by the Grok Build CLI itself) as a progress ring, plus time remaining until the period resets — refreshed on an interval and on key press.
- Reads Grok Build's own local `~/.grok/logs/unified.jsonl` log for its most recent `"billing: fetched credits config"` entry — no API key, no network calls, no cost per refresh. Log format based on a redacted sample shared in [issue #6](https://github.com/ENjxzlt/Claude-Code-Usage-Streamcontroller/issues/6) by [@parkour86](https://github.com/parkour86); unofficial/reverse-engineered, not documented by xAI.
- Optional bottom-label mode reads the most recently active session's `updates.jsonl` for the last completed turn's token count or approximate USD cost.
- On Flatpak installs, log reads run via `flatpak-spawn --host` (same mechanism the Claude Code Usage plugin uses for `ccusage`), since the plugin's sandboxed filesystem view doesn't see `~/.grok` directly.
- Ring and label colors in xAI's monochrome brand palette, with hand-picked amber/red status accents since the brand kit itself has no traffic-light colors.
