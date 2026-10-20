# Tater Roku TV — Verba

Voice control for **Roku TVs** over your local network, using the Roku
**External Control Protocol (ECP)** on port `8060`, plus a **YouTube
deep-linker** with five selectable resolvers. The headline feature is
**per-satellite (SAT) routing**: link each TV profile to a satellite so a
request spoken at one satellite only reaches the TV in that room — never the
TVs in the others.

This is a single-file [Tater](https://github.com/TaterTotterson/Tater)
**Verba** (one `.py` module). It is designed to drop straight into Tater's
`verba/` directory or be installed from this repository as a Verba source.

## What it does

- **Playback** — play / pause / stop (Roku's `Play` key is a play-pause toggle)
- **Mute / unmute**
- **Volume** — up or down by N steps
- **HDMI input** — switch to a given HDMI input
- **Power** — on / off
- **Navigation** — home, back
- **YouTube** — "play *X* on YouTube" searches and launches the top result

Example phrases:

- "Pause the TV"
- "Turn the volume up on the great room TV"
- "Mute the kitchen TV"
- "Switch the living room TV to HDMI 2"
- "Turn off the bedroom TV"
- "Mute the television in the kitchen"
- "Turn off the telly"
- "Play lofi hip hop radio on YouTube on the TV"

## Installation

The package is a shop manifest (`manifest.json`) describing one Verba
(`verba/roku_tv.py`). Two ways to install:

**Option A — add this repository as a Verba source (recommended).** In the
Tater WebUI, open **Verbas → Repositories**, add this repo's manifest URL as a
trusted source, then **Install** `roku_tv` from the Store tab. Tater verifies
the file's `sha256` against the manifest and writes it into its `verba/`
directory.

**Option B — drop the file in manually.** Copy `verba/roku_tv.py` into your
Tater installation's `verba/` directory and restart (or reload) Tater. The
file is self-contained; it only imports Tater's own modules plus `aiohttp`
(both already present in the Tater runtime).

Then **enable** the Roku TV verba on the **Verbas → Installed** tab.

## Enabling ECP on the TV

ECP (port 8060) is built into every Roku TV, but it can be switched off. If
the verba reports the TV as unreachable, check this on the **TV itself**:

1. Open **Settings → System → Control by mobile apps**
   (on some firmware: *Settings → System → Advanced system settings →
   Control by mobile apps*).
2. Make sure **Control by mobile apps** / **Network access** is set to
   **Default** — that is the setting that allows full ECP control
   (playback, volume, power, input switching). Choose a less restrictive
   option than *Limited* if your firmware offers that choice.
3. Confirm the TV and the Tater host are on the **same network/VLAN** — guest
   or IoT Wi-Fi networks that isolate clients (AP isolation) will block
   port 8060.

You can verify reachability from the Tater host before configuring anything:

```sh
curl -s "http://<TV_IP>:8060/query/device-info" | head -5
```

If that returns device info XML, ECP is enabled and the verba will work with
that IP.

### Turning the TV *on* remotely (Fast Start)

The ECP `PowerOn` keypress can only wake a TV that is still reachable on the
network while in standby. On the **TV itself**, go to
**Settings → System → Power** and switch **Fast Start** **On**. With Fast
Start off the TV powers down completely (and drops off the network), so
"turn on the TV" will report the TV as unreachable until you switch it on
with the remote. Everything else (playback, volume, inputs) works while the
TV is on, with or without Fast Start.

## Configuration

Open the Roku TV verba's **settings** (from the Verbas page). You'll see two
groups.

### YouTube

| Field | Meaning |
| --- | --- |
| **YouTube resolver method** | How "play *X* on YouTube" is turned into a video (see below). |
| **YouTube Data API key** | Only shown/used when the method is `OFFICIAL_API`. |

### TV Profiles & SAT Routing

This is where the multi-TV routing is set up. The panel starts with **one**
profile row; use the **+ Add TV profile** button to add as many rows as you
have TVs, and each row's **Delete** button removes it (the rows below shift
up, so there are no gaps). The list is saved when you press **Save settings**.

Each **row** is one TV profile:

| Field | Meaning |
| --- | --- |
| **TV *n* name** | A label for the TV, e.g. `Great Room`. Also used to match a spoken room ("the great room TV"). |
| **TV *n* IP** | The Roku's IP address, e.g. `192.168.1.100`. |
| **TV *n* satellite** | The satellite this TV belongs to. Requests from it go only here. Leave `(unlinked)` if you target this TV by name only. |

Plus two routing controls:

| Field | Meaning |
| --- | --- |
| **When the speaking satellite has no linked TV** | The fallback mode (below). |
| **Default TV** | The profile used by the *Default TV* fallback mode. |

### Fallback modes

What happens when a request arrives from a satellite with **no linked TV** (or
from the WebUI / Discord, where there is no satellite at all):

- **Use the Default TV, else ask** — use the *Default TV* profile; if none is
  set but exactly one TV is configured, use that one; otherwise Tater asks
  which TV you mean. *(safest default)*
- **Strict: never guess** — only act on an explicit satellite/room/TV-name
  match; any other request gets a clarifying question and **no TV is touched**.
- **Use the single / first TV** — fall back to the first configured TV when
  there's no match. Simplest, but can hit the wrong TV in multi-TV setups.

## How per-SAT routing works

When you speak to a **satellite** (Sat1, VoicePE, ReSpeaker, Reachy, etc.),
Tater tags the request with the satellite's identity (`device_id` /
`satellite_selector`) and, where known, its **area/room**. When the Roku TV
verba runs, it picks exactly one TV, in this order:

1. **Explicit TV name** — if the request names a TV ("the kitchen TV"), that
   configured profile is used.
2. **Linked satellite** — the profile whose *satellite* matches the requesting
   satellite. This is the core of the feature.
3. **Room/area** — a profile whose name matches the requesting satellite's
   area/room.
4. **Fallback mode** — as configured above.

So with "Great Room" linked to the great room's satellite, "pause the TV"
spoken there pauses **only** the great room TV. The same phrase at the
kitchen's satellite pauses the kitchen TV.

> **Tip:** give each satellite a distinct *area/room* in Tater and name your TV
> profiles to match — that makes room-based phrasing ("the great room TV") work
> even for TVs you left unlinked.

## YouTube resolvers

| Method | Notes |
| --- | --- |
| `YTDLP` (default) | Keyless, uses `yt-dlp`. Most reliable; requires `yt-dlp` installed. |
| `SEARCH_PYTHON` | Keyless; requires `youtube-search-python`. |
| `YOUTUBE_SEARCH` | Keyless; requires `youtube-search`. |
| `HTML_SCRAPER` | Keyless, **zero extra dependencies** — scrapes YouTube's results page. Good fallback if you don't want to install anything. |
| `OFFICIAL_API` | Uses the YouTube Data API v3; requires a **YouTube Data API key**. |

Resolvers are imported lazily, so the verba loads even if a resolver's package
isn't installed — it only reports a clear error if you *select* a resolver whose
dependency is missing (and suggests `HTML_SCRAPER`).

## Dependencies

- **Runtime (already in Tater):** `aiohttp`, and Tater's own modules.
- **Optional, per resolver:** `yt-dlp`, `youtube-search-python`, `youtube-search`.
  `HTML_SCRAPER` and `OFFICIAL_API` need nothing beyond the HTTP client.

Install whatever you use in the same environment Tater runs in, e.g.
`pip install yt-dlp`.

## How it works (under the hood)

- **ECP client** — `POST http://<ip>:8060/keypress/<key>` for keys,
  `POST http://<ip>:8060/launch/837?contentId=<videoId>&mediaType=video` to
  launch YouTube.
- **Intent routing** — the planning model passes the user's complete request
  in `query` (natural-language contract); the verba works out the action,
  volume steps, HDMI input, and TV target, then maps it to an ECP action on
  the resolved TV.
- **Settings** — stored in Tater's Redis under `verba_settings:roku_tv`; the
  TV profiles are kept as a compact JSON list. The satellite dropdown is built
  from Tater's satellite registry (`tater:voice:satellites:registry:v1`).

## Repository layout

This public repository intentionally contains only:

- `manifest.json` — the Verba shop manifest (id `roku_tv`, `sha256`-verified).
- `verba/roku_tv.py` — the Verba.
- `README.md` — this file.
- `.gitignore` — keeps the repo limited to the four files above.
