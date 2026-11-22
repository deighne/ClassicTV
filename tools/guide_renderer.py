#!/usr/bin/env python3
"""
tools/guide_renderer.py — Pre-rendered 90s Prevue-style TV Guide video generator (Channel 1).

Generates 15-minute (900s) 960x720 @ 30fps H.264 MP4 chunks aligned to wall-clock
900-second boundaries, featuring:
  - Top 1/3 (960x240): Station ID, live date/time clock, rotating featured show card
    with program still, and channel highlights.
  - Bottom 2/3 (960x480): Smoothly scrolling channel grid (5 rows visible at once)
    with channel number badge, channel name, 100x60 episode still from Jellyfin,
    Now Playing title, Next Up title, progress bar, and minutes remaining.
  - Audio: Smooth jazz loop (from resources/guide_audio/ if present, or a built-in
    warm Rhodes/bass/brushes smooth-jazz synthesizer).
  - Tight Prune: Keeps [now - 15m, now + horizon] on disk (~600 MB steady state).
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("guide_renderer")

WIDTH = 1280
HEIGHT = 720
FPS = 30
CHUNK_SECONDS = 900  # 15 minutes
HEADER_HEIGHT = 240  # Top 1/3
COL_HEADER_HEIGHT = 36
VIEWPORT_Y = HEADER_HEIGHT + COL_HEADER_HEIGHT  # 276
VIEWPORT_HEIGHT = HEIGHT - VIEWPORT_Y  # 444
ROW_HEIGHT = 88  # ~5.04 rows visible in 444px
SCROLL_PX_PER_FRAME = 1.2  # 36 px/s -> ~48.8s per 20-channel loop
CHICAGO_TZ = ZoneInfo("America/Chicago")

CHUNK_FILE_RE = re.compile(r"^guide-(\d+)\.mp4$", re.IGNORECASE)
SIG_FILE_RE = re.compile(r"^guide-(\d+)\.sig$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Font & visual helpers
# ---------------------------------------------------------------------------

def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _format_program_title(title: str, episode_title: str) -> str:
    """Combine show title and episode title without duplicating identical names."""
    ep = (episode_title or "").strip()
    if ep and ep.lower() != (title or "").strip().lower():
        return f"{title} — {ep}"
    return title


def _truncate_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if not text:
        return ""
    bbox = draw.textbbox((0, 0), text, font=font)
    if (bbox[2] - bbox[0]) <= max_width:
        return text
    ellipsis = "..."
    for i in range(len(text) - 1, 0, -1):
        candidate = text[:i].rstrip() + ellipsis
        b = draw.textbbox((0, 0), candidate, font=font)
        if (b[2] - b[0]) <= max_width:
            return candidate
    return ellipsis


# ---------------------------------------------------------------------------
# Schedule & Thumbnail Fetcher
# ---------------------------------------------------------------------------

class JellyfinGuideDataClient:
    """Fetches channel list, future schedule slots, and episode stills from Jellyfin."""

    def __init__(self, base_url: str, thumb_cache_dir: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.thumb_cache_dir = thumb_cache_dir
        self.thumb_cache_dir.mkdir(parents=True, exist_ok=True)
        self._mem_thumbs: dict[str, Image.Image] = {}

    def get_channels(self) -> list[dict]:
        url = f"{self.base_url}/ClassicTV/channels"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        channels = data.get("Channels", [])
        # Exclude the guide channel itself from the scrolling listings if desired,
        # or keep all content channels sorted by Number.
        filtered = [c for c in channels if c.get("Id") != "guide"]
        filtered.sort(key=lambda c: c.get("Number") or 9999)
        return filtered

    def get_slot(self, channel_id: str, unix_t: int) -> dict:
        url = f"{self.base_url}/ClassicTV/channel/{urllib.parse.quote(channel_id)}?t={unix_t}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("Failed to fetch slot for %s at t=%d: %s", channel_id, unix_t, exc)
            return {"ChannelId": channel_id, "Title": "Offline", "Duration": 0, "StartOffset": 0}

    def get_thumbnail(self, item_id: str, channel_name: str, size: tuple[int, int] = (100, 60)) -> Image.Image:
        cache_key = f"{item_id or channel_name}_{size[0]}x{size[1]}"
        if cache_key in self._mem_thumbs:
            return self._mem_thumbs[cache_key]

        if item_id and not item_id.startswith("guide-"):
            disk_path = self.thumb_cache_dir / f"{item_id}.jpg"
            raw_bytes: bytes | None = None
            if disk_path.exists():
                try:
                    raw_bytes = disk_path.read_bytes()
                except Exception:
                    raw_bytes = None
            else:
                for img_type in ("Primary", "Thumb", "Backdrop"):
                    url = f"{self.base_url}/Items/{urllib.parse.quote(item_id)}/Images/{img_type}?maxWidth=320&quality=85"
                    try:
                        req = urllib.request.Request(url)
                        with urllib.request.urlopen(req, timeout=3) as resp:
                            if resp.status == 200:
                                raw_bytes = resp.read()
                                disk_path.write_bytes(raw_bytes)
                                break
                    except Exception:
                        continue

            if raw_bytes:
                try:
                    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
                    img = self._center_crop_resize(img, size)
                    self._mem_thumbs[cache_key] = img
                    return img
                except Exception:
                    pass

        placeholder = self._make_placeholder_thumb(channel_name, size)
        self._mem_thumbs[cache_key] = placeholder
        return placeholder

    @staticmethod
    def _center_crop_resize(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
        tw, th = target_size
        iw, ih = img.size
        if iw <= 0 or ih <= 0:
            return Image.new("RGB", target_size, (20, 40, 80))
        target_ratio = tw / th
        src_ratio = iw / ih
        if src_ratio > target_ratio:
            new_w = int(ih * target_ratio)
            left = (iw - new_w) // 2
            img = img.crop((left, 0, left + new_w, ih))
        else:
            new_h = int(iw / target_ratio)
            top = (ih - new_h) // 2
            img = img.crop((0, top, iw, top + new_h))
        return img.resize(target_size, Image.Resampling.LANCZOS)

    @staticmethod
    def _make_placeholder_thumb(label: str, size: tuple[int, int]) -> Image.Image:
        tw, th = size
        img = Image.new("RGB", size, (18, 38, 76))
        draw = ImageDraw.Draw(img)
        # Retro SMPTE-inspired subtle color bars at bottom of placeholder
        bar_colors = [
            (192, 192, 192),
            (192, 192, 0),
            (0, 192, 192),
            (0, 192, 0),
            (192, 0, 192),
            (192, 0, 0),
            (0, 0, 192),
        ]
        bw = max(1, tw // len(bar_colors))
        for i, col in enumerate(bar_colors):
            draw.rectangle([i * bw, th - 10, (i + 1) * bw, th], fill=col)
        font = _load_font(max(10, th // 5), bold=True)
        initials = "".join(w[0].upper() for w in (label or "TV").split()[:3])
        bbox = draw.textbbox((0, 0), initials, font=font)
        tx = (tw - (bbox[2] - bbox[0])) // 2
        ty = (th - 10 - (bbox[3] - bbox[1])) // 2
        draw.text((tx, ty), initials, fill=(240, 230, 140), font=font)
        return img


# ---------------------------------------------------------------------------
# Smooth Jazz Audio Generator / Resolver
# ---------------------------------------------------------------------------

def ensure_smooth_jazz_wav(audio_dir: Path, duration_sec: int = 900) -> Path:
    """Return path to a smooth-jazz audio file of at least `duration_sec`.

    If external MP3/M4A/WAV files exist in `audio_dir`, uses the first one found.
    Otherwise synthesizes a warm stereo Rhodes electric piano + fretless bass +
    brushed hi-hat smooth-jazz loop (`synthesized_smooth_jazz.wav`).
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("*.mp3", "*.m4a", "*.wav", "*.flac", "*.ogg"):
        matches = sorted(audio_dir.glob(ext))
        if matches:
            return matches[0]

    synth_path = audio_dir / "synthesized_smooth_jazz_60s.wav"
    if synth_path.exists() and synth_path.stat().st_size > 100_000:
        return synth_path

    logger.info("Synthesizing 64-second stereo Rhodes smooth-jazz loop at %s...", synth_path)
    sample_rate = 22050
    loop_seconds = 64  # 16 bars at 60 BPM (4 seconds per bar)
    total_samples = sample_rate * loop_seconds

    # Warm smooth-jazz chord progression (Fmaj9 -> Em7 -> Dm9 -> Cmaj9 / G13sus4)
    chords = [
        # Bar 1: Fmaj9 (F2, F3, A3, C4, E4, G4)
        (87.31, [174.61, 220.00, 261.63, 329.63, 392.00], [523.25, 659.25, 587.33, 523.25]),
        # Bar 2: Em7 (E2, G3, B3, D4, G4)
        (82.41, [164.81, 196.00, 246.94, 293.66, 392.00], [493.88, 587.33, 523.25, 392.00]),
        # Bar 3: Dm9 (D2, F3, A3, C4, E4)
        (73.42, [146.83, 174.61, 220.00, 261.63, 329.63], [440.00, 523.25, 659.25, 587.33]),
        # Bar 4: G13sus4 -> Cmaj9 (G2, F3, A3, B3, E4)
        (98.00, [174.61, 220.00, 246.94, 329.63, 392.00], [587.33, 523.25, 440.00, 392.00]),
    ]

    buf = bytearray(total_samples * 4)  # 16-bit stereo
    two_pi = 2.0 * math.pi

    for i in range(total_samples):
        t = i / sample_rate
        bar_idx = int(t // 4.0) % len(chords)
        bar_t = t % 4.0
        beat_idx = int(bar_t)  # 0, 1, 2, 3
        beat_t = bar_t - beat_idx

        bass_freq, pad_freqs, melody_freqs = chords[bar_idx]

        # 1. Fretless warm bass (fundamental + soft 2nd harmonic with pluck decay)
        bass_env = math.exp(-bar_t * 1.1) * min(1.0, bar_t * 40.0)
        if bar_t >= 2.0:
            t2 = bar_t - 2.0
            bass_env = math.exp(-t2 * 1.3) * min(1.0, t2 * 40.0)
            bass_f = bass_freq * (1.5 if beat_idx == 3 else 1.0)
        else:
            bass_f = bass_freq
        bass = bass_env * (
            0.22 * math.sin(two_pi * bass_f * t)
            + 0.06 * math.sin(two_pi * (bass_f * 2.0) * t)
        )

        # 2. Rhodes electric piano chord pad (gentle stereo tremolo + bell tine attack)
        pad_env = math.exp(-bar_t * 0.55) * min(1.0, bar_t * 30.0)
        tremolo_l = 0.85 + 0.15 * math.sin(two_pi * 3.5 * t)
        tremolo_r = 0.85 - 0.15 * math.sin(two_pi * 3.5 * t)
        pad_sum = 0.0
        for pf in pad_freqs:
            pad_sum += math.sin(two_pi * pf * t) + 0.12 * math.exp(-bar_t * 4.0) * math.sin(two_pi * (pf * 3.0) * t)
        pad_val = (pad_sum / len(pad_freqs)) * 0.20 * pad_env

        # 3. Gentle Rhodes lead notes on beats
        mel_f = melody_freqs[beat_idx]
        mel_env = math.exp(-beat_t * 2.4) * min(1.0, beat_t * 50.0)
        melody = mel_env * (
            0.10 * math.sin(two_pi * mel_f * t)
            + 0.03 * math.exp(-beat_t * 8.0) * math.sin(two_pi * (mel_f * 2.0) * t)
        )

        left = bass + pad_val * tremolo_l + melody * 0.9
        right = bass + pad_val * tremolo_r + melody * 1.1

        li = max(-32000, min(32000, int(left * 24000)))
        ri = max(-32000, min(32000, int(right * 24000)))
        struct.pack_into("<hh", buf, i * 4, li, ri)

    with wave.open(str(synth_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(buf)

    return synth_path


# ---------------------------------------------------------------------------
# Visual Compositor (1/3 Header + 2/3 Scrolling Strip)
# ---------------------------------------------------------------------------

class GuideVisualBuilder:
    def __init__(self, client: JellyfinGuideDataClient) -> None:
        self.client = client
        self.font_logo = _load_font(26, bold=True)
        self.font_clock = _load_font(22, bold=True)
        self.font_col = _load_font(15, bold=True)
        self.font_ch_num = _load_font(26, bold=True)
        self.font_ch_name = _load_font(17, bold=True)
        self.font_now = _load_font(20, bold=True)
        self.font_next = _load_font(15, bold=False)
        self.font_pill = _load_font(15, bold=True)
        self.font_promo_hdr = _load_font(15, bold=True)
        self.font_promo_title = _load_font(19, bold=True)
        self.font_promo_sub = _load_font(14, bold=False)

    def render_top_region(
        self,
        unix_t: int,
        channels: list[dict],
        slots_by_id: dict[str, dict],
        featured_index: int,
    ) -> Image.Image:
        """Render the top 1280x276 region (240px 1/3 header + 36px column header bar)."""
        img = Image.new("RGB", (WIDTH, VIEWPORT_Y), (8, 18, 38))
        draw = ImageDraw.Draw(img)

        # Top banner bar (y: 0..52)
        draw.rectangle([0, 0, WIDTH, 52], fill=(16, 36, 74))
        draw.line([(0, 52), (WIDTH, 52)], fill=(240, 200, 60), width=2)

        # Classic TV emblem badge
        draw.rounded_rectangle([20, 8, 274, 44], radius=6, fill=(210, 165, 30), outline=(255, 230, 120), width=2)
        draw.text((30, 12), "CLASSIC TV  CH 1", fill=(10, 20, 42), font=self.font_logo)

        draw.text((292, 15), "PROGRAM GUIDE", fill=(190, 215, 255), font=self.font_clock)

        # Live local clock (America/Chicago)
        local_dt = datetime.fromtimestamp(unix_t, tz=CHICAGO_TZ)
        clock_str = local_dt.strftime("%a %b %-d   %-I:%M %p").upper()
        cb = draw.textbbox((0, 0), clock_str, font=self.font_clock)
        draw.text((WIDTH - (cb[2] - cb[0]) - 24, 15), clock_str, fill=(255, 230, 110), font=self.font_clock)

        # Left Card (x: 20..690, y: 64..228): Featured "ON RIGHT NOW" with large still
        draw.rounded_rectangle([20, 64, 690, 228], radius=8, fill=(14, 30, 62), outline=(45, 85, 150), width=2)
        if channels:
            feat_ch = channels[featured_index % len(channels)]
            cid = feat_ch["Id"]
            slot = slots_by_id.get(cid, {})
            now_title = slot.get("Title") or "Offline"
            now_ep = (slot.get("EpisodeTitle") or "").strip()
            if now_ep.lower() == now_title.lower():
                now_ep = ""
            item_id = slot.get("JellyfinItemId") or ""
            dur = float(slot.get("Duration") or 0)
            off = float(slot.get("StartOffset") or 0)
            mins_left = max(1, round((dur - off) / 60)) if dur > 0 else 0

            thumb = self.client.get_thumbnail(item_id, feat_ch.get("Name", ""), size=(184, 138))
            img.paste(thumb, (34, 77))
            draw.rectangle([34, 77, 218, 215], outline=(240, 200, 60), width=2)

            draw.text((236, 76), "FEATURED NOW AIRING", fill=(240, 200, 60), font=self.font_promo_hdr)
            ch_line = f"CH {feat_ch.get('Number', 0):02d}  •  {feat_ch.get('Name', '').upper()}"
            draw.text(
                (236, 98),
                _truncate_text(draw, ch_line, self.font_ch_name, 430),
                fill=(130, 195, 255),
                font=self.font_ch_name,
            )
            draw.text(
                (236, 122 if now_ep else 128),
                _truncate_text(draw, now_title, self.font_promo_title, 430),
                fill=(255, 255, 255),
                font=self.font_promo_title,
            )
            if now_ep:
                draw.text(
                    (236, 146),
                    _truncate_text(draw, now_ep, self.font_promo_sub, 430),
                    fill=(225, 235, 250),
                    font=self.font_promo_sub,
                )

            next_item = slot.get("NextItem") or {}
            next_title = next_item.get("Title") or "Continuous Programming"
            next_ep = (next_item.get("EpisodeTitle") or "").strip()
            next_display = _format_program_title(next_title, next_ep)
            draw.text(
                (236, 168 if now_ep else 160),
                _truncate_text(draw, f"Up Next: {next_display}", self.font_promo_sub, 430),
                fill=(180, 200, 230),
                font=self.font_promo_sub,
            )
            if mins_left > 0:
                pill_y = 192 if now_ep else 188
                pill_lbl = f"{mins_left} MIN REMAINING"
                pb = draw.textbbox((0, 0), pill_lbl, font=self.font_promo_sub)
                pw = (pb[2] - pb[0]) + 28
                draw.rounded_rectangle([236, pill_y, 236 + pw, pill_y + 28], radius=5, fill=(28, 64, 128))
                draw.text((250, pill_y + 6), pill_lbl, fill=(255, 230, 110), font=self.font_promo_sub)

        # Right Card (x: 706..1260, y: 64..228): Quick Tune Directory / Highlights
        draw.rounded_rectangle([706, 64, 1260, 228], radius=8, fill=(14, 30, 62), outline=(45, 85, 150), width=2)
        draw.text((726, 76), "DIRECT REMOTE TUNING", fill=(240, 200, 60), font=self.font_promo_hdr)
        draw.text(
            (726, 100),
            "Enter any 1 or 2-digit channel number on your",
            fill=(220, 230, 245),
            font=self.font_promo_sub,
        )
        draw.text(
            (726, 120),
            "remote to jump straight to that broadcast:",
            fill=(220, 230, 245),
            font=self.font_promo_sub,
        )
        highlight_cols = [
            (726, ["02 Anime", "03 Disney Toons", "04 Nicktoons"]),
            (896, ["05 Cartoon Net", "08 Superheroes", "12 It's 1997"]),
            (1076, ["15 Sitcoms", "18 Grown-Up", "20 MTV"]),
        ]
        for col_x, rows in highlight_cols:
            for r_idx, item_str in enumerate(rows):
                draw.text((col_x, 148 + r_idx * 22), item_str, fill=(145, 205, 255), font=self.font_promo_sub)

        # Column Header Bar (y: 240..276)
        draw.rectangle([0, HEADER_HEIGHT, WIDTH, VIEWPORT_Y], fill=(24, 52, 104))
        draw.line([(0, HEADER_HEIGHT), (WIDTH, HEADER_HEIGHT)], fill=(120, 170, 240), width=2)
        draw.line([(0, VIEWPORT_Y - 1), (WIDTH, VIEWPORT_Y - 1)], fill=(240, 200, 60), width=2)

        draw.text((36, HEADER_HEIGHT + 9), "CH", fill=(255, 230, 110), font=self.font_col)
        draw.text((116, HEADER_HEIGHT + 9), "CHANNEL", fill=(255, 230, 110), font=self.font_col)
        draw.text((380, HEADER_HEIGHT + 9), "PREVIEW", fill=(255, 230, 110), font=self.font_col)
        draw.text((504, HEADER_HEIGHT + 9), "NOW PLAYING  /  NEXT UP", fill=(255, 230, 110), font=self.font_col)
        draw.text((1130, HEADER_HEIGHT + 9), "REMAINING", fill=(255, 230, 110), font=self.font_col)

        return img

    def render_channel_strip(
        self,
        channels: list[dict],
        slots_by_id: dict[str, dict],
    ) -> Image.Image:
        """Render the full vertical strip of all channels (`1280 x (len(channels) * 88)`)."""
        total_h = max(ROW_HEIGHT, len(channels) * ROW_HEIGHT)
        strip = Image.new("RGB", (WIDTH, total_h), (10, 25, 49))
        draw = ImageDraw.Draw(strip)

        for idx, ch in enumerate(channels):
            y0 = idx * ROW_HEIGHT
            y1 = y0 + ROW_HEIGHT
            bg = (10, 25, 49) if idx % 2 == 0 else (16, 36, 68)
            draw.rectangle([0, y0, WIDTH, y1], fill=bg)
            draw.line([(0, y1 - 1), (WIDTH, y1 - 1)], fill=(32, 64, 118), width=1)

            ch_num = ch.get("Number") or 0
            ch_name = (ch.get("Name") or ch.get("Id") or "").upper()
            is_mirror = bool(ch.get("Mirror"))

            # 1. Channel Number Badge (x: 24..92)
            draw.rounded_rectangle(
                [24, y0 + 18, 92, y0 + 70],
                radius=7,
                fill=(28, 58, 118),
                outline=(240, 200, 60),
                width=2,
            )
            num_str = f"{ch_num:02d}"
            nb = draw.textbbox((0, 0), num_str, font=self.font_ch_num)
            nx = 24 + (68 - (nb[2] - nb[0])) // 2
            ny = y0 + 18 + (52 - (nb[3] - nb[1])) // 2 - 2
            draw.text((nx, ny), num_str, fill=(255, 225, 90), font=self.font_ch_num)

            # 2. Channel Name (x: 114..348)
            draw.text(
                (114, y0 + 33),
                _truncate_text(draw, ch_name, self.font_ch_name, 234),
                fill=(185, 215, 255),
                font=self.font_ch_name,
            )

            slot = slots_by_id.get(ch["Id"], {})
            item_id = slot.get("JellyfinItemId") or ""
            now_title = slot.get("Title") or "Offline"
            now_ep = (slot.get("EpisodeTitle") or "").strip()
            dur = float(slot.get("Duration") or 0.0)
            off = float(slot.get("StartOffset") or 0.0)
            next_item = slot.get("NextItem") or {}
            next_title = next_item.get("Title") or ""
            next_ep = (next_item.get("EpisodeTitle") or "").strip()

            if is_mirror and (not item_id or now_title.lower() == "offline"):
                now_title = "Live Mirror Channel"
                now_ep = ""
                next_title = f"Tune to Ch {ch_num:02d} when active"
                next_ep = ""

            # 3. Episode Still Thumbnail (x: 364..470, y: y0+14..y0+74 -> 106x60)
            thumb = self.client.get_thumbnail(item_id, ch_name, size=(106, 60))
            strip.paste(thumb, (364, y0 + 14))
            draw.rectangle([364, y0 + 14, 470, y0 + 74], outline=(110, 155, 220), width=1)

            # 4. Now Playing & Next Up (x: 490..1080)
            is_offline = now_title.lower() == "offline"
            now_color = (150, 160, 180) if is_offline else (255, 255, 255)
            now_display = now_title if is_offline else _format_program_title(now_title, now_ep)
            draw.text(
                (490, y0 + 16),
                _truncate_text(draw, now_display, self.font_now, 590),
                fill=now_color,
                font=self.font_now,
            )

            if next_title:
                next_str = f"Next: {_format_program_title(next_title, next_ep)}"
            elif is_offline:
                next_str = "Station Currently Offline"
            else:
                next_str = "Continuous Broadcast"

            draw.text(
                (490, y0 + 48),
                _truncate_text(draw, next_str, self.font_next, 590),
                fill=(135, 195, 250),
                font=self.font_next,
            )

            # 5. Time Remaining Pill & Progress Bar (x: 1104..1256)
            if dur > 0 and not is_offline:
                mins_left = max(1, round((dur - off) / 60.0))
                progress = max(0.0, min(1.0, off / dur))
                draw.rounded_rectangle([1104, y0 + 16, 1256, y0 + 46], radius=6, fill=(24, 54, 108))
                pill_text = f"{mins_left}m left"
                pb = draw.textbbox((0, 0), pill_text, font=self.font_pill)
                px = 1104 + (152 - (pb[2] - pb[0])) // 2
                draw.text((px, y0 + 22), pill_text, fill=(255, 230, 110), font=self.font_pill)

                # Progress bar (x: 1104..1256, y: y0+56..y0+66)
                draw.rectangle([1104, y0 + 56, 1256, y0 + 66], fill=(18, 34, 64), outline=(60, 100, 165), width=1)
                fill_w = int(150 * progress)
                if fill_w > 0:
                    draw.rectangle([1105, y0 + 57, 1105 + fill_w, y0 + 65], fill=(80, 200, 120))
            else:
                draw.rounded_rectangle([1104, y0 + 26, 1256, y0 + 56], radius=6, fill=(28, 40, 68))
                lbl = "MIRROR" if is_mirror else "OFFLINE"
                pb = draw.textbbox((0, 0), lbl, font=self.font_pill)
                px = 1104 + (152 - (pb[2] - pb[0])) // 2
                draw.text((px, y0 + 32), lbl, fill=(160, 180, 210), font=self.font_pill)

        return strip


# ---------------------------------------------------------------------------
# Chunk Video Renderer
# ---------------------------------------------------------------------------

def render_guide_chunk(
    client: JellyfinGuideDataClient,
    chunk_start: int,
    output_path: Path,
    audio_dir: Path,
    duration_sec: int = CHUNK_SECONDS,
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Render a single `duration_sec` MP4 guide video starting at `chunk_start`."""
    t0 = time.monotonic()
    channels = client.get_channels()
    if not channels:
        raise RuntimeError("No enabled channels returned by /ClassicTV/channels")

    builder = GuideVisualBuilder(client)
    strip_height = len(channels) * ROW_HEIGHT
    pass_duration_sec = max(30, int(round(strip_height / (SCROLL_PX_PER_FRAME * FPS))))

    # Pre-fetch schedule snapshots for each scroll pass in the chunk
    num_passes = max(1, math.ceil(duration_sec / pass_duration_sec) + 1)
    pass_strip_views: list[memoryview] = []
    pass_slots: list[dict[str, dict]] = []
    row_stride = WIDTH * 3
    viewport_byte_len = VIEWPORT_HEIGHT * row_stride

    logger.info(
        "Precomputing %d scroll passes (%ds each) for chunk %d (%d channels)...",
        num_passes,
        pass_duration_sec,
        chunk_start,
        len(channels),
    )

    for p_idx in range(num_passes):
        sample_t = chunk_start + p_idx * pass_duration_sec
        slots_by_id: dict[str, dict] = {}
        for ch in channels:
            cid = ch["Id"]
            if ch.get("Mirror") and p_idx > 0:
                slots_by_id[cid] = pass_slots[0][cid]
            else:
                slots_by_id[cid] = client.get_slot(cid, sample_t)
        pass_slots.append(slots_by_id)
        strip_img = builder.render_channel_strip(channels, slots_by_id)
        # Double the strip vertically so any crop window [y_off, y_off + VIEWPORT_HEIGHT]
        # is a single contiguous zero-copy memoryview slice per frame.
        doubled = Image.new("RGB", (WIDTH, strip_height * 2))
        doubled.paste(strip_img, (0, 0))
        doubled.paste(strip_img, (0, strip_height))
        pass_strip_views.append(memoryview(doubled.tobytes()))

    audio_file = ensure_smooth_jazz_wav(audio_dir, duration_sec)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".tmp.mp4")

    # Use hardware VideoToolbox encoder on macOS if available; fall back to libx264
    vcodec_args = (
        ["-c:v", "h264_videotoolbox", "-b:v", "1800k", "-pix_fmt", "yuv420p"]
        if sys.platform == "darwin"
        else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    )

    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        # Video input: raw RGB24 frames from stdin
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "pipe:0",
        # Audio input: looped smooth jazz
        "-stream_loop",
        "-1",
        "-i",
        str(audio_file),
        "-t",
        str(duration_sec),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        *vcodec_args,
        "-g",
        str(FPS * 2),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(tmp_path),
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None

    total_frames = duration_sec * FPS
    current_header_key = (-1, -1)
    header_bytes = b""

    try:
        for f in range(total_frames):
            elapsed_sec = f / FPS
            wall_t = chunk_start + int(elapsed_sec)
            pass_idx = min(len(pass_strip_views) - 1, int(elapsed_sec // pass_duration_sec))

            # Update top header once every 15 seconds (rotates featured card) or on minute change
            minute_bucket = wall_t // 60
            feat_idx = int(elapsed_sec // 15) % len(channels)
            header_key = (minute_bucket, feat_idx)
            if header_key != current_header_key:
                top_img = builder.render_top_region(
                    wall_t,
                    channels,
                    pass_slots[pass_idx],
                    feat_idx,
                )
                header_bytes = top_img.tobytes()
                current_header_key = header_key

            y_off = int(f * SCROLL_PX_PER_FRAME) % strip_height
            start_byte = y_off * row_stride
            proc.stdin.write(header_bytes)
            proc.stdin.write(pass_strip_views[pass_idx][start_byte : start_byte + viewport_byte_len])
    finally:
        proc.stdin.close()
        rc = proc.wait()

    if rc != 0:
        if tmp_path.exists():
            tmp_path.unlink()
        raise RuntimeError(f"ffmpeg exited with code {rc}")

    tmp_path.replace(output_path)
    elapsed = time.monotonic() - t0
    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(
        "Rendered %s (%ds) in %.1fs (%.2f MB)",
        output_path.name,
        duration_sec,
        elapsed,
        size_mb,
    )


# ---------------------------------------------------------------------------
# Rolling Buffer & Tight Prune Manager
# ---------------------------------------------------------------------------

def prune_old_chunks(cache_dir: Path, now_unix: int, grace_seconds: int = CHUNK_SECONDS) -> int:
    """Delete guide-{ts}.mp4 files whose end time (ts + 900) is older than `now_unix - grace_seconds`."""
    if not cache_dir.exists():
        return 0
    removed = 0
    cutoff_start = now_unix - CHUNK_SECONDS - grace_seconds
    for f in cache_dir.iterdir():
        m = CHUNK_FILE_RE.match(f.name) or SIG_FILE_RE.match(f.name)
        if not m:
            continue
        ts = int(m.group(1))
        if ts < cutoff_start:
            try:
                f.unlink()
                if CHUNK_FILE_RE.match(f.name):
                    removed += 1
                    logger.info("Pruned expired guide chunk: %s", f.name)
            except Exception as exc:
                logger.warning("Failed to prune %s: %s", f.name, exc)
    return removed


def compute_schedule_signature(
    client: JellyfinGuideDataClient,
    channels: list[dict],
    chunk_start: int,
) -> str:
    """Hash non-mirror channel slots at `chunk_start` to detect schedule drift."""
    rows = []
    for c in sorted(channels, key=lambda ch: (ch.get("Number") or 9999, ch.get("Id") or "")):
        if c.get("Mirror"):
            continue
        cid = c.get("Id") or ""
        slot = client.get_slot(cid, chunk_start)
        rows.append([
            cid,
            c.get("Number"),
            c.get("Name"),
            slot.get("JellyfinItemId") or None,
            int(round(slot.get("StartOffset") or 0)),
        ])
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def invalidate_cached_chunks(cache_dir: Path) -> int:
    """Delete all cached guide-*.mp4 and guide-*.sig files in `cache_dir`."""
    if not cache_dir.exists():
        return 0
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and (CHUNK_FILE_RE.match(f.name) or SIG_FILE_RE.match(f.name)):
            try:
                f.unlink()
                if CHUNK_FILE_RE.match(f.name):
                    removed += 1
            except Exception as exc:
                logger.warning("Failed to invalidate %s: %s", f.name, exc)
    return removed


def maintain_rolling_buffer(
    client: JellyfinGuideDataClient,
    cache_dir: Path,
    audio_dir: Path,
    horizon_hours: float = 4.0,
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    num_chunks = max(1, int(round((horizon_hours * 3600) / CHUNK_SECONDS)))

    # Invalidate pre-rendered chunks if the enabled channel roster or active schedule changed.
    try:
        channels = client.get_channels()
        roster = [
            (c.get("Id"), c.get("Number"), c.get("Name"))
            for c in channels
        ]
        roster_hash = hashlib.sha256(
            json.dumps(roster, sort_keys=True).encode("utf-8")
        ).hexdigest()
        hash_file = cache_dir / ".channels_hash"
        prev_hash = hash_file.read_text(encoding="utf-8").strip() if hash_file.exists() else None
        if prev_hash is not None and prev_hash != roster_hash:
            removed = invalidate_cached_chunks(cache_dir)
            logger.info(
                "Channel roster changed (%s -> %s); invalidated %d cached guide chunks.",
                prev_hash[:8],
                roster_hash[:8],
                removed,
            )
        else:
            now_unix = int(time.time())
            current_chunk = (now_unix // CHUNK_SECONDS) * CHUNK_SECONDS
            active_mp4 = cache_dir / f"guide-{current_chunk}.mp4"
            active_sig = cache_dir / f"guide-{current_chunk}.sig"
            if active_mp4.exists() and active_sig.exists():
                live_sig = compute_schedule_signature(client, channels, current_chunk)
                prev_sig = active_sig.read_text(encoding="utf-8").strip()
                if prev_sig != live_sig:
                    removed = invalidate_cached_chunks(cache_dir)
                    logger.info(
                        "Schedule drift detected for %s (%s -> %s); invalidated %d cached guide chunks.",
                        active_mp4.name,
                        prev_sig[:8],
                        live_sig[:8],
                        removed,
                    )
        hash_file.parent.mkdir(parents=True, exist_ok=True)
        hash_file.write_text(roster_hash, encoding="utf-8")
    except Exception as exc:
        logger.warning("Channel/schedule hash check failed: %s", exc)

    while True:
        now_unix = int(time.time())
        prune_old_chunks(cache_dir, now_unix, grace_seconds=CHUNK_SECONDS)
        current_chunk = (now_unix // CHUNK_SECONDS) * CHUNK_SECONDS

        # Keep web/guide/preview.mp4 pointing to the active 15m chunk if available
        active_path = cache_dir / f"guide-{current_chunk}.mp4"
        if active_path.exists():
            web_preview = Path(__file__).resolve().parent.parent / "web" / "guide" / "preview.mp4"
            try:
                web_preview.parent.mkdir(parents=True, exist_ok=True)
                tmp_link = web_preview.with_suffix(".tmp.mp4")
                if tmp_link.exists() or tmp_link.is_symlink():
                    tmp_link.unlink()
                os.symlink(active_path, tmp_link)
                tmp_link.replace(web_preview)
            except Exception as exc:
                logger.warning("Failed to update web preview symlink: %s", exc)

        # Find the earliest chunk in the forward horizon that is not yet ready
        target_chunk = None
        for i in range(num_chunks):
            c_start = current_chunk + i * CHUNK_SECONDS
            out_path = cache_dir / f"guide-{c_start}.mp4"
            if not (out_path.exists() and out_path.stat().st_size > 100_000):
                target_chunk = c_start
                break

        if target_chunk is None:
            logger.info("All %d forward chunks up to horizon (%.1fh) are ready.", num_chunks, horizon_hours)
            break

        out_path = cache_dir / f"guide-{target_chunk}.mp4"
        offset_m = (target_chunk - current_chunk) // 60
        logger.info("Rendering missing forward chunk: %s (slot offset +%dm)", out_path.name, offset_m)
        render_guide_chunk(
            client=client,
            chunk_start=target_chunk,
            output_path=out_path,
            audio_dir=audio_dir,
            duration_sec=CHUNK_SECONDS,
            ffmpeg_bin=ffmpeg_bin,
        )
        try:
            sig_path = cache_dir / f"guide-{target_chunk}.sig"
            sig_path.write_text(
                compute_schedule_signature(client, client.get_channels(), target_chunk),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to write schedule signature for %s: %s", out_path.name, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Classic TV Pre-Rendered Guide Generator")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("JELLYFIN_URL", "http://localhost:8096"),
        help="Jellyfin server base URL (default: http://localhost:8096)",
    )
    parser.add_argument(
        "--cache-dir",
        default=os.path.expanduser("~/Library/Application Support/jellyfin/data/classictv-guide"),
        help="Directory to store guide-<ts>.mp4 chunks",
    )
    parser.add_argument(
        "--audio-dir",
        default=os.path.expanduser("~/projects/ClassicTV3.0/resources/guide_audio"),
        help="Directory containing background smooth jazz audio tracks",
    )
    parser.add_argument(
        "--horizon-hours",
        type=float,
        default=4.0,
        help="Forward rolling buffer horizon in hours (default: 4.0 = 16 chunks)",
    )
    parser.add_argument(
        "--preview-output",
        default=None,
        help="Render a single preview MP4 to this path and exit",
    )
    parser.add_argument(
        "--preview-duration",
        type=int,
        default=90,
        help="Duration in seconds when rendering --preview-output (default: 90s)",
    )
    parser.add_argument(
        "--ffmpeg-bin",
        default=os.environ.get("FFMPEG_BIN", "ffmpeg"),
        help="Path to ffmpeg binary",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cache_dir = Path(args.cache_dir)
    audio_dir = Path(args.audio_dir)
    thumb_dir = cache_dir / "thumbs"

    ffmpeg_bin = args.ffmpeg_bin
    if not shutil.which(ffmpeg_bin):
        for candidate in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/Applications/Jellyfin.app/Contents/MacOS/ffmpeg"):
            if os.path.exists(candidate):
                ffmpeg_bin = candidate
                break

    client = JellyfinGuideDataClient(args.base_url, thumb_dir)

    if args.preview_output:
        now_unix = int(time.time())
        chunk_start = (now_unix // CHUNK_SECONDS) * CHUNK_SECONDS
        render_guide_chunk(
            client=client,
            chunk_start=chunk_start,
            output_path=Path(args.preview_output),
            audio_dir=audio_dir,
            duration_sec=args.preview_duration,
            ffmpeg_bin=ffmpeg_bin,
        )
        return 0

    maintain_rolling_buffer(
        client=client,
        cache_dir=cache_dir,
        audio_dir=audio_dir,
        horizon_hours=args.horizon_hours,
        ffmpeg_bin=ffmpeg_bin,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
