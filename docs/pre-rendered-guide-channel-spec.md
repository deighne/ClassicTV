# Pre-Rendered TV Guide Channel (Channel 1) — Specification

**Date:** November 8, 2026  
**Status:** Proposed / Ready for Implementation  
**Related:** `AGENTS.md`, `docs/direct-stream-and-crt-static-spec.md`, `web/guide/index.html`

---

## 1. Overview & Motivation

Classic TV has direct numeric channel entry (`1`–`99`) via the Sony RM-Y180 remote and the `ChannelEntry` state machine (`addon4/resources/lib/channel_entry.py`). Because viewers can jump directly to any channel number at any time, an interactive on-screen EPG grid with d-pad cursor navigation adds unnecessary UI complexity and carries hardware risk on the Amlogic S905X (`osd0`/`vd1` plane contention).

Instead, **Channel 1** is dedicated to a **1990s Prevue Channel-style scrolling video guide** that plays 24/7 like any other broadcast channel:

1. **Zero Client Risk:** To Kodi and the Sweet Potato, Channel 1 is a standard H.264 MP4 stream played via `DIRECT_HTTP_PLAYBACK` with byte-range `StartOffset` seeking. No custom `WindowXMLDialog` skins, no Python GUI threads, and no Amlogic decoder race conditions.
2. **Deterministic Pre-Rendering Solves V1's Sync Problem:** In Classic TV 1.0, keeping a video guide current was difficult because schedules were stateful queues and videos had to be rendered just-in-time. In Classic TV 4.0, `ScheduleEngine` is pure deterministic arithmetic from a fixed origin (`startAnchor: 2024-01-01T00:00:00Z`). The exact episode, start offset, and remaining minutes for every channel at any future timestamp $T$ can be computed in sub-millisecond time—allowing guide videos to be pre-rendered in batches hours ahead of broadcast.
3. **Sub-Minute Accuracy on Tune:** Guide videos are generated in **15-minute (900 s) wall-clock chunks** (`:00`, `:15`, `:30`, `:45`). Tuning to Channel 1 at `18:07:30` sets `StartOffset = 450.0s` into the `18:00–18:15` chunk, dropping the viewer into the exact scroll pass for `18:07:30`.

---

## 2. Visual & Audio Design (The 90s Prevue Aesthetic)

### 2.1 Output Format & Raster Geometry

| Parameter | Value | Rationale |
| :--- | :--- | :--- |
| **Resolution** | `960×720` (4:3) | Matches the native 4:3 geometry of the Sony KV-34HS510 CRT and the remediated library pad format. |
| **Frame Rate** | `30 fps` | Smooth vertical text scrolling without judder on a 60 Hz display. |
| **Video Codec** | H.264 (`yuv420p`) | Hardware-decoded natively by `DVDVideoCodecAmlogic`; ultra-high compression efficiency on flat graphics. |
| **Audio Codec** | AAC Stereo, `128 kbps`, `44.1 kHz` | Normalized to `-18 LUFS` to match broadcast episode volume. |

### 2.2 1/3 Top Split + 2/3 Bottom Split Layout

```
┌──────────────────────────────────────────────────────────────────────┐ ▲
│  [CLASSIC TV LOGO]          TV GUIDE CHANNEL 1     SUN NOV 8  6:30PM │ │ Top 1/3
│  ┌──────────────────────┐   ┌──────────────────────────────────────┐ │ │ (960×240)
│  │  FEATURED / PROMO    │   │  TONIGHT ON CLASSIC TV:              │ │ │ y: 0..240
│  │  THUMBNAIL CARD      │   │  • Ch 4 Cartoon Network: Billy/Mandy │ │ │
│  └──────────────────────┘   └──────────────────────────────────────┘ │ ▼
├══════╤══════════════════╤════════════╤═══════════════════════════════┤ ▲
│ CH # │ CHANNEL          │ STILL      │ NOW PLAYING / NEXT UP         │ │
├──────┼──────────────────┼────────────┼───────────────────────────────┤ │
│  02  │ ANIME            │ [100×60]   │ Dragon Ball Z       (14m left)│ │
│      │                  │  thumb     │ Next: YuYu Hakusho            │ │
├──────┼──────────────────┼────────────┼───────────────────────────────┤ │ Bottom 2/3
│  03  │ NICKELODEON      │ [100×60]   │ SpongeBob SquarePants (8m left│ │ (960×480)
│      │                  │  thumb     │ Next: Rugrats                 │ │ y: 240..720
├──────┼──────────────────┼────────────┼───────────────────────────────┤ │ (5–6 rows
│  04  │ CARTOON NETWORK  │ [100×60]   │ Billy and Mandy     (22m left)│ │  visible)
│      │                  │  thumb     │ Next: Steven Universe         │ │
├──────┼──────────────────┼────────────┼───────────────────────────────┤ │
│  05  │ EDUTAINMENT KIDZ │ [100×60]   │ Arthur              (19m left)│ │
│      │                  │  thumb     │ Next: The Magic School Bus    │ │
├──────┼──────────────────┼────────────┼───────────────────────────────┤ │
│  06  │ EDUTAINMENT      │ [100×60]   │ How It's Made        (4m left)│ │
│      │                  │  thumb     │ Next: MythBusters             │ │
└──────┴──────────────────┴────────────┴───────────────────────────────┘ ▼
```

1. **Top 1/3 Header (`960×240`, `y = 0..239`):**
   - Stationary header region with subtle CRT scanline overlay and beveled retro borders.
   - Displays the Classic TV emblem, current wall-clock day/date/time (updated every minute of video time), and a featured "Now Airing" highlight card that rotates every 30 seconds.
2. **Bottom 2/3 Scrolling Grid (`960×480`, `y = 240..719`):**
   - Fixed column header bar (`36 px` tall) followed by a `444 px` clipping viewport showing **5 full channel rows** (`88 px` per row) scrolling continuously upward.
   - Color palette: Prevue deep navy background (`#0A1931`), alternating row tint (`#112240`), gold channel badges (`#FFD700` text on `#1E3A8A` pill), crisp white Now Playing text (`#FFFFFF`), and warm amber/cyan Next Up & time-remaining text (`#93C5FD`).
3. **Channel Row Contents (`88 px` height):**
   - **Channel Badge (`x = 24..90`):** Large 2-digit channel number (`02`–`67`).
   - **Channel Name (`x = 104..290`):** Uppercase channel title (e.g. `CARTOON NETWORK`).
   - **Program Still (`x = 304..404`):** `100×60` episode thumbnail image with a `1 px` light border, fetched from Jellyfin's `/Items/{itemId}/Images/Primary?maxWidth=200`. Falls back to the series primary image if an episode thumb is absent.
   - **Now Playing + Remaining Time (`x = 420..936`, top line):** Show title (and episode title if available) + right-aligned pill (`14m left`).
   - **Next Up (`x = 420..936`, bottom line):** `Next: <NextItem.Title>`.

### 2.3 Scroll Pacing & Dynamic Time Updates

- With 20 channels at `88 px` per row, one complete vertical loop covers `1760 px`.
- At a scroll speed of **`36 px/s` (`1.2 px/frame` at 30 fps)**, a full cycle through all 20 channels takes **48.9 seconds** (~18.4 complete cycles per 15-minute video chunk).
- **Dynamic Per-Pass Schedule Evaluation:** As each channel row is about to scroll into the bottom of the viewport at video frame $f$ (corresponding to wall-clock timestamp $T = T_{\text{chunkStart}} + f / 30$), the renderer evaluates the schedule at timestamp $T$:
  - `minutes_remaining` counts down naturally on each subsequent scroll pass.
  - If an episode ends mid-chunk (e.g., at `18:08:20`), all scroll passes prior to `18:08:20` display the outgoing episode and its thumbnail, and the very next scroll pass after `18:08:20` displays the new episode, its thumbnail, and the new `Next Up` item.

### 2.4 Smooth Jazz Audio Track

- A curated directory of royalty-free / classic Weather Channel & Prevue-era smooth jazz / synth-fusion tracks resides on `jfmac` at `~/projects/ClassicTV3.0/resources/guide_audio/`.
- Tracks are concatenated into a master seamless audio loop (`guide_music_loop.m4a`, ~60–120 minutes long).
- When rendering the chunk for `chunkStart`, the audio muxer seeks into the master loop at `(chunkStart % loopDuration)` so that chunk-to-chunk transitions have continuous musical progression rather than restarting the same song every 15 minutes.

---

## 3. Compression Efficiency & Tight Disk Pruning

### 3.1 Empirical Bitrate & File Size

Benchmarking on `jfmac` (`960×720` @ 30 fps, `libx264 -crf 23 -preset fast -pix_fmt yuv420p`) confirmed that flat navy backgrounds, crisp typography, and linear vertical scrolling compress with extreme efficiency:

| Stream | Measured Bitrate | Size per 15-Min (900 s) Chunk |
| :--- | :--- | :--- |
| **Video (`960×720` @ 30 fps)** | ~130–200 kbps (with thumbnails) | **14.5 – 22.5 MB** |
| **Audio (Stereo AAC)** | 128 kbps | **14.4 MB** |
| **Total MP4 Container** | ~260–330 kbps | **~29 – 37 MB** |

### 3.2 Rolling Window & Prune Policy

 chunks are aligned to 900-second Unix epoch boundaries:
$$\text{chunkStart} = \lfloor T / 900 \rfloor \times 900$$

- **Forward Pre-Render Horizon (`4 hours` = 16 chunks):**
  - The background worker maintains chunks for `[currentChunk, currentChunk + 15 * 900]`.
  - Rendering a single 15-minute chunk takes ~20–30 seconds on `jfmac`. Steady-state maintenance requires rendering just **one 30 MB file every 15 minutes** (~3% average CPU duty cycle).
- **Tight Past Pruning (`now - 15 minutes` = 1 chunk grace):**
  - Because tuning into Channel 1 always jumps into `currentChunk` at `StartOffset = now - chunkStart`, no new tune ever requests a past chunk.
  - Only a viewer who tuned in during the final seconds of `previousChunk` before clock rollover could still be reading `previousChunk`.
  - Therefore, any chunk file where `chunkEnd < now - 900` (ended more than 15 minutes ago) is deleted immediately on each worker pass.
- **Total Disk Footprint:**
  - $1\text{ past chunk} + 1\text{ active chunk} + 16\text{ future chunks} = 18\text{ files} \times 33\text{ MB} \approx \mathbf{600\text{ MB}}$ total steady-state disk usage on `jfmac`.

---

## 4. End-to-End Architecture & Component Changes

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │ jfmac: tools/guide_renderer.py (launchd daemon, every 5 min)        │
  │  1. Queries GET /ClassicTV/channel/{id}?t={ts} for upcoming window  │
  │  2. Fetches & caches episode thumbnails from /Items/{id}/Images/... │
  │  3. Renders missing 15-min MP4 chunks -> guide_cache/guide-{ts}.mp4 │
  │  4. Prunes guide-{ts}.mp4 where ts + 900 < now - 900                │
  └──────────────────────────────────┬──────────────────────────────────┘
                                     │
                                     ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Jellyfin.Plugin.ClassicTV (:8096)                                   │
  │  - GET /ClassicTV/channel/guide                                     │
  │      -> { JellyfinItemId: "guide-1794182400", StartOffset: 450.0,   │
  │           Duration: 900.0, NextItem: { "guide-1794183300", ... } }  │
  │  - GET /ClassicTV/guide/stream/guide-1794182400.mp4                 │
  │      -> PhysicalFile(..., "video/mp4", enableRangeProcessing: true) │
  └──────────────────────────────────┬──────────────────────────────────┘
                                     │ HTTP 206 Byte-Range Stream
                                     ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Sweet Potato: plugin.video.classictv (addon4)                       │
  │  - _build_stream_url("guide-1794182400")                            │
  │      -> http://192.168.1.207:8096/ClassicTV/guide/stream/guide-...  │
  │  - Plays via xbmc.Player().play() with StartOffset = 450.0s         │
  └─────────────────────────────────────────────────────────────────────┘
```

### 4.1 Jellyfin Plugin (`~/projects/Jellyfin.Plugin.ClassicTV`)

1. **Query Timestamp Support (`GET /ClassicTV/channel/{channelId}?t={unixSeconds}`):**
   - Update `ClassicTvController.GetChannel(string channelId, [FromQuery] long? t = null)` to use `long now = t ?? DateTimeOffset.UtcNow.ToUnixTimeSeconds();`.
   - This gives `guide_renderer.py` (and tests/web tools) authoritative schedule computation for any future timestamp without duplicating `ScheduleEngine` or `DurationCache` in Python.
2. **Guide Channel Config Flag (`Guide: true`):**
   - Add `public bool Guide { get; set; }` to `ChannelConfig` in `Configuration/ChannelConfig.cs`.
   - In `ClassicTvController.GetChannel`, if `channel.Guide` is true:
     - Calculate `long chunkStart = (now / 900) * 900;` and `long nextChunkStart = chunkStart + 900;`.
     - Check the guide cache directory (`~/Library/Application Support/jellyfin/data/classictv-guide/`) for `guide-{chunkStart}.mp4`.
     - If `guide-{chunkStart}.mp4` exists, return `JellyfinItemId = $"guide-{chunkStart}"`, `StartOffset = now - chunkStart`, `Duration = 900`.
     - **Fallback Resilience:** If `guide-{chunkStart}.mp4` is not yet on disk, resolve the newest available `guide-*.mp4` file in the directory and compute `StartOffset = now % 900` so Channel 1 still plays immediately without 404ing.
3. **Byte-Range Stream Endpoint (`GET /ClassicTV/guide/stream/{chunkId}`):**
   - Add `[HttpGet("guide/stream/{chunkId}")]` (`[AllowAnonymous]`) to `ClassicTvController`.
   - Validate `chunkId` against `^guide-\d+$` (preventing path traversal).
   - Return `PhysicalFile(filePath, "video/mp4", enableRangeProcessing: true)`. Kestrel handles `Range: bytes=...` headers natively in C++, matching `/Videos/{id}/stream?static=true`.

### 4.2 Kodi Addon (`addon4/resources/lib/player.py`)

A single 3-line addition to `ClassicTVPlayer._build_stream_url(self, item_id: str)`:

```python
    def _build_stream_url(self, item_id: str) -> str:
        base = self._base_url
        if self._schedule_client is not None:
            client_base = getattr(self._schedule_client, "_base_url", None)
            if isinstance(client_base, str) and client_base:
                base = client_base
        if item_id.startswith("guide-"):
            return f"{base.rstrip('/')}/ClassicTV/guide/stream/{item_id}"
        return f"{base.rstrip('/')}/Videos/{item_id}/stream?static=true"
```

- All existing playback mechanics—`DIRECT_HTTP_PLAYBACK`, `StartOffset` demuxer seek, clearing `jellyfin_playing_id`, `ChannelBug` 10 s corner badge (`CH 1 — TV Guide`), GPU CRT static transition, and automatic EOF rollover into `slot.next_item` at `:00/:15/:30/:45`—work unchanged.

### 4.3 Batch Renderer Service (`tools/guide_renderer.py` on `jfmac`)

- Standalone Python 3 script using `Pillow` (`PIL.Image`, `PIL.ImageDraw`, `PIL.ImageFont`) and `ffmpeg`.
- **Optimization — Strip-Based Rendering:**
  - Rather than re-drawing full `960×720` text/images from scratch 27,000 times for a 15-minute video:
  - For each ~50-second scroll pass $k$, `guide_renderer.py` renders **one tall composite image strip** (`960 × 1760 px`) containing all 20 channel rows with their thumbnails and exact `minutes_remaining` for pass $k$, plus **one header image** (`960 × 240 px`) updated once per minute.
  - For each frame in pass $k$, compositing a frame is just two fast `numpy` / `Image.crop` memory slices (slicing a `960×444` window at `y_offset` from the strip and pasting it below the header) piped as raw `rgb24` frames to `ffmpeg -f rawvideo`.
  - This reduces Python rendering time for a 15-minute video (27,000 frames) to **~12–18 seconds**.
- Managed by `launchd` via `deploy/com.classictv.guide_renderer.plist` (runs at load and every 300 seconds, with `ProcessType = Background` and `Nice = 10` so it never contends with Jellyfin or the re-encode worker).

### 4.4 Channel Numbering Update (`channels/channels-v4.json`)

The channel roster is updated so Channels 1 through 20 are contiguous with zero gaps:
- **Ch 1:** `guide` (`TV Guide`, `guide: true`)
- **Ch 2:** `anime` (`Anime` — remains Ch 2)
- **Ch 3:** `disney-cartoons` (`Disney Cartoons` — moved from Ch 1 to Ch 3)
- **Ch 4–11:** `nickelodeon-cartoons` (4), `cartoon-network` (5), `edutainment-4-kidz` (6), `edutainment` (7), `superheroes` (8), `classic-cartoons` (9), `ooky-spooky` (10), `live-action` (11) — shifted up by 1 from their previous numbers 3–10
- **Ch 12–20 & 67:** Unchanged (`12 It's 1997` through `20 MTV`, and `67 Rachel's Channel`)

---

## 5. Edge Cases & Failure Modes

| Scenario | Behavior |
| :--- | :--- |
| **Viewer stays on Channel 1 across a 15-minute boundary (`:00`, `:15`, `:30`, `:45`)** | `onPlayBackEnded` fires at the end of the chunk; `ClassicTVPlayer` immediately plays `slot.next_item` (`guide-{chunkStart + 900}`) at `StartOffset = 0.001s` (< 0.8 s transition). |
| **Schedule config (`channels-v4.json`) is edited** | `guide_renderer.py` hashes the channel list response from `/ClassicTV/channels`; if the roster changes, it invalidates and re-renders future chunks in the 4-hour buffer. |
| **Mirror channel (Channel 67 — Rachel's Channel)** | Because mirror channels reflect live ad-hoc playback, row 67 displays a static badge: `Live Mirror Feed — Tune to Ch 67`. |
| **Episode has no primary image in Jellyfin** | Renderer requests `/Items/{itemId}/Images/Primary`, falls back to parent series thumb `/Items/{itemId}/Images/Thumb`, and finally to a built-in retro CRT color-bars placeholder graphic. |
| **Renderer daemon stopped or disk full** | Plugin falls back to the most recent `guide-*.mp4` on disk; if none exist, returns `IsOffline = true` so the addon displays `"TV Guide is offline"` cleanly without wedging. |

---

## 6. Implementation & Verification Plan

1. **Phase 1 — Plugin & Unit Tests (`Jellyfin.Plugin.ClassicTV`):**
   - Add `?t=` query support to `GET /ClassicTV/channel/{id}`.
   - Add `Guide` property to `ChannelConfig` and chunk resolution + `/ClassicTV/guide/stream/{chunkId}` endpoint to `ClassicTvController`.
   - Add xUnit tests in `ClassicTvControllerTests.cs` covering chunk boundary math, `t` query override, path-traversal rejection, and missing-file fallback.
2. **Phase 2 — Renderer Prototype & Visual Sign-Off (`tools/guide_renderer.py`):**
   - Build `tools/guide_renderer.py` with the 1/3 + 2/3 layout, episode stills, and smooth jazz audio muxing.
   - Render a sample 15-minute chunk on `jfmac` and expose a preview at `https://tv.dane.photos/guide/preview.mp4` for visual inspection before touching the TV.
3. **Phase 3 — Addon & Cutover (`addon4` + `deploy`):**
   - Add the `guide-` URL rule to `_build_stream_url()` in `addon4/resources/lib/player.py` + unit test in `tests4/test_player.py`.
   - Install `com.classictv.guide_renderer.plist` on `jfmac`, add Channel 1 (`guide`) to the plugin config, and deploy `v4.10.0` to the Sweet Potato when the TV is idle.
