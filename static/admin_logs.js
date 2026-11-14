(function () {
  var root = document.getElementById('log-browser');
  if (!root) return;
  var fileSelect = document.getElementById('log-file');
  var toggle = document.getElementById('log-toggle');
  var clear = document.getElementById('log-clear');
  var download = document.getElementById('log-download');
  var levelSelect = document.getElementById('log-level');
  var keywordInput = document.getElementById('log-keyword');
  var autoScroll = document.getElementById('log-autoscroll');
  var status = document.getElementById('log-status');
  var viewer = document.getElementById('log-viewer');
  var currentName = root.dataset.initialName || '';
  var entries = [];
  var cursor = null;
  var paused = false;
  var busy = false;
  var timer = null;
  var generation = 0;
  var controller = null;

  function setStatus(text) {
    status.textContent = text;
  }

  function abortPending() {
    generation += 1;
    window.clearTimeout(timer);
    if (controller) controller.abort();
    controller = null;
    busy = false;
  }

  function lineLevel(text, previous) {
    var match = text.match(/^\d{4}-\d{2}-\d{2} [0-9:,]+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) /);
    return match ? match[1] : previous;
  }

  function render() {
    var wantedLevel = levelSelect.value;
    var keyword = keywordInput.value.trim().toLocaleLowerCase();
    var visible = entries.filter(function (entry) {
      return (!wantedLevel || entry.level === wantedLevel) &&
        (!keyword || entry.text.toLocaleLowerCase().indexOf(keyword) >= 0);
    });
    var fragment = document.createDocumentFragment();
    visible.forEach(function (entry) {
      var line = document.createElement('div');
      line.className = 'log-line log-' + entry.level.toLocaleLowerCase();
      line.textContent = entry.text;
      fragment.appendChild(line);
    });
    if (!visible.length) {
      var empty = document.createElement('div');
      empty.className = 'log-empty';
      empty.textContent = entries.length ? '没有符合当前过滤条件的日志。' : '暂无日志内容。';
      fragment.appendChild(empty);
    }
    viewer.replaceChildren(fragment);
    if (autoScroll.checked) viewer.scrollTop = viewer.scrollHeight;
  }

  function appendLines(lines) {
    var previous = entries.length ? entries[entries.length - 1].level : 'OTHER';
    lines.forEach(function (text) {
      previous = lineLevel(text, previous);
      entries.push({text: text, level: previous});
    });
    if (entries.length > 1000) entries = entries.slice(entries.length - 1000);
  }

  function schedule(delay) {
    window.clearTimeout(timer);
    if (!paused && !document.hidden && currentName) {
      timer = window.setTimeout(poll, delay === undefined ? 3000 : delay);
    }
  }

  function updateDownload() {
    download.href = root.dataset.downloadUrl.replace('__NAME__', encodeURIComponent(currentName));
    download.hidden = !currentName;
  }

  function poll() {
    window.clearTimeout(timer);
    if (paused || document.hidden || !currentName || busy) return;
    var gen = generation;
    var nextDelay = 3000;
    busy = true;
    controller = new AbortController();
    var params = new URLSearchParams({name: currentName});
    if (cursor) {
      params.set('offset', cursor.offset);
      params.set('dev', cursor.dev);
      params.set('ino', cursor.ino);
    }
    if (!entries.length) setStatus('正在读取 ' + currentName + ' …');
    fetch(root.dataset.tailUrl + '?' + params.toString(), {
      cache: 'no-store',
      headers: {'Accept': 'application/json'},
      signal: controller.signal
    }).then(function (resp) {
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp.json();
    }).then(function (data) {
      if (gen !== generation) return;
      cursor = data.cursor;
      if (data.reset) entries = [];
      appendLines(data.lines || []);
      render();
      nextDelay = data.has_more ? 250 : 3000;
      var message = data.message || ('最后更新 ' + new Date().toLocaleTimeString() +
        ' · 当前保留 ' + entries.length + ' 行');
      setStatus(message);
    }).catch(function (error) {
      if (gen !== generation || error.name === 'AbortError') return;
      setStatus('读取失败：' + error.message + '，将自动重试。');
    }).then(function () {
      if (gen !== generation) return;
      busy = false;
      controller = null;
      schedule(nextDelay);
    });
  }

  fileSelect.addEventListener('change', function () {
    abortPending();
    currentName = fileSelect.value;
    cursor = null;
    entries = [];
    paused = false;
    toggle.textContent = '暂停';
    updateDownload();
    render();
    poll();
  });

  toggle.addEventListener('click', function () {
    paused = !paused;
    toggle.textContent = paused ? '继续' : '暂停';
    if (paused) {
      abortPending();
      setStatus('已暂停，游标保持不变。');
    } else {
      poll();
    }
  });

  clear.addEventListener('click', function () {
    entries = [];
    render();
    setStatus('视图已清空；未修改日志文件，后续新日志仍会继续显示。');
  });

  levelSelect.addEventListener('change', render);
  keywordInput.addEventListener('input', render);
  autoScroll.addEventListener('change', render);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      abortPending();
    } else if (!paused) {
      poll();
    }
  });

  updateDownload();
  render();
  poll();
})();
