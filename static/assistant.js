// 教师端右下角数字助手：保持当前标签页内的对话上下文（sessionStorage）
(function () {
  const root = document.getElementById('assistant');
  if (!root) return;

  const fab = document.getElementById('assistant-fab');
  const panel = document.getElementById('assistant-panel');
  const body = document.getElementById('assistant-body');
  const form = document.getElementById('assistant-form');
  const input = document.getElementById('assistant-input');
  const sendBtn = document.getElementById('assistant-send');
  const resetBtn = document.getElementById('assistant-reset');
  const closeBtn = document.getElementById('assistant-close');
  const csrf = document.querySelector('meta[name="csrf-token"]');
  const key = 'gradelogy.assistant.' + (root.dataset.user || 'me');

  let messages = [];
  try {
    messages = JSON.parse(sessionStorage.getItem(key) || '[]');
  } catch (e) {
    messages = [];
  }

  const TOOL_LABEL = {
    ask_help: '查使用文档',
    list_courses: '查课程列表',
    list_lectures: '查讲次列表',
    lecture_status: '查作业情况',
    find_student: '查学生',
    student_report: '查批改结果'
  };

  function save() {
    try {
      sessionStorage.setItem(key, JSON.stringify(messages.slice(-20)));
    } catch (e) { /* 隐私模式忽略 */ }
  }

  function avatar() {
    const img = document.createElement('img');
    img.className = 'assistant-avatar';
    img.src = root.dataset.avatar;
    img.alt = '';
    img.setAttribute('aria-hidden', 'true');
    return img;
  }

  function bubble(role, text) {
    const div = document.createElement('div');
    div.className = 'assistant-msg assistant-' + role;
    div.textContent = text;
    if (role === 'assistant') {
      const row = document.createElement('div');
      row.className = 'assistant-msg-row';
      row.appendChild(avatar());
      row.appendChild(div);
      body.appendChild(row);
    } else {
      body.appendChild(div);
    }
    body.scrollTop = body.scrollHeight;
    return div;
  }

  function chips(trace) {
    if (!trace || !trace.length) return;
    const box = document.createElement('div');
    box.className = 'assistant-trace';
    trace.forEach(function (t) {
      const span = document.createElement('span');
      span.textContent = TOOL_LABEL[t.tool] || t.tool;
      box.appendChild(span);
    });
    body.appendChild(box);
    body.scrollTop = body.scrollHeight;
  }

  function renderAll() {
    body.innerHTML = '';
    if (!messages.length) {
      const hint = document.createElement('p');
      hint.className = 'assistant-hint';
      hint.textContent = '可以问我：某次作业的完成情况、某个学生的批改结果、某个功能怎么用。';
      body.appendChild(hint);
      return;
    }
    messages.forEach(function (m) { bubble(m.role, m.content); });
  }

  function pending() {
    const row = document.createElement('div');
    row.className = 'assistant-msg-row';
    row.appendChild(avatar());
    const div = document.createElement('div');
    div.className = 'assistant-msg assistant-assistant assistant-pending';
    div.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
    row.appendChild(div);
    body.appendChild(row);
    body.scrollTop = body.scrollHeight;
    return row;
  }

  async function send() {
    const text = input.value.trim();
    if (!text || sendBtn.disabled) return;
    input.value = '';
    messages.push({ role: 'user', content: text });
    bubble('user', text);
    save();

    sendBtn.disabled = true;
    sendBtn.textContent = '…';
    const dot = pending();
    try {
      const resp = await fetch('/assistant/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf ? csrf.content : ''
        },
        body: JSON.stringify({ messages: messages.slice(-12) })
      });
      const data = await resp.json().catch(function () { return {}; });
      dot.remove();
      if (!resp.ok) {
        bubble('error', data.error || ('请求失败（' + resp.status + '）'));
        return;
      }
      chips(data.trace);
      const answer = data.reply || '（没有返回内容）';
      messages.push({ role: 'assistant', content: answer });
      bubble('assistant', answer);
      save();
    } catch (e) {
      dot.remove();
      bubble('error', '网络异常：' + e.message);
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = '发送';
      input.focus();
    }
  }

  function open() {
    panel.hidden = false;
    fab.classList.add('assistant-fab-open');
    renderAll();
    input.focus();
  }

  function close() {
    panel.hidden = true;
    fab.classList.remove('assistant-fab-open');
  }

  fab.addEventListener('click', function () {
    if (panel.hidden) { open(); } else { close(); }
  });
  closeBtn.addEventListener('click', close);
  resetBtn.addEventListener('click', function () {
    if (!messages.length || confirm('清空当前对话？')) {
      messages = [];
      save();
      renderAll();
    }
  });
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    send();
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
})();
