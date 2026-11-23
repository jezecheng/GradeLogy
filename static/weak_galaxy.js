(function () {
  'use strict';

  var shell = document.getElementById('wp-galaxy-shell');
  if (!shell) return;
  var box = document.getElementById('wp-galaxy');
  var loading = document.getElementById('wp-galaxy-loading');
  var errorBox = document.getElementById('wp-galaxy-error');
  var courseBox = document.getElementById('wp-galaxy-courses');
  var note = document.getElementById('wp-galaxy-note');
  var listBox = document.getElementById('wp-galaxy-list');
  var viewButtons = Array.prototype.slice.call(
    document.querySelectorAll('.wp-galaxy-view'));

  if (!window.echarts) {
    fail();
    return;
  }

  var STATUS = {
    weak: { label: '薄弱', color: '#fb923c' },
    reviewed: { label: '已复习', color: '#fbbf24' },
    mastered: { label: '已掌握', color: '#34d399' }
  };

  var courses = [];
  var activeIndex = 0;
  var viewMode = 'chart';
  var chart = null;
  var fitTimers = [];
  var roamTouched = false;
  var resizeTimer = null;

  viewButtons.forEach(function (button) {
    button.addEventListener('click', function () {
      var mode = button.dataset.view;
      if (!mode || mode === viewMode) return;
      viewMode = mode;
      viewButtons.forEach(function (item) {
        item.classList.toggle('is-active', item === button);
      });
      renderActive();
    });
  });

  fetch(shell.dataset.url, { headers: { Accept: 'application/json' } })
    .then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    })
    .then(function (payload) {
      courses = payload.courses || [];
      if (loading) loading.style.display = 'none';
      if (!courses.length) {
        empty();
        return;
      }
      renderCourses();
      renderActive();
    })
    .catch(fail);

  function fail() {
    if (loading) loading.style.display = 'none';
    if (errorBox) errorBox.hidden = false;
  }

  function empty() {
    if (box) box.style.display = 'none';
    if (courseBox) courseBox.innerHTML = '';
    if (note) {
      note.hidden = false;
      note.textContent = '薄弱标签还没有匹配到课程知识图谱，可先查看下方列表。';
    }
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function litCount(course) {
    return course.nodes.filter(function (node) { return node.weak; }).length;
  }

  function renderCourses() {
    if (!courseBox) return;
    courseBox.innerHTML = courses.map(function (course, index) {
      var label = course.nodes.length
        ? '点亮 ' + litCount(course)
        : '未关联图谱';
      return '<button type="button" class="wp-galaxy-course'
        + (index === activeIndex ? ' is-active' : '') + '" data-index="' + index + '">'
        + escapeHtml(course.name) + ' · ' + label + '</button>';
    }).join('');
    Array.prototype.forEach.call(
      courseBox.querySelectorAll('.wp-galaxy-course'), function (button) {
        button.addEventListener('click', function () {
          var index = Number(button.dataset.index);
          if (!isFinite(index) || index === activeIndex) return;
          activeIndex = index;
          renderCourses();
          renderActive();
        });
      });
  }

  function renderNote(course) {
    if (!note) return;
    if (!course.nodes.length) {
      note.hidden = false;
      note.textContent = '该课程的 ' + course.weak_total
        + ' 个薄弱知识点都还没有关联知识图谱，已在下方列表中展示；关联图谱后会在这里点亮。';
      return;
    }
    if (course.unmatched > 0) {
      note.hidden = false;
      note.textContent = '另有 ' + course.unmatched
        + ' 个薄弱标签未匹配到知识图谱，已在下方列表中展示。';
    } else {
      note.hidden = true;
      note.textContent = '';
    }
  }

  function renderActive() {
    var course = courses[activeIndex];
    if (!course) return;
    renderNote(course);
    if (viewMode === 'list') {
      if (box) box.hidden = true;
      if (listBox) listBox.hidden = false;
      renderList(course);
      return;
    }
    if (listBox) listBox.hidden = true;
    if (box) box.hidden = false;
    renderChart();
    if (chart) chart.resize();
  }

  function verdictSymbol(verdict) {
    if (verdict === '对') return '✓';
    if (verdict === '半对') return '◐';
    return '✗';
  }

  function rateColor(rate) {
    var value = Math.max(0, Math.min(100, Number(rate) || 0));
    var from = value <= 50 ? [52, 211, 153] : [251, 191, 36];
    var to = value <= 50 ? [251, 191, 36] : [239, 68, 68];
    var amount = value <= 50 ? value / 50 : (value - 50) / 50;
    var rgb = from.map(function (channel, index) {
      return Math.round(channel + (to[index] - channel) * amount);
    });
    return '#' + rgb.map(function (channel) {
      return channel.toString(16).padStart(2, '0');
    }).join('');
  }

  function renderList(course) {
    if (!listBox) return;
    var nodes = course.nodes.filter(function (node) {
      return node.weak;
    }).sort(function (a, b) {
      return (b.wrong_count + b.partial_count) - (a.wrong_count + a.partial_count)
        || String(a.name).localeCompare(String(b.name), 'zh');
    });
    if (!nodes.length) {
      listBox.innerHTML = '<p class="wp-galaxy-list-empty">'
        + '该课程暂无匹配到知识图谱的薄弱点，可查看下方卡片列表。</p>';
      return;
    }
    listBox.innerHTML = nodes.map(function (node) {
      var status = STATUS[node.status] || STATUS.weak;
      var trend = (node.trend || []).map(verdictSymbol).join(' ');
      var meta = '错 ' + node.wrong_count + ' / 半对 ' + node.partial_count
        + ' / 共 ' + node.total_count + ' 题'
        + (trend ? ' · 近 ' + node.trend.length + ' 次 ' + trend : '');
      return '<div class="wp-galaxy-list-item" data-key="' + escapeHtml(node.key) + '">'
        + '<span class="wp-gl-name">' + escapeHtml(node.name) + '</span>'
        + '<span class="wp-gl-meta" style="color:' + status.color + '">'
        + status.label + '</span>'
        + '<span class="wp-gl-meta">' + escapeHtml(meta) + '</span>'
        + '<span class="wp-gl-rate" style="color:' + rateColor(node.error_rate) + '">'
        + '错误率 ' + node.error_rate + '%</span>'
        + '</div>';
    }).join('');
    Array.prototype.forEach.call(
      listBox.querySelectorAll('.wp-galaxy-list-item'), function (row) {
        row.addEventListener('click', function () { focusCard(row.dataset.key); });
      });
  }

  function renderChart() {
    var course = courses[activeIndex];
    if (!course) return;
    if (!chart) {
      chart = echarts.init(box, null, { renderer: 'canvas' });
      chart.on('graphRoam', function () { roamTouched = true; });
    }
    // 整门课都没有匹配到图谱：清空画布，只保留上方提示
    if (!course.nodes.length) {
      chart.clear();
      return;
    }

    var lit = {};
    course.nodes.forEach(function (node) {
      if (node.weak) lit[String(node.id)] = true;
    });

    var data = course.nodes.map(function (node) {
      if (!node.weak) {
        return {
          id: String(node.id),
          name: node.name,
          kind: 'context',
          category: node.category,
          definition: node.definition,
          symbolSize: 7,
          itemStyle: {
            color: '#1e293b',
            borderColor: 'rgba(148,163,184,.35)',
            borderWidth: 0.8,
            opacity: 0.8
          },
          label: { show: false }
        };
      }
      var status = STATUS[node.status] || STATUS.weak;
      var weight = node.wrong_count + node.partial_count * 0.6;
      var errorRate = Math.max(0, Math.min(100, Number(node.error_rate) || 0));
      var severityColor = rateColor(errorRate);
      return {
        id: String(node.id),
        name: node.name,
        kind: 'weak',
        key: node.key,
        status: node.status,
        statusLabel: status.label,
        category: node.category,
        definition: node.definition,
        rightCount: node.right_count,
        partialCount: node.partial_count,
        wrongCount: node.wrong_count,
        totalCount: node.total_count,
        errorRate: node.error_rate || 0,
        trend: node.trend || [],
        tags: node.tags || [],
        // Error rate drives visual severity; raw miss count adds modest emphasis.
        symbolSize: Math.min(42, 14 + Math.sqrt(weight + 1) * 3.5 + errorRate * 0.13),
        itemStyle: {
          color: severityColor,
          borderColor: status.color,
          borderWidth: node.status === 'weak' ? 3 : 2,
          shadowColor: severityColor,
          shadowBlur: node.status === 'weak' ? 24 : 12
        },
        label: {
          show: true,
          color: node.status === 'weak' ? '#fed7aa' : '#e2e8f0'
        }
      };
    });

    var edges = course.edges.map(function (edge) {
      var warm = lit[String(edge.src)] || lit[String(edge.dst)];
      return {
        source: String(edge.src),
        target: String(edge.dst),
        relation: edge.relation,
        symbol: edge.relation === 'related' ? ['none', 'none'] : ['none', 'arrow'],
        symbolSize: edge.relation === 'prerequisite' ? 6 : 4,
        lineStyle: {
          color: warm ? '#f59e0b' : '#334155',
          width: warm ? 1.2 : 0.7,
          opacity: warm ? 0.5 : 0.22,
          curveness: 0.15
        }
      };
    });

    chart.setOption({
      backgroundColor: 'transparent',
      animationDuration: 300,
      tooltip: {
        confine: true,
        backgroundColor: 'rgba(9,14,26,.94)',
        borderColor: 'rgba(251,146,60,.4)',
        textStyle: { color: '#e2e8f0', fontSize: 12 },
        formatter: function (params) {
          if (params.dataType !== 'node' || !params.data) return '';
          var node = params.data;
          if (node.kind === 'context') {
            return '<b>' + escapeHtml(node.name) + '</b>'
              + '<br><span style="color:#94a3b8">背景知识 · 用于理解前后置关系</span>';
          }
          var lines = ['<b>' + escapeHtml(node.name) + '</b>',
            node.statusLabel + ' · 错误率 ' + node.errorRate + '%',
            '错 ' + node.wrongCount + ' / 半对 ' + node.partialCount
              + ' / 共 ' + node.totalCount + ' 题'];
          if (node.trend.length) {
            lines.push('<span style="color:#94a3b8">近 ' + node.trend.length + ' 次：'
              + node.trend.map(verdictSymbol).join(' ') + '</span>');
          }
          if (node.tags.length) {
            lines.push('<span style="color:#94a3b8">标签：'
              + escapeHtml(node.tags.join('、')) + '</span>');
          }
          if (node.definition) {
            lines.push('<span style="color:#94a3b8">'
              + escapeHtml(node.definition.slice(0, 70)) + '</span>');
          }
          lines.push('<span style="color:#fdba74">点击定位到下方卡片</span>');
          return lines.join('<br>');
        }
      },
      series: [{
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        zoom: 1,
        scaleLimit: { min: 0.2, max: 6 },
        force: {
          initLayout: 'circular',
          repulsion: 220,
          gravity: 0.08,
          edgeLength: [60, 160],
          friction: 0.25,
          layoutAnimation: true
        },
        label: {
          position: 'right',
          distance: 4,
          fontSize: 11,
          textBorderColor: 'rgba(5,9,20,.92)',
          textBorderWidth: 2.5
        },
        labelLayout: { hideOverlap: true },
        lineStyle: { curveness: 0.15 },
        emphasis: {
          focus: 'adjacency',
          scale: false,
          label: { show: true, color: '#ffffff' },
          itemStyle: {
            borderColor: '#ffffff',
            borderWidth: 2,
            shadowBlur: 30,
            shadowColor: 'rgba(251,191,36,.85)'
          },
          lineStyle: { opacity: 0.95, width: 2 }
        },
        blur: {
          itemStyle: { opacity: 0.1 },
          label: { opacity: 0.06 },
          lineStyle: { opacity: 0.05 }
        },
        data: data,
        edges: edges
      }]
    }, true);

    chart.off('click');
    chart.on('click', function (params) {
      if (params.dataType !== 'node' || !params.data) return;
      if (params.data.kind !== 'weak') return;
      focusCard(params.data.key);
    });
    scheduleFit();
  }

  function focusCard(key) {
    if (!key) return;
    var allButton = document.querySelector('.wp-filter-btn[data-filter="all"]');
    if (allButton && !allButton.classList.contains('is-active')) allButton.click();
    var card = document.querySelector(
      '.weak-point-card[data-group="' + String(key).replace(/"/g, '\\"') + '"]');
    if (!card) return;
    var panel = card.closest('details');
    if (panel) panel.open = true;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('is-focus');
    window.setTimeout(function () { card.classList.remove('is-focus'); }, 1800);
  }

  function scheduleFit() {
    roamTouched = false;
    fitTimers.forEach(function (timer) { clearTimeout(timer); });
    fitTimers = [700, 2000].map(function (delay) {
      return window.setTimeout(function () {
        if (!roamTouched) fitToView();
      }, delay);
    });
  }

  function fitToView() {
    if (!chart) return;
    var series = chart.getModel().getSeriesByIndex(0);
    if (!series) return;
    var data = series.getData();
    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    for (var i = 0; i < data.count(); i += 1) {
      var layout = data.getItemLayout(i);
      if (!layout || !isFinite(layout[0]) || !isFinite(layout[1])) continue;
      minX = Math.min(minX, layout[0]);
      minY = Math.min(minY, layout[1]);
      maxX = Math.max(maxX, layout[0]);
      maxY = Math.max(maxY, layout[1]);
    }
    if (!isFinite(minX) || !isFinite(maxX)) return;
    var width = Math.max(1, maxX - minX);
    var height = Math.max(1, maxY - minY);
    if (width > 20000 || height > 20000) return;
    var zoom = Math.min(chart.getWidth() / width, chart.getHeight() / height) * 0.86;
    chart.setOption({
      series: [{
        center: [(minX + maxX) / 2, (minY + maxY) / 2],
        zoom: Math.min(Math.max(zoom, 0.3), 2.4)
      }]
    });
  }

  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (chart) chart.resize();
    }, 120);
  });
}());
