// 通用交互：生成类按钮的「处理中」状态与「覆盖确认」弹窗
(function () {
  // 按钮进入处理中：禁用 + 小圆环动画，避免重复提交
  window.busyOn = function (btn, label) {
    if (!btn || btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    btn.dataset.label = btn.textContent;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.classList.add('is-busy');
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>' + (label || '处理中…');
  };

  window.busyOff = function (btn) {
    if (!btn || btn.dataset.busy !== '1') return;
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    btn.classList.remove('is-busy');
    btn.textContent = btn.dataset.label || '';
    delete btn.dataset.busy;
    delete btn.dataset.label;
  };

  // Queue-backed AI forms enter the same visible busy state whether the task
  // starts immediately or waits behind another background job.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    var btn = event.submitter || form.querySelector('button[type="submit"],button:not([type])');
    if (event.defaultPrevented || !form || !form.matches ||
        (!form.matches('[data-ai-queue-form], [data-ai-submit-form]') &&
         !(btn && btn.dataset.aiBusyLabel))) return;
    window.busyOn(btn, (btn && btn.dataset.aiBusyLabel) || form.dataset.aiBusyLabel || '生成中…');
  });

  var reloadScheduled = false;
  function pollQueuedForm(form) {
    var operationId = form.dataset.aiOperationId;
    var statusUrl = form.dataset.aiStatusUrl;
    if (!operationId || !statusUrl) return;
    var stopped = false;
    var busy = false;
    function poll() {
      if (stopped || busy || !document.documentElement.contains(form)) return;
      if (document.hidden) {
        window.setTimeout(poll, 1800);
        return;
      }
      busy = true;
      fetch(statusUrl, { headers: { 'Accept': 'application/json' }, cache: 'no-store' })
        .then(function (response) {
          if (!response.ok) throw new Error('状态查询失败');
          return response.json();
        })
        .then(function (data) {
          if (data.status === 'queued' || data.status === 'running') {
            window.setTimeout(poll, 1800);
            return;
          }
          stopped = true;
          if (!reloadScheduled) {
            reloadScheduled = true;
            window.location.reload();
          }
        })
        .catch(function () { window.setTimeout(poll, 2500); })
        .finally(function () { busy = false; });
    }
    poll();
  }
  document.querySelectorAll('[data-ai-operation-id][data-ai-status-url]')
    .forEach(pollQueuedForm);

  // 确认弹窗：返回 Promise<boolean>，确认才继续
  window.confirmDialog = function (message, okText, title) {
    return new Promise(function (resolve) {
      var dlg = document.createElement('dialog');
      dlg.className = 'modal confirm-modal';
      var h = document.createElement('h2');
      h.textContent = title || '确认重新生成';
      var p = document.createElement('p');
      p.className = 'muted';
      p.textContent = message;
      var actions = document.createElement('div');
      actions.className = 'modal-actions';
      var no = document.createElement('button');
      no.type = 'button';
      no.className = 'ghost';
      no.textContent = '取消';
      var yes = document.createElement('button');
      yes.type = 'button';
      yes.textContent = okText || '确认生成';
      actions.appendChild(no);
      actions.appendChild(yes);
      dlg.appendChild(h);
      dlg.appendChild(p);
      dlg.appendChild(actions);
      document.body.appendChild(dlg);
      var settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        if (dlg.open) dlg.close();
        dlg.remove();
        resolve(value);
      }
      no.addEventListener('click', function () { finish(false); });
      yes.addEventListener('click', function () { finish(true); });
      dlg.addEventListener('cancel', function (e) { e.preventDefault(); finish(false); });
      dlg.addEventListener('click', function (e) { if (e.target === dlg) finish(false); });
      dlg.showModal();
    });
  };
})();

// 移动端：给列数较多的表格包一层横向滚动容器，避免撑破页面
(function () {
  function wrapTables() {
    var tables = document.querySelectorAll('table');
    for (var i = 0; i < tables.length; i++) {
      var table = tables[i];
      if (table.parentElement && table.parentElement.classList.contains('table-scroll')) continue;
      var headRow = table.rows[0];
      var cols = headRow ? headRow.cells.length : 0;
      var wrap = document.createElement('div');
      wrap.className = 'table-scroll';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
      if (cols >= 5) table.style.minWidth = Math.max(560, cols * 88) + 'px';
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wrapTables);
  } else {
    wrapTables();
  }
})();
