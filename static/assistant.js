// 右下角数字助手（管理员/老师/学生共用）：保持当前标签页内的对话上下文（sessionStorage）
// 桌面端支持拖拽左上/左下角调整尺寸；提供 15/18 两档字号；回答按 Markdown + LaTeX 渲染。
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
  const fontButtons = Array.from(document.querySelectorAll('.assistant-font-choice'));
  const resizeHandles = Array.from(document.querySelectorAll('.assistant-resize[data-anchor]'));
  const callout = document.getElementById('assistant-callout');
  const csrf = document.querySelector('meta[name="csrf-token"]');
  const user = root.dataset.user || 'me';
  const key = 'gradelogy.assistant.' + user;
  const seenKey = 'gradelogy.assistant.seen.' + user;
  const sizeKey = 'gradelogy.assistant.size.' + user;
  const fontKey = 'gradelogy.assistant.font.' + user;
  const hintText = root.dataset.hint || '可以问我系统怎么用，或查询你的数据。';
  const FONT_SIZES = [15, 18];

  let messages = [];
  try {
    messages = JSON.parse(sessionStorage.getItem(key) || '[]');
  } catch (e) {
    messages = [];
  }

  function hasSeen() {
    try { return localStorage.getItem(seenKey) === '1'; } catch (e) { return true; }
  }
  function markSeen() {
    try { localStorage.setItem(seenKey, '1'); } catch (e) { /* 隐私模式忽略 */ }
  }

  const TOOL_LABEL = {
    ask_help: '查使用文档',
    list_courses: '查课程列表',
    list_lectures: '查讲次列表',
    lecture_status: '查作业情况',
    find_student: '查学生',
    student_report: '查批改结果',
    my_courses: '查我的课程',
    my_lectures: '查我的作业',
    my_pending: '查未交作业',
    my_report: '查我的批改',
    my_exam_scores: '查我的成绩',
    admin_overview: '查系统概况',
    admin_list_users: '查用户',
    admin_list_courses: '查全部课程',
    admin_ai_usage: '查 AI 用量'
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

  // ---------- Markdown + LaTeX 渲染 ----------
  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  // 只放行 http/https/mailto 与相对地址，阻止 javascript:/data: 等协议
  function safeUrl(url) {
    const s = String(url || '').trim();
    if (!s || /[\u0000-\u001f\u007f]/.test(s)) return '';
    const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(s);
    if (scheme && !/^(https?|mailto)$/i.test(scheme[1])) return '';
    if (!scheme && (s.indexOf('//') === 0 || s.indexOf('\\') >= 0)) return '';
    return s;
  }

  function inlineMarkdown(escaped) {
    let s = escaped;
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, label, url) {
      const href = safeUrl(url);
      if (!href) return label;
      return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">'
        + label + '</a>';
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    return s;
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '')
      .split('|').map(function (cell) { return cell.trim(); });
  }

  function renderMarkdown(source) {
    const math = [];
    // 先把数学片段抽成占位符，避免被 Markdown 规则误伤（与原报告渲染逻辑一致）
    let text = String(source == null ? '' : source);
    text = text.replace(/\$\$[\s\S]+?\$\$/g, function (m) {
      math.push(m);
      return '\u0000M' + (math.length - 1) + '\u0000';
    });
    text = text.replace(/\$[^$\n]+\$/g, function (m) {
      math.push(m);
      return '\u0000M' + (math.length - 1) + '\u0000';
    });

    const lines = text.split(/\r?\n/);
    const html = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }

      if (line.trim().indexOf('```') === 0) {
        const lang = line.trim().slice(3).trim();
        const code = [];
        i += 1;
        while (i < lines.length && lines[i].trim().indexOf('```') !== 0) {
          code.push(lines[i]);
          i += 1;
        }
        i += 1;
        html.push('<pre><code' + (lang ? ' class="lang-' + escapeHtml(lang) + '"' : '')
          + '>' + escapeHtml(code.join('\n')) + '</code></pre>');
        continue;
      }

      if (/^\s*>/.test(line)) {
        const quotes = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          quotes.push(lines[i].replace(/^\s*>\s?/, ''));
          i += 1;
        }
        html.push('<blockquote>' + quotes.map(function (q) {
          return '<p>' + inlineMarkdown(escapeHtml(q)) + '</p>';
        }).join('') + '</blockquote>');
        continue;
      }

      if (/^\s*(?:-{3,}|\*{3,})\s*$/.test(line)) {
        html.push('<hr>');
        i += 1;
        continue;
      }

      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        html.push('<h' + level + '>' + inlineMarkdown(escapeHtml(heading[2]))
          + '</h' + level + '>');
        i += 1;
        continue;
      }

      if (line.trim().indexOf('|') === 0 && i + 1 < lines.length
          && /^\s*\|[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
        const rows = [splitRow(line)];
        i += 2;
        while (i < lines.length && lines[i].trim().indexOf('|') === 0) {
          rows.push(splitRow(lines[i]));
          i += 1;
        }
        html.push('<table>');
        rows.forEach(function (cells, r) {
          const tag = r === 0 ? 'th' : 'td';
          html.push('<tr>' + cells.map(function (cell) {
            return '<' + tag + '>' + inlineMarkdown(escapeHtml(cell)) + '</' + tag + '>';
          }).join('') + '</tr>');
        });
        html.push('</table>');
        continue;
      }

      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
          i += 1;
        }
        html.push('<ol>' + items.map(function (item) {
          return '<li>' + inlineMarkdown(escapeHtml(item)) + '</li>';
        }).join('') + '</ol>');
        continue;
      }

      if (/^\s*[-*+]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
          i += 1;
        }
        html.push('<ul>' + items.map(function (item) {
          return '<li>' + inlineMarkdown(escapeHtml(item)) + '</li>';
        }).join('') + '</ul>');
        continue;
      }

      // 普通段落：连续非空行合并，单换行保留为 <br>
      const para = [];
      while (i < lines.length && lines[i].trim()
             && !/^\s*(?:```|>|#{1,4}\s|\d+\.\s+|[-*+]\s+)/.test(lines[i])
             && lines[i].trim().indexOf('|') !== 0) {
        para.push(lines[i]);
        i += 1;
      }
      html.push('<p>' + para.map(function (row) {
        return inlineMarkdown(escapeHtml(row));
      }).join('<br>') + '</p>');
    }

    // 还原数学片段：转义后交给 KaTeX 读取文本，避免 < > 被当作标签
    return html.join('').replace(/\u0000M(\d+)\u0000/g, function (m, n) {
      const raw = math[Number(n)];
      return raw == null ? '' : escapeHtml(raw);
    });
  }

  // KaTeX 脚本为 defer 加载，渲染时轮询等待 renderKatexIn 就绪
  function renderMath(el) {
    if (!el) return;
    let tries = 0;
    (function attempt() {
      if (typeof window.renderKatexIn === 'function') {
        window.renderKatexIn(el);
        return;
      }
      if (tries < 50) {
        tries += 1;
        window.setTimeout(attempt, 100);
      }
    })();
  }

  function bubble(role, text) {
    const div = document.createElement('div');
    div.className = 'assistant-msg assistant-' + role;
    if (role === 'assistant') {
      const md = document.createElement('div');
      md.className = 'assistant-md';
      md.innerHTML = renderMarkdown(text);
      div.appendChild(md);
    } else {
      div.textContent = text;
    }
    if (role === 'assistant') {
      const row = document.createElement('div');
      row.className = 'assistant-msg-row';
      row.appendChild(avatar());
      row.appendChild(div);
      body.appendChild(row);
      renderMath(div);
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
      hint.textContent = hintText;
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

  // ---------- 桌面端拖拽调整尺寸 ----------
  const MIN_W = 300;
  const MIN_H = 320;
  const MAX_W = 760;

  function isDesktop() {
    return window.matchMedia('(min-width: 521px)').matches;
  }

  function sizeLimits() {
    return {
      w: Math.min(MAX_W, Math.max(MIN_W, window.innerWidth - 48)),
      h: Math.max(MIN_H, window.innerHeight - 120)
    };
  }

  let savedSize = null;
  try {
    const stored = JSON.parse(localStorage.getItem(sizeKey) || 'null');
    if (stored && typeof stored.w === 'number' && typeof stored.h === 'number') {
      savedSize = stored;
    }
  } catch (e) { savedSize = null; }

  function applySize() {
    if (!savedSize || !isDesktop()) {
      panel.style.width = '';
      panel.style.height = '';
      panel.style.top = '';
      panel.style.bottom = '';
      return;
    }
    const limit = sizeLimits();
    panel.style.width = Math.round(Math.max(MIN_W, Math.min(savedSize.w, limit.w))) + 'px';
    const height = Math.round(Math.max(MIN_H, Math.min(savedSize.h, limit.h)));
    panel.style.height = height + 'px';
    const maxOffset = Math.max(8, window.innerHeight - height - 12);
    if (typeof savedSize.top === 'number') {
      const top = Math.max(8, Math.min(savedSize.top, maxOffset));
      panel.style.top = Math.round(top) + 'px';
      panel.style.bottom = 'auto';
    } else if (typeof savedSize.bottom === 'number') {
      const bottom = Math.max(8, Math.min(savedSize.bottom, maxOffset));
      panel.style.bottom = Math.round(bottom) + 'px';
      panel.style.top = 'auto';
    } else {
      panel.style.top = '';
      panel.style.bottom = '';
    }
  }

  let drag = null;
  function startResize(e) {
      if (!isDesktop() || e.button !== 0) return;
      e.preventDefault();
      const rect = panel.getBoundingClientRect();
      const topAnchored = root.dataset.resizeAnchor === 'top';
      drag = {
        anchor: e.currentTarget.dataset.anchor,
        handle: e.currentTarget,
        pointerId: e.pointerId,
        x: e.clientX,
        y: e.clientY,
        w: rect.width,
        h: rect.height,
        top: rect.top,
        bottom: rect.bottom,
        topAnchored: topAnchored
      };
      if (drag.handle.setPointerCapture) drag.handle.setPointerCapture(e.pointerId);
      document.body.classList.add('assistant-resizing');
  }

  function resizePanel(e) {
      if (!drag) return;
      const limit = sizeLimits();
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      const w = Math.max(MIN_W, Math.min(drag.w - dx, limit.w));
      const growsUp = drag.anchor === 'top-left';
      let h = drag.h + (growsUp ? -dy : dy);
      const maxH = Math.max(MIN_H, Math.min(
        limit.h,
        growsUp ? drag.bottom - 12 : window.innerHeight - drag.top - 12));
      h = Math.max(MIN_H, Math.min(h, maxH));
      panel.style.width = Math.round(w) + 'px';
      panel.style.height = Math.round(h) + 'px';

      // 左上锚点固定面板右下角；左下锚点固定右上角。
      if (drag.anchor === 'top-left' && drag.topAnchored) {
        panel.style.top = Math.round(drag.bottom - h) + 'px';
        panel.style.bottom = 'auto';
      } else if (drag.anchor === 'bottom-left' && !drag.topAnchored) {
        panel.style.bottom = Math.round(window.innerHeight - drag.top - h) + 'px';
        panel.style.top = 'auto';
      }
  }

  function endResize(e) {
      if (!drag) return;
      const activeDrag = drag;
      drag = null;
      document.body.classList.remove('assistant-resizing');
      if (activeDrag.handle.hasPointerCapture &&
          activeDrag.handle.hasPointerCapture(activeDrag.pointerId)) {
        activeDrag.handle.releasePointerCapture(activeDrag.pointerId);
      }
      savedSize = {
        w: Math.round(parseFloat(panel.style.width) || panel.offsetWidth),
        h: Math.round(parseFloat(panel.style.height) || panel.offsetHeight)
      };
      const top = parseFloat(panel.style.top);
      const bottom = parseFloat(panel.style.bottom);
      if (Number.isFinite(top)) savedSize.top = top;
      else if (Number.isFinite(bottom)) savedSize.bottom = bottom;
      try {
        localStorage.setItem(sizeKey, JSON.stringify(savedSize));
      } catch (err) { /* 隐私模式忽略 */ }
  }

  resizeHandles.forEach(function (handle) {
    handle.addEventListener('pointerdown', startResize);
    handle.addEventListener('pointermove', resizePanel);
    handle.addEventListener('pointerup', endResize);
    handle.addEventListener('pointercancel', endResize);
  });
  if (resizeHandles.length) {
    applySize();
    window.addEventListener('resize', applySize);
  }

  // ---------- 明确选择 15 / 18 号字号 ----------
  let fontSize = 15;
  try {
    const stored = parseInt(localStorage.getItem(fontKey) || '', 10);
    if (FONT_SIZES.includes(stored)) {
      fontSize = stored;
    } else if (stored >= 0 && stored <= 4) {
      // 兼容旧版循环字号索引 [14, 15, 16, 18, 20]。
      fontSize = stored >= 3 ? 18 : 15;
    }
  } catch (e) { fontSize = 15; }

  function applyFont() {
    root.style.setProperty('--assistant-font-size', fontSize + 'px');
    fontButtons.forEach(function (button) {
      const active = Number(button.dataset.fontSize) === fontSize;
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.title = active ? '当前字号 ' + fontSize + 'px' : '切换到 ' + button.dataset.fontSize + 'px';
    });
  }

  fontButtons.forEach(function (button) {
    button.addEventListener('click', function () {
      fontSize = Number(button.dataset.fontSize);
      try {
        localStorage.setItem(fontKey, String(fontSize));
      } catch (e) { /* 隐私模式忽略 */ }
      applyFont();
    });
  });
  applyFont();

  function open() {
    panel.hidden = false;
    fab.classList.add('assistant-fab-open');
    fab.setAttribute('aria-expanded', 'true');
    hideCallout();
    stopAttention();
    markSeen();
    renderAll();
    input.focus();
  }

  function close() {
    panel.hidden = true;
    fab.classList.remove('assistant-fab-open');
    fab.setAttribute('aria-expanded', 'false');
  }

  function showCallout() {
    if (panel.hidden === false || !callout) return;
    callout.hidden = false;
    callout.classList.add('assistant-callout-in');
    window.setTimeout(hideCallout, 9000);
  }

  function hideCallout() {
    if (callout) callout.hidden = true;
  }

  function startAttention() {
    if (hasSeen()) return;
    fab.classList.add('assistant-fab-attn');
    window.setTimeout(showCallout, 700);
  }

  function stopAttention() {
    fab.classList.remove('assistant-fab-attn');
  }

  fab.addEventListener('click', function () {
    if (panel.hidden) { open(); } else { close(); }
  });
  if (callout) {
    callout.addEventListener('click', open);
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !panel.hidden) { close(); fab.focus(); }
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

  startAttention();
})();
