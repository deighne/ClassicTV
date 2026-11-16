# Classic TV

A personal, wall-clock-synchronized broadcast television simulator powered by **Jellyfin**.

Themed channels play 24/7 in continuous rotation so tuning in drops you mid-episode just like real cable or OTA television — across a CRT driven by Kodi, a Roku TV, iOS/tvOS, or any web browser.

---

## Architecture

Classic TV 4.0 splits scheduling from playback:

- **Schedule Engine (Jellyfin Plugin)** — [`Jellyfin.Plugin.ClassicTV`](https://github.com/deighne/Jellyfin.Plugin.ClassicTV) runs inside your Jellyfin server, exposes `/ClassicTV/channels` and `/ClassicTV/channel/{id}`, precomputes schedules, and caches episode durations from your Jellyfin library.
- **Web Interface (`web/`)** — Static dashboard (`/`), live browser player (`/watch/`), program guide (`/guide/`), channel lineup (`/channels/`), channel editor (`/manage/`), system status (`/status/`), and CRT calibration patterns (`/crt/`).
- **Kodi / CoreELEC Addon (`addon4/`)** — Thin remote + playback client (`plugin.video.classictv`) for dedicated CRT/SBC setups (e.g. Le Potato + Sony Trinitron). Supports channel up/down, digit entry, channel-bug overlay, browse mode, and boot-resilience.
- **Roku Channel (`roku/`)** — BrightScript/SceneGraph app with live channel bar, Mini Guide, direct/HLS stream fallback, and Quick Connect or password auth.
- **Apple Apps (`ios/`, `appletv/`)** — Native SwiftUI clients for iOS and tvOS.
- **Operations & Media Tools (`tools/`)** — Pre-rendered Channel 1 video guide (`guide_renderer.py`), auth proxy (`tv_auth.py`), status monitor (`classictv-status.py`), log aggregator (`classictv-logs.py`), and channel setup helpers.

---

## Prerequisites

- **Jellyfin Server** with the `Jellyfin.Plugin.ClassicTV` plugin installed.
- **Optional reverse proxy** (e.g. Caddy) if serving the web UI over HTTPS or protecting `/manage/` and `/status/` with [`tools/tv_auth.py`](tools/tv_auth.py).
- **Optional clients:**
  - CoreELEC / Kodi device with SSH enabled (for `addon4/`)
  - Roku device with Developer Mode enabled (for `roku/`)
  - Xcode (for `ios/` and `appletv/`)
  - Python 3.10+ and Node.js 18+ (for `tools/` and tests)

---

## Installation

### 1. Clone and configure environment

```bash
git clone https://github.com/deighne/ClassicTV.git classic-tv
cd classic-tv

cp .env.example .env
cp web/config.example.js web/config.js
```

Edit `.env` for your hosts and credentials (all values are gitignored):

```dotenv
JFMAC_HOST=192.168.1.10          # Jellyfin server LAN IP
SWEET_POTATO_IP=192.168.1.20     # Kodi/CoreELEC device LAN IP
SERVER_HOST=user@192.168.1.10    # SSH target for server deploys
DEVICE_HOST=root@192.168.1.20    # SSH target for Kodi device deploys
JELLYFIN_BASE_URL=http://192.168.1.10:8096
JELLYFIN_API_KEY=your-jellyfin-api-key
```

Edit `web/config.js` so the web UI knows how to reach your Jellyfin server:

```js
window.CLASSICTV_CONFIG = {
  jellyfinUrl: 'https://jellyfin.example.com',
  lanHosts: ['localhost', '192.168.1.10'],
  deviceHost: '192.168.1.20',
  jellyfinServerHost: '192.168.1.10',
};
```

### 2. Install the Jellyfin Plugin

Install [`Jellyfin.Plugin.ClassicTV`](https://github.com/deighne/Jellyfin.Plugin.ClassicTV) into your Jellyfin server's `plugins/` directory (from [GitHub Releases](https://github.com/deighne/Jellyfin.Plugin.ClassicTV/releases), or via `web/plugins/manifest.json`) and restart Jellyfin. Verify it is live:

```bash
curl -s http://localhost:8096/ClassicTV/channels
```

### 3. Deploy the Web Interface

Serve `web/` behind a reverse proxy so `/ClassicTV/*` proxies to Jellyfin `:8096` and static files serve from `web/`. See [`deploy/Caddyfile.example`](deploy/Caddyfile.example) for a complete Caddy setup, including optional admin route protection via `tools/tv_auth.py`.

To deploy `web/` to a remote host over SSH:

```bash
bash deploy/deploy-jfmac.sh
```

### 4. Deploy Clients (Optional)

- **Kodi / CoreELEC (`addon4/`):**
  ```bash
  bash deploy/deploy-sweet-potato-4.sh
  ```
  Deploys `plugin.video.classictv`, the IR keymap (`addon4/keymaps/keyboard.xml`), and the boot-wait systemd unit (`classictv-wait-for-jellyfin.service`).

- **Roku (`roku/`):**
  Enable Developer Mode on your Roku, then build and sideload:
  ```bash
  bash deploy/deploy-roku.sh <roku-ip> <dev-password>
  ```
  Or pass no arguments to build `dist/classictv-roku.zip` for manual upload at `http://<roku-ip>`.

- **iOS / tvOS (`ios/`, `appletv/`):**
  Open `ios/ClassicTV.xcodeproj` in Xcode, update the bundle identifier / signing team, and build to your device or simulator.

---

## Customization

### Channels, Blocks, and Show Weights

Start from [`channels/channels-v4.example.json`](channels/channels-v4.example.json) or edit channels live in the web UI at `/manage/` (admin login required) or in Jellyfin's Dashboard plugin settings.

Key fields in each channel entry:

| Field | Purpose |
|---|---|
| `id` | Unique slug (e.g. `sitcoms`, `anime`). |
| `name` | Display name shown in the guide and channel bug. |
| `number` | Authoritative channel number (`1`–`999`, `0` = auto). |
| `enabled` | Set `false` to hide a channel without deleting it. |
| `startAnchor` | Fixed ISO-8601 origin (e.g. `2024-01-01T00:00:00Z`) so schedules never reset daily. |
| `blocks` | Time-of-day programming blocks (`startHour`, `endHour`, `shows`). |
| `shows[].weight` | Controls round-robin spacing/order, **not** episode count — every episode plays once per cycle. |

> **Note:** Changing a channel's show list or episode durations changes its total cycle length, which shifts the current playback position.

### Pre-Rendered Guide Channel (Channel 1)

Classic TV supports a 1990s-style scrolling video program guide on Channel 1:

1. Register the guide channel in your config:
   ```bash
   JELLYFIN_API_KEY=... python3 tools/add_guide_channel.py --apply
   ```
2. Render guide chunks with [`tools/guide_renderer.py`](tools/guide_renderer.py) (or schedule it with the included `launchd` plist in `deploy/`).

### Mirror Channels

Create a channel that follows whatever another Jellyfin user is currently watching:

```bash
JELLYFIN_API_KEY=... python3 tools/add_mirror_channel.py --user friend --number 67 --apply
```

### Web Theme, CRT Calibration, and Remote Keymaps

- **Web Theme:** Colors, typography, and layout tokens live in [`web/shared/theme.css`](web/shared/theme.css).
- **Jellyfin Login/CSS Theme:** Optional Jellyfin UI styling lives in [`web/jellyfin-theme/`](web/jellyfin-theme/) and can be applied with `tools/apply_jellyfin_theme.py`.
- **CRT Test Patterns:** Visit `/crt/` for overscan, geometry, convergence, and color-bar calibration patterns.
- **Kodi Remote Keymap:** Customize IR/CEC button mappings in [`addon4/keymaps/keyboard.xml`](addon4/keymaps/keyboard.xml) (channel up/down, digit entry, EPG toggle, browse-mode controls).

---

## Development & Testing

```bash
# Python unit tests (Kodi addon + tools)
python3 -m pytest tests4/ -q

# Headless Chrome web smoke suite (serves web/ locally with mocked APIs)
node tools/smoke_web.mjs
```

---

## License

MIT — see [`LICENSE`](LICENSE).
