"""
Unit tests for addon/resources/lib/media_cache.py.

Covers:
- LRU eviction: oldest by last_accessed removed first when over capacity (Req 19.5, 19.6)
- LRU eviction: active (currently playing) item is never evicted (Req 19.7)
- LRU eviction: all items playing → skip eviction, log WARNING (Req 19.8)
- prefetch happy path: downloads, marks is_complete=True, .part file renamed (Req 19.3, 19.4)
- prefetch failure: deletes .part, logs WARNING, is_complete stays False (Req 19.4)
- get_local_url: returns None for incomplete items (Req 19.5)
- get_local_url: returns None for absent items (Req 19.5)
- get_local_url: returns local path for complete items (Req 19.5)
- get_local_url: updates last_accessed on each call (Req 19.5)

Requirements: 19.5, 19.6, 19.7, 19.8
"""

import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load Kodi stubs from conftest, then add addon lib to path.
# conftest.py is auto-loaded by pytest, but we import it explicitly for
# clarity when running this file directly.
# ---------------------------------------------------------------------------

sys.path.insert(0, "tests")
sys.path.insert(0, "addon/resources/lib")

from media_cache import MediaCache, CacheEntry


# ---------------------------------------------------------------------------
# Fixture: fresh MediaCache rooted under tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Return a MediaCache whose CACHE_DIR is an isolated temp directory.

    Overriding CACHE_DIR on the class prevents any test from accidentally
    writing to /storage/classictv_cache/.  The fixture yields a fully
    initialised cache with an empty in-memory index.
    """
    monkeypatch.setattr(MediaCache, "CACHE_DIR", str(tmp_path))
    return MediaCache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    jellyfin_id: str,
    *,
    cache_dir: str,
    size_bytes: int = 100,
    last_accessed: float = 0.0,
    is_complete: bool = True,
) -> CacheEntry:
    """Create a CacheEntry and touch the corresponding .bin file on disk."""
    local_path = f"{cache_dir}/{jellyfin_id}.bin"
    # Write a placeholder file so evict_if_needed can os.unlink it.
    with open(local_path, "wb") as fh:
        fh.write(b"\x00" * size_bytes)
    return CacheEntry(
        jellyfin_id=jellyfin_id,
        local_path=local_path,
        size_bytes=size_bytes,
        last_accessed=last_accessed,
        is_complete=is_complete,
    )


def _fake_urlopen(content: bytes, *, timeout_side_effect=None):
    """Return a context-manager mock that yields a response with *content*."""
    resp = MagicMock()
    # Simulate chunked reads: first call returns content, second returns b"".
    resp.read.side_effect = [content, b""]
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)

    if timeout_side_effect is not None:
        cm = MagicMock()
        cm.__enter__ = MagicMock(side_effect=timeout_side_effect)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    return resp


# ===========================================================================
# Tests: get_local_url
# ===========================================================================

class TestGetLocalUrl:

    def test_absent_item_returns_none(self, cache):
        """Req 19.5: get_local_url returns None when the ID is not in the cache."""
        result = cache.get_local_url("nonexistent-id")
        assert result is None

    def test_incomplete_item_returns_none(self, cache, tmp_path):
        """Req 19.5: get_local_url returns None when is_complete is False."""
        entry = _make_entry(
            "incomplete-id",
            cache_dir=str(tmp_path),
            is_complete=False,
        )
        cache._cache["incomplete-id"] = entry

        result = cache.get_local_url("incomplete-id")
        assert result is None

    def test_complete_item_returns_local_path(self, cache, tmp_path):
        """Req 19.5: get_local_url returns the local path for a complete item."""
        entry = _make_entry("complete-id", cache_dir=str(tmp_path))
        cache._cache["complete-id"] = entry

        result = cache.get_local_url("complete-id")
        assert result == entry.local_path

    def test_updates_last_accessed(self, cache, tmp_path):
        """Req 19.5: get_local_url updates last_accessed to approximately now."""
        entry = _make_entry(
            "ts-id",
            cache_dir=str(tmp_path),
            last_accessed=0.0,
        )
        cache._cache["ts-id"] = entry

        before = time.time()
        cache.get_local_url("ts-id")
        after = time.time()

        assert before <= cache._cache["ts-id"].last_accessed <= after

    def test_last_accessed_increases_on_second_call(self, cache, tmp_path):
        """Each successive call to get_local_url moves last_accessed forward."""
        entry = _make_entry("ts2-id", cache_dir=str(tmp_path), last_accessed=0.0)
        cache._cache["ts2-id"] = entry

        cache.get_local_url("ts2-id")
        first_ts = cache._cache["ts2-id"].last_accessed

        # Tiny sleep to guarantee time.time() advances on this platform.
        time.sleep(0.01)
        cache.get_local_url("ts2-id")
        second_ts = cache._cache["ts2-id"].last_accessed

        assert second_ts >= first_ts


# ===========================================================================
# Tests: prefetch
# ===========================================================================

class TestPrefetch:

    def test_happy_path_marks_complete(self, cache, tmp_path):
        """Req 19.3: successful download sets is_complete=True in the cache."""
        fake_data = b"fake media content"

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(fake_data)):
            cache.prefetch("new-id", "http://jf/stream/new-id")

        assert "new-id" in cache._cache
        assert cache._cache["new-id"].is_complete is True

    def test_happy_path_part_file_removed(self, cache, tmp_path):
        """Req 19.3: .part temp file must not exist after a successful download."""
        fake_data = b"video bytes"

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(fake_data)):
            cache.prefetch("part-id", "http://jf/stream/part-id")

        part_path = str(tmp_path / "part-id.bin.part")
        assert not __import__("os").path.exists(part_path)

    def test_happy_path_final_file_exists(self, cache, tmp_path):
        """The renamed .bin file must exist on disk after a successful download."""
        fake_data = b"media payload"

        with patch("urllib.request.urlopen", return_value=_fake_urlopen(fake_data)):
            cache.prefetch("file-id", "http://jf/stream/file-id")

        final_path = str(tmp_path / "file-id.bin")
        assert __import__("os").path.exists(final_path)

    def test_happy_path_size_bytes_recorded(self, cache, tmp_path):
        """size_bytes in the CacheEntry matches the downloaded content length."""
        fake_data = b"x" * 512
        with patch("urllib.request.urlopen", return_value=_fake_urlopen(fake_data)):
            cache.prefetch("size-id", "http://jf/stream/size-id")

        assert cache._cache["size-id"].size_bytes == 512

    def test_idempotent_when_already_cached(self, cache, tmp_path):
        """Req 19.3: calling prefetch again on a complete item is a no-op."""
        entry = _make_entry("dup-id", cache_dir=str(tmp_path))
        cache._cache["dup-id"] = entry
        original_path = entry.local_path

        # urlopen must NOT be called for an already-complete item.
        with patch("urllib.request.urlopen") as mock_open:
            cache.prefetch("dup-id", "http://jf/stream/dup-id")
            mock_open.assert_not_called()

        assert cache._cache["dup-id"].local_path == original_path

    def test_failure_deletes_part_file(self, cache, tmp_path):
        """Req 19.4: on download error, the .part file is cleaned up."""
        import os

        def _failing_open(*args, **kwargs):
            # Create the .part file to simulate a mid-transfer failure.
            part = str(tmp_path / "fail-id.bin.part")
            with open(part, "wb") as fh:
                fh.write(b"partial")
            raise OSError("network error")

        with patch("urllib.request.urlopen", side_effect=_failing_open):
            cache.prefetch("fail-id", "http://jf/stream/fail-id")

        part_path = str(tmp_path / "fail-id.bin.part")
        assert not os.path.exists(part_path)

    def test_failure_does_not_set_complete(self, cache, tmp_path):
        """Req 19.4: a failed prefetch must not create a CacheEntry with is_complete=True."""
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            cache.prefetch("err-id", "http://jf/stream/err-id")

        entry = cache._cache.get("err-id")
        # Either the entry doesn't exist, or if it does it must not be complete.
        assert entry is None or entry.is_complete is False

    def test_failure_logs_warning(self, cache, tmp_path):
        """Req 19.4: prefetch failure emits a WARNING-level log."""
        import xbmc as _xbmc

        logged_levels: list[int] = []

        def _capturing_log(msg: str, level: int = 1) -> None:
            logged_levels.append(level)

        with patch.object(sys.modules["xbmc"], "log", side_effect=_capturing_log):
            with patch("urllib.request.urlopen", side_effect=OSError("boom")):
                cache.prefetch("warn-id", "http://jf/stream/warn-id")

        assert _xbmc.LOGWARNING in logged_levels


# ===========================================================================
# Tests: evict_if_needed — LRU ordering
# ===========================================================================

class TestEvictIfNeeded:

    def test_no_eviction_when_under_capacity(self, cache, tmp_path, monkeypatch):
        """No items removed when total size is under MEDIA_CACHE_MAX_BYTES."""
        monkeypatch.setattr(
            "media_cache.MEDIA_CACHE_MAX_BYTES", 10 * 1024 ** 3
        )
        entry = _make_entry("small-id", cache_dir=str(tmp_path), size_bytes=100)
        cache._cache["small-id"] = entry

        cache.evict_if_needed()

        assert "small-id" in cache._cache

    def test_oldest_item_evicted_first(self, cache, tmp_path, monkeypatch):
        """Req 19.5, 19.6: item with the lowest last_accessed is removed first."""
        # Cap at 250 bytes so two 100-byte items fit but three do not.
        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 250)

        old = _make_entry("old-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        mid = _make_entry("mid-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=2.0)
        new = _make_entry("new-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=3.0)

        cache._cache = {"old-id": old, "mid-id": mid, "new-id": new}

        cache.evict_if_needed()

        # Total was 300, cap 250 → need to shed ≥50 bytes → "old-id" goes first.
        assert "old-id" not in cache._cache
        assert "new-id" in cache._cache

    def test_eviction_continues_until_under_cap(self, cache, tmp_path, monkeypatch):
        """Multiple items evicted if needed to get under the cap."""
        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 150)

        e1 = _make_entry("e1", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        e2 = _make_entry("e2", cache_dir=str(tmp_path), size_bytes=100, last_accessed=2.0)
        e3 = _make_entry("e3", cache_dir=str(tmp_path), size_bytes=100, last_accessed=3.0)

        cache._cache = {"e1": e1, "e2": e2, "e3": e3}

        cache.evict_if_needed()

        total = sum(e.size_bytes for e in cache._cache.values())
        assert total <= 150

    def test_eviction_removes_file_from_disk(self, cache, tmp_path, monkeypatch):
        """Evicted item's .bin file is deleted from the filesystem."""
        import os

        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 150)

        old = _make_entry("disk-old", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        new = _make_entry("disk-new", cache_dir=str(tmp_path), size_bytes=100, last_accessed=2.0)

        cache._cache = {"disk-old": old, "disk-new": new}
        evicted_path = old.local_path

        cache.evict_if_needed()

        assert not os.path.exists(evicted_path)


# ===========================================================================
# Tests: evict_if_needed — active item protection (Req 19.7)
# ===========================================================================

class TestEvictActiveItemProtection:

    def _player_playing(self, path: str):
        """Return a mock player stub that reports *path* as the playing file."""
        p = MagicMock()
        p.isPlaying.return_value = True
        p.getPlayingFile.return_value = path
        return p

    def test_active_item_not_evicted(self, cache, tmp_path, monkeypatch):
        """Req 19.7: currently playing item is never removed by eviction."""
        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 150)

        active = _make_entry("active-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        passive = _make_entry("passive-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=2.0)

        cache._cache = {"active-id": active, "passive-id": passive}
        cache._player = self._player_playing(active.local_path)

        cache.evict_if_needed()

        # The active item must survive even though it has the lowest last_accessed.
        assert "active-id" in cache._cache

    def test_inactive_item_evicted_before_active(self, cache, tmp_path, monkeypatch):
        """Req 19.7: the non-playing item is evicted when the only alternative is
        the currently playing file."""
        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 150)

        active = _make_entry("playing-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        passive = _make_entry("idle-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=2.0)

        cache._cache = {"playing-id": active, "idle-id": passive}
        cache._player = self._player_playing(active.local_path)

        cache.evict_if_needed()

        assert "idle-id" not in cache._cache
        assert "playing-id" in cache._cache

    def test_no_player_set_allows_normal_eviction(self, cache, tmp_path, monkeypatch):
        """When _player is None eviction proceeds normally (no crash)."""
        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 150)

        old = _make_entry("np-old", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        new = _make_entry("np-new", cache_dir=str(tmp_path), size_bytes=100, last_accessed=2.0)

        cache._cache = {"np-old": old, "np-new": new}
        cache._player = None

        cache.evict_if_needed()

        assert "np-old" not in cache._cache


# ===========================================================================
# Tests: evict_if_needed — all items playing (Req 19.8)
# ===========================================================================

class TestEvictAllPlayingSkip:

    def test_skip_eviction_when_all_items_playing(self, cache, tmp_path, monkeypatch):
        """Req 19.8: eviction is skipped when every cached item is currently playing."""
        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 50)

        # Single item; it IS the playing file.
        only = _make_entry("only-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        cache._cache = {"only-id": only}

        player = MagicMock()
        player.isPlaying.return_value = True
        player.getPlayingFile.return_value = only.local_path
        cache._player = player

        cache.evict_if_needed()

        # Item must NOT have been removed.
        assert "only-id" in cache._cache

    def test_warning_logged_when_all_items_playing(self, cache, tmp_path, monkeypatch):
        """Req 19.8: a WARNING is logged when eviction must be skipped."""
        import xbmc as _xbmc

        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 50)

        only = _make_entry("log-id", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        cache._cache = {"log-id": only}

        player = MagicMock()
        player.isPlaying.return_value = True
        player.getPlayingFile.return_value = only.local_path
        cache._player = player

        logged_levels: list[int] = []

        def _capturing_log(msg: str, level: int = 1) -> None:
            logged_levels.append(level)

        with patch.object(sys.modules["xbmc"], "log", side_effect=_capturing_log):
            cache.evict_if_needed()

        assert _xbmc.LOGWARNING in logged_levels

    def test_non_playing_item_still_evicted_when_one_item_is_playing(
        self, cache, tmp_path, monkeypatch
    ):
        """Req 19.7, 19.8: when only one of two items is playing, normal LRU
        eviction proceeds and removes the non-playing candidate.

        This confirms the all-playing guard does NOT fire when a valid
        eviction candidate exists alongside the playing item.
        """
        monkeypatch.setattr("media_cache.MEDIA_CACHE_MAX_BYTES", 150)

        e1 = _make_entry("mp-1", cache_dir=str(tmp_path), size_bytes=100, last_accessed=1.0)
        e2 = _make_entry("mp-2", cache_dir=str(tmp_path), size_bytes=100, last_accessed=2.0)
        cache._cache = {"mp-1": e1, "mp-2": e2}

        # Player is playing e1; e2 is a valid candidate.
        player = MagicMock()
        player.isPlaying.return_value = True
        player.getPlayingFile.return_value = e1.local_path
        cache._player = player

        cache.evict_if_needed()

        # e1 (playing) must survive; e2 (non-playing, older) must be evicted.
        assert "mp-1" in cache._cache
        assert "mp-2" not in cache._cache
