import json
import os
import shlex
import subprocess
import threading
from datetime import datetime, timezone

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

DEFAULT_LOG_PATH = "~/.grok/logs/unified.jsonl"
DEFAULT_SESSIONS_DIR = "~/.grok/sessions"
DEFAULT_SECONDARY = "reset"  # "reset" | "cost" | "tokens"
_SECONDARY_OPTIONS = ("reset", "cost", "tokens")
DEFAULT_REFRESH_SECONDS = 60
MIN_REFRESH_SECONDS = 15
COMMAND_TIMEOUT = 10

BILLING_MSG = "billing: fetched credits config"

# The billing entry can be far behind in a log that's been accumulating for
# months, and a session's updates.jsonl can carry huge usage numbers per
# line but each line is still only a few KB - these tail sizes comfortably
# cover "the last few CLI invocations" without ever reading a multi-MB file
# in full on every refresh.
LOG_TAIL_BYTES = 262_144
SESSION_TAIL_BYTES = 65_536

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
# Sit around the center % only so the groove does not run under the top
# "Grok" or bottom time-left labels (it used to be drawn at 97% of the key
# and ran straight under both). Keep the stroke modest so 10% and 100%
# still fit in the hole.
RING_THICKNESS = 72
RING_INSET = 48
# Mine Shaft (XAI_SLATE @ 90) composites to ~RGB 17 on a black Stream Deck
# tile and the unused groove vanishes. Paper at ~160 stays monochrome and
# reads as a full circle; fill is still opaque 255 so used % stays brighter.
RING_TRACK_COLOR = (*XAI_PAPER, 160)
RING_OVERFLOW_COLOR = (*XAI_CRIMSON_DARK, 255)


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


def is_in_flatpak() -> bool:
    return os.path.isfile("/.flatpak-info")


def humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def humanize_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _run_host_command(command: str, timeout: int = COMMAND_TIMEOUT):
    """
    Runs a shell command that reads from the Grok Build CLI's log files on
    the *host*. StreamController is commonly distributed as a Flatpak,
    which sandboxes the plugin process's filesystem view - `flatpak-spawn
    --host` (the same mechanism the sibling Claude Usage plugin uses to
    reach `ccusage`) runs the command on the host system instead, where
    `~/.grok` actually lives.
    """
    if is_in_flatpak():
        argv = ["flatpak-spawn", "--host", "bash", "-lc", command]
    else:
        argv = ["bash", "-lc", command]

    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        # Without an explicit cwd, the child inherits StreamController's
        # sandbox-internal working directory (e.g. /app/bin/StreamController).
        # flatpak-spawn --host then asks the host portal to chdir into that
        # same path before running the command - which doesn't exist on the
        # host, so the whole call fails with "Portal call failed: Failed to
        # start command". Pin it to the user's home directory instead, which
        # exists on both sides.
        cwd=os.path.expanduser("~"),
    )


def _infer_percent(config: dict):
    """
    Return `creditUsagePercent` only when xAI sent it. Do not infer 0%
    from `historyLen` — that field is 0 even on SuperGrok lines that also
    had 25% / 52%. A missing key is handled at render time: if we still
    have a billing period, the key shows an empty ring + 0% (under the
    ~1% report bar); if we have no billing row at all, the G icon.
    """
    return config.get("creditUsagePercent")


def _find_latest_billing_entry(text: str):
    """Scans a chunk of unified.jsonl (newest lines last) for the most
    recent "billing: fetched credits config" entry."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or BILLING_MSG not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("msg") != BILLING_MSG:
            continue

        ctx = obj.get("ctx") or {}
        config = ctx.get("config") or {}
        period = config.get("currentPeriod") or {}
        return {
            "percent": _infer_percent(config),
            "period_end": period.get("end") or config.get("billingPeriodEnd"),
            "tier": ctx.get("subscriptionTier"),
        }
    return None


def _find_latest_usage_entry(text: str):
    """Scans a chunk of a session's updates.jsonl (newest lines last) for
    the most recent completed turn's usage object."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or "turn_completed" not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        update = ((obj.get("params") or {}).get("update")) or {}
        if update.get("sessionUpdate") != "turn_completed":
            continue
        usage = update.get("usage")
        if usage:
            return usage
    return None


def fetch_billing_status(log_path: str, tail_bytes: int = LOG_TAIL_BYTES):
    """
    Tails the Grok Build CLI's unified log for the most recent "billing:
    fetched credits config" entry, which the CLI writes every time it
    refreshes its quota. This log format is unofficial - reverse-engineered
    from a real log sample, not documented by xAI - so it may need updating
    if a future Grok Build release changes its logging.

    Returns None (not an error) if the log exists but has no such entry
    within the tail window - e.g. Grok Build just hasn't been run recently
    enough for one to still be in range. Raises RuntimeError only for
    genuine read failures (missing file, permissions, timeout, ...).
    """
    proc = _run_host_command(f"tail -c {tail_bytes} {shlex.quote(log_path)}")
    if proc.returncode != 0:
        message = proc.stderr.strip() or f"could not read {log_path}"
        raise RuntimeError(message)
    return _find_latest_billing_entry(proc.stdout)


def fetch_last_turn_usage(sessions_dir: str):
    """
    Finds the most recently modified `updates.jsonl` under the Grok Build
    sessions directory and returns its latest completed turn's usage
    object, or None if anything about this lookup fails - it's a purely
    optional, best-effort secondary data point, so failures here should
    never turn the key red the way a `fetch_billing_status` failure does.
    """
    quoted_dir = shlex.quote(sessions_dir)
    find_command = (
        f"find {quoted_dir} -type f -name updates.jsonl -printf '%T@ %p\\n' "
        "2>/dev/null | sort -rn | head -n1 | cut -d' ' -f2-"
    )
    try:
        find_proc = _run_host_command(find_command)
    except Exception:  # noqa: BLE001 - best-effort, never fatal
        return None

    latest_file = find_proc.stdout.strip()
    if find_proc.returncode != 0 or not latest_file:
        return None

    try:
        tail_proc = _run_host_command(f"tail -c {SESSION_TAIL_BYTES} {shlex.quote(latest_file)}")
    except Exception:  # noqa: BLE001 - best-effort, never fatal
        return None
    if tail_proc.returncode != 0:
        return None

    return _find_latest_usage_entry(tail_proc.stdout)


def _seconds_until(iso_timestamp: str | None):
    if not iso_timestamp:
        return None
    try:
        end_dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end_dt - datetime.now(timezone.utc)).total_seconds()


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
        (error, or no billing entry found yet)."""
        icon_path = os.path.join(self.plugin_base.PATH, "assets", "icon.png")
        if os.path.isfile(icon_path):
            self.set_media(media_path=icon_path, size=0.55, valign=-0.65)

    def on_remove(self):
        self._stop_event.set()

    def on_key_down(self):
        # Manual refresh on key press, without waiting for the timer.
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
        settings.setdefault("log_path", DEFAULT_LOG_PATH)
        settings.setdefault("sessions_dir", DEFAULT_SESSIONS_DIR)
        settings.setdefault("secondary", DEFAULT_SECONDARY)
        settings.setdefault("refresh_seconds", DEFAULT_REFRESH_SECONDS)
        self.set_settings(settings)
        return settings

    def get_config_rows(self) -> list:
        settings = self._settings()

        # AdwEntryRow (unlike AdwActionRow/AdwComboRow below) has no "subtitle"
        # property - passing one as a construct kwarg crashes with
        # "TypeError: gobject 'AdwEntryRow' doesn't support property
        # 'subtitle'" as soon as the settings panel is opened. Set it as a
        # tooltip instead so the hint isn't lost entirely.
        log_path_row = Adw.EntryRow(title=self.tr("grok-usage.log-path.title"))
        log_path_row.set_tooltip_text(self.tr("grok-usage.log-path.subtitle"))
        log_path_row.set_text(str(settings.get("log_path", DEFAULT_LOG_PATH)))
        log_path_row.connect("notify::text", self._on_log_path_changed)

        sessions_dir_row = Adw.EntryRow(title=self.tr("grok-usage.sessions-dir.title"))
        sessions_dir_row.set_tooltip_text(self.tr("grok-usage.sessions-dir.subtitle"))
        sessions_dir_row.set_text(str(settings.get("sessions_dir", DEFAULT_SESSIONS_DIR)))
        sessions_dir_row.connect("notify::text", self._on_sessions_dir_changed)

        secondary_row = Adw.ComboRow(
            title=self.tr("grok-usage.secondary.title"), subtitle=self.tr("grok-usage.secondary.subtitle")
        )
        secondary_options = Gtk.StringList()
        secondary_options.append(self.tr("grok-usage.secondary.reset"))
        secondary_options.append(self.tr("grok-usage.secondary.cost"))
        secondary_options.append(self.tr("grok-usage.secondary.tokens"))
        secondary_row.set_model(secondary_options)
        secondary_row.set_selected(_SECONDARY_OPTIONS.index(settings.get("secondary", DEFAULT_SECONDARY)))
        secondary_row.connect("notify::selected", self._on_secondary_changed)

        refresh_row = Adw.EntryRow(title=self.tr("grok-usage.refresh.title"))
        refresh_row.set_text(str(settings.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)))
        refresh_row.connect("notify::text", self._on_refresh_changed)

        return [log_path_row, sessions_dir_row, secondary_row, refresh_row]

    def _on_log_path_changed(self, entry, _):
        settings = self.get_settings()
        settings["log_path"] = entry.get_text().strip() or DEFAULT_LOG_PATH
        self.set_settings(settings)

    def _on_sessions_dir_changed(self, entry, _):
        settings = self.get_settings()
        settings["sessions_dir"] = entry.get_text().strip() or DEFAULT_SESSIONS_DIR
        self.set_settings(settings)

    def _on_secondary_changed(self, row, _):
        settings = self.get_settings()
        settings["secondary"] = _SECONDARY_OPTIONS[row.get_selected()]
        self.set_settings(settings)

    def _on_refresh_changed(self, entry, _):
        settings = self.get_settings()
        settings["refresh_seconds"] = _parse_int(
            entry.get_text(), DEFAULT_REFRESH_SECONDS, minimum=MIN_REFRESH_SECONDS
        )
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
        log_path = os.path.expanduser(str(settings.get("log_path", DEFAULT_LOG_PATH)))
        secondary = settings.get("secondary", DEFAULT_SECONDARY)

        try:
            billing = fetch_billing_status(log_path)
            error = None
        except Exception as e:  # noqa: BLE001 - surface any failure on the key
            billing = None
            error = str(e)

        usage = None
        if error is None and billing is not None and secondary in ("cost", "tokens"):
            sessions_dir = os.path.expanduser(str(settings.get("sessions_dir", DEFAULT_SESSIONS_DIR)))
            usage = fetch_last_turn_usage(sessions_dir)

        GLib.idle_add(self._render, billing, usage, error, settings)

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def _render(self, billing, usage, error, settings):
        if error is not None:
            self._set_static_icon()
            self.set_center_label(text="!", font_size=22, **LABEL_OUTLINE)
            self.set_bottom_label(text=self.tr("grok-usage.label.error"), font_size=10, **LABEL_OUTLINE)
            self.set_background_color(COLOR_CRIT)
            log.error(f"[GrokUsage] {error}")
            return False

        if billing is None:
            self._set_static_icon()
            self.set_center_label(text="", font_size=22, **LABEL_OUTLINE)
            self.set_bottom_label(text=self.tr("grok-usage.label.no-data"), font_size=10, **LABEL_OUTLINE)
            self.set_background_color(COLOR_NONE)
            return False

        percent = billing.get("percent")
        if percent is None:
            # Period is known but xAI omitted the field (typical until ~1%).
            # Empty ring + 0% matches the normal key; do not use the G icon
            # or a "–" overlay.
            percent = 0
        else:
            percent = round(float(percent))
        center_text = f"{percent}%"
        # Wider text needs a smaller font to still fit inside the ring's hole.
        if percent >= 100:
            center_size = 15
        elif percent >= 10:
            center_size = 18
        else:
            center_size = 20
        if percent >= 90:
            color = COLOR_CRIT
        elif percent >= 70:
            color = COLOR_WARN
        else:
            color = COLOR_OK
        # The ring itself already carries the status color, so leave the
        # key's tile background neutral instead of double-signalling.
        self.set_media(image=render_ring_image(percent, color), size=0.72)
        self.set_background_color(COLOR_NONE)

        secondary = settings.get("secondary", DEFAULT_SECONDARY)
        bottom_text = ""
        if secondary == "cost" and usage and usage.get("costUsdTicks") is not None:
            # costUsdTicks' unit isn't documented anywhere - this assumes
            # nanodollars (1e9 ticks = $1), inferred from the sample value
            # in the GitHub issue that requested this plugin. Flag it if it
            # ever looks obviously wrong for your account.
            bottom_text = f"${usage['costUsdTicks'] / 1_000_000_000:.2f}"
        elif secondary == "tokens" and usage and usage.get("totalTokens") is not None:
            bottom_text = self.tr("grok-usage.label.last-turn-tokens").format(
                tokens=humanize_tokens(int(usage["totalTokens"]))
            )
        else:
            remaining = _seconds_until(billing.get("period_end"))
            if remaining is not None:
                bottom_text = self.tr("grok-usage.label.time-left").format(time=humanize_seconds(remaining))

        self.set_center_label(text=center_text, font_size=center_size, **LABEL_OUTLINE)
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
