#!/usr/bin/env node
/**
 * Headless Chrome smoke test suite for the Classic TV web interface (web/).
 *
 * Serves web/ from a local HTTP server with mocked /ClassicTV/* and /status.json
 * endpoints, intercepts the configured Jellyfin URL via CDP Fetch so auth and
 * Quick Connect flows are tested end-to-end without network dependencies, and
 * verifies every page loads with zero uncaught JS errors, console errors, or
 * failed local assets.
 *
 * Usage:
 *   node tools/smoke_web.mjs
 */

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');
const WEB_ROOT = path.join(REPO_ROOT, 'web');

const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean);

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};

// ---------------------------------------------------------------------------
// Mock data & API state
// ---------------------------------------------------------------------------
function createMockState() {
  return {
    adminToken: 'mock-admin-token-123',
    viewerToken: 'mock-viewer-token-456',
    qcPollCount: 0,
    lastAuthHeaders: [],
    channels: [
      {
        id: 'anime',
        name: 'Anime',
        number: 2,
        enabled: true,
        type: 'series',
        maxRating: 'TV-14',
        allowUnrated: true,
        genres: ['Animation'],
        blocks: [
          {
            id: 'blk-1',
            name: 'Prime Time',
            startHour: 0,
            endHour: 24,
            shows: [{ id: 'series-1', name: 'Cowboy Bebop', weight: 1 }],
          },
        ],
      },
      {
        id: 'comedy',
        name: 'Comedy',
        number: 5,
        enabled: true,
        type: 'series',
        maxRating: 'TV-PG',
        allowUnrated: true,
        genres: ['Comedy'],
        blocks: [
          {
            id: 'blk-2',
            name: 'All Day',
            startHour: 0,
            endHour: 24,
            shows: [{ id: 'series-2', name: 'Seinfeld', weight: 1 }],
          },
        ],
      },
    ],
  };
}

function mockPublicChannels(state) {
  return {
    Channels: state.channels
      .filter((c) => c.enabled)
      .map((c) => ({
        Id: c.id,
        Name: c.name,
        Number: c.number,
        Mirror: false,
      })),
  };
}

function mockChannelSlot(id) {
  const nowSec = Math.floor(Date.now() / 1000);
  const slotMap = {
    anime: {
      ChannelId: 'anime',
      ItemId: 'ep-bebop-1',
      JellyfinItemId: 'ep-bebop-1',
      StartOffset: 300,
      Title: 'Cowboy Bebop',
      EpisodeTitle: 'Asteroid Blues',
      SeriesName: 'Cowboy Bebop',
      EpisodeInfo: 'S01E01',
      SeasonNumber: 1,
      EpisodeNumber: 1,
      MediaType: 'Episode',
      StartTimeUnix: nowSec - 300,
      EndTimeUnix: nowSec + 1200,
      DurationSeconds: 1500,
      OffsetSeconds: 300,
      ServerTimeUnix: nowSec,
    },
    comedy: {
      ChannelId: 'comedy',
      ItemId: 'ep-seinfeld-1',
      Title: 'Seinfeld',
      EpisodeTitle: 'The Soup Nazi',
      SeriesName: 'Seinfeld',
      EpisodeInfo: 'S07E06',
      SeasonNumber: 7,
      EpisodeNumber: 6,
      MediaType: 'Episode',
      StartTimeUnix: nowSec - 120,
      EndTimeUnix: nowSec + 1200,
      DurationSeconds: 1320,
      OffsetSeconds: 120,
      ServerTimeUnix: nowSec,
    },
  };
  return slotMap[id] || null;
}

function startMockServer(state) {
  const server = http.createServer((req, res) => {
    const urlObj = new URL(req.url, 'http://127.0.0.1');
    const pathname = urlObj.pathname;

    // Mock ClassicTV public & manage API
    if (pathname === '/ClassicTV/channels') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(mockPublicChannels(state)));
      return;
    }

    if (pathname.startsWith('/ClassicTV/channel/')) {
      const id = decodeURIComponent(pathname.slice('/ClassicTV/channel/'.length));
      const slot = mockChannelSlot(id);
      if (!slot) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Not found' }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(slot));
      return;
    }

    if (pathname === '/ClassicTV/manage/channels') {
      const auth = req.headers.authorization || '';
      if (!auth.includes(state.adminToken)) {
        res.writeHead(403, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Admin required' }));
        return;
      }
      if (req.method === 'GET') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ channels: state.channels }));
        return;
      }
      if (req.method === 'POST') {
        let body = '';
        req.on('data', (c) => { body += c; });
        req.on('end', () => {
          const parsed = JSON.parse(body);
          if (Array.isArray(parsed.channels)) state.channels = parsed.channels;
          res.writeHead(204);
          res.end();
        });
        return;
      }
    }

    if (pathname === '/ClassicTV/manage/library/series') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        Series: [
          { Id: 'series-1', Name: 'Cowboy Bebop', EpisodeCount: 26 },
          { Id: 'series-2', Name: 'Seinfeld', EpisodeCount: 180 },
        ],
      }));
      return;
    }

    if (pathname === '/ClassicTV/manage/library/movies') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ Movies: [{ Id: 'mov-1', Name: 'Akira' }] }));
      return;
    }

    if (pathname === '/status.json') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        generated_at: new Date().toISOString(),
        overall: 'ok',
        jellyfin: { ok: true, channels: state.channels.length },
        sweet_potato: { reachable: true, kodi_up: true, playing: true },
        daemons: {},
      }));
      return;
    }

    if (pathname === '/incidents.json') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify([]));
      return;
    }

    // Serve config.example.js when local gitignored web/config.js is absent
    let relPath = decodeURIComponent(pathname);
    if (relPath.endsWith('/')) relPath += 'index.html';
    let filePath = path.resolve(WEB_ROOT, '.' + relPath);
    if (relPath === '/config.js' && !fs.existsSync(filePath)) {
      filePath = path.join(WEB_ROOT, 'config.example.js');
    }
    if (!filePath.startsWith(WEB_ROOT)) {
      res.writeHead(403);
      res.end('Forbidden');
      return;
    }

    fs.stat(filePath, (err, st) => {
      if (err || !st.isFile()) {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Not Found');
        return;
      }
      const ext = path.extname(filePath).toLowerCase();
      res.writeHead(200, { 'Content-Type': MIME_TYPES[ext] || 'application/octet-stream' });
      fs.createReadStream(filePath).pipe(res);
    });
  });

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      resolve({ server, baseUrl: `http://127.0.0.1:${addr.port}` });
    });
  });
}

// ---------------------------------------------------------------------------
// Minimal Chrome DevTools Protocol client (Node 22 native WebSocket)
// ---------------------------------------------------------------------------
function findChrome() {
  for (const candidate of CHROME_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error('Google Chrome / Chromium binary not found');
}

function launchChrome(chromeBin) {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ctv-smoke-chrome-'));
  const proc = spawn(chromeBin, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--remote-debugging-port=0',
    `--user-data-dir=${userDataDir}`,
    'about:blank',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });

  return new Promise((resolve, reject) => {
    let stderr = '';
    const timer = setTimeout(() => reject(new Error(`Chrome launch timeout:\n${stderr}`)), 15000);
    proc.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
      const m = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (m) {
        clearTimeout(timer);
        resolve({ proc, wsUrl: m[1], userDataDir });
      }
    });
    proc.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`Chrome exited early (${code}):\n${stderr}`));
    });
  });
}

class CdpClient {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();

    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id) {
        const p = this.pending.get(msg.id);
        if (p) {
          this.pending.delete(msg.id);
          if (msg.error) p.reject(new Error(`${msg.error.message} (${msg.error.code})`));
          else p.resolve(msg.result);
        }
      } else if (msg.method) {
        const key = `${msg.sessionId || ''}:${msg.method}`;
        for (const fn of this.listeners.get(key) || []) fn(msg.params);
      }
    });
  }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });
    return new CdpClient(ws);
  }

  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
    });
  }

  on(method, fn, sessionId = '') {
    const key = `${sessionId}:${method}`;
    if (!this.listeners.has(key)) this.listeners.set(key, []);
    this.listeners.get(key).push(fn);
  }

  close() {
    this.ws.close();
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitFor(fn, label, timeoutMs = 8000) {
  const start = Date.now();
  let lastErr;
  while (Date.now() - start < timeoutMs) {
    try {
      const val = await fn();
      if (val) return val;
    } catch (err) {
      lastErr = err;
    }
    await sleep(100);
  }
  throw new Error(`Timed out waiting for: ${label}${lastErr ? ` (${lastErr.message})` : ''}`);
}

// ---------------------------------------------------------------------------
// Page session helper with console/error/network tracking + Jellyfin mock
// ---------------------------------------------------------------------------
async function createPage(cdp, state) {
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });

  const errors = [];
  const failedAssets = [];

  await cdp.send('Page.enable', {}, sessionId);
  await cdp.send('Runtime.enable', {}, sessionId);
  const cfgPath = fs.existsSync(path.join(WEB_ROOT, 'config.js'))
    ? path.join(WEB_ROOT, 'config.js')
    : path.join(WEB_ROOT, 'config.example.js');
  const cfgMatch = fs.readFileSync(cfgPath, 'utf8').match(/jellyfinUrl\s*:\s*['"]([^'"]+)['"]/);
  const jellyfinPattern = `${(cfgMatch ? cfgMatch[1] : 'https://jellyfin.example.com').replace(/\/+$/, '')}/*`;

  await cdp.send('Network.enable', {}, sessionId);
  await cdp.send('Fetch.enable', {
    patterns: [
      { urlPattern: 'https://jellyfin.example.com/*' },
      ...(jellyfinPattern !== 'https://jellyfin.example.com/*' ? [{ urlPattern: jellyfinPattern }] : []),
    ],
  }, sessionId);

  cdp.on('Runtime.exceptionThrown', (p) => {
    const d = p.exceptionDetails;
    errors.push(`Uncaught exception: ${d.exception?.description || d.text}`);
  }, sessionId);

  cdp.on('Runtime.consoleAPICalled', (p) => {
    if (p.type === 'error') {
      const text = (p.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
      errors.push(`console.error: ${text}`);
    }
  }, sessionId);

  cdp.on('Network.responseReceived', (p) => {
    const { url, status } = p.response;
    if (
      url.startsWith('http://127.0.0.1') &&
      status >= 400 &&
      !url.includes('/ClassicTV/manage/') &&
      !url.endsWith('/favicon.ico')
    ) {
      failedAssets.push(`${status} ${url}`);
    }
  }, sessionId);

  cdp.on('Fetch.requestPaused', async (p) => {
    const reqUrl = new URL(p.request.url);
    const headers = p.request.headers || {};
    const method = p.request.method;

    // Handle CORS preflight
    if (method === 'OPTIONS') {
      await cdp.send('Fetch.fulfillRequest', {
        requestId: p.requestId,
        responseCode: 204,
        responseHeaders: [
          { name: 'Access-Control-Allow-Origin', value: '*' },
          { name: 'Access-Control-Allow-Headers', value: 'Authorization, Content-Type, X-Emby-Authorization' },
          { name: 'Access-Control-Allow-Methods', value: 'GET, POST, OPTIONS' },
        ],
      }, sessionId);
      return;
    }

    const cors = [
      { name: 'Access-Control-Allow-Origin', value: '*' },
      { name: 'Content-Type', value: 'application/json' },
    ];

    const fulfill = (code, body) => cdp.send('Fetch.fulfillRequest', {
      requestId: p.requestId,
      responseCode: code,
      responseHeaders: cors,
      body: Buffer.from(JSON.stringify(body)).toString('base64'),
    }, sessionId);

    // Enforce Jellyfin 12 header requirement: reject X-Emby-Authorization if Authorization missing
    const authHeader = headers.Authorization || headers.authorization || '';
    state.lastAuthHeaders.push({ path: reqUrl.pathname, authHeader, hasEmby: !!(headers['X-Emby-Authorization'] || headers['x-emby-authorization']) });

    if (reqUrl.pathname === '/Users/AuthenticateByName') {
      if (!authHeader.startsWith('MediaBrowser ')) {
        return fulfill(400, { error: 'Missing Authorization header' });
      }
      const body = JSON.parse(p.request.postData || '{}');
      if (body.Username === 'viewer') {
        return fulfill(200, {
          AccessToken: state.viewerToken,
          User: { Id: 'u-viewer', Name: 'viewer' },
        });
      }
      if (body.Username === 'dane' && body.Pw === 'secret') {
        return fulfill(200, {
          AccessToken: state.adminToken,
          User: { Id: 'u-admin', Name: 'dane' },
        });
      }
      return fulfill(401, { error: 'Invalid credentials' });
    }

    if (reqUrl.pathname === '/QuickConnect/Initiate') {
      if (!authHeader.startsWith('MediaBrowser ')) {
        return fulfill(400, { error: 'Missing Authorization header' });
      }
      state.qcPollCount = 0;
      return fulfill(200, { Secret: 'qc-secret-1', Code: '424242' });
    }

    if (reqUrl.pathname === '/QuickConnect/Connect') {
      if (!authHeader.startsWith('MediaBrowser ')) {
        return fulfill(400, { error: 'Missing Authorization header' });
      }
      state.qcPollCount += 1;
      return fulfill(200, { Authenticated: state.qcPollCount >= 1 });
    }

    if (reqUrl.pathname === '/Users/AuthenticateWithQuickConnect') {
      if (!authHeader.startsWith('MediaBrowser ')) {
        return fulfill(400, { error: 'Missing Authorization header' });
      }
      return fulfill(200, {
        AccessToken: state.adminToken,
        User: { Id: 'u-admin', Name: 'dane' },
      });
    }

    if (reqUrl.pathname === '/ClassicTV/channels') {
      if (!authHeader.includes('Token=')) {
        return fulfill(401, { error: 'Missing Authorization Token' });
      }
      return fulfill(200, mockPublicChannels(state));
    }

    if (reqUrl.pathname.startsWith('/ClassicTV/channel/')) {
      if (!authHeader.includes('Token=')) {
        return fulfill(401, { error: 'Missing Authorization Token' });
      }
      const id = decodeURIComponent(reqUrl.pathname.slice('/ClassicTV/channel/'.length));
      const slot = mockChannelSlot(id);
      return slot ? fulfill(200, slot) : fulfill(404, { error: 'Not found' });
    }

    if (reqUrl.pathname.endsWith('/PlaybackInfo')) {
      return fulfill(200, {
        MediaSources: [{ Id: 'ms-1', SupportsDirectPlay: true, Container: 'mp4', MediaStreams: [] }],
      });
    }

    if (reqUrl.pathname.startsWith('/Videos/')) {
      return cdp.send('Fetch.fulfillRequest', {
        requestId: p.requestId,
        responseCode: 200,
        responseHeaders: [
          { name: 'Access-Control-Allow-Origin', value: '*' },
          { name: 'Content-Type', value: 'video/mp4' },
        ],
        body: '',
      }, sessionId);
    }

    return fulfill(404, { error: `Unmocked Jellyfin path: ${reqUrl.pathname}` });
  }, sessionId);

  async function navigate(url) {
    errors.length = 0;
    failedAssets.length = 0;
    const loaded = new Promise((res) => {
      const handler = () => res();
      cdp.on('Page.loadEventFired', handler, sessionId);
    });
    await cdp.send('Page.navigate', { url }, sessionId);
    await loaded;
    await sleep(300);
  }

  async function evalJs(expression) {
    const res = await cdp.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
    }, sessionId);
    if (res.exceptionDetails) {
      throw new Error(res.exceptionDetails.exception?.description || res.exceptionDetails.text);
    }
    return res.result?.value;
  }

  async function close() {
    await cdp.send('Target.closeTarget', { targetId });
  }

  return { navigate, evalJs, errors, failedAssets, close };
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------
async function run() {
  const state = createMockState();
  const { server, baseUrl } = await startMockServer(state);
  const chromeBin = findChrome();
  const chrome = await launchChrome(chromeBin);
  const cdp = await CdpClient.connect(chrome.wsUrl);

  const results = [];
  async function test(name, fn) {
    try {
      await fn();
      console.log(`  PASS  ${name}`);
      results.push({ name, ok: true });
    } catch (err) {
      console.error(`  FAIL  ${name}\n        ${err.message}`);
      results.push({ name, ok: false, error: err.message });
    }
  }

  try {
    // 1. All static + API-backed pages load cleanly with shared nav
    const navPages = [
      { path: '/', titleMatch: 'Classic TV', extra: () => true },
      { path: '/guide/', titleMatch: 'Guide', extra: () => true },
      { path: '/channels/', titleMatch: 'Channels', extra: () => true },
      { path: '/status/', titleMatch: 'Status', extra: () => true },
      { path: '/crt/', titleMatch: 'CRT', extra: () => true },
    ];

    for (const p of navPages) {
      await test(`Page ${p.path} loads cleanly with shared nav`, async () => {
        const page = await createPage(cdp, state);
        try {
          await page.navigate(`${baseUrl}${p.path}`);
          await waitFor(() => page.evalJs(`document.readyState === 'complete'`), 'document ready');
          const info = await page.evalJs(`({
            title: document.title,
            hasTopbar: !!document.querySelector('.ctv-topbar'),
            navHrefs: Array.from(document.querySelectorAll('.ctv-nav-link')).map(a => a.getAttribute('href')),
            bodyLen: document.body.innerText.trim().length
          })`);
          assert(info.hasTopbar, 'Missing .ctv-topbar');
          assert(info.navHrefs.includes('/manage/'), `Missing /manage/ nav link: ${JSON.stringify(info.navHrefs)}`);
          assert(info.bodyLen > 40, 'Page body unexpectedly empty');
          assert(page.failedAssets.length === 0, `Failed assets: ${page.failedAssets.join(', ')}`);
          assert(page.errors.length === 0, `Page JS errors:\n${page.errors.join('\n')}`);
        } finally {
          await page.close();
        }
      });
    }

    // 2. /channels/ renders mocked channels & now-playing slots
    await test('/channels/ renders live channel roster from API', async () => {
      const page = await createPage(cdp, state);
      try {
        await page.navigate(`${baseUrl}/channels/`);
        const text = await waitFor(async () => {
          const t = await page.evalJs(`document.body.innerText`);
          return t.includes('Anime') && t.includes('Cowboy Bebop') ? t : null;
        }, 'Anime / Cowboy Bebop rendered on /channels/');
        assert(text.includes('Seinfeld'), 'Expected Seinfeld on /channels/');
        assert(page.errors.length === 0, `JS errors: ${page.errors.join('; ')}`);
      } finally {
        await page.close();
      }
    });

    // 3. /watch/ renders login screen and signs in cleanly
    await test('/watch/ renders login and authenticates via Authorization header', async () => {
      const page = await createPage(cdp, state);
      try {
        await page.navigate(`${baseUrl}/watch/`);
        await waitFor(
          () => page.evalJs(`!document.getElementById('login-screen')?.classList.contains('hidden')`),
          'watch login screen visible',
        );
        await page.evalJs(`(() => {
          const s = document.getElementById('server-url');
          if (s) s.value = 'https://jellyfin.example.com';
          document.getElementById('username').value = 'dane';
          document.getElementById('password').value = 'secret';
          document.getElementById('login-btn').click();
        })()`);
        await waitFor(async () => {
          const visible = await page.evalJs(`!document.getElementById('tv-stage')?.classList.contains('hidden')`);
          if (visible) return true;
          const err = await page.evalJs(`document.getElementById('login-error')?.textContent || ''`);
          if (err) throw new Error('Watch login error: ' + err);
          return false;
        }, 'tv stage visible');
        await waitFor(
          () => page.evalJs(`(document.getElementById('drawer-list')?.innerText || '').includes('Anime')`),
          'watch loaded channels into drawer with Token auth',
        );
        const sentToken = state.lastAuthHeaders.some(
          (h) => h.path === '/ClassicTV/channels' && h.authHeader.includes('Token='),
        );
        assert(sentToken, 'Expected /watch/ to send Authorization Token to /ClassicTV/channels');
        assert(page.errors.length === 0, `JS errors: ${page.errors.join('; ')}`);
      } finally {
        await page.close();
      }
    });

    // 4. /manage/ password sign-in, channel list render (viewshow), and editor modal
    await test('/manage/ password login -> renders channels -> opens editor modal', async () => {
      const page = await createPage(cdp, state);
      try {
        await page.navigate(`${baseUrl}/manage/`);
        const loginVisible = await waitFor(
          () => page.evalJs(`!document.getElementById('manage-login').classList.contains('hidden')`),
          'manage login visible',
        );
        assert(loginVisible, 'Expected #manage-login to be visible');

        await page.evalJs(`(() => {
          document.getElementById('manage-user').value = 'dane';
          document.getElementById('manage-pass').value = 'secret';
          document.getElementById('manage-login-form').dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
        })()`);

        await waitFor(async () => {
          const text = await page.evalJs(`document.getElementById('ctv-channel-list')?.innerText || ''`);
          return text.includes('Anime') && text.includes('Comedy') ? text : null;
        }, 'channel cards rendered in #ctv-channel-list');

        // Verify New Channel modal opens
        await page.evalJs(`document.getElementById('ctv-btn-new-channel').click()`);
        const modalOpen = await waitFor(
          () => page.evalJs(`document.getElementById('ctv-editor-overlay').style.display !== 'none'`),
          'editor modal open',
        );
        assert(modalOpen, 'Expected #ctv-editor-overlay to open');
        assert(page.errors.length === 0, `JS errors: ${page.errors.join('; ')}`);
      } finally {
        await page.close();
      }
    });

    // 5. /manage/ Quick Connect flow
    await test('/manage/ Quick Connect sign-in loads channels', async () => {
      const page = await createPage(cdp, state);
      try {
        await page.navigate(`${baseUrl}/manage/`);
        await page.evalJs(`localStorage.clear(); location.reload();`);
        await waitFor(
          () => page.evalJs(`!document.getElementById('manage-login').classList.contains('hidden')`),
          'manage login visible after reload',
        );
        await page.evalJs(`document.getElementById('manage-qc-btn').click()`);
        await waitFor(
          () => page.evalJs(`document.getElementById('manage-qc-code')?.textContent?.trim() === '424242'`),
          'Quick Connect code displayed',
        );
        await waitFor(async () => {
          const text = await page.evalJs(`document.getElementById('ctv-channel-list')?.innerText || ''`);
          return text.includes('Anime') ? text : null;
        }, 'Quick Connect authenticated and loaded channels', 10000);
        assert(page.errors.length === 0, `JS errors: ${page.errors.join('; ')}`);
      } finally {
        await page.close();
      }
    });

    // 6. /manage/ non-admin user gets 403 guard message and stays on login
    await test('/manage/ rejects non-admin user with permission error', async () => {
      const page = await createPage(cdp, state);
      try {
        await page.navigate(`${baseUrl}/manage/`);
        await page.evalJs(`localStorage.clear(); location.reload();`);
        await waitFor(
          () => page.evalJs(`!document.getElementById('manage-login').classList.contains('hidden')`),
          'manage login visible',
        );
        await page.evalJs(`(() => {
          document.getElementById('manage-user').value = 'viewer';
          document.getElementById('manage-pass').value = 'anything';
          document.getElementById('manage-login-form').dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
        })()`);
        const errText = await waitFor(
          () => page.evalJs(`document.getElementById('manage-login-err')?.textContent || ''`),
          'non-admin error message',
        );
        assert(errText.includes('administrator'), `Unexpected error text: ${errText}`);
        const pageHidden = await page.evalJs(`document.getElementById('ClassicTVConfigPage').classList.contains('hidden')`);
        assert(pageHidden, '#ClassicTVConfigPage should remain hidden for non-admin');
      } finally {
        await page.close();
      }
    });
  } finally {
    cdp.close();
    chrome.proc.kill('SIGKILL');
    server.close();
    fs.rmSync(chrome.userDataDir, { recursive: true, force: true });
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nSmoke summary: ${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length === 0 ? 0 : 1);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
