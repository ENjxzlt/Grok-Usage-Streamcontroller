import json
import os
import re
import threading
import urllib.error
import urllib.request

import gi
from loguru import logger as log
from PIL import Image, ImageDraw

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib

from src.backend.PluginManager.ActionBase import ActionBase

# ---------------------------------------------------------------------------
# Defaults & helpers
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4-fast"
DEFAULT_METRIC = "requests"  # "requests" or "tokens"
DEFAULT_REFRESH_SECONDS = 60
MIN_REFRESH_SECONDS = 30
REQUEST_TIMEOUT = 20

# xAI's brand kit is deliberately monochrome (near-black / near-white, plus a
# "Mine Shaft" gray accent - https://x.ai/legal/brand-guidelines) - there's
# no traffic-light palette to draw on for a usage gauge, so the warn/crit
# stops below are hand-picked, unaffiliated colors chosen only for legibility.
XAI_INK = (10, 10, 10)
XAI_PAPER = (245, 245, 245)
XAI_SLATE = (49, 49, 49)
XAI_AMBER = (224, 168, 62)
XAI_CRIMSON = (196, 64, 58)
XAI_CRIMSON_DARK = (128, 36, 32)

COLOR_OK = [*XAI_PAPER, 255]
COLOR_WARN = [*XAI_AMBER, 255]
COLOR_CRIT = [*XAI_CRIMSON, 255]
COLOR_NONE = [0, 0, 0, 0]

LABEL_OUTLINE = {"outline_width": 2, "outline_color": [*XAI_INK, 190]}

# Rendered at 4x and downsampled - PIL's arc drawing has no anti-aliasing of
# its own, so this is a cheap way to avoid a jagged ring on the key.
RING_CANVAS = 1024
RING_OUTPUT = 256
RING_THICKNESS = 90
RING_INSET = 70
RING_TRACK_COLOR = (*XAI_SLATE, 90)
RING_OVERFLOW_COLOR = (*XAI_CRIMSON_DARK, 255)

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def render_ring_image(percent: float, color) -> "Image.Image":
    """
    Draws a circular progress ring (0-100%, clockwise from the top) as a
    transparent-background RGBA image, meant to be used as the key's media
    so it sits behind the text labels.
    """
    size = RING_CANVAS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bbox = [RING_INSET, RING_INSET, size - RING_INSET, size - RING_INSET]

    draw.arc(bbox, start=0, end=360, fill=RING_TRACK_COLOR, width=RING_THICKNESS)

    sweep = max(0.0, min(percent, 100.0)) / 100.0 * 360.0
    if sweep > 0.5:
        # Start at the top (12 o'clock) and sweep clockwise.
        draw.arc(bbox, start=-90, end=-90 + sweep, fill=tuple(color), width=RING_THICKNESS)

    if percent > 100:
        outer = [c + (-40 if i < 2 else 40) for i, c in enumerate(bbox)]
        draw.arc(outer, start=0, end=360, fill=RING_OVERFLOW_COLOR, width=28)

    return img.resize((RING_OUTPUT, RING_OUTPUT), Image.LANCZOS)


def humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def humanize_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def _parse_int_header(value):
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_reset_seconds(value):
    """
    xAI's `x-ratelimit-reset-*` headers show up either as a plain number of
    seconds ("6") or as a Go-style compound duration string ("1h2m3s",
    "6m0s", "500ms"), mirroring the OpenAI-compatible header convention.
    Handles both; returns None if the value is missing or unparseable.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass

    total = 0.0
    matched = False
    for amount, unit in _DURATION_RE.findall(value):
        matched = True
        amount = float(amount)
        if unit == "h":
            total += amount * 3600
        elif unit == "m":
            total += amount * 60
        elif unit == "ms":
            total += amount / 1000
        else:  # "s"
            total += amount
    return total if matched else None


def _extract_error_message(body: str):
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return error.get("message") or str(error)
    if isinstance(error, str):
        return error
    return body[:200]


def _has_rate_limit_headers(headers) -> bool:
    return (
        headers.get("x-ratelimit-remaining-requests") is not None
        or headers.get("x-ratelimit-remaining-tokens") is not None
    )


def fetch_rate_limits(base_url: str, api_key: str, model: str, timeout: int = REQUEST_TIMEOUT) -> dict:
    """
    Sends a minimal (1 max-token) chat completion to `<base_url>/chat/completions`
    and reads the `x-ratelimit-*` response headers, which xAI's API populates
    on every inference call for the model's current request- and
    token-per-window budget. There is no free/read-only endpoint for this -
    every refresh is one tiny, real, billed request.

    Raises RuntimeError if the request fails or the response carries no
    rate-limit headers at all (bad API key, unknown model, ...).
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = response.headers
            response.read()
    except urllib.error.HTTPError as e:
        headers = e.headers
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - best-effort error body
            body = ""
        if not _has_rate_limit_headers(headers):
            raise RuntimeError(_extract_error_message(body) or f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason)) from e
    except TimeoutError as e:
        raise RuntimeError("request timed out") from e

    if not _has_rate_limit_headers(headers):
        raise RuntimeError("response had no rate-limit headers - check the model name")

    return {
        "limit_requests": _parse_int_header(headers.get("x-ratelimit-limit-requests")),
        "remaining_requests": _parse_int_header(headers.get("x-ratelimit-remaining-requests")),
        "reset_requests": _parse_reset_seconds(headers.get("x-ratelimit-reset-requests")),
        "limit_tokens": _parse_int_header(headers.get("x-ratelimit-limit-tokens")),
        "remaining_tokens": _parse_int_header(headers.get("x-ratelimit-remaining-tokens")),
        "reset_tokens": _parse_reset_seconds(headers.get("x-ratelimit-reset-tokens")),
    }


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------


class GrokUsage(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True

        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def on_ready(self):
        self._set_static_icon()

        self.set_top_label(text=self.tr("grok-usage.label.top"), font_size=12, **LABEL_OUTLINE)
        self.set_center_label(text=self.tr("grok-usage.label.loading"), font_size=20, **LABEL_OUTLINE)
        self.set_bottom_label(text="", font_size=11, **LABEL_OUTLINE)

        self._start_worker()

    def _set_static_icon(self):
        """Falls back to the plain plugin icon when there's no ring to draw
        (missing API key, error, or a metric xAI didn't return headers for)."""
        icon_path = os.path.join(self.plugin_base.PATH, "assets", "icon.png")
        if os.path.isfile(icon_path):
            self.set_media(media_path=icon_path, size=0.55, valign=-0.65)

    def on_remove(self):
        self._stop_event.set()

    def on_key_down(self):
        # Manual refresh on key press, without waiting for the timer. Like
        # every refresh, this makes one tiny real request against the xAI API.
        threading.Thread(
            target=self._refresh_once, daemon=True, name="GrokUsage-manual-refresh"
        ).start()

    def tr(self, key: str) -> str:
        return self.plugin_base.lm.get(key)

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #

    def _settings(self) -> dict:
        settings = self.get_settings()
        settings.setdefault("api_key", "")
        settings.setdefault("base_url", DEFAULT_BASE_URL)
        settings.setdefault("model", DEFAULT_MODEL)
        settings.setdefault("metric", DEFAULT_METRIC)
        settings.setdefault("refresh_seconds", DEFAULT_REFRESH_SECONDS)
        settings.setdefault("show_remaining", False)
        self.set_settings(settings)
        return settings

    def get_config_rows(self) -> list:
        settings = self._settings()

        # AdwPasswordEntryRow (masked input with a visibility toggle) has
        # been part of libadwaita since 1.2. Fall back to a plain EntryRow
        # on an older runtime instead of failing to load the whole plugin.
        password_row_cls = getattr(Adw, "PasswordEntryRow", None) or Adw.EntryRow
        api_key_row = password_row_cls(
            title=self.tr("grok-usage.api-key.title"),
            subtitle=self.tr("grok-usage.api-key.subtitle"),
        )
        api_key_row.set_text(str(settings.get("api_key", "")))
        api_key_row.connect("notify::text", self._on_api_key_changed)

        base_url_row = Adw.EntryRow(title=self.tr("grok-usage.base-url.title"))
        base_url_row.set_text(str(settings.get("base_url", DEFAULT_BASE_URL)))
        base_url_row.connect("notify::text", self._on_base_url_changed)

        model_row = Adw.EntryRow(title=self.tr("grok-usage.model.title"))
        model_row.set_text(str(settings.get("model", DEFAULT_MODEL)))
        model_row.connect("notify::text", self._on_model_changed)

        metric_row = Adw.ComboRow(
            title=self.tr("grok-usage.metric.title"),
            subtitle=self.tr("grok-usage.metric.subtitle"),
        )
        metric_options = Gtk.StringList()
        metric_options.append(self.tr("grok-usage.metric.requests"))
        metric_options.append(self.tr("grok-usage.metric.tokens"))
        metric_row.set_model(metric_options)
        metric_row.set_selected(1 if settings.get("metric") == "tokens" else 0)
        metric_row.connect("notify::selected", self._on_metric_changed)

        refresh_row = Adw.EntryRow(title=self.tr("grok-usage.refresh.title"), subtitle=self.tr("grok-usage.refresh.subtitle"))
        refresh_row.set_text(str(settings.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)))
        refresh_row.connect("notify::text", self._on_refresh_changed)

        remaining_row = Adw.ActionRow(
            title=self.tr("grok-usage.show-remaining.title"),
            subtitle=self.tr("grok-usage.show-remaining.subtitle"),
        )
        remaining_switch = Gtk.Switch(
            active=bool(settings.get("show_remaining", False)), valign=Gtk.Align.CENTER
        )
        remaining_switch.connect("notify::active", self._on_show_remaining_changed)
        remaining_row.add_suffix(remaining_switch)
        remaining_row.set_activatable_widget(remaining_switch)

        return [api_key_row, base_url_row, model_row, metric_row, refresh_row, remaining_row]

    def _on_api_key_changed(self, entry, _):
        settings = self.get_settings()
        settings["api_key"] = entry.get_text().strip()
        self.set_settings(settings)

    def _on_base_url_changed(self, entry, _):
        settings = self.get_settings()
        settings["base_url"] = entry.get_text().strip() or DEFAULT_BASE_URL
        self.set_settings(settings)

    def _on_model_changed(self, entry, _):
        settings = self.get_settings()
        settings["model"] = entry.get_text().strip() or DEFAULT_MODEL
        self.set_settings(settings)

    def _on_metric_changed(self, row, _):
        settings = self.get_settings()
        settings["metric"] = "tokens" if row.get_selected() == 1 else "requests"
        self.set_settings(settings)

    def _on_refresh_changed(self, entry, _):
        settings = self.get_settings()
        settings["refresh_seconds"] = _parse_int(
            entry.get_text(), DEFAULT_REFRESH_SECONDS, minimum=MIN_REFRESH_SECONDS
        )
        self.set_settings(settings)

    def _on_show_remaining_changed(self, switch, _):
        settings = self.get_settings()
        settings["show_remaining"] = switch.get_active()
        self.set_settings(settings)

    # ------------------------------------------------------------------ #
    # Background refresh
    # ------------------------------------------------------------------ #

    def _start_worker(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name=f"GrokUsage-{self.action_id}"
        )
        self._worker_thread.start()

    def _worker_loop(self):
        while not self._stop_event.is_set():
            self._refresh_once()
            interval = max(
                MIN_REFRESH_SECONDS,
                int(self._settings().get("refresh_seconds", DEFAULT_REFRESH_SECONDS)),
            )
            self._stop_event.wait(interval)

    def _refresh_once(self):
        settings = self._settings()
        api_key = str(settings.get("api_key", "")).strip()
        if not api_key:
            GLib.idle_add(self._render_no_key)
            return

        base_url = settings.get("base_url", DEFAULT_BASE_URL)
        model = settings.get("model", DEFAULT_MODEL)
        try:
            data = fetch_rate_limits(base_url, api_key, model)
            error = None
        except Exception as e:  # noqa: BLE001 - surface any failure on the key
            data = None
            error = str(e)

        GLib.idle_add(self._render, data, error, settings)

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def _render_no_key(self):
        self._set_static_icon()
        self.set_center_label(text="–", font_size=22, **LABEL_OUTLINE)
        self.set_bottom_label(text=self.tr("grok-usage.label.no-key"), font_size=10, **LABEL_OUTLINE)
        self.set_background_color(COLOR_NONE)
        return False

    def _render(self, data, error, settings):
        if error is not None:
            self._set_static_icon()
            self.set_center_label(text="!", font_size=22, **LABEL_OUTLINE)
            self.set_bottom_label(text=self.tr("grok-usage.label.error"), font_size=10, **LABEL_OUTLINE)
            self.set_background_color(COLOR_CRIT)
            log.error(f"[GrokUsage] {error}")
            return False

        metric = settings.get("metric", DEFAULT_METRIC)
        show_remaining = bool(settings.get("show_remaining", False))

        if metric == "tokens":
            limit, remaining, reset_seconds = (
                data.get("limit_tokens"),
                data.get("remaining_tokens"),
                data.get("reset_tokens"),
            )
        else:
            limit, remaining, reset_seconds = (
                data.get("limit_requests"),
                data.get("remaining_requests"),
                data.get("reset_requests"),
            )

        if limit and remaining is not None:
            used = max(0, limit - remaining)
            percent = round((used / limit) * 100)
            center_text = f"{percent}%"
            if percent >= 90:
                color = COLOR_CRIT
            elif percent >= 70:
                color = COLOR_WARN
            else:
                color = COLOR_OK
            # The ring itself already carries the status color, so leave the
            # key's tile background neutral instead of double-signalling.
            self.set_media(image=render_ring_image(percent, color), size=0.97)
            self.set_background_color(COLOR_NONE)
        else:
            # xAI didn't return headers for the selected metric - fall back
            # to whatever raw number is available instead of a blank key.
            center_text = humanize_tokens(remaining) if remaining is not None else "–"
            self._set_static_icon()
            self.set_background_color(COLOR_NONE)

        if show_remaining and remaining is not None:
            key = "grok-usage.label.tokens-left" if metric == "tokens" else "grok-usage.label.requests-left"
            bottom_text = self.tr(key).format(n=humanize_tokens(remaining))
        elif reset_seconds is not None:
            bottom_text = self.tr("grok-usage.label.reset").format(time=humanize_seconds(reset_seconds))
        else:
            bottom_text = ""

        self.set_center_label(text=center_text, font_size=20, **LABEL_OUTLINE)
        self.set_bottom_label(text=bottom_text, font_size=11, **LABEL_OUTLINE)
        return False


# ---------------------------------------------------------------------------
# Small standalone helpers
# ---------------------------------------------------------------------------


def _parse_int(value, default: int, minimum: int | None = None) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    return result
