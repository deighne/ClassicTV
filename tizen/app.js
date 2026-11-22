(() => {
  'use strict';

  const AUTH_KEY = 'classictv_tizen_auth';
  const LAST_CHANNEL_KEY = 'classictv_tizen_last_channel';
  const CC_PREF_KEY = 'classictv_tizen_cc_enabled';
  const DEVICE_ID_KEY = 'classictv_tizen_device_id';

  const STATIC_MIN_MS = 550;
  const STATIC_POST_PLAY_MS = 220;
  const BUG_DISPLAY_MS = 8000;
  const DIGIT_TIMEOUT_MS = 1500;
  const HINT_IDLE_MS = 4500;
  const MIRROR_POLL_MS = 5000;
  const EXIT_CONFIRM_MS = 2500;

  // Tizen remote key codes
  const KEY = {
    LEFT: 37,
    UP: 38,
    RIGHT: 39,
    DOWN: 40,
    ENTER: 13,
    ESCAPE: 27,
    RETURN: 10009,
    CH_UP: 427,
    CH_DOWN: 428,
    PAGE_UP: 33,
    PAGE_DOWN: 34,
    GUIDE: 458,
    INFO: 457,
    CAPTION: 10221,
    MEDIA_PLAY_PAUSE: 10252,
    MEDIA_PLAY: 415,
    MEDIA_PAUSE: 19,
    MEDIA_STOP: 413,
    RED: 403,
  };

  // DOM
  const loginScreen = document.getElementById('login-screen');
  const loginForm = document.getElementById('login-form');
  const serverInput = document.getElementById('server-url');
  const usernameInput = document.getElementById('username');
  const passwordInput = document.getElementById('password');
  const loginBtn = document.getElementById('login-btn');
  const qcBtn = document.getElementById('qc-btn');
  const qcBox = document.getElementById('qc-box');
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

  const osdHint = document.getElementById('osd-hint');
  const exitToast = document.getElementById('exit-toast');

  const drawerEl = document.getElementById('channel-drawer');
  const drawerList = document.getElementById('drawer-list');
  const drawerCc = document.getElementById('drawer-cc');
  const drawerSignout = document.getElementById('drawer-signout');

  const state = {
    auth: null,
    channels: [],
    currentChannel: null,
    currentSlot: null,
    tuneSeq: 0,
    staticActive: false,
    staticShownAt: 0,
    staticTimer: null,
    staticRaf: null,
    staticFrameIdx: 0,
    staticLastTick: 0,
    bugTimer: null,
    hintTimer: null,
    digitBuffer: '',
    digitTimer: null,
    mirrorTimer: null,
    retuneTimer: null,
    qcTimer: null,
    qcSecret: null,
    loadedMetadataHandler: null,
    exitArmedUntil: 0,
    exitTimer: null,
    ccEnabled: localStorage.getItem(CC_PREF_KEY) === '1',
    subtitleStreams: [],
    subtitleBlobUrls: [],
  };

  // Pre-generate 4 crisp 640x360 CRT static frames
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

  function registerTizenKeys() {
    try {
      if (window.tizen && window.tizen.tvinputdevice) {
        const keys = [
          'ChannelUp',
          'ChannelDown',
          'Guide',
          'Info',
          'Caption',
          'MediaPlayPause',
          'MediaPlay',
          'MediaPause',
          'MediaStop',
          'ColorF0Red',
          '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
        ];
        // Register individually so one unsupported key on a TV model doesn't abort the rest
        keys.forEach((k) => {
          try {
            window.tizen.tvinputdevice.registerKey(k);
          } catch (_) {}
        });
      }
    } catch (_) {}
  }

  function exitTizenApp() {
    try {
      if (window.tizen && window.tizen.application) {
        window.tizen.application.getCurrentApplication().exit();
      }
    } catch (_) {}
  }

  function getDeviceId() {
    let id = localStorage.getItem(DEVICE_ID_KEY);
    if (!id) {
      id = 'ctv-tizen-' + Math.random().toString(36).slice(2, 10);
      localStorage.setItem(DEVICE_ID_KEY, id);
    }
    return id;
  }

  function authHeader(token) {
    const base = `MediaBrowser Client="ClassicTV Tizen", Device="Samsung Smart TV", DeviceId="${getDeviceId()}", Version="1.0.0"`;
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

  function tvApiBase() {
    return (state.auth && state.auth.serverUrl) || 'https://jellyfin.example.com';
  }

  // CRT Static
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

  function flashHint() {
    osdHint.classList.remove('hidden');
    if (state.hintTimer) clearTimeout(state.hintTimer);
    state.hintTimer = setTimeout(() => {
      osdHint.classList.add('hidden');
    }, HINT_IDLE_MS);
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
      li.tabIndex = 0;
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
        closeDrawer();
        tuneChannel(ch);
      });
      drawerList.appendChild(li);
    });
  }

  async function refreshDrawerGuide() {
    await Promise.allSettled(
      state.channels.map(async (ch) => {
        try {
          const r = await fetch(`${tvApiBase()}/ClassicTV/channel/${encodeURIComponent(ch.Id)}`);
          if (!r.ok) return;
          const slot = await r.json();
          ch._nowPlaying = slot.EpisodeTitle && slot.EpisodeTitle !== slot.Title
            ? `${slot.Title} — ${slot.EpisodeTitle}`
            : (slot.Title || '');
        } catch (_) {}
      })
    );
    if (!drawerEl.classList.contains('hidden')) {
      state.channels.forEach((ch) => {
        const li = drawerList.querySelector(`[data-channel-id="${ch.Id}"]`);
        const nowEl = li && li.querySelector('.drawer-now');
        if (nowEl) nowEl.textContent = ch._nowPlaying || '';
      });
    }
  }

  function openDrawer() {
    drawerEl.classList.remove('hidden');
    drawerSignout.tabIndex = 0;
    renderDrawer();
    const active = drawerList.querySelector('.drawer-item.active') || drawerList.firstElementChild;
    if (active) active.focus();
    refreshDrawerGuide();
  }

  function closeDrawer() {
    drawerEl.classList.add('hidden');
    drawerSignout.tabIndex = -1;
    if (document.activeElement && document.activeElement.blur) {
      document.activeElement.blur();
    }
  }

  function teardownPlayback() {
    if (state.mirrorTimer) {
      clearInterval(state.mirrorTimer);
      state.mirrorTimer = null;
    }
    if (state.retuneTimer) {
      clearTimeout(state.retuneTimer);
      state.retuneTimer = null;
    }
    if (state.loadedMetadataHandler) {
      video.removeEventListener('loadedmetadata', state.loadedMetadataHandler);
      state.loadedMetadataHandler = null;
    }
    while (video.firstChild) {
      video.removeChild(video.firstChild);
    }
    video.removeAttribute('src');
    video.load();
    state.subtitleStreams = [];
    if (state.subtitleBlobUrls.length > 0) {
      state.subtitleBlobUrls.forEach((u) => {
        try { URL.revokeObjectURL(u); } catch (_) {}
      });
      state.subtitleBlobUrls = [];
    }
  }

  function updateCcUi() {
    if (!drawerCc) return;
    drawerCc.textContent = state.ccEnabled ? 'CC: ON' : 'CC: OFF';
    drawerCc.classList.toggle('active', state.ccEnabled);
  }

  function applySubtitleTrackMode() {
    for (let i = 0; i < video.textTracks.length; i++) {
      video.textTracks[i].mode = (state.ccEnabled && i === 0) ? 'showing' : 'disabled';
    }
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
    if (subs.length === 0) return;

    const { serverUrl, accessToken } = state.auth;
    const seq = state.tuneSeq;
    subs.forEach((s, idx) => {
      const track = document.createElement('track');
      track.kind = 'subtitles';
      track.label = s.DisplayTitle || s.Language || `Track ${idx + 1}`;
      track.srclang = s.Language || 'en';
      const vttUrl = `${serverUrl}/Videos/${encodeURIComponent(itemId)}/${encodeURIComponent(sourceId)}/Subtitles/${s.Index}/Stream.vtt?api_key=${encodeURIComponent(accessToken)}`;
      track.default = state.ccEnabled && idx === 0;
      video.appendChild(track);

      // Fetch VTT via XHR/fetch -> blob URL so Tizen file:// origin never blocks <track>
      fetch(vttUrl)
        .then((res) => (res.ok ? res.text() : Promise.reject(new Error('vtt'))))
        .then((vttText) => {
          if (seq !== state.tuneSeq) return;
          const blobUrl = URL.createObjectURL(new Blob([vttText], { type: 'text/vtt' }));
          state.subtitleBlobUrls.push(blobUrl);
          track.src = blobUrl;
          applySubtitleTrackMode();
        })
        .catch(() => {
          if (seq !== state.tuneSeq) return;
          track.src = vttUrl;
          applySubtitleTrackMode();
        });
    });

    video.addEventListener('loadedmetadata', applySubtitleTrackMode, { once: true });
    setTimeout(applySubtitleTrackMode, 60);
  }

  function toggleSubtitles() {
    state.ccEnabled = !state.ccEnabled;
    localStorage.setItem(CC_PREF_KEY, state.ccEnabled ? '1' : '0');
    updateCcUi();
    applySubtitleTrackMode();
    if (state.currentChannel) {
      const idx = state.channels.findIndex((c) => c.Id === state.currentChannel.Id);
      const chNum = formatChannelNumber(state.currentChannel, idx >= 0 ? idx : 0);
      showBug(`CH ${chNum}`, state.currentChannel.Name, state.ccEnabled ? 'CC: ON' : 'CC: OFF', 2500);
    }
  }

  function tizenDeviceProfile() {
    return {
      MaxStreamingBitrate: 40000000,
      MaxStaticBitrate: 40000000,
      DirectPlayProfiles: [
        { Container: 'mp4,m4v,mov,mkv', Type: 'Video', VideoCodec: 'h264,hevc', AudioCodec: 'aac,ac3,eac3,mp3' },
        { Container: 'ts', Type: 'Video', VideoCodec: 'h264,hevc', AudioCodec: 'aac,ac3,mp3' },
      ],
      TranscodingProfiles: [
        {
          Container: 'ts',
          Type: 'Video',
          VideoCodec: 'h264',
          AudioCodec: 'aac,mp3',
          Protocol: 'hls',
          Context: 'Streaming',
          MaxAudioChannels: '6',
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
        DeviceProfile: tizenDeviceProfile(),
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

  function playUrl(url, offsetSeconds) {
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

      const cleanupSeek = () => {
        video.removeEventListener('loadeddata', trySeek);
        video.removeEventListener('canplay', trySeek);
        video.removeEventListener('playing', trySeek);
        video.removeEventListener('seeked', onSeeked);
      };

      const onLoaded = () => {
        video.removeEventListener('loadedmetadata', onLoaded);
        if (state.loadedMetadataHandler === onLoaded) {
          state.loadedMetadataHandler = null;
        }
        if (seq !== state.tuneSeq) {
          cleanupSeek();
          if (!settled) { settled = true; resolve(); }
          return;
        }
        trySeek();
        video.play()
          .then(() => { trySeek(); })
          .catch((err) => console.error('ClassicTV Tizen play error:', err))
          .finally(() => {
            if (!settled) { settled = true; resolve(); }
          });
      };

      state.loadedMetadataHandler = onLoaded;
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

  function startMirrorPolling(channel) {
    if (state.mirrorTimer) clearInterval(state.mirrorTimer);
    state.mirrorTimer = setInterval(async () => {
      if (!state.currentChannel || state.currentChannel.Id !== channel.Id) return;
      try {
        const res = await fetch(`${tvApiBase()}/ClassicTV/channel/${encodeURIComponent(channel.Id)}`);
        if (!res.ok) return;
        const slot = await res.json();
        if (!state.currentSlot || slot.JellyfinItemId !== state.currentSlot.JellyfinItemId) {
          tuneChannel(channel);
          return;
        }
        if (slot.Paused && !video.paused) {
          video.pause();
        } else if (!slot.Paused && video.paused) {
          video.play().catch(() => {});
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

    showStatic();
    showBug(`CH ${chNum}`, channel.Name, 'Tuning…');
    flashHint();
    teardownPlayback();

    try {
      const slotRes = await fetch(`${tvApiBase()}/ClassicTV/channel/${encodeURIComponent(channel.Id)}`);
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
        throw new Error('No item scheduled');
      }

      const labelTitle = slot.EpisodeTitle && slot.EpisodeTitle !== slot.Title
        ? `${slot.Title} — ${slot.EpisodeTitle}`
        : (slot.Title || channel.Name);
      showBug(`CH ${chNum}`, channel.Name, labelTitle);

      if (itemId.startsWith('guide-')) {
        const guideUrl = `${tvApiBase()}/ClassicTV/guide/stream/${encodeURIComponent(itemId)}`;
        await playUrl(guideUrl, offset);
        return;
      }

      const info = await fetchPlaybackInfo(itemId);
      if (seq !== state.tuneSeq) return;
      const source = info && info.MediaSources && info.MediaSources[0];
      if (!source) {
        throw new Error('No playable media source');
      }

      attachSubtitles(itemId, source.Id, source.MediaStreams);
      const { serverUrl, accessToken } = state.auth;

      if (source.SupportsDirectPlay) {
        const directUrl = `${serverUrl}/Videos/${encodeURIComponent(itemId)}/stream?static=true&MediaSourceId=${encodeURIComponent(source.Id)}&api_key=${encodeURIComponent(accessToken)}`;
        await playUrl(directUrl, offset);
      } else if (source.TranscodingUrl) {
        const transUrl = source.TranscodingUrl.startsWith('http')
          ? source.TranscodingUrl
          : `${serverUrl}${source.TranscodingUrl}`;
        await playUrl(transUrl, offset);
      } else {
        const hlsUrl = `${serverUrl}/Videos/${encodeURIComponent(itemId)}/master.m3u8?MediaSourceId=${encodeURIComponent(source.Id)}&VideoCodec=h264&AudioCodec=aac&VideoBitrate=12000000&AudioBitrate=320000&api_key=${encodeURIComponent(accessToken)}`;
        await playUrl(hlsUrl, offset);
      }

      if (channel.Mirror) {
        startMirrorPolling(channel);
      }
    } catch (err) {
      if (seq !== state.tuneSeq) return;
      console.error('ClassicTV Tizen tune error:', err);
      showBug(`CH ${chNum}`, channel.Name, err.message || 'Channel offline');
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

  function handleDigit(digit) {
    if (state.digitTimer) clearTimeout(state.digitTimer);
    if (state.digitBuffer.length < 4) {
      state.digitBuffer += digit;
    }
    showBug(`CH ${state.digitBuffer}_`, 'Direct Tune', '', 2000);
    state.digitTimer = setTimeout(() => {
      const targetNum = parseInt(state.digitBuffer, 10);
      state.digitBuffer = '';
      if (!Number.isFinite(targetNum)) return;
      const found = state.channels.find((ch, idx) => formatChannelNumber(ch, idx) === targetNum);
      if (found) {
        tuneChannel(found);
      } else {
        showBug(`CH ${targetNum}`, 'Not Found', '', 2000);
      }
    }, DIGIT_TIMEOUT_MS);
  }

  async function loadChannelsAndTune() {
    const res = await fetch(`${tvApiBase()}/ClassicTV/channels`);
    if (!res.ok) {
      throw new Error(`Channels HTTP ${res.status}`);
    }
    const raw = await res.json();
    state.channels = sortChannels((raw && raw.Channels) || []);
    if (state.channels.length === 0) {
      showBug('CH --', 'No Channels', 'Configure channels in Jellyfin', 0);
      return;
    }
    const savedId = localStorage.getItem(LAST_CHANNEL_KEY);
    const initial = state.channels.find((c) => c.Id === savedId) || state.channels[0];
    await tuneChannel(initial);
  }

  function showTvStage() {
    loginScreen.classList.add('hidden');
    tvStage.classList.remove('hidden');
    flashHint();
  }

  function signOut() {
    teardownPlayback();
    hideStatic(true);
    closeDrawer();
    clearAuth();
    tvStage.classList.add('hidden');
    loginScreen.classList.remove('hidden');
    loginError.textContent = '';
    usernameInput.focus();
  }

  async function authenticate(serverUrl, username, password) {
    const cleanUrl = serverUrl.trim().replace(/\/+$/, '');
    const res = await fetch(`${cleanUrl}/Users/AuthenticateByName`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: authHeader(null),
      },
      body: JSON.stringify({ Username: username, Pw: password }),
    });
    if (!res.ok) {
      throw new Error(res.status === 401 ? 'Invalid username or password' : `Auth HTTP ${res.status}`);
    }
    const data = await res.json();
    if (!data || !data.AccessToken || !data.User || !data.User.Id) {
      throw new Error('Unexpected authentication response');
    }
    saveAuth({
      serverUrl: cleanUrl,
      accessToken: data.AccessToken,
      userId: data.User.Id,
      username: data.User.Name || username,
    });
  }

  // Video lifecycle events
  video.addEventListener('playing', () => {
    hideStatic(false);
  });

  video.addEventListener('ended', () => {
    if (state.currentChannel) {
      tuneChannel(state.currentChannel);
    }
  });

  video.addEventListener('error', () => {
    if (!video.getAttribute('src') || !state.currentChannel || state.retuneTimer) return;
    const failedSeq = state.tuneSeq;
    const failedChannel = state.currentChannel;
    state.retuneTimer = setTimeout(() => {
      state.retuneTimer = null;
      if (state.tuneSeq === failedSeq && state.currentChannel && state.currentChannel.Id === failedChannel.Id) {
        tuneChannel(failedChannel);
      }
    }, 2000);
  });

  // Login D-pad & submit
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    loginError.textContent = '';
    loginBtn.disabled = true;
    loginBtn.textContent = 'Connecting…';
    try {
      await authenticate(serverInput.value, usernameInput.value, passwordInput.value);
      showTvStage();
      await loadChannelsAndTune();
    } catch (err) {
      loginError.textContent = err.message || 'Sign-in failed';
    } finally {
      loginBtn.disabled = false;
      loginBtn.textContent = 'Tune In';
    }
  });

  function stopQuickConnect() {
    if (state.qcTimer) {
      clearInterval(state.qcTimer);
      state.qcTimer = null;
    }
    state.qcSecret = null;
    qcBox.classList.add('hidden');
    qcCodeEl.textContent = '------';
    qcBtn.textContent = 'Quick Connect';
  }

  qcBtn.addEventListener('click', async () => {
    if (state.qcTimer) {
      stopQuickConnect();
      return;
    }
    loginError.textContent = '';
    const cleanUrl = (serverInput.value || 'https://jellyfin.example.com').trim().replace(/\/+$/, '');
    try {
      const res = await fetch(`${cleanUrl}/QuickConnect/Initiate`, {
        method: 'POST',
        headers: { Authorization: authHeader(null) },
      });
      if (!res.ok) throw new Error(`Quick Connect HTTP ${res.status}`);
      const init = await res.json();
      state.qcSecret = init.Secret;
      qcCodeEl.textContent = init.Code || '------';
      qcBox.classList.remove('hidden');
      qcBtn.textContent = 'Cancel Quick Connect';

      state.qcTimer = setInterval(async () => {
        if (!state.qcSecret) return;
        try {
          const checkRes = await fetch(`${cleanUrl}/QuickConnect/Connect?secret=${encodeURIComponent(state.qcSecret)}`, {
            headers: { Authorization: authHeader(null) },
          });
          if (!checkRes.ok) return;
          const check = await checkRes.json();
          if (!check.Authenticated) return;

          const authRes = await fetch(`${cleanUrl}/Users/AuthenticateWithQuickConnect`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Authorization: authHeader(null),
            },
            body: JSON.stringify({ Secret: state.qcSecret }),
          });
          if (!authRes.ok) throw new Error(`Quick Connect auth HTTP ${authRes.status}`);
          const data = await authRes.json();
          stopQuickConnect();
          saveAuth({
            serverUrl: cleanUrl,
            accessToken: data.AccessToken,
            userId: data.User.Id,
            username: data.User.Name || 'Jellyfin User',
          });
          showTvStage();
          await loadChannelsAndTune();
        } catch (err) {
          stopQuickConnect();
          loginError.textContent = err.message || 'Quick Connect failed';
        }
      }, 2000);
    } catch (err) {
      stopQuickConnect();
      loginError.textContent = err.message || 'Quick Connect failed';
    }
  });

  updateCcUi();
  if (drawerCc) drawerCc.addEventListener('click', toggleSubtitles);
  drawerSignout.addEventListener('click', signOut);

  function handleLoginKey(e) {
    const code = e.keyCode;
    const fields = [serverInput, usernameInput, passwordInput, loginBtn, qcBtn];
    const idx = fields.indexOf(document.activeElement);
    if (code === KEY.DOWN) {
      e.preventDefault();
      fields[(idx + 1 + fields.length) % fields.length].focus();
    } else if (code === KEY.UP) {
      e.preventDefault();
      fields[(idx - 1 + fields.length) % fields.length].focus();
    } else if (code === KEY.RETURN || code === KEY.ESCAPE) {
      e.preventDefault();
      exitTizenApp();
    }
  }

  function handleDrawerKey(e) {
    const code = e.keyCode;
    const items = Array.from(drawerList.querySelectorAll('.drawer-item'));
    if (code === KEY.DOWN) {
      e.preventDefault();
      const idx = items.indexOf(document.activeElement);
      if ((document.activeElement === drawerSignout || document.activeElement === drawerCc) && items.length > 0) {
        items[0].focus();
      } else if (idx >= 0 && idx < items.length - 1) {
        items[idx + 1].focus();
      } else if (items.length > 0) {
        items[0].focus();
      }
    } else if (code === KEY.UP) {
      e.preventDefault();
      const idx = items.indexOf(document.activeElement);
      if (idx > 0) {
        items[idx - 1].focus();
      } else if (idx === 0) {
        (drawerCc || drawerSignout).focus();
      } else if (items.length > 0) {
        items[items.length - 1].focus();
      }
    } else if (code === KEY.LEFT && document.activeElement === drawerSignout && drawerCc) {
      e.preventDefault();
      drawerCc.focus();
    } else if (code === KEY.RIGHT && document.activeElement === drawerCc) {
      e.preventDefault();
      drawerSignout.focus();
    } else if (code === KEY.RIGHT || code === KEY.RETURN || code === KEY.ESCAPE || code === KEY.GUIDE) {
      e.preventDefault();
      closeDrawer();
    } else if (code === KEY.ENTER && document.activeElement) {
      e.preventDefault();
      document.activeElement.click();
    }
  }

  function handleStageKey(e) {
    const code = e.keyCode;
    if (code >= 48 && code <= 57) {
      e.preventDefault();
      handleDigit(String(code - 48));
      return;
    }
    if (code >= 96 && code <= 105) {
      e.preventDefault();
      handleDigit(String(code - 96));
      return;
    }

    if (e.key === 'ChannelUp') {
      e.preventDefault();
      stepChannel(1);
      return;
    }
    if (e.key === 'ChannelDown') {
      e.preventDefault();
      stepChannel(-1);
      return;
    }

    switch (code) {
      case KEY.UP:
      case KEY.CH_UP:
      case KEY.PAGE_UP:
        e.preventDefault();
        stepChannel(1);
        break;
      case KEY.DOWN:
      case KEY.CH_DOWN:
      case KEY.PAGE_DOWN:
        e.preventDefault();
        stepChannel(-1);
        break;
      case KEY.LEFT:
      case KEY.GUIDE:
        e.preventDefault();
        openDrawer();
        break;
      case KEY.ENTER:
      case KEY.INFO:
        e.preventDefault();
        if (state.currentChannel) {
          const idx = state.channels.findIndex((c) => c.Id === state.currentChannel.Id);
          const chNum = formatChannelNumber(state.currentChannel, idx >= 0 ? idx : 0);
          const title = state.currentSlot
            ? (state.currentSlot.EpisodeTitle && state.currentSlot.EpisodeTitle !== state.currentSlot.Title
              ? `${state.currentSlot.Title} — ${state.currentSlot.EpisodeTitle}`
              : (state.currentSlot.Title || ''))
            : '';
          showBug(`CH ${chNum}`, state.currentChannel.Name, title);
        }
        flashHint();
        break;
      case KEY.RIGHT:
      case KEY.CAPTION:
        e.preventDefault();
        toggleSubtitles();
        break;
      case KEY.MEDIA_PLAY_PAUSE:
      case KEY.MEDIA_PAUSE:
      case KEY.MEDIA_PLAY:
        e.preventDefault();
        video.muted = !video.muted;
        if (state.currentChannel) {
          const idx = state.channels.findIndex((c) => c.Id === state.currentChannel.Id);
          const chNum = formatChannelNumber(state.currentChannel, idx >= 0 ? idx : 0);
          showBug(`CH ${chNum}`, state.currentChannel.Name, video.muted ? 'MUTED' : 'AUDIO ON', 2000);
        }
        break;
      case KEY.RED:
        e.preventDefault();
        signOut();
        break;
      case KEY.RETURN:
      case KEY.ESCAPE: {
        e.preventDefault();
        const now = Date.now();
        if (now < state.exitArmedUntil) {
          exitTizenApp();
        } else {
          state.exitArmedUntil = now + EXIT_CONFIRM_MS;
          exitToast.classList.remove('hidden');
          if (state.exitTimer) clearTimeout(state.exitTimer);
          state.exitTimer = setTimeout(() => {
            exitToast.classList.add('hidden');
          }, EXIT_CONFIRM_MS);
        }
        break;
      }
      default:
        break;
    }
  }

  document.addEventListener('keydown', (e) => {
    if (!loginScreen.classList.contains('hidden')) {
      handleLoginKey(e);
    } else if (!drawerEl.classList.contains('hidden')) {
      handleDrawerKey(e);
    } else {
      handleStageKey(e);
    }
  });

  // Boot
  registerTizenKeys();
  const stored = loadStoredAuth();
  if (stored) {
    state.auth = stored;
    showTvStage();
    loadChannelsAndTune().catch((err) => {
      console.error('ClassicTV Tizen boot error:', err);
      signOut();
      loginError.textContent = err.message || 'Unable to connect to Jellyfin';
    });
  } else {
    usernameInput.focus();
  }
})();
