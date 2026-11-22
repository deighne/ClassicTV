from pathlib import Path
import sys
import types
from unittest.mock import patch

if "PIL" not in sys.modules:
    try:
        import PIL  # noqa: F401
    except ImportError:
        pil_mod = types.ModuleType("PIL")
        for sub in ("Image", "ImageDraw", "ImageFont"):
            sub_mod = types.ModuleType(f"PIL.{sub}")
            if sub == "Image":
                sub_mod.Image = object
            elif sub == "ImageDraw":
                sub_mod.ImageDraw = object
            elif sub == "ImageFont":
                sub_mod.ImageFont = object
            setattr(pil_mod, sub, sub_mod)
            sys.modules[f"PIL.{sub}"] = sub_mod
        sys.modules["PIL"] = pil_mod

import tools.guide_renderer as gr


class _FakeClient:
    def __init__(self, channels, slots_by_id):
        self._channels = channels
        self._slots_by_id = slots_by_id

    def get_channels(self):
        return self._channels

    def get_slot(self, channel_id, unix_t):
        return self._slots_by_id.get(channel_id, {"JellyfinItemId": None, "StartOffset": 0})


def test_schedule_signature_ignores_mirror_channels():
    channels = [
        {"Id": "its-1997", "Number": 12, "Name": "It's 1997", "Mirror": False},
        {"Id": "rachel", "Number": 67, "Name": "Rachel", "Mirror": True},
    ]
    c1 = _FakeClient(channels, {
        "its-1997": {"JellyfinItemId": "ep-friends", "StartOffset": 120.2},
        "rachel": {"JellyfinItemId": "movie-a", "StartOffset": 10.0},
    })
    c2 = _FakeClient(channels, {
        "its-1997": {"JellyfinItemId": "ep-friends", "StartOffset": 120.4},
        "rachel": {"JellyfinItemId": "movie-b", "StartOffset": 99.0},
    })
    assert gr.compute_schedule_signature(c1, channels, 1700000100) == gr.compute_schedule_signature(
        c2, channels, 1700000100
    )

    c3 = _FakeClient(channels, {
        "its-1997": {"JellyfinItemId": "ep-cheers", "StartOffset": 120.0},
        "rachel": {"JellyfinItemId": "movie-b", "StartOffset": 99.0},
    })
    assert gr.compute_schedule_signature(c1, channels, 1700000100) != gr.compute_schedule_signature(
        c3, channels, 1700000100
    )


def test_maintain_rolling_buffer_invalidates_on_schedule_drift(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    now_unix = 1700000100
    chunk = (now_unix // gr.CHUNK_SECONDS) * gr.CHUNK_SECONDS
    channels = [{"Id": "its-1997", "Number": 12, "Name": "It's 1997", "Mirror": False}]

    old_client = _FakeClient(channels, {"its-1997": {"JellyfinItemId": "ep-friends", "StartOffset": 300}})
    new_client = _FakeClient(channels, {"its-1997": {"JellyfinItemId": "ep-cheers", "StartOffset": 618}})

    # Pre-populate .channels_hash and two chunks with the old schedule signature
    import hashlib, json
    roster_hash = hashlib.sha256(
        json.dumps([("its-1997", 12, "It's 1997")], sort_keys=True).encode("utf-8")
    ).hexdigest()
    (cache_dir / ".channels_hash").write_text(roster_hash, encoding="utf-8")

    old_sig = gr.compute_schedule_signature(old_client, channels, chunk)
    for offset in (0, gr.CHUNK_SECONDS):
        mp4 = cache_dir / f"guide-{chunk + offset}.mp4"
        mp4.write_bytes(b"0" * 200_000)
        (cache_dir / f"guide-{chunk + offset}.sig").write_text(old_sig, encoding="utf-8")

    rendered = []

    def fake_render(client, chunk_start, output_path, audio_dir, duration_sec, ffmpeg_bin):
        rendered.append(chunk_start)
        output_path.write_bytes(b"1" * 200_000)

    with patch.object(gr.time, "time", return_value=now_unix), patch.object(
        gr, "render_guide_chunk", side_effect=fake_render
    ):
        gr.maintain_rolling_buffer(new_client, cache_dir, audio_dir, horizon_hours=0.25)

    assert rendered == [chunk]
    assert (cache_dir / f"guide-{chunk}.sig").read_text(encoding="utf-8").strip() == gr.compute_schedule_signature(
        new_client, channels, chunk
    )
    assert not (cache_dir / f"guide-{chunk + gr.CHUNK_SECONDS}.mp4").exists()
