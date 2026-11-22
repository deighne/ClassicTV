(() => {
  'use strict';

  const AUTH_KEY = 'classictv_web_auth';
  const LAST_CHANNEL_KEY = 'classictv_last_channel';
  const CC_PREF_KEY = 'classictv_cc_enabled';
  const STATIC_MIN_MS = 550;
  const STATIC_POST_PLAY_MS = 220;
  const BUG_DISPLAY_MS = 8000;
  const DIGIT_TIMEOUT_MS = 1500;
  const HUD_IDLE_MS = 3000;
  const MIRROR_POLL_MS = 5000;

  // DOM elements
  const loginScreen = document.getElementById('login-screen');
  const loginForm = document.getElementById('login-form');
  const serverInput = document.getElementById('server-url');
  const usernameInput = document.getElementById('username');
  const passwordInput = document.getElementById('password');
  const loginBtn = document.getElementById('login-btn');
  const qcBtn = document.getElementById('qc-btn');
  const qcBox = document.getElementById('qc-box');
  const qcLabelEl = document.getElementById('qc-label');
  const qcCodeEl = document.getElementById('qc-code');
  const loginError = document.getElementById('login-error');

  const tvStage = document.getElementById('tv-stage');
  const video = document.getElementById('tv-video');
  const staticCanvas = document.getElementById('crt-static');
  const staticCtx = staticCanvas.getContext('2d');

  const bugEl = document.getElementById('channel-bug');
  const bugNumEl = document.getElementById('bug-number');
  const bugNameEl = document.getElementById('bug-name');
  const bugTitleEl = document.getElementById('bug-title');

  const powerOverlay = document.getElementById('power-overlay');
  const powerBtn = document.getElementById('power-btn');

  const drawerEl = document.getElementById('channel-drawer');
  const drawerList = document.getElementById('drawer-list');
  const drawerClose = document.getElementById('drawer-close');

  const hudTop = document.getElementById('hud-top');
  const hudBar = document.getElementById('hud-bar');
  const hudStatus = document.getElementById('hud-status');
  const numpadEl = document.getElementById('numpad');
  const btnNumpad = document.getElementById('btn-numpad');
  const numpadClear = document.getElementById('numpad-clear');
  const numpadClose = document.getElementById('numpad-close');
  const btnChannels = document.getElementById('btn-channels');
  const btnChDown = document.getElementById('btn-ch-down');
  const btnChUp = document.getElementById('btn-ch-up');
  const btnGuide = document.getElementById('btn-guide');
  const btnCc = document.getElementById('btn-cc');
  const btnMute = document.getElementById('btn-mute');
  const btnFullscreen = document.getElementById('btn-fullscreen');
  const btnSignout = document.getElementById('btn-signout');

  const state = {
    auth: null,
    channels: [],
    currentChannel: null,
    currentSlot: null,
    hls: null,
    tuneSeq: 0,
    staticActive: false,
    staticShownAt: 0,
    staticTimer: null,
    staticRaf: null,
    staticFrameIdx: 0,
    staticLastTick: 0,
    bugTimer: null,
    hudTimer: null,
    digitBuffer: '',
    digitTimer: null,
    mirrorTimer: null,
    errorTimer: null,
    qcTimer: null,
    qcSecret: null,
    ccEnabled: localStorage.getItem(CC_PREF_KEY) === '1',
    subtitleStreams: [],
  };

  // Pre-generate 4 crisp 640x360 CRT static noise frames
  const staticFrames = (() => {
    const W = staticCanvas.width;
    const H = staticCanvas.height;
    const frames = [];
    for (let f = 0; f < 4; f++) {
      const img = staticCtx.createImageData(W, H);
      const d = img.data;
      for (let y = 0; y < H; y++) {
        const scan = (y & 1) ? 0.88 : 1.0;
        let prev = (Math.random() * 230 + 10) | 0;
        for (let x = 0; x < W; x++) {
          const r = Math.random();
          let v;
          if (r < 0.04) {
            v = (230 + Math.random() * 25) | 0;
          } else if (r < 0.08) {
            v = (Math.random() * 25) | 0;
          } else {
            const raw = (15 + Math.random() * 220) | 0;
            v = (0.82 * raw + 0.18 * prev) | 0;
          }
          prev = v;
          const out = Math.max(0, Math.min(255, (v * scan) | 0));
          const idx = (y * W + x) * 4;
          d[idx] = out;
          d[idx + 1] = out;
          d[idx + 2] = out;
          d[idx + 3] = 240;
        }
      }
      frames.push(img);
    }
    return frames;
  })();

  function defaultServerUrl() {
    if (window.ClassicTVLogin) return window.ClassicTVLogin.getJellyfinBase();
    const cfg = window.CLASSICTV_CONFIG || {};
    return (cfg.jellyfinUrl || 'https://jellyfin.example.com').replace(/\/+$/, '');
  }

  function tvApiBase() {
    const cfg = window.CLASSICTV_CONFIG || {};
    const lanHosts = cfg.lanHosts || ['localhost'];
    const h = window.location.hostname;
    if ((cfg.webHost && h === cfg.webHost) || lanHosts.includes(h)) {
      return window.location.origin;
    }
    return (state.auth && state.auth.serverUrl) || defaultServerUrl();
  }

  function getDeviceId() {
    let id = localStorage.getItem('classictv_device_id');
    if (!id) {
      id = 'ctv-web-' + Math.random().toString(36).slice(2, 10);
      localStorage.setItem('classictv_device_id', id);
    }
    return id;
  }

  function authHeader(token) {
    const base = `MediaBrowser Client="ClassicTV Web", Device="Web Browser", DeviceId="${getDeviceId()}", Version="1.0.0"`;
    return token ? `${base}, Token="${token}"` : base;
  }

  function loadStoredAuth() {
    try {
      const raw = localStorage.getItem(AUTH_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (parsed && parsed.serverUrl && parsed.accessToken && parsed.userId) {
        return parsed;
      }
    } catch (_) {}
    return null;
  }

  function saveAuth(auth) {
    state.auth = auth;
    localStorage.setItem(AUTH_KEY, JSON.stringify(auth));
  }

  function clearAuth() {
    state.auth = null;
    localStorage.removeItem(AUTH_KEY);
  }

  // CRT Static helpers
  function renderStaticTick(now) {
    if (!state.staticActive) return;
    if (now - state.staticLastTick >= 55) {
      state.staticLastTick = now;
      state.staticFrameIdx = (state.staticFrameIdx + 1) % staticFrames.length;
      staticCtx.putImageData(staticFrames[state.staticFrameIdx], 0, 0);
    }
    state.staticRaf = requestAnimationFrame(renderStaticTick);
  }

  function showStatic() {
    if (state.staticTimer) {
      clearTimeout(state.staticTimer);
      state.staticTimer = null;
    }
    state.staticActive = true;
    state.staticShownAt = performance.now();
    state.staticLastTick = 0;
    staticCanvas.classList.remove('hidden');
    if (!state.staticRaf) {
      state.staticRaf = requestAnimationFrame(renderStaticTick);
    }
  }

  function hideStatic(immediate = false) {
    if (!state.staticActive) return;
    const elapsed = performance.now() - state.staticShownAt;
    const wait = immediate ? 0 : Math.max(STATIC_POST_PLAY_MS, STATIC_MIN_MS - elapsed);
    if (state.staticTimer) {
      clearTimeout(state.staticTimer);
      state.staticTimer = null;
    }
    if (wait > 0) {
      state.staticTimer = setTimeout(() => hideStatic(true), wait);
      return;
    }
    state.staticActive = false;
    staticCanvas.classList.add('hidden');
    if (state.staticRaf) {
      cancelAnimationFrame(state.staticRaf);
      state.staticRaf = null;
    }
  }

  // Channel Bug OSD
  function showBug(numLabel, name, title = '', durationMs = BUG_DISPLAY_MS) {
    bugNumEl.textContent = numLabel;
    bugNameEl.textContent = name;
    bugTitleEl.textContent = title || '';
    bugEl.classList.remove('hidden');
    if (state.bugTimer) {
      clearTimeout(state.bugTimer);
      state.bugTimer = null;
    }
    if (durationMs > 0) {
      state.bugTimer = setTimeout(() => {
        bugEl.classList.add('hidden');
      }, durationMs);
    }
  }

  function formatChannelNumber(ch, index) {
    return ch && ch.Number > 0 ? ch.Number : index + 1;
  }

  function sortChannels(raw) {
    return raw.slice().sort((a, b) => {
      const na = a.Number > 0 ? a.Number : 9999;
      const nb = b.Number > 0 ? b.Number : 9999;
      return na - nb;
    });
  }

  function renderDrawer() {
    drawerList.innerHTML = '';
    state.channels.forEach((ch, idx) => {
      const li = document.createElement('li');
      li.className = 'drawer-item';
      li.dataset.channelId = ch.Id;
      if (state.currentChannel && state.currentChannel.Id === ch.Id) {
        li.classList.add('active');
      }
      const num = document.createElement('span');
      num.className = 'drawer-num';
      num.textContent = String(formatChannelNumber(ch, idx));
      const info = document.createElement('div');
      info.className = 'drawer-info';
      const name = document.createElement('span');
      name.className = 'drawer-name';
      name.textContent = ch.Name;
      const now = document.createElement('span');
      now.className = 'drawer-now';
      now.textContent = ch._nowPlaying || '';
      info.appendChild(name);
      info.appendChild(now);
      li.appendChild(num);
      li.appendChild(info);
      li.addEventListener('click', () => {
        drawerEl.classList.add('hidden');
        tuneChannel(ch);
      });
      drawerList.appendChild(li);
    });
  }

  function tvFetchOptions() {
    const token = state.auth && state.auth.accessToken;
    return token ? { headers: { Authorization: authHeader(token) } } : {};
  }

  async function refreshDrawerGuide() {
    await Promise.allSettled(
      state.channels.map(async (ch) => {
        try {
          const r = await fetch(`${tvApiBase()}/ClassicTV/channel/${encodeURIComponent(ch.Id)}`, tvFetchOptions());
          if (!r.ok) return;
          const slot = await r.json();
          ch._nowPlaying = slot.EpisodeTitle && slot.EpisodeTitle !== slot.Title
            ? `${slot.Title} — ${slot.EpisodeTitle}`
            : (slot.Title || '');
        } catch (_) {}
      })
    );
    if (!drawerEl.classList.contains('hidden')) {
      renderDrawer();
    }
  }

  function teardownPlayback() {
    if (state.mirrorTimer) {
      clearInterval(state.mirrorTimer);
      state.mirrorTimer = null;
    }
    if (state.errorTimer) {
      clearTimeout(state.errorTimer);
      state.errorTimer = null;
    }
    if (state.hls) {
      state.hls.destroy();
      state.hls = null;
    }
    while (video.firstChild) {
      video.removeChild(video.firstChild);
    }
    video.removeAttribute('src');
    video.load();
    state.subtitleStreams = [];
    btnCc.classList.add('hidden');
    btnCc.classList.remove('active');
  }

  function subtitleScore(s) {
    const codec = String(s.Codec || '').toLowerCase();
    const isText = s.IsTextSubtitleStream === true ||
      ['srt', 'subrip', 'vtt', 'webvtt', 'ass', 'ssa', 'smi', 'ttml'].includes(codec);
    const lang = String(s.Language || '').toLowerCase();
    const isEng = lang === 'eng' || lang === 'en' || lang.startsWith('en-');
    return (isText ? 4 : 0) + (isEng ? 2 : 0) + (s.IsDefault ? 1 : 0);
  }

  function attachSubtitles(itemId, sourceId, mediaStreams) {
    const subs = (mediaStreams || [])
      .filter((s) => s.Type === 'Subtitle')
      .sort((a, b) => subtitleScore(b) - subtitleScore(a));
    state.subtitleStreams = subs;
    if (subs.length === 0) {
      btnCc.classList.add('hidden');
      return;
    }
    btnCc.classList.remove('hidden');
    btnCc.classList.toggle('active', state.ccEnabled);

    const { serverUrl, accessToken } = state.auth;
    subs.forEach((s, idx) => {
      const track = document.createElement('track');
      track.kind = 'subtitles';
      track.label = s.DisplayTitle || s.Language || `Track ${idx + 1}`;
      track.srclang = s.Language || 'en';
      track.src = `${serverUrl}/Videos/${encodeURIComponent(itemId)}/${encodeURIComponent(sourceId)}/Subtitles/${s.Index}/Stream.vtt?api_key=${encodeURIComponent(accessToken)}`;
      track.default = state.ccEnabled && idx === 0;
      video.appendChild(track);
    });

    const applyTrackMode = () => {
      for (let i = 0; i < video.textTracks.length; i++) {
        video.textTracks[i].mode = (state.ccEnabled && i === 0) ? 'showing' : 'disabled';
      }
    };
    setTimeout(applyTrackMode, 50);
  }

  function browserDeviceProfile() {
    return {
      MaxStreamingBitrate: 40000000,
      MaxStaticBitrate: 40000000,
      DirectPlayProfiles: [
        { Container: 'mp4,m4v,mov', Type: 'Video', VideoCodec: 'h264', AudioCodec: 'aac,mp3,opus' },
        { Container: 'webm', Type: 'Video', VideoCodec: 'vp8,vp9,av1', AudioCodec: 'opus,vorbis' },
      ],
      TranscodingProfiles: [
        {
          Container: 'ts',
          Type: 'Video',
          VideoCodec: 'h264',
          AudioCodec: 'aac,mp3',
          Protocol: 'hls',
          Context: 'Streaming',
          MaxAudioChannels: '2',
          MinSegments: 1,
          BreakOnNonKeyFrames: true,
        },
      ],
      SubtitleProfiles: [
        { Format: 'vtt', Method: 'External' },
      ],
    };
  }

  async function fetchPlaybackInfo(itemId) {
    const { serverUrl, accessToken, userId } = state.auth;
    const url = `${serverUrl}/Items/${encodeURIComponent(itemId)}/PlaybackInfo?UserId=${encodeURIComponent(userId)}&IsPlayback=true&AutoOpenLiveStream=true`;
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: authHeader(accessToken),
      },
      body: JSON.stringify({
        DeviceProfile: browserDeviceProfile(),
        UserId: userId,
        IsPlayback: true,
        AutoOpenLiveStream: true,
        MaxStreamingBitrate: 40000000,
      }),
    });
    if (res.status === 401) {
      signOut();
      throw new Error('Session expired');
    }
    if (!res.ok) {
      throw new Error(`PlaybackInfo HTTP ${res.status}`);
    }
    return res.json();
  }

  async function attemptPlay() {
    try {
      await video.play();
      powerOverlay.classList.add('hidden');
    } catch (err) {
      if (err && err.name === 'NotAllowedError') {
        powerOverlay.classList.remove('hidden');
      } else {
        console.error('ClassicTV Web: play() error', err);
      }
    }
  }

  function playDirectUrl(url, offsetSeconds) {
    return new Promise((resolve) => {
      const seq = state.tuneSeq;
      let settled = false;
      let seekDone = !(offsetSeconds > 0);
      let attempts = 0;

      const targetTime = () => {
        if (!(offsetSeconds > 0)) return 0;
        if (Number.isFinite(video.duration) && video.duration > 1) {
          return Math.min(offsetSeconds, Math.max(0, video.duration - 1));
        }
        return offsetSeconds;
      };

      const cleanupSeek = () => {
        video.removeEventListener('loadeddata', trySeek);
        video.removeEventListener('canplay', trySeek);
        video.removeEventListener('playing', trySeek);
        video.removeEventListener('seeked', onSeeked);
      };

      const trySeek = () => {
        if (seq !== state.tuneSeq || seekDone) return;
        const target = targetTime();
        if (Math.abs(video.currentTime - target) <= 3) {
          seekDone = true;
          cleanupSeek();
          return;
        }
        if (attempts >= 6) {
          cleanupSeek();
          return;
        }
        attempts += 1;
        try {
          video.currentTime = target;
        } catch (_) {}
      };

      const onSeeked = () => {
        if (seq !== state.tuneSeq) return;
        const target = targetTime();
        if (Math.abs(video.currentTime - target) <= 3) {
          seekDone = true;
          cleanupSeek();
        }
      };

      const onLoaded = () => {
        video.removeEventListener('loadedmetadata', onLoaded);
        if (seq !== state.tuneSeq) {
          cleanupSeek();
          if (!settled) { settled = true; resolve(); }
          return;
        }
        trySeek();
        attemptPlay()
          .then(() => { trySeek(); })
          .finally(() => {
            if (!settled) { settled = true; resolve(); }
          });
      };

      video.addEventListener('loadedmetadata', onLoaded);
      if (!seekDone) {
        video.addEventListener('loadeddata', trySeek);
        video.addEventListener('canplay', trySeek);
        video.addEventListener('playing', trySeek);
        video.addEventListener('seeked', onSeeked);
      }
      video.src = url;
    });
  }

  function playHlsUrl(url, offsetSeconds) {
    if (window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls({
        startPosition: offsetSeconds > 0 ? offsetSeconds : -1,
        maxBufferLength: 30,
      });
      state.hls = hls;
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        attemptPlay();
      });
      hls.on(window.Hls.Events.ERROR, (_, data) => {
        if (data && data.fatal) {
          console.error('ClassicTV Web: fatal HLS error', data);
          if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
            hls.startLoad();
          } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError();
          }
        }
      });
      return;
    }
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      playDirectUrl(url, offsetSeconds);
      return;
    }
    hudStatus.textContent = 'HLS is not supported in this browser.';
    hideStatic(true);
  }

  function startMirrorPolling(channel) {
    if (state.mirrorTimer) clearInterval(state.mirrorTimer);
    state.mirrorTimer = setInterval(async () => {
      if (!state.currentChannel || state.currentChannel.Id !== channel.Id) return;
      try {
        const res = await fetch(`${tvApiBase()}/ClassicTV/channel/${encodeURIComponent(channel.Id)}`, tvFetchOptions());
        if (!res.ok) return;
        const slot = await res.json();
        if (!state.currentSlot || slot.JellyfinItemId !== state.currentSlot.JellyfinItemId) {
          tuneChannel(channel);
          return;
        }
        if (slot.Paused && !video.paused) {
          video.pause();
        } else if (!slot.Paused && video.paused) {
          attemptPlay();
        }
        if (!slot.Paused && typeof slot.StartOffset === 'number') {
          const drift = Math.abs(video.currentTime - slot.StartOffset);
          if (drift > 4) {
            video.currentTime = slot.StartOffset;
          }
        }
      } catch (_) {}
    }, MIRROR_POLL_MS);
  }

  async function tuneChannel(channel) {
    if (!channel) return;
    const seq = ++state.tuneSeq;
    const idx = state.channels.findIndex((c) => c.Id === channel.Id);
    const chNum = formatChannelNumber(channel, idx >= 0 ? idx : 0);

    state.currentChannel = channel;
    localStorage.setItem(LAST_CHANNEL_KEY, channel.Id);
    renderDrawer();

    showStatic();
    showBug(String(chNum), channel.Name, 'Tuning…');
    hudStatus.textContent = `CH ${chNum} · ${channel.Name}`;
    teardownPlayback();

    try {
      const slotRes = await fetch(`${tvApiBase()}/ClassicTV/channel/${encodeURIComponent(channel.Id)}`, tvFetchOptions());
      if (seq !== state.tuneSeq) return;
      if (!slotRes.ok) {
        throw new Error(`Channel offline (HTTP ${slotRes.status})`);
      }
      const slot = await slotRes.json();
      if (seq !== state.tuneSeq) return;
      state.currentSlot = slot;

      const itemId = slot.JellyfinItemId;
      const offset = slot.StartOffset || 0;
      if (!itemId) {
        throw new Error('No item scheduled right now');
      }

      const labelTitle = slot.EpisodeTitle && slot.EpisodeTitle !== slot.Title
        ? `${slot.Title} — ${slot.EpisodeTitle}`
        : (slot.Title || channel.Name);
      showBug(String(chNum), channel.Name, labelTitle);
      hudStatus.textContent = `CH ${chNum} · ${channel.Name} — ${labelTitle}`;

      // Pre-rendered Channel 1 TV Guide MP4
      if (itemId.startsWith('guide-')) {
        const guideUrl = `${tvApiBase()}/ClassicTV/guide/stream/${encodeURIComponent(itemId)}`;
        await playDirectUrl(guideUrl, offset);
        return;
      }

      const info = await fetchPlaybackInfo(itemId);
      if (seq !== state.tuneSeq) return;
      const source = info && info.MediaSources && info.MediaSources[0];
      if (!source) {
        throw new Error('No playable media source returned');
      }

      attachSubtitles(itemId, source.Id, source.MediaStreams);
      const { serverUrl, accessToken } = state.auth;

      if (source.SupportsDirectPlay) {
        const directUrl = `${serverUrl}/Videos/${encodeURIComponent(itemId)}/stream?static=true&MediaSourceId=${encodeURIComponent(source.Id)}&api_key=${encodeURIComponent(accessToken)}`;
        await playDirectUrl(directUrl, offset);
      } else if (source.TranscodingUrl) {
        const transUrl = source.TranscodingUrl.startsWith('http')
          ? source.TranscodingUrl
          : `${serverUrl}${source.TranscodingUrl}`;
        playHlsUrl(transUrl, offset);
      } else {
        const hlsUrl = `${serverUrl}/Videos/${encodeURIComponent(itemId)}/master.m3u8?MediaSourceId=${encodeURIComponent(source.Id)}&VideoCodec=h264&AudioCodec=aac&VideoBitrate=12000000&AudioBitrate=320000&api_key=${encodeURIComponent(accessToken)}`;
        playHlsUrl(hlsUrl, offset);
      }

      if (channel.Mirror) {
        startMirrorPolling(channel);
      }
    } catch (err) {
      if (seq !== state.tuneSeq) return;
      console.error('ClassicTV Web: tune error', err);
      showBug(String(chNum), channel.Name, err.message || 'Channel offline');
      hudStatus.textContent = `CH ${chNum} · ${err.message || 'Channel offline'}`;
      hideStatic(true);
    }
  }

  function stepChannel(delta) {
    if (state.channels.length === 0) return;
    const idx = state.currentChannel
      ? state.channels.findIndex((c) => c.Id === state.currentChannel.Id)
      : 0;
    const nextIdx = (idx + delta + state.channels.length) % state.channels.length;
    tuneChannel(state.channels[nextIdx]);
  }

  function handleDigitKey(digit) {
    state.digitBuffer += digit;
    showBug(`${state.digitBuffer}_`, 'Channel Entry', '', 2500);
    if (state.digitTimer) clearTimeout(state.digitTimer);
    state.digitTimer = setTimeout(() => {
      const targetNum = parseInt(state.digitBuffer, 10);
      state.digitBuffer = '';
      const match = state.channels.find((ch, idx) => formatChannelNumber(ch, idx) === targetNum);
      if (match) {
        tuneChannel(match);
      } else {
        showBug(String(targetNum), 'Not Found', '', 2000);
      }
    }, DIGIT_TIMEOUT_MS);
  }

  function bumpHud() {
    hudTop.classList.remove('idle');
    hudBar.classList.remove('idle');
    if (state.hudTimer) clearTimeout(state.hudTimer);
    state.hudTimer = setTimeout(() => {
      if (drawerEl.classList.contains('hidden') && numpadEl.classList.contains('hidden')) {
        hudTop.classList.add('idle');
        hudBar.classList.add('idle');
      }
    }, HUD_IDLE_MS);
  }

  async function startSession() {
    loginScreen.classList.add('hidden');
    tvStage.classList.remove('hidden');
    bumpHud();

    try {
      const res = await fetch(`${tvApiBase()}/ClassicTV/channels`, tvFetchOptions());
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      state.channels = sortChannels(data.Channels || []);
      renderDrawer();

      if (state.channels.length === 0) {
        hudStatus.textContent = 'No Classic TV channels enabled.';
        return;
      }

      const requestedId = new URLSearchParams(window.location.search).get('channel');
      const lastId = localStorage.getItem(LAST_CHANNEL_KEY);
      const initial = state.channels.find((c) => c.Id === requestedId)
        || state.channels.find((c) => c.Id === lastId)
        || state.channels[0];
      tuneChannel(initial);
    } catch (err) {
      hudStatus.textContent = `Failed to load channels: ${err.message}`;
    }
  }

  function signOut() {
    teardownPlayback();
    hideStatic(true);
    clearAuth();
    tvStage.classList.add('hidden');
    loginScreen.classList.remove('hidden');
    serverInput.value = defaultServerUrl();
  }

  // Event listeners
  video.addEventListener('playing', () => hideStatic(false));
  video.addEventListener('ended', () => {
    if (state.currentChannel) {
      tuneChannel(state.currentChannel);
    }
  });
  video.addEventListener('error', () => {
    if (!video.getAttribute('src') || !state.currentChannel || state.errorTimer) return;
    const failedSeq = state.tuneSeq;
    const failedChannel = state.currentChannel;
    state.errorTimer = setTimeout(() => {
      state.errorTimer = null;
      if (state.tuneSeq === failedSeq && state.currentChannel && state.currentChannel.Id === failedChannel.Id) {
        tuneChannel(failedChannel);
      }
    }, 2000);
  });

  function updateMuteButton() {
    const muted = Boolean(video.muted);
    btnMute.classList.toggle('is-muted', muted);
    btnMute.setAttribute('aria-label', muted ? 'Unmute' : 'Mute');
    btnMute.setAttribute('title', muted ? 'Unmute (M)' : 'Mute (M)');
  }

  powerBtn.addEventListener('click', () => {
    video.muted = false;
    updateMuteButton();
    attemptPlay();
  });

  video.addEventListener('click', () => {
    if (hudBar.classList.contains('idle')) {
      bumpHud();
    } else {
      hudTop.classList.add('idle');
      hudBar.classList.add('idle');
    }
  });

  btnChUp.addEventListener('click', () => stepChannel(1));
  btnChDown.addEventListener('click', () => stepChannel(-1));
  btnGuide.addEventListener('click', () => {
    const guide = state.channels.find((c, idx) => formatChannelNumber(c, idx) === 1 || c.Id === 'guide');
    if (guide) tuneChannel(guide);
  });
  btnChannels.addEventListener('click', () => {
    drawerEl.classList.toggle('hidden');
    numpadEl.classList.add('hidden');
    if (!drawerEl.classList.contains('hidden')) {
      refreshDrawerGuide();
    }
    bumpHud();
  });
  drawerClose.addEventListener('click', () => drawerEl.classList.add('hidden'));

  btnNumpad.addEventListener('click', () => {
    numpadEl.classList.toggle('hidden');
    bumpHud();
  });
  numpadClose.addEventListener('click', () => numpadEl.classList.add('hidden'));
  numpadClear.addEventListener('click', () => {
    state.digitBuffer = '';
    if (state.digitTimer) clearTimeout(state.digitTimer);
    bugEl.classList.add('hidden');
  });
  numpadEl.querySelectorAll('[data-digit]').forEach((btn) => {
    btn.addEventListener('click', () => {
      handleDigitKey(btn.getAttribute('data-digit'));
      bumpHud();
    });
  });

  btnMute.addEventListener('click', () => {
    video.muted = !video.muted;
    updateMuteButton();
  });
  video.addEventListener('volumechange', updateMuteButton);
  updateMuteButton();

  btnCc.addEventListener('click', () => {
    state.ccEnabled = !state.ccEnabled;
    localStorage.setItem(CC_PREF_KEY, state.ccEnabled ? '1' : '0');
    btnCc.classList.toggle('active', state.ccEnabled);
    for (let i = 0; i < video.textTracks.length; i++) {
      video.textTracks[i].mode = (state.ccEnabled && i === 0) ? 'showing' : 'disabled';
    }
  });

  function isFullscreen() {
    return Boolean(document.fullscreenElement || document.webkitFullscreenElement);
  }

  function updateFullscreenButton() {
    const fs = isFullscreen();
    btnFullscreen.classList.toggle('is-fullscreen', fs);
    btnFullscreen.setAttribute('aria-label', fs ? 'Exit Fullscreen' : 'Fullscreen');
    btnFullscreen.setAttribute('title', fs ? 'Exit Fullscreen (F)' : 'Fullscreen (F)');
  }

  document.addEventListener('fullscreenchange', updateFullscreenButton);
  document.addEventListener('webkitfullscreenchange', updateFullscreenButton);

  btnFullscreen.addEventListener('click', () => {
    const target = tvStage || document.documentElement;
    if (isFullscreen()) {
      const exitFn = document.exitFullscreen || document.webkitExitFullscreen;
      if (exitFn) Promise.resolve(exitFn.call(document)).catch(() => {});
      return;
    }
    if (typeof target.requestFullscreen === 'function') {
      Promise.resolve(target.requestFullscreen()).catch(() => {
        if (typeof video.webkitEnterFullscreen === 'function') {
          try { video.webkitEnterFullscreen(); } catch (_) {}
        }
      });
    } else if (typeof target.webkitRequestFullscreen === 'function') {
      target.webkitRequestFullscreen();
    } else if (typeof video.webkitEnterFullscreen === 'function') {
      video.webkitEnterFullscreen();
    }
  });

  btnSignout.addEventListener('click', signOut);

  document.addEventListener('mousemove', bumpHud);
  document.addEventListener('touchstart', bumpHud, { passive: true });
  document.addEventListener('keydown', (e) => {
    if (!loginScreen.classList.contains('hidden')) return;
    bumpHud();
    if (e.key >= '0' && e.key <= '9') {
      handleDigitKey(e.key);
    } else if (e.key === 'ArrowUp' || e.key === 'PageUp') {
      e.preventDefault();
      stepChannel(1);
    } else if (e.key === 'ArrowDown' || e.key === 'PageDown') {
      e.preventDefault();
      stepChannel(-1);
    } else if (e.key === 'm' || e.key === 'M') {
      btnMute.click();
    } else if (e.key === 'f' || e.key === 'F') {
      btnFullscreen.click();
    } else if (e.key === 'g' || e.key === 'G') {
      btnGuide.click();
    } else if (e.key === 'c' || e.key === 'C') {
      if (!btnCc.classList.contains('hidden')) btnCc.click();
    } else if (e.key === 'i' || e.key === 'I') {
      if (state.currentChannel && state.currentSlot) {
        const idx = state.channels.findIndex((c) => c.Id === state.currentChannel.Id);
        const num = formatChannelNumber(state.currentChannel, idx >= 0 ? idx : 0);
        const title = state.currentSlot.EpisodeTitle && state.currentSlot.EpisodeTitle !== state.currentSlot.Title
          ? `${state.currentSlot.Title} — ${state.currentSlot.EpisodeTitle}`
          : (state.currentSlot.Title || '');
        showBug(String(num), state.currentChannel.Name, title);
      }
    } else if (e.key === 'Escape') {
      drawerEl.classList.add('hidden');
      numpadEl.classList.add('hidden');
    }
  });

  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    loginError.textContent = '';
    loginBtn.disabled = true;
    const serverUrl = serverInput.value.trim().replace(/\/+$/, '');
    const username = usernameInput.value.trim();
    const password = passwordInput.value;

    try {
      const data = await window.ClassicTVLogin.authenticateByName(serverUrl, authHeader(''), username, password);
      saveAuth({
        serverUrl,
        accessToken: data.AccessToken,
        userId: data.User && data.User.Id,
        username: data.User && data.User.Name,
        deviceId: getDeviceId(),
      });
      startSession();
    } catch (err) {
      loginError.textContent = err.message || 'Sign-in failed.';
    } finally {
      loginBtn.disabled = false;
    }
  });

  const qcController = window.ClassicTVLogin.bindQuickConnect({
    getServerUrl: () => serverInput.value || defaultServerUrl(),
    getAuthHeader: () => authHeader(''),
    qcBtn,
    qcBox,
    qcCodeEl,
    qcHintEl: qcLabelEl,
    errEl: loginError,
    idleButtonText: 'Sign In with Quick Connect',
    idleHintText: 'ENTER CODE IN JELLYFIN → QUICK CONNECT (CLICK TO COPY)',
    onSuccess: (data, serverUrl) => {
      saveAuth({
        serverUrl,
        accessToken: data.AccessToken,
        userId: data.User && data.User.Id,
        username: data.User && data.User.Name,
        deviceId: getDeviceId(),
      });
      startSession();
    },
  });

  function stopQuickConnect() {
    qcController.stop();
  }

  // Boot
  const stored = loadStoredAuth();
  if (stored) {
    state.auth = stored;
    startSession();
  } else {
    serverInput.value = defaultServerUrl();
    loginScreen.classList.remove('hidden');
  }
})();
