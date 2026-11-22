/* Server-authoritative idle-session warning, with same-browser tab coordination. */
(function () {
  var body = document.body;
  var userId = body && body.dataset.uiUser;
  if (!userId || !body.dataset.sessionIdleTimeout) return;

  var idleTimeout = Number(body.dataset.sessionIdleTimeout) * 1000;
  var warningAfter = Number(body.dataset.sessionIdleWarning) * 1000;
  var statusUrl = body.dataset.sessionStatusUrl;
  var activityUrl = body.dataset.sessionActivityUrl;
  var logoutUrl = body.dataset.sessionLogoutUrl;
  var loginUrl = body.dataset.sessionLoginUrl;
  var csrf = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrf ? csrf.content : '';
  var dialog = document.getElementById('session-idle-dialog');
  var countdown = document.getElementById('session-idle-countdown');
  var message = document.getElementById('session-idle-message');
  var continueButton = document.getElementById('session-idle-continue');
  var logoutButton = document.getElementById('session-idle-logout');
  var activityKey = 'gradelogy.session.activity.' + userId;
  var logoutKey = 'gradelogy.session.logout.' + userId;
  var lastActivityAt = Number(body.dataset.sessionLastActivity || 0) * 1000;
  var lastPingAt = 0;
  var checking = false;
  var redirecting = false;
  var nativeFetch = window.fetch.bind(window);
  var channel = null;

  // Mark same-origin fetch requests so an expired session is not mistaken for
  // a successful HTML login redirect by JSON/AJAX callers.
  window.fetch = function (input, init) {
    var options = Object.assign({}, init || {});
    try {
      var requestUrl = input instanceof Request ? input.url : input;
      if (new URL(requestUrl, window.location.href).origin === window.location.origin) {
        var headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
        if (!headers.has('X-Requested-With')) headers.set('X-Requested-With', 'XMLHttpRequest');
        options.headers = headers;
      }
    } catch (e) {}
    return nativeFetch(input, options).then(function (response) {
      if (response.status === 401 && response.headers.get('X-Session-Expired') === '1') {
        goToLogin(false);
      }
      return response;
    });
  };

  function writeStorage(key, value) {
    try { window.localStorage.setItem(key, String(value)); } catch (e) {}
  }

  function broadcast(messageValue) {
    if (channel) {
      try { channel.postMessage(messageValue); } catch (e) {}
    }
  }

  function hideDialog() {
    if (dialog && dialog.open) dialog.close();
    if (message) message.textContent = '页面中尚未保存的内容不会自动提交，请先保存或选择继续使用。';
    if (continueButton) continueButton.disabled = false;
  }

  function setActivity(timestamp, confirmed) {
    if (!timestamp || !Number.isFinite(timestamp)) return;
    if (confirmed) {
      lastActivityAt = timestamp;
    } else if (timestamp > lastActivityAt) {
      lastActivityAt = timestamp;
    }
    if (dialog && dialog.open && Date.now() - lastActivityAt < warningAfter) hideDialog();
  }

  function goToLogin() {
    if (redirecting) return;
    redirecting = true;
    writeStorage(logoutKey, Date.now());
    broadcast({type: 'logout', at: Date.now()});
    window.location.replace(loginUrl + '?next=' + encodeURIComponent(window.location.pathname));
  }

  function goToLogout() {
    if (redirecting) return;
    redirecting = true;
    writeStorage(logoutKey, Date.now());
    broadcast({type: 'logout', at: Date.now()});
    window.location.replace(logoutUrl);
  }

  function showDialog() {
    if (!dialog || dialog.open || redirecting) return;
    try { dialog.showModal(); } catch (e) { dialog.setAttribute('open', ''); }
  }

  function applyServerStatus(data) {
    var serverTime = Number(data.lastActivityAt) * 1000;
    if (!serverTime) return;
    // A newer same-browser activity can be in flight from another tab. Keep it
    // locally and synchronize it when this tab is visible; the server still
    // decides whether the session is valid.
    if (lastActivityAt > serverTime + 1500 && Date.now() - lastActivityAt < idleTimeout) {
      setActivity(lastActivityAt, false);
      if (!document.hidden) sendActivity(true);
    } else {
      setActivity(serverTime, true);
    }
  }

  function validateSession() {
    if (checking || redirecting) return;
    checking = true;
    window.fetch(statusUrl, {headers: {'Accept': 'application/json'}, credentials: 'same-origin'})
      .then(function (response) {
        if (!response.ok) {
          if (response.status === 401) goToLogin(false);
          else if (message && dialog && dialog.open) message.textContent = '暂时无法验证登录状态，请检查网络后重试。';
          return null;
        }
        return response.json();
      })
      .then(function (data) { if (data && data.ok) applyServerStatus(data); })
      .catch(function () {
        if (message && dialog && dialog.open) message.textContent = '暂时无法验证登录状态，请检查网络后重试。';
      })
      .finally(function () { checking = false; });
  }

  function sendActivity(force) {
    var now = Date.now();
    if (!force && now - lastPingAt < 60000) return;
    lastPingAt = now;
    window.fetch(activityUrl, {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken
      },
      body: '{}'
    }).then(function (response) {
      if (!response.ok) {
        if (response.status === 401) goToLogin(false);
        else {
          if (continueButton) continueButton.disabled = false;
          if (message && dialog && dialog.open) message.textContent = '无法续期登录状态，请检查网络后重试。';
        }
        return null;
      }
      return response.json();
    }).then(function (data) {
      if (!data || !data.ok) return;
      setActivity(Number(data.lastActivityAt) * 1000, true);
      writeStorage(activityKey, Date.now());
      broadcast({type: 'activity', at: Date.now()});
    }).catch(function () {
      if (continueButton) continueButton.disabled = false;
      if (message && dialog && dialog.open) message.textContent = '无法续期登录状态，请检查网络后重试。';
    });
  }

  function userActivity(event) {
    if (redirecting) return;
    // Interacting with the warning dialog must not close it before its button's
    // click/keyboard activation is dispatched; only “continue” renews the session.
    if (dialog && dialog.open && event && dialog.contains(event.target)) return;
    var now = Date.now();
    setActivity(now, false);
    writeStorage(activityKey, now);
    broadcast({type: 'activity', at: now});
    sendActivity(Boolean(dialog && dialog.open));
  }

  function tick() {
    if (redirecting) return;
    var remaining = lastActivityAt + idleTimeout - Date.now();
    if (remaining <= 0) {
      showDialog();
      if (countdown) countdown.textContent = '00:00';
      validateSession();
      return;
    }
    if (remaining <= idleTimeout - warningAfter) showDialog();
    if (countdown && dialog && dialog.open) {
      var seconds = Math.ceil(remaining / 1000);
      countdown.textContent = String(Math.floor(seconds / 60)).padStart(2, '0') + ':' +
        String(seconds % 60).padStart(2, '0');
    }
  }

  if (continueButton) {
    continueButton.addEventListener('click', function () {
      continueButton.disabled = true;
      if (message) message.textContent = '正在向服务器确认登录状态…';
      sendActivity(true);
    });
  }
  if (logoutButton) logoutButton.addEventListener('click', goToLogout);

  ['pointerdown', 'keydown', 'touchstart', 'wheel', 'input', 'change'].forEach(function (eventName) {
    document.addEventListener(eventName, userActivity, {
      capture: true,
      passive: !['keydown', 'input', 'change'].includes(eventName)
    });
  });

  document.addEventListener('click', function (event) {
    var link = event.target && event.target.closest ? event.target.closest('a[href]') : null;
    if (!link) return;
    try {
      if (new URL(link.href, window.location.href).pathname === new URL(logoutUrl, window.location.href).pathname) {
        writeStorage(logoutKey, Date.now());
        broadcast({type: 'logout', at: Date.now()});
      }
    } catch (e) {}
  }, true);

  window.addEventListener('storage', function (event) {
    if (event.key === logoutKey && event.newValue) goToLogin(false);
    if (event.key === activityKey && event.newValue) setActivity(Number(event.newValue), false);
  });
  if ('BroadcastChannel' in window) {
    try {
      channel = new BroadcastChannel('gradelogy-session-' + userId);
      channel.onmessage = function (event) {
        var data = event.data || {};
        if (data.type === 'logout') goToLogin(false);
        if (data.type === 'activity') setActivity(Number(data.at), false);
      };
    } catch (e) {}
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) validateSession();
  });
  window.addEventListener('online', validateSession);

  tick();
  window.setInterval(tick, 1000);
})();
