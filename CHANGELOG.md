# Changelog

## 1.0.0

- Initial release: shows Grok (xAI) API rate-limit usage — percentage of a configurable model's requests-per-window or tokens-per-window budget, plus time remaining until reset — refreshed on an interval and on key press.
- Reads xAI's `x-ratelimit-*` response headers via a minimal (1 max-token) chat completion request; no local usage logs exist for the xAI API, so every refresh is a small real API call.
- Ring and label colors in xAI's monochrome brand palette (near-black/near-white), with hand-picked amber/red status accents since the brand kit itself has no traffic-light colors.
- Settings: API key, base URL, model, ring metric (requests vs. tokens), refresh interval, and a toggle to show the raw remaining count instead of the reset countdown.
