// Classic TV — Shared Jellyfin Login & Quick Connect Helper
(() => {
  'use strict';

  function getJellyfinBase() {
    const cfg = window.CLASSICTV_CONFIG || {};
    const lanHosts = cfg.lanHosts || ['localhost', '127.0.0.1'];
    const h = window.location.hostname;
    if (lanHosts.includes(h)) {
      return `http://${h}:8096`;
    }
    return (cfg.jellyfinUrl || 'https://jellyfin.example.com').replace(/\/+$/, '');
  }

  async function authenticateByName(serverUrl, authHeaderValue, username, password) {
    const res = await fetch(`${serverUrl}/Users/AuthenticateByName`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: authHeaderValue,
      },
      body: JSON.stringify({ Username: username, Pw: password }),
    });
    if (!res.ok) {
      throw new Error(res.status === 401 ? 'Invalid username or password.' : `Sign-in failed (HTTP ${res.status}).`);
    }
    return res.json();
  }

  function bindQuickConnect(opts) {
    const {
      getServerUrl,
      getAuthHeader,
      qcBtn,
      qcBox,
      qcCodeEl,
      qcHintEl,
      errEl,
      onSuccess,
      idleButtonText = 'Sign In with Quick Connect',
      cancelButtonText = 'Cancel Quick Connect',
      idleHintText = 'ENTER CODE IN JELLYFIN → QUICK CONNECT (CLICK TO COPY)',
      pollMs = 2000,
    } = opts;

    let timer = null;
    let secret = '';

    function stop() {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
      secret = '';
      if (qcBox) qcBox.classList.add('hidden');
      if (qcCodeEl) qcCodeEl.textContent = '------';
      if (qcBtn) qcBtn.textContent = idleButtonText;
    }

    function copyCode() {
      const code = (qcCodeEl && qcCodeEl.textContent || '').trim();
      if (!code || code === '------') return;
      if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
        window.navigator.clipboard.writeText(code).catch(() => {});
      }
      if (qcHintEl) {
        qcHintEl.textContent = 'COPIED TO CLIPBOARD!';
        setTimeout(() => {
          if (secret && qcHintEl) qcHintEl.textContent = idleHintText;
        }, 1500);
      }
    }

    if (qcBox) {
      qcBox.addEventListener('click', copyCode);
      qcBox.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          copyCode();
        }
      });
    }

    if (qcBtn) {
      qcBtn.addEventListener('click', async () => {
        if (timer) {
          stop();
          return;
        }
        if (errEl) errEl.textContent = '';
        const serverUrl = getServerUrl().trim().replace(/\/+$/, '');
        try {
          const res = await fetch(`${serverUrl}/QuickConnect/Initiate`, {
            method: 'POST',
            headers: { Authorization: getAuthHeader() },
          });
          if (!res.ok) throw new Error(`Quick Connect unavailable (HTTP ${res.status})`);
          const init = await res.json();
          secret = init.Secret;
          if (qcCodeEl) qcCodeEl.textContent = init.Code || '------';
          if (qcHintEl) qcHintEl.textContent = idleHintText;
          if (qcBox) qcBox.classList.remove('hidden');
          qcBtn.textContent = cancelButtonText;

          timer = setInterval(async () => {
            if (!secret) return;
            try {
              const checkRes = await fetch(
                `${serverUrl}/QuickConnect/Connect?secret=${encodeURIComponent(secret)}`,
                { headers: { Authorization: getAuthHeader() } }
              );
              if (!checkRes.ok) return;
              const check = await checkRes.json();
              if (!check.Authenticated) return;

              const authRes = await fetch(`${serverUrl}/Users/AuthenticateWithQuickConnect`, {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  Authorization: getAuthHeader(),
                },
                body: JSON.stringify({ Secret: secret }),
              });
              if (!authRes.ok) throw new Error(`Quick Connect auth HTTP ${authRes.status}`);
              const data = await authRes.json();
              stop();
              await onSuccess(data, serverUrl);
            } catch (err) {
              stop();
              if (errEl) errEl.textContent = err.message || 'Quick Connect failed.';
            }
          }, pollMs);
        } catch (err) {
          stop();
          if (errEl) errEl.textContent = err.message || 'Quick Connect failed.';
        }
      });
    }

    return { stop };
  }

  window.ClassicTVLogin = {
    getJellyfinBase,
    authenticateByName,
    bindQuickConnect,
  };
})();
