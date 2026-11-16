"""
Player-side named constants for Classic TV 3.0.

All timeouts, durations, retry counts, and limits are defined here with their
rationale so that every magic number has exactly one place to change.

No source file should reference a bare numeric literal for any of these values
outside of this module (Requirement 29.3).
"""

# ---------------------------------------------------------------------------
# Channel Bug overlay
# ---------------------------------------------------------------------------

CHANNEL_BUG_DISPLAY_SECONDS = 6
# CRT phosphor burn-in protection — do not increase.
# The Sony KV-34HS510 shadow mask will develop permanent image retention if
# a static overlay sits in one spot for more than ~10 seconds. 6 seconds is
# enough to read channel info; any longer risks the screen.

# ---------------------------------------------------------------------------
# Direct digit channel entry
# ---------------------------------------------------------------------------

DIGIT_ENTRY_TIMEOUT_SECONDS = 1.5
# Wait between digit keypresses before auto-confirming the channel number.
# 1.5s matches the mental rhythm of pressing remote keys — long enough that
# two-digit entries don't accidentally confirm early, short enough that the
# UI doesn't feel sluggish.

CHANNEL_ENTRY_MAX_DIGITS = 3
# Maximum digits accepted for a direct channel number entry (channels 1–999).
# Three digits covers all plausible channel numbers without requiring ENTER
# for the common case.

CHANNEL_ENTRY_TIMEOUT_SECONDS = 1.5
# Inactivity timeout for the digit accumulation state machine. Intentionally
# the same value as DIGIT_ENTRY_TIMEOUT_SECONDS; defined separately so each
# usage site can reference a semantically accurate name.

CHANNEL_NOT_FOUND_DISPLAY_SECONDS = 2
# Duration the "CH N — Not Found" message stays on screen before clearing.
# 2 seconds is enough to read a short error message without feeling
# like an obstacle if the viewer immediately tries a different number.

# ---------------------------------------------------------------------------
# Channel Static overlay
# ---------------------------------------------------------------------------

CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS = 8
# Dismiss the full-screen snow overlay if playback never starts.
# 8 seconds covers the worst-case startup path (schedule fetch + decode
# init + first keyframe). Beyond 8s the viewer just sees static forever,
# which is worse than an error message.

# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

WATCHDOG_INTERVAL_SECONDS = 10
# How often the background watchdog thread checks whether playback is active.
# 10 seconds is the maximum acceptable blank-screen duration before the
# watchdog recovers. Shorter intervals waste CPU on a low-power ARM board.

# ---------------------------------------------------------------------------
# Amlogic hardware decoder release
# ---------------------------------------------------------------------------

DECODER_RELEASE_POLL_TIMEOUT_SECONDS = 2
# Maximum time to wait for the Amlogic hardware decoder to release after
# stopping playback. The Le Potato's S905X decoder can hold the hardware
# lock for ~1s after xbmc.Player.stop(); 2s is a safe upper bound.

DECODER_RELEASE_POLL_INTERVAL_MS = 200
# How often to poll isPlaying() while waiting for the decoder to release.
# 200ms balances responsiveness against busy-polling on the ARM CPU.

# ---------------------------------------------------------------------------
# Startup retries
# ---------------------------------------------------------------------------

STARTUP_RETRY_MAX = 6
# Maximum number of schedule fetch attempts during addon startup.
# 6 retries × 10s interval = 60 seconds total. This gives jfmac time to
# boot and become reachable after a simultaneous power-on of both devices.

STARTUP_RETRY_INTERVAL_SECONDS = 10
# Delay between startup retries. 10s gives the Schedule_Service a full
# restart window before the next attempt.

# ---------------------------------------------------------------------------
# EOF retune retries
# ---------------------------------------------------------------------------

EOF_RETRY_MAX = 3
# Retries on a failed schedule fetch at EOF before falling back to the
# Offline_Clip. 3 retries × 2s = 6 seconds of tolerance for transient
# network hiccups without leaving the viewer on a black screen for long.

EOF_RETRY_INTERVAL_SECONDS = 2
# Delay between EOF retune retries. 2 seconds is long enough for a transient
# DNS or TCP failure to clear, short enough to feel instantaneous to the viewer.

EOF_OFFLINE_RETRY_INTERVAL_SECONDS = 30
# While the Offline_Clip is playing due to a failed retune, retry the schedule
# fetch every 30 seconds. This throttles polling while jfmac is unreachable
# without giving up on recovery.

# ---------------------------------------------------------------------------
# Channel change fetch
# ---------------------------------------------------------------------------

CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS = 3
# Abort the schedule fetch during a channel change if no response arrives
# within 3 seconds. If this fires, the Player dismisses Channel_Static and
# shows "Channel unavailable" rather than leaving the viewer in static forever.

# ---------------------------------------------------------------------------
# Mid-episode seek strategy
# ---------------------------------------------------------------------------

SEEK_TOLERANCE_SECONDS = 2.0
# Acceptable error between the target Seek_Offset and the achieved playback
# position. ±2 seconds is imperceptible on a 22-minute sitcom and matches
# the empirical accuracy of Kodi's seekTime() on the Amlogic decoder.

SEEK_MAX_RETRIES = 3
# Post-start seek retry count before logging an error and continuing from
# the best-achieved position. 3 retries × 500ms = 1.5s of seek correction
# attempts, which covers typical Amlogic decoder startup variance.

SEEK_RETRY_INTERVAL_MS = 500
# Delay between post-start seek retry attempts. 500ms gives the decoder time
# to settle after seekTime() before sampling the position.

SEEK_CALLBACK_MAX_BLOCKING_MS = 50
# Maximum time onPlayBackStarted is allowed to block before returning to Kodi.
# The Amlogic decoder shares the Python callback thread with its own player
# state machine; blocking longer than this causes the decoder to time out and
# abort the stream. Seek correction runs on a daemon thread to stay within
# this budget. (Requirement 39)

# ---------------------------------------------------------------------------
# Rachel mode (CH67)
# ---------------------------------------------------------------------------

RACHEL_POLL_INTERVAL_SECONDS = 2
# How often the Rachel poller thread calls GET /rachel-anchor.
# 2-second intervals keep the viewer within ±2s of Rachel's playback position
# (matching SEEK_TOLERANCE_SECONDS) while keeping network chatter minimal.

RACHEL_POLL_TIMEOUT_SECONDS = 5
# Per-request HTTP timeout when calling the /rachel-anchor endpoint.
# 5 seconds is generous enough to cover a slow jfmac response without
# blocking the poll thread indefinitely on a hung connection.

RACHEL_DRIFT_THRESHOLD_SECONDS = 5.0
# Seek to re-sync if the computed drift between local and Rachel's position
# exceeds this value. 5 seconds of drift is noticeable and worth correcting;
# smaller corrections create a choppy experience.

# ---------------------------------------------------------------------------
# TV Guide overlay
# ---------------------------------------------------------------------------

GUIDE_CACHE_TTL_SECONDS = 30
# In-memory cache lifetime for the guide API response. 30 seconds means
# re-opening the guide within half a minute requires no network round-trip,
# which keeps the overlay snappy on the slow ARM CPU.

GUIDE_FETCH_TIMEOUT_SECONDS = 3
# Abort the /guide fetch if no response arrives within 3 seconds.
# If this fires, the Guide overlay shows an error message and closes after 2s.

# ---------------------------------------------------------------------------
# Media cache (prefetch)
# ---------------------------------------------------------------------------

MEDIA_CACHE_MAX_BYTES = 10 * 1024 ** 3
# 10 GB maximum cache size on /storage (Sweet Potato SD card).
# The Le Potato's SD card is 32 GB; 10 GB leaves 22 GB for CoreELEC, Kodi,
# and the OS. LRU eviction keeps the cache under this limit.

MEDIA_PREFETCH_INTERVAL_SECONDS = 1800
# How often the background prefetch thread runs (30 minutes).
# Prefetching the next item 30 minutes in advance ensures it's local before
# it plays, without churning the SD card with constant downloads.
