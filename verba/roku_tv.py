"""Verba: Roku TV controller & YouTube launcher (Tater tool ``roku_tv``).

Loaded from the ``verba/`` directory by ``verba_loader.py``, which expects a
module-level ``verba`` instance of :class:`ToolVerba`. One Verba = one tool:
this file is the ``roku_tv`` tool for Hydra.

Capabilities
------------
* Controls a Roku TV over its local **External Control Protocol (ECP)** on
  port ``8060``: playback (play/pause/stop), mute/unmute, volume up/down,
  HDMI input switching, power on/off, and home/back navigation.
* Launches a YouTube video from a natural-language search using one of five
  selectable resolvers (see ``youtube_resolver_method``).

Per-satellite (SAT) routing
---------------------------
Roku TVs are configured as **profiles** (name + IP + linked satellite). When a
voice request arrives *from* a satellite, only the TV profile linked to that
satellite is controlled — so "pause the TV" spoken at the great room's
satellite never touches the TV in another room. A profile can also be targeted
explicitly by name via the ``tv`` argument.

Everything lives in this one file because a Tater Verba package is a single
``.py`` module; the spec's three source modules (``roku_client``,
``youtube_resolver``, ``roku_verba_module``) are inlined below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from verba_base import ToolVerba
from verba_result import action_failure, action_success
from verba_settings import get_verba_settings

logger = logging.getLogger("roku_tv")

SETTINGS_CATEGORY = "roku_tv"
SATELLITE_REGISTRY_KEY = "tater:voice:satellites:registry:v1"
YOUTUBE_APP_ID = "837"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5)
_MAX_VOLUME_STEPS = 30
_FALLBACK_MODES = ("default", "strict", "single_or_first")

# Empty spare profile rows rendered beyond the saved profiles so the WebUI's
# "+ Add TV profile" button (which just reveals the next hidden row) can grow
# the list without re-fetching the settings panel.
_SPARE_PROFILE_ROWS = 8

# Voice command -> Roku ECP keypress key. Note: the ECP "Play" key is a
# play/pause *toggle*, so play/pause/stop all map to it (matching the spec).
_COMMAND_KEY_MAP = {
    "play": "Play",
    "pause": "Play",
    "stop": "Play",
    "mute": "VolumeMute",
    "unmute": "VolumeMute",
    "home": "Home",
    "back": "Back",
    "power_on": "PowerOn",
    "power_off": "PowerOff",
}

# Voice command -> user-facing phrase for the summary line.
_COMMAND_PHRASE = {
    "play": "resumed",
    "pause": "paused",
    "stop": "stopped",
    "mute": "muted",
    "unmute": "unmuted",
    "home": "went to the home screen on",
    "back": "went back on",
    "power_on": "turned on",
    "power_off": "turned off",
}


class ResolverMethod(str, Enum):
    YTDLP = "YTDLP"
    SEARCH_PYTHON = "SEARCH_PYTHON"
    YOUTUBE_SEARCH = "YOUTUBE_SEARCH"
    HTML_SCRAPER = "HTML_SCRAPER"
    OFFICIAL_API = "OFFICIAL_API"


class ResolverError(Exception):
    """Raised when the selected resolver cannot run (missing dep / missing key)."""

    def __init__(self, message: str, code: str = "resolver_error"):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Roku ECP client (spec section 1)
# ---------------------------------------------------------------------------
class RokuClient:
    """Low-level async client for the Roku External Control Protocol (port 8060)."""

    def __init__(self, roku_ip: str):
        self.ip = str(roku_ip or "").strip()
        self.base_url = f"http://{self.ip}:8060"

    async def _post(self, path: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> bool:
        url = self.base_url + path
        try:
            cfg = aiohttp.ClientTimeout(total=timeout or 5)
            async with aiohttp.ClientSession(timeout=cfg) as session:
                async with session.post(url, params=params or None, data=b"") as resp:
                    return resp.status == 200
        except Exception as exc:  # noqa: BLE001 - network errors are a normal "unreachable"
            logger.debug("Roku ECP %s failed: %s", url, exc)
            return False

    async def send_keypress(self, key: str) -> bool:
        safe_key = urllib.parse.quote(str(key or ""), safe="")
        return await self._post(f"/keypress/{safe_key}")

    async def launch_app(self, app_id: str, params: Optional[dict] = None) -> bool:
        return await self._post(f"/launch/{app_id}", params=params, timeout=8)

    async def adjust_volume(self, direction: str, steps: int = 1) -> int:
        key = "VolumeUp" if str(direction or "").strip().lower() == "up" else "VolumeDown"
        steps = max(1, min(int(steps or 1), _MAX_VOLUME_STEPS))
        sent = 0
        for _ in range(steps):
            if await self.send_keypress(key):
                sent += 1
        return sent

    async def type_text(self, text: str) -> None:
        for char in str(text or ""):
            await self.send_keypress(f"Lit_{urllib.parse.quote(char, safe='')}")


# ---------------------------------------------------------------------------
# YouTube resolvers (spec section 2)
# ---------------------------------------------------------------------------
class YouTubeResolver:
    """Strategy pattern over the five YouTube search -> video-id backends."""

    def __init__(self, method: ResolverMethod = ResolverMethod.YTDLP, api_key: str = ""):
        self.method = method
        self.api_key = str(api_key or "").strip()

    async def get_video_id(self, query: str) -> Optional[str]:
        query = str(query or "").strip()
        if not query:
            return None
        method = self.method
        if method == ResolverMethod.YTDLP:
            return await asyncio.to_thread(self._resolve_ytdlp, query)
        if method == ResolverMethod.SEARCH_PYTHON:
            return await asyncio.to_thread(self._resolve_search_python, query)
        if method == ResolverMethod.YOUTUBE_SEARCH:
            return await asyncio.to_thread(self._resolve_youtube_search, query)
        if method == ResolverMethod.HTML_SCRAPER:
            return await asyncio.to_thread(self._resolve_html_scraper, query)
        if method == ResolverMethod.OFFICIAL_API:
            return await asyncio.to_thread(self._resolve_official_api, query)
        return None

    # -- individual strategies (sync; run via asyncio.to_thread) -----------
    def _resolve_ytdlp(self, query: str) -> Optional[str]:
        try:
            import yt_dlp  # noqa: WPS433 - optional dependency
        except ImportError as exc:
            raise ResolverError(
                "yt-dlp is not installed, so the YTDLP resolver can't run. "
                "Install yt-dlp or switch the YouTube resolver method to HTML_SCRAPER (no extra packages).",
                code="resolver_missing_dep",
            ) from exc
        opts = {"quiet": True, "extract_flat": True, "skip_download": True, "noplaylist": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False) or {}
                entries = info.get("entries") or []
                if entries:
                    return entries[0].get("id")
        except ResolverError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("YTDLP resolve failed: %s", exc)
        return None

    def _resolve_search_python(self, query: str) -> Optional[str]:
        try:
            from youtubesearchpython import VideosSearch  # noqa: WPS433
        except ImportError as exc:
            raise ResolverError(
                "youtube-search-python is not installed. Install it or switch the resolver method to HTML_SCRAPER.",
                code="resolver_missing_dep",
            ) from exc
        try:
            res = VideosSearch(query, limit=1).result() or {}
            result = res.get("result") or []
            if result:
                return result[0].get("id")
        except Exception as exc:  # noqa: BLE001
            logger.debug("youtube-search-python resolve failed: %s", exc)
        return None

    def _resolve_youtube_search(self, query: str) -> Optional[str]:
        try:
            from youtube_search import YoutubeSearch  # noqa: WPS433
        except ImportError as exc:
            raise ResolverError(
                "youtube-search is not installed. Install it or switch the resolver method to HTML_SCRAPER.",
                code="resolver_missing_dep",
            ) from exc
        try:
            res = YoutubeSearch(query, max_results=1).to_dict() or []
            return res[0].get("id") if res else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("youtube-search resolve failed: %s", exc)
        return None

    def _resolve_html_scraper(self, query: str) -> Optional[str]:
        import requests  # available in the Tater runtime

        encoded = urllib.parse.quote(query)
        url = f"https://www.youtube.com/results?search_query={encoded}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        }
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
                return ids[0] if ids else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("HTML scraper resolve failed: %s", exc)
        return None

    def _resolve_official_api(self, query: str) -> Optional[str]:
        if not self.api_key:
            raise ResolverError(
                "A YouTube Data API key is required for the OFFICIAL_API resolver method. "
                "Set it in the Roku TV settings, or switch to a keyless method.",
                code="resolver_missing_key",
            )
        import requests  # available in the Tater runtime

        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "relevance",
            "maxResults": 1,
            "key": self.api_key,
        }
        try:
            res = requests.get(url, params=params, timeout=8).json() or {}
            items = res.get("items") or []
            if items:
                return items[0].get("id", {}).get("videoId")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Official API resolve failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Small pure helpers (kept module-level so they are easy to unit-test)
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _effective_name(profile: Dict[str, Any], index: int) -> str:
    name = _text(profile.get("name"))
    return name or f"TV {index + 1}"


def _parse_profiles(raw: Any) -> List[Dict[str, str]]:
    """Parse the stored ``tv_profiles`` value into a clean list of {name, ip, sat}."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(items, list):
        return []
    out: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ip = _text(item.get("ip"))
        if not ip:
            continue
        out.append(
            {
                "name": _text(item.get("name")),
                "ip": ip,
                "sat": _text(item.get("sat")),
            }
        )
    return out


def _match_profile_by_name(profiles: List[Dict[str, str]], wanted: str) -> Optional[Tuple[int, Dict[str, str]]]:
    w = _text(wanted).lower()
    if not w:
        return None
    for idx, profile in enumerate(profiles):  # exact match first
        if _effective_name(profile, idx).lower() == w:
            return idx, profile
    for idx, profile in enumerate(profiles):  # then a fuzzy containment match
        name = _effective_name(profile, idx).lower()
        if w in name or name in w:
            return idx, profile
    return None


def _sat_area(sat: Dict[str, Any]) -> str:
    meta = sat.get("metadata") if isinstance(sat.get("metadata"), dict) else {}
    for key in ("area_name", "room_name", "room", "area"):
        value = _text(meta.get(key))
        if value:
            return value
    return ""


def _sat_label(sat: Dict[str, Any]) -> str:
    selector = _text(sat.get("selector"))
    name = _text(sat.get("name"))
    area = _sat_area(sat)
    parts: List[str] = []
    if name:
        parts.append(name)
    if area:
        parts.append(f"({area})")
    if selector:
        parts.append(f"[{selector}]")
    return " ".join(parts) or selector or "(satellite)"


def _load_satellites(redis_client: Any) -> List[Dict[str, Any]]:
    try:
        raw = redis_client.get(SATELLITE_REGISTRY_KEY)
        if not raw:
            return []
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        out: List[Dict[str, Any]] = []
        seen = set()
        for item in parsed:
            row = item if isinstance(item, dict) else {}
            selector = _text(row.get("selector"))
            if not selector or selector in seen:
                continue
            seen.add(selector)
            out.append(row)
        return out
    except Exception:  # noqa: BLE001 - no satellites is a fine (empty) state
        return []


def _origin_text(origin: Any, keys: Tuple[str, ...]) -> str:
    if not isinstance(origin, dict):
        return ""
    for key in keys:
        value = _text(origin.get(key))
        if value:
            return value
    return ""


def _parse_resolver_method(raw: Any) -> ResolverMethod:
    value = _text(raw).upper()
    try:
        return ResolverMethod(value)
    except ValueError:
        return ResolverMethod.YTDLP


def _parse_steps(raw: Any) -> int:
    try:
        n = int(str(raw or "").strip())
    except (TypeError, ValueError):
        return 5
    return max(1, min(n, _MAX_VOLUME_STEPS))


def _load_settings() -> Dict[str, str]:
    try:
        return get_verba_settings(SETTINGS_CATEGORY) or {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# The Verba
# ---------------------------------------------------------------------------
class RokuTvVerba(ToolVerba):
    name = "roku_tv"
    verba_name = "Roku TV"
    pretty_name = "Roku TV"
    version = "1.0.3"
    usage = '{"function": "roku_tv", "arguments": {"command": "power_on"}}'
    platforms = ["webui", "discord", "voice_core"]
    notifier = False
    verba_dec = (
        "Controls Roku TVs over the network (ECP) and launches YouTube, routing each "
        "request to the TV linked to the speaking satellite."
    )
    description = (
        "Voice control for Roku TVs using the External Control Protocol (port 8060): "
        "playback, mute, volume, HDMI input, power, and navigation, plus a YouTube "
        "deep-linker with five selectable resolvers. Each TV profile can be linked to a "
        "satellite so a spoken request only reaches that TV."
    )
    # NOTE: the planning LLM only sees the first ~77 characters of when_to_use
    # plus the single `usage` example, so the lead sentence must cover the main uses.
    when_to_use = (
        "Turn a Roku TV on or off, play/pause, volume, mute, HDMI input, or YouTube. "
        "Use for 'turn on the TV', 'pause the TV', 'turn the volume up', 'switch to "
        "HDMI 2', 'turn off the bedroom TV', or 'play lofi radio on YouTube'. Set tv "
        "to a configured TV name to target it; otherwise the speaking satellite's "
        "linked TV is used."
    )
    how_to_use = (
        "Pass command (play/pause/stop/mute/unmute/home/back/power_on/power_off/"
        "volume_up/volume_down/switch_input/youtube). For YouTube use command=youtube with "
        "query. volume_up/volume_down take optional steps; switch_input takes input_number. "
        "Optionally set tv to a configured TV name; if omitted the TV is chosen from the "
        "requesting satellite's linked profile or the configured fallback mode. The user may "
        "also call it 'the television', 'the tube', or 'the telly' — all mean this Roku TV."
    )
    common_needs = ["command"]
    example_calls = [
        "Pause the TV",
        "Play the TV",
        "Mute the kitchen TV",
        "Turn the volume up on the great room TV",
        "Switch the living room TV to HDMI 2",
        "Turn off the bedroom TV",
        "Go to the home screen on the great room TV",
        "Mute the television in the kitchen",
        "Turn off the telly",
        "Play lofi hip hop radio on YouTube on the TV",
    ]
    missing_info_prompts = [
        "Which TV do you mean, and what would you like it to do?",
    ]
    settings_category = SETTINGS_CATEGORY
    required_settings = {
        "youtube_resolver_method": {
            "type": "select",
            "label": "YouTube resolver method",
            "description": (
                "How 'play X on YouTube' is turned into a video. YTDLP and the search "
                "libraries need their package installed; HTML_SCRAPER needs no extra packages."
            ),
            "default": "YTDLP",
            "options": [
                {"value": "YTDLP", "label": "YTDLP (yt-dlp, keyless)"},
                {"value": "SEARCH_PYTHON", "label": "youtube-search-python (keyless)"},
                {"value": "YOUTUBE_SEARCH", "label": "youtube-search (keyless)"},
                {"value": "HTML_SCRAPER", "label": "HTML scraper (no extra deps)"},
                {"value": "OFFICIAL_API", "label": "Official YouTube Data API (needs key)"},
            ],
        },
        "youtube_api_key": {
            "type": "password",
            "label": "YouTube Data API key",
            "description": "Only used when the resolver method is OFFICIAL_API.",
            "default": "",
            "show_when": {"key": "youtube_resolver_method", "equals": "OFFICIAL_API"},
        },
    }
    argument_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": [
                    "play", "pause", "stop", "mute", "unmute", "home", "back",
                    "power_on", "power_off", "volume_up", "volume_down",
                    "switch_input", "youtube",
                ],
                "description": (
                    "The action to perform (power_on/power_off turn the TV on/off). For "
                    "'play X on YouTube' use command=youtube with query. volume_up/"
                    "volume_down adjust volume; switch_input changes HDMI input."
                ),
            },
            "query": {
                "type": "string",
                "description": "For command=youtube: what to search on YouTube, in the user's words (e.g. 'lofi hip hop radio').",
            },
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "For volume commands: up or down (usually implied by the command).",
            },
            "steps": {
                "type": "string",
                "description": "For volume commands: how many steps to change by (default 5).",
            },
            "input_number": {
                "type": "string",
                "description": "For switch_input: the HDMI input number to switch to (e.g. '1').",
            },
            "tv": {
                "type": "string",
                "description": (
                    "Optional: which configured TV to target, by its name or room "
                    "(e.g. 'Great Room' or 'kitchen'). Do NOT put the device word here — "
                    "'TV', 'television', 'tube', and 'telly' all just mean the TV. If omitted, "
                    "the TV is chosen from the speaking satellite's linked profile or the "
                    "configured fallback mode."
                ),
            },
        },
        "required": ["command"],
    }

    # -- WebUI settings hooks ------------------------------------------------
    def webui_settings_fields(self, fields, current_settings, redis_client, notifier_destination_catalog=None):
        """Append the 'TV Profiles & SAT Routing' section to the settings panel."""
        fields = list(fields or [])
        current = dict(current_settings or {})
        profiles = _parse_profiles(current.get("tv_profiles"))
        satellites = _load_satellites(redis_client)

        fields.append(
            {
                "key": "section_tv_routing",
                "label": "TV Profiles & SAT Routing",
                "type": "section",
                "description": (
                    "Link each Roku TV profile to a satellite. A voice request from a "
                    "satellite only controls the TV linked to it, so rooms stay independent."
                ),
            }
        )
        fields.append(
            {
                "key": "fallback_mode",
                "label": "When the speaking satellite has no linked TV",
                "type": "select",
                "description": "What to do when a request arrives without a clear TV match.",
                "value": current.get("fallback_mode") or "default",
                "default": "default",
                "options": [
                    {"value": "default", "label": "Use the Default TV, else ask"},
                    {"value": "strict", "label": "Strict: never guess (always ask)"},
                    {"value": "single_or_first", "label": "Use the single / first TV"},
                ],
            }
        )
        default_options = [{"value": "", "label": "(none)"}]
        for idx, profile in enumerate(profiles):
            default_options.append({"value": _effective_name(profile, idx), "label": _effective_name(profile, idx)})
        fields.append(
            {
                "key": "default_tv",
                "label": "Default TV",
                "type": "select",
                "description": "Used by the 'Default TV' fallback mode when no satellite match is found.",
                "value": current.get("default_tv") or "",
                "default": "",
                "options": default_options,
            }
        )

        # Dynamic rows: a hidden counter (`tv_profile_slots`) says how many rows
        # are visible. The WebUI's "+ Add TV profile" button raises it (revealing
        # the next empty row) and each row's Delete button clears that row and
        # shifts the ones below it up. On save, visible rows collapse into the
        # canonical `tv_profiles` JSON value (rows with no IP are dropped).
        slots = max(len(profiles), 1)
        max_rows = slots + _SPARE_PROFILE_ROWS
        fields.append(
            {
                "key": "tv_profile_slots",
                "type": "hidden",
                "label": "TV profile rows",
                "description": "How many TV profile rows are currently shown.",
                "value": slots,
                "default": slots,
            }
        )

        sat_options = [{"value": "", "label": "(unlinked)"}]
        for sat in satellites:
            sat_options.append({"value": _text(sat.get("selector")), "label": _sat_label(sat)})
        for idx in range(max_rows):
            profile = profiles[idx] if idx < len(profiles) else {}
            visible = {"source_key": "tv_profile_slots", "at_least": idx + 1}
            base = f"profile_{idx}_"
            fields.append(
                {
                    "key": base + "name",
                    "label": f"TV {idx + 1} name",
                    "type": "text",
                    "description": "e.g. Great Room (used to name the TV and match rooms).",
                    "value": profile.get("name", ""),
                    "default": "",
                    "show_when": dict(visible),
                }
            )
            fields.append(
                {
                    "key": base + "ip",
                    "label": f"TV {idx + 1} IP",
                    "type": "text",
                    "description": "The Roku's IP address, e.g. 192.168.1.100.",
                    "value": profile.get("ip", ""),
                    "default": "",
                    "show_when": dict(visible),
                }
            )
            fields.append(
                {
                    "key": base + "sat",
                    "label": f"TV {idx + 1} satellite",
                    "type": "select",
                    "description": "The satellite this TV belongs to. Requests from it go only here.",
                    "value": profile.get("sat", ""),
                    "default": "",
                    "options": sat_options,
                    "show_when": dict(visible),
                }
            )
            fields.append(
                {
                    "key": base + "delete",
                    "label": f"Delete TV {idx + 1}",
                    "type": "button",
                    "description": "Removes this row and moves the TVs below it up.",
                    "value": "",
                    "button": {
                        "action": "delete_slot",
                        "slot_key": "tv_profile_slots",
                        "prefix": "profile_",
                        "keys": ["name", "ip", "sat"],
                        "index": idx,
                        "min": 1,
                    },
                    "show_when": dict(visible),
                }
            )
        fields.append(
            {
                "key": "tv_profiles_add",
                "label": "+ Add TV profile",
                "type": "button",
                "description": "Adds an empty TV profile row below the existing ones.",
                "value": "",
                "button": {
                    "action": "increment_slot",
                    "slot_key": "tv_profile_slots",
                    "step": 1,
                    "min": 1,
                },
                # Hidden once the pool of rendered rows is exhausted (re-opening
                # the settings panel renders new spare rows around a larger list).
                "show_when": {
                    "source_key": "tv_profile_slots",
                    "any_of": [str(n) for n in range(1, max_rows)],
                },
            }
        )
        return fields

    def webui_prepare_settings_values(self, values, redis_client=None):
        """Collapse the profile rows into the canonical `tv_profiles` JSON value."""
        values = dict(values or {})
        # Pure view state from the add/delete row UI; never persisted.
        values.pop("tv_profile_slots", None)

        profiles: List[Dict[str, str]] = []
        row_keys = [key for key in values if re.fullmatch(r"profile_\d+_(name|ip|sat|delete)", str(key))]
        indices = sorted({int(str(key).split("_")[1]) for key in row_keys})
        for idx in indices:
            ip = _text(values.get(f"profile_{idx}_ip"))
            if not ip:
                continue
            profiles.append(
                {
                    "name": _text(values.get(f"profile_{idx}_name")),
                    "ip": ip,
                    "sat": _text(values.get(f"profile_{idx}_sat")),
                }
            )
        for key in row_keys:
            values.pop(key, None)

        values["tv_profiles"] = json.dumps(profiles, ensure_ascii=False)

        # Keep default_tv valid: it must reference an existing profile name.
        default_tv = _text(values.get("default_tv"))
        if default_tv and not _match_profile_by_name(profiles, default_tv):
            values["default_tv"] = ""

        fallback_mode = _text(values.get("fallback_mode")).lower()
        if fallback_mode not in _FALLBACK_MODES:
            values["fallback_mode"] = "default"
        else:
            values["fallback_mode"] = fallback_mode
        return values

    # -- platform handlers ---------------------------------------------------
    async def handle_voice_core(self, args, llm_client, origin=None, request_text=""):
        return await self._run(args, request_text, llm_client, origin)

    async def handle_webui(self, args, llm_client, origin=None, request_text=""):
        return await self._run(args, request_text, llm_client, origin)

    async def handle_discord(self, args, llm_client, origin=None, request_text=""):
        return await self._run(args, request_text, llm_client, origin)

    # -- core logic ----------------------------------------------------------
    def _resolve_target(
        self,
        profiles: List[Dict[str, str]],
        origin: Any,
        tv_name: str,
        default_tv: str,
        fallback_mode: str,
    ) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, Any]]]:
        """Pick exactly one TV. Returns (target, None) or (None, failure)."""
        if not profiles:
            return None, action_failure(
                code="no_tv_configured",
                message="No Roku TV profiles are configured.",
                say_hint="Ask the user to add a TV profile (name and IP) in the Roku TV settings.",
            )

        # 1) Explicit TV name wins.
        if tv_name:
            match = _match_profile_by_name(profiles, tv_name)
            if match:
                idx, profile = match
                return {"ip": profile["ip"], "label": _effective_name(profile, idx)}, None
            names = ", ".join(_effective_name(p, i) for i, p in enumerate(profiles))
            return None, action_failure(
                code="unknown_tv",
                message=f"No TV named {tv_name} is configured.",
                say_hint=f"That TV isn't configured. The configured TVs are: {names}. Ask which one to use.",
            )

        # 2) Route by the requesting satellite (SAT), then by its room/area.
        req_sat = _origin_text(origin, ("satellite_selector", "device_id"))
        if req_sat:
            for idx, profile in enumerate(profiles):
                if profile["sat"] and profile["sat"] == req_sat:
                    return {"ip": profile["ip"], "label": _effective_name(profile, idx)}, None
        req_area = _origin_text(origin, ("area_name", "room_name", "room"))
        if req_area:
            match = _match_profile_by_name(profiles, req_area)
            if match:
                idx, profile = match
                return {"ip": profile["ip"], "label": _effective_name(profile, idx)}, None

        # 3) Apply the configured fallback mode.
        if fallback_mode == "single_or_first":
            return {"ip": profiles[0]["ip"], "label": _effective_name(profiles[0], 0)}, None
        if fallback_mode == "default":
            if default_tv:
                match = _match_profile_by_name(profiles, default_tv)
                if match:
                    idx, profile = match
                    return {"ip": profile["ip"], "label": _effective_name(profile, idx)}, None
            if len(profiles) == 1:
                return {"ip": profiles[0]["ip"], "label": _effective_name(profiles[0], 0)}, None
            names = ", ".join(_effective_name(p, i) for i, p in enumerate(profiles))
            return None, action_failure(
                code="no_tv_match",
                message="I couldn't tell which TV you mean.",
                say_hint=f"Ask which TV to use. The configured TVs are: {names}.",
            )
        # strict (and any unknown value) -> never guess
        names = ", ".join(_effective_name(p, i) for i, p in enumerate(profiles))
        return None, action_failure(
            code="no_tv_match",
            message="This satellite has no TV linked to it.",
            say_hint=f"Ask which TV to control by name. The configured TVs are: {names}.",
        )

    async def _run(self, args, request_text="", llm_client=None, origin=None) -> Dict[str, Any]:
        args = args if isinstance(args, dict) else {}
        if not isinstance(origin, dict):
            origin = args.get("origin") if isinstance(args.get("origin"), dict) else {}

        settings = _load_settings()
        profiles = _parse_profiles(settings.get("tv_profiles"))
        fallback_mode = _text(settings.get("fallback_mode")).lower() or "default"
        default_tv = _text(settings.get("default_tv"))
        resolver_method = _parse_resolver_method(settings.get("youtube_resolver_method"))
        api_key = _text(settings.get("youtube_api_key"))

        command = _text(args.get("command")).lower()
        query = _text(args.get("query"))
        direction = _text(args.get("direction")).lower()
        steps_raw = args.get("steps")
        input_number = _text(args.get("input_number"))
        tv_name = _text(args.get("tv"))

        if not command and query:
            command = "youtube"
        if not command:
            return action_failure(
                code="missing_command",
                message="No Roku TV action was specified.",
                say_hint="Ask what to do with the TV (e.g. pause, volume up, switch input, play something on YouTube).",
            )

        target, failure = self._resolve_target(profiles, origin, tv_name, default_tv, fallback_mode)
        if failure is not None:
            return failure
        client = RokuClient(target["ip"])
        label = target["label"]

        if command == "youtube":
            if not query:
                return action_failure(
                    code="missing_query",
                    message="No YouTube search was provided.",
                    say_hint="Ask what to play on YouTube.",
                )
            resolver = YouTubeResolver(method=resolver_method, api_key=api_key)
            try:
                video_id = await resolver.get_video_id(query)
            except ResolverError as exc:
                return action_failure(
                    code=exc.code,
                    message=exc.message,
                    say_hint="Explain the YouTube resolver issue and suggest switching to the HTML scraper method in the Roku TV settings.",
                )
            if not video_id:
                return action_failure(
                    code="no_video",
                    message=f"No YouTube video was found for {query}.",
                    say_hint="Tell the user no video was found and ask them to rephrase the search.",
                )
            if await client.launch_app(YOUTUBE_APP_ID, {"contentId": video_id, "mediaType": "video"}):
                return action_success(
                    facts={"tv": label, "video_id": video_id, "query": query},
                    summary_for_user=f"Playing the top YouTube result for {query} on the {label} TV.",
                    say_hint=f"Say you're playing the top YouTube result for {query} on the {label} TV.",
                )
            return action_failure(
                code="ecp_unreachable",
                message=f"Could not reach the {label} TV at {target['ip']}.",
                say_hint="Tell the user the TV could not be reached on the network.",
            )

        if command in ("volume_up", "volume_down"):
            direction = direction or ("up" if command == "volume_up" else "down")
            steps = _parse_steps(steps_raw)
            sent = await client.adjust_volume(direction, steps)
            if sent:
                return action_success(
                    facts={"tv": label, "direction": direction, "steps": sent},
                    summary_for_user=f"Turned the volume {direction} by {sent} on the {label} TV.",
                    say_hint=f"Say you turned the volume {direction} on the {label} TV.",
                )
            return action_failure(
                code="ecp_unreachable",
                message=f"Could not reach the {label} TV at {target['ip']}.",
                say_hint="Tell the user the TV could not be reached on the network.",
            )

        if command == "switch_input":
            if not input_number:
                return action_failure(
                    code="missing_input",
                    message="No HDMI input number was provided.",
                    say_hint="Ask which HDMI input to switch to.",
                )
            if await client.send_keypress(f"InputHDMI{input_number}"):
                return action_success(
                    facts={"tv": label, "input": input_number},
                    summary_for_user=f"Switched the {label} TV to HDMI {input_number}.",
                    say_hint=f"Say you switched the {label} TV to HDMI {input_number}.",
                )
            return action_failure(
                code="ecp_unreachable",
                message=f"Could not reach the {label} TV at {target['ip']}.",
                say_hint="Tell the user the TV could not be reached on the network.",
            )

        key = _COMMAND_KEY_MAP.get(command)
        if not key:
            return action_failure(
                code="unknown_command",
                message=f"Unknown Roku command: {command}.",
                say_hint="Tell the user that action isn't supported and list what is (play, pause, mute, volume, input, power, home, back).",
            )
        if await client.send_keypress(key):
            phrase = _COMMAND_PHRASE.get(command, command)
            return action_success(
                facts={"tv": label, "command": command},
                summary_for_user=f"{phrase.title()} the {label} TV.",
                say_hint=f"Confirm you {phrase} the {label} TV.",
            )
        return action_failure(
            code="ecp_unreachable",
            message=f"Could not reach the {label} TV at {target['ip']}.",
            say_hint="Tell the user the TV could not be reached on the network.",
        )


verba = RokuTvVerba()
