# Classic TV — Samsung Tizen TV App

10-foot HTML5/JS/CSS client for Samsung Tizen Smart TVs (`tv-samsung` 5.0+, 1920×1080).

## Features
- **Jellyfin Authentication & `PlaybackInfo`**: DirectPlay (`mp4`/`mkv`/`ts` H.264/HEVC) with HLS fallback and `StartOffset` seeking.
- **CRT Tuning Static**: 4-frame `640×360` procedural snow + scanlines during channel switches.
- **Sony Trinitron Channel Bug**: Phosphor-green OSD showing channel number, name, and current program/episode title.
- **Channel Guide Drawer**: Live "Now Playing" list across all channels, navigable with D-pad.
- **Mirror Channels**: 5 s polling with pause/play sync and >4 s drift correction.

## Samsung Remote Controls
- **Up / Down / Channel Up / Channel Down**: Step channels
- **0–9**: Direct channel number tune (1.5 s timeout)
- **Left / Guide**: Open Channel Guide drawer
- **Right / Return** (in drawer): Close Channel Guide drawer
- **OK / Info**: Show Channel Bug OSD
- **CC / Subtitle**: Toggle closed captions
- **Play / Pause**: Mute / unmute audio
- **Red**: Sign out
- **Return** (twice on live TV): Exit app

## Building & Sideloading
1. Open **Tizen Studio** → **File → Open Projects from File System…** → select `tizen/`.
2. Create a **Samsung** author + distributor certificate in **Certificate Manager** (include your TV's DUID).
3. Put your Samsung TV into **Developer Mode** (Apps → `12345` → Developer Mode ON + your Mac's IP) and connect via **Device Manager**.
4. Right-click the project → **Run As → Tizen Web Application** (or run `bash deploy/deploy-tizen.sh <TV_IP>` if `tizen` and `sdb` are on your `PATH`).
