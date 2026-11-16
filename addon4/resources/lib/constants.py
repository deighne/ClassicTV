"""
Named constants for Classic TV 4.0.

Seek-related constants from 3.0 are gone — the Jellyfin addon handles
seeking via StartOffset, so Classic TV never calls seekTime().
"""

# ---------------------------------------------------------------------------
# Channel Bug overlay
# ---------------------------------------------------------------------------

CHANNEL_BUG_DISPLAY_SECONDS = 10
# Display duration for the channel bug overlay (approved: 10 s). Burn-in risk is
# kept low by the overlay's small footprint (360x120 px), corner position, ~40%
# opacity background, and off-white text — no sustained bright static region.

CHANNEL_STATIC_MAX_SECONDS = 4.5
# Safety upper bound for the full-screen CRT static snow shown during a tune.
# Normally dismissed after onAVStarted() + CHANNEL_STATIC_POST_AV_SECONDS.

CHANNEL_STATIC_MIN_SECONDS = 0.55
# Minimum total duration for CRT static snow during a channel change so even a
# fast local stream switch has a crisp burst of static that masks teardown.

CHANNEL_STATIC_POST_AV_SECONDS = 0.50
# Extra settle time to keep CRT static visible AFTER Kodi fires onAVStarted().
# On the Amlogic S905X, CRendererAML performs a display-plane adjustment ~480 ms
# after onAVStarted() while DVDVideoCodecAmlogic fills its reference frame queue
# at StartOffset; holding static for 0.50 s hides the initial freeze/black gap.

CHANNEL_STATIC_FRAME_COUNT = 4
# Number of flipx/flipy CRT static variants in ChannelBug.xml (5010–5013).

CHANNEL_STATIC_FRAME_INTERVAL_SECONDS = 0.055
# Interval between CRT static frame steps (~18 fps).

# ---------------------------------------------------------------------------
# Direct digit channel entry
# ---------------------------------------------------------------------------

CHANNEL_ENTRY_MAX_DIGITS = 3
CHANNEL_ENTRY_TIMEOUT_SECONDS = 1.5
CHANNEL_ENTRY_AMBIGUOUS_TIMEOUT_SECONDS = 3.5
# Used instead of the timeout above while the digits typed so far are the start
# of a longer channel number (typing "1" with channels 10-18 present). The IR
# remote is slow between presses; tuning channel 1 after 1.5s made 12 unreachable.
CHANNEL_NOT_FOUND_DISPLAY_SECONDS = 2

# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

WATCHDOG_INTERVAL_SECONDS = 10
# How often the watchdog checks whether the Jellyfin addon is still playing.
# On the Amlogic S905X this is also the maximum blank-screen duration before
# the watchdog triggers a retune.

# ---------------------------------------------------------------------------
# Startup retries
# ---------------------------------------------------------------------------

STARTUP_RETRY_MAX = 6
# 6 retries × 10s = 60s total. Covers simultaneous cold boot of both devices.

STARTUP_RETRY_INTERVAL_SECONDS = 10

# ---------------------------------------------------------------------------
# Schedule fetch
# ---------------------------------------------------------------------------

SCHEDULE_FETCH_TIMEOUT_SECONDS = 5
# Per-request HTTP timeout for GET /ClassicTV/channel/{id}.

CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS = 3
# Shorter timeout during channel changes — viewer is waiting behind static.

EOF_RETRY_MAX = 3
EOF_RETRY_INTERVAL_SECONDS = 2
EOF_OFFLINE_RETRY_INTERVAL_SECONDS = 30

# How many consecutive None-slot results (in the offline retry loop) before
# the watchdog checks whether the channel still exists in the channel list.
# A disappeared (disabled/deleted) channel triggers a tune to the first
# available channel rather than looping forever.
CHANNEL_DISAPPEARED_CHECK_INTERVAL = 3

# ---------------------------------------------------------------------------
# Amlogic hardware decoder
# ---------------------------------------------------------------------------

DECODER_RELEASE_POLL_TIMEOUT_SECONDS = 2
DECODER_RELEASE_POLL_INTERVAL_MS = 200
# After stopping playback, poll isPlaying() every 200ms for up to 2s
# waiting for the S905X decoder to release its hardware lock.

# ---------------------------------------------------------------------------
# Watchdog grace period
# ---------------------------------------------------------------------------

PLAY_GRACE_SECONDS = 30
# RunPlugin() returns immediately — the Jellyfin addon starts playback
# asynchronously. The watchdog must not fire for this many seconds after
# play_slot() is called, or it will interrupt the stream before it starts.
# 30 s, not 15: normal starts take 3-9 s, but with the Jellyfin addon busy
# (library list loading, stuck plugin scripts) starts have taken 20-90 s, and
# a replay issued at 15 s only piled another plugin script onto the jam.

# ---------------------------------------------------------------------------
# User-browse suspension (minimal Tier 2 subset)
# ---------------------------------------------------------------------------

RESUME_PENDING_GRACE_SECONDS = 5
# After browse playback ends naturally, wait this long before reclaiming the
# player. Gives the viewer a moment to pick the next thing; starting any other
# playback in this window cancels the reclaim and stays in browse mode.

STOP_SOURCE_GUARD_SECONDS = 3
# A stop caused by Classic TV itself (channel change) or by a natural episode
# EOF can fire onPlayBackStopped shortly after. A stop within this window of
# either event is NOT treated as the viewer leaving Classic TV, so the watchdog
# still advances the channel at EOF and channel changes recover normally.
# Stops outside that window suspend the watchdog (the viewer is browsing).

# ---------------------------------------------------------------------------
# Boot resilience
# ---------------------------------------------------------------------------

JELLYFIN_READY_TIMEOUT_SECONDS = 90
# Maximum time to wait for the Jellyfin server to become reachable on startup.
# Covers simultaneous cold boot of both devices over a slow home network.

JELLYFIN_READY_POLL_SECONDS = 2
# How often to poll /System/Info/Public during the readiness gate.

JELLYFIN_READY_NOTIFY_SECONDS = 8
# Minimum interval between "waiting for Jellyfin" OSD notifications, to avoid
# flooding the screen with overlapping toasts during the polling loop.

PLAYBACK_CONFIRM_TIMEOUT_SECONDS = 25
# How long to wait for onPlayBackStarted() to fire after play_slot() is called.
# Covers the worst-case Jellyfin addon stream-start latency on the S905X.

JELLYFIN_RELOAD_COOLDOWN_SECONDS = 300
# Minimum time between successive Jellyfin addon reloads. Prevents a rapid
# reload storm if the underlying problem is not the wedged addon state.

JELLYFIN_RELOAD_WAIT_SECONDS = 7
# Sleep between disabling and re-enabling the Jellyfin addon during a reload.
# Must exceed Kodi's 5-second CPythonInvoker script-stop timeout ("script didn't
# stop in 5 seconds - lets kill it") so the old service instance fully exits
# before the new one starts and populates Window(Home) auth properties.

JELLYFIN_RELOAD_JSONRPC_TIMEOUT_SECONDS = 10
# Timeout for Addons.SetAddonEnabled JSON-RPC calls. Prevents a hung RPC
# from blocking the self-heal path indefinitely.

WEDGE_FAILURE_THRESHOLD = 3
# Number of consecutive unconfirmed playback attempts (server reachable but
# onPlayBackStarted never fires) that triggers a Jellyfin addon reload.

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

SUBTITLE_STATE_LOG_DELAY_SECONDS = 3
# Kodi fills in stream properties a moment after onPlayBackStarted; wait this
# long before logging subtitle availability for the item that just started.

# ---------------------------------------------------------------------------
# Mirror channels (follow another Jellyfin user's playback)
# ---------------------------------------------------------------------------

MIRROR_POLL_INTERVAL_SECONDS = 3
# How often a tuned mirror channel re-reads the mirrored user's position.

MIRROR_DRIFT_RETUNE_SECONDS = 20
# Re-issue the play command when our position differs from theirs by more than
# this. Must stay well above normal jitter: each retune costs several seconds of
# black while the Jellyfin addon starts the stream.

MIRROR_IDLE_POLLS_BEFORE_LEAVING = 3
# Consecutive idle polls before giving up on the channel and returning to TV.
# Covers the gap between one episode ending and the next starting.

MIRROR_DEFAULT_LEAD_SECONDS = 6
MIRROR_MIN_LEAD_SECONDS = 2
MIRROR_MAX_LEAD_SECONDS = 12
# A retune starts this far ahead of the mirrored position to cover stream
# start-up (measured launch time, clamped to this range; default before the
# first measurement).

# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

STOP_BEFORE_PLAY = False
# False (current): a channel change issues play() directly and lets Kodi replace
# the running stream. Measured on the device (30 tunes, clean Kodi restart): median
# key-to-picture 4.0 s, against 7.1 s with the old sequence, and no decoder trouble,
# stalls or crashes. True: stop, wait for the Amlogic decoder to release, then play
# (the original, conservative sequence). See docs/channel-switching-performance.md
# section 7. Mirror retunes and entering browse mode still stop first.

DIRECT_HTTP_PLAYBACK = True
# True (default): play_slot() plays http://{base}/Videos/{id}/stream?static=true
# directly via xbmc.Player().play(url, listitem) with StartOffset set on the
# ListItem. Eliminates the 3.5-7.5s CPythonInvoker cold-start overhead of
# spawning plugin.video.jellyfin on every tune and episode boundary, and keeps
# 24/7 TV playback from jamming or corrupting plugin.video.jellyfin's service
# state overnight. False: legacy PlayMedia(plugin://plugin.video.jellyfin/...)
# delegation.
