/* 通用教师交互：分区、筛选、折叠与编辑保护；不替换业务表单提交。 */
(function () {
  'use strict';
  function initialize() {
    var user = document.body.dataset.uiUser || 'anonymous';
    function read(key) { try { return sessionStorage.getItem('gradelogy.ui.' + user + '.' + location.pathname + '.' + key); } catch (_) { return null; } }
    function write(key, value) { try { sessionStorage.setItem('gradelogy.ui.' + user + '.' + location.pathname + '.' + key, value); } catch (_) {} }
    var clock = document.getElementById('teacher-current-time');
    if (clock) {
      var epoch = Number(clock.dataset.epochMs);
      var browserStart = Date.now();
      var formatter;
      try {
        formatter = new Intl.DateTimeFormat('zh-CN', {
          timeZone: clock.dataset.timezone || undefined,
          year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
        });
      } catch (_) {}
      function updateClock() {
        var date = new Date(Number.isFinite(epoch) ? epoch + Date.now() - browserStart : Date.now());
        if (!formatter) {
          clock.textContent = date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0')
            + '-' + String(date.getDate()).padStart(2, '0') + ' '
            + String(date.getHours()).padStart(2, '0') + ':'
            + String(date.getMinutes()).padStart(2, '0') + ':'
            + String(date.getSeconds()).padStart(2, '0');
          return;
        }
        var parts = {};
        formatter.formatToParts(date).forEach(function (part) { parts[part.type] = part.value; });
        clock.textContent = parts.year + '-' + parts.month + '-' + parts.day + ' '
          + parts.hour + ':' + parts.minute + ':' + parts.second;
      }
      updateClock();
      window.setInterval(updateClock, 1000);
    }
    document.querySelectorAll('[data-tab-group]').forEach(function (group) {
      var tabs = Array.from(group.querySelectorAll('[data-tab]')).filter(function (tab) { return tab.closest('[data-tab-group]') === group; });
      var panels = Array.from(group.querySelectorAll('[data-tab-panel]')).filter(function (panel) { return panel.closest('[data-tab-group]') === group; });
      function show(id, update) {
        if (!panels.some(function (p) { return p.id === id; })) return;
        tabs.forEach(function (t) { var selected = t.dataset.tab === id; t.setAttribute('role', 'tab'); t.setAttribute('aria-selected', selected); t.setAttribute('aria-controls', t.dataset.tab); t.tabIndex = selected ? 0 : -1; });
        panels.forEach(function (p) { p.hidden = p.id !== id; p.setAttribute('role', 'tabpanel'); p.setAttribute('aria-labelledby', 'tab-' + p.id); });
        write('tab.' + group.dataset.tabGroup, id);
        if (update) history.replaceState(null, '', '#' + id);
        window.dispatchEvent(new Event('resize'));
      }
      tabs.forEach(function (t, index) {
        t.id = 'tab-' + t.dataset.tab;
        t.addEventListener('click', function () { show(t.dataset.tab, true); });
        t.addEventListener('keydown', function (e) {
          var target;
          if (e.key === 'ArrowRight') target = tabs[(index + 1) % tabs.length];
          if (e.key === 'ArrowLeft') target = tabs[(index + tabs.length - 1) % tabs.length];
          if (e.key === 'Home') target = tabs[0];
          if (e.key === 'End') target = tabs[tabs.length - 1];
          if (target) { e.preventDefault(); show(target.dataset.tab, true); target.focus(); }
        });
      });
      var hash = location.hash.slice(1), stored = read('tab.' + group.dataset.tabGroup);
      var target = panels.find(function (p) { return p.id === hash || (hash && p.querySelector('[id="' + CSS.escape(hash) + '"]')); });
      show(target ? target.id : panels.some(function (p) { return p.id === stored; }) ? stored : tabs[0].dataset.tab, false);
      group.querySelectorAll('[data-show-tab]').forEach(function (b) { b.addEventListener('click', function () { show(b.dataset.showTab, true); }); });
    });
    document.querySelectorAll('[data-ui-dialog]').forEach(function (b) { b.addEventListener('click', function () { var dialog = document.getElementById(b.dataset.uiDialog); if (dialog && !dialog.open) dialog.showModal(); }); });
    document.querySelectorAll('[data-ui-close]').forEach(function (b) { b.addEventListener('click', function () { b.closest('dialog').close(); }); });
    document.querySelectorAll('[data-open-details]').forEach(function (b) { b.addEventListener('click', function () { var details = document.getElementById(b.dataset.openDetails); details.open = true; details.scrollIntoView({block: 'start', behavior: 'smooth'}); details.querySelector('summary').focus(); }); });
    document.querySelectorAll('[data-about-open]').forEach(function (b) { b.addEventListener('click', function () { var more = document.getElementById('teacher-more'); if (more && more.open) more.close(); }, true); });
    if (location.hash.startsWith('#prompt-')) { var prompt = document.getElementById(location.hash.slice(1)); if (prompt) prompt.open = true; }
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { var queue = document.getElementById('grading-queue'); if (queue) { queue.classList.remove('open'); queue.querySelector('button').setAttribute('aria-expanded','false'); } } });
    document.querySelectorAll('[data-open-assistant]').forEach(function (b) { b.addEventListener('click', function () { document.getElementById('assistant-fab').click(); }); });
    document.querySelectorAll('[data-open-tasks]').forEach(function (b) { b.addEventListener('click', function () { document.querySelector('.grading-queue-btn').click(); }); });
    var tagForm = document.querySelector('.tag-form[data-tag-busy="1"]');
    if (tagForm && tagForm.dataset.operationId && tagForm.dataset.statusUrl) {
      var tagPollStopped = false;
      var tagPollTimer = 0;
      function scheduleTagPoll() {
        if (!tagPollStopped) tagPollTimer = window.setTimeout(pollTagStatus, 2500);
      }
      function pollTagStatus() {
        fetch(tagForm.dataset.statusUrl + '?operation_id='
          + encodeURIComponent(tagForm.dataset.operationId), {
          headers: { Accept: 'application/json' }, cache: 'no-store'
        }).then(function (response) {
          if (!response.ok) throw new Error('状态查询失败');
          return response.json();
        }).then(function (data) {
          if (!data.ok) throw new Error(data.error || '状态查询失败');
          if (data.status === 'queued' || data.status === 'running') {
            scheduleTagPoll();
          } else {
            tagPollStopped = true;
            window.location.reload();
          }
        }).catch(scheduleTagPoll);
      }
      document.addEventListener('visibilitychange', function () {
        if (document.hidden || tagPollStopped) return;
        window.clearTimeout(tagPollTimer);
        pollTagStatus();
      });
      pollTagStatus();
    }
    document.querySelectorAll('[data-fold]').forEach(function (details) {
      var saved = read('fold.' + details.dataset.fold);
      if (saved !== null && details.dataset.forceOpen !== '1') details.open = saved === '1';
      details.addEventListener('toggle', function () { write('fold.' + details.dataset.fold, details.open ? '1' : '0'); });
    });
    document.querySelectorAll('[data-filter]').forEach(function (input) {
      input.addEventListener('input', function () {
        var q = input.value.toLocaleLowerCase().trim(), count = 0;
        document.querySelectorAll(input.dataset.filter).forEach(function (item) { item.hidden = q && !(item.dataset.search || item.textContent).toLocaleLowerCase().includes(q); if (!item.hidden) count++; });
        var result = document.getElementById(input.dataset.filterStatus); if (result) result.textContent = count ? '找到 ' + count + ' 项' : '没有匹配结果';
      });
    });
    var dirty = new Set();
    document.querySelectorAll('form[data-edit-guard]').forEach(function (form) {
      form.addEventListener('input', function () { dirty.add(form); var hint = form.querySelector('[data-dirty-hint]'); if (hint) hint.textContent = '有未保存修改'; });
      form.addEventListener('submit', function () { dirty.delete(form); });
    });
    window.addEventListener('beforeunload', function (e) { if (dirty.size) { e.preventDefault(); e.returnValue = ''; } });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize); else initialize();
})();
