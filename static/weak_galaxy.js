(function () {
  'use strict';

  var shell = document.getElementById('wp-galaxy-shell');
  if (!shell) return;
  var box = document.getElementById('wp-galaxy');
  var loading = document.getElementById('wp-galaxy-loading');
  var errorBox = document.getElementById('wp-galaxy-error');
  var courseBox = document.getElementById('wp-galaxy-courses');
  var note = document.getElementById('wp-galaxy-note');

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
  var chart = null;
  var fitTimers = [];
  var roamTouched = false;
  var resizeTimer = null;

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
      renderChart();
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
      return '<button type="button" class="wp-galaxy-course'
        + (index === activeIndex ? ' is-active' : '') + '" data-index="' + index + '">'
        + escapeHtml(course.name) + ' · 点亮 ' + litCount(course) + '</button>';
    }).join('');
    Array.prototype.forEach.call(
      courseBox.querySelectorAll('.wp-galaxy-course'), function (button) {
        button.addEventListener('click', function () {
          var index = Number(button.dataset.index);
          if (!isFinite(index) || index === activeIndex) return;
          activeIndex = index;
          renderCourses();
          renderChart();
        });
      });
  }

  function renderNote(course) {
    if (!note) return;
    if (course.unmatched > 0) {
      note.hidden = false;
      note.textContent = '另有 ' + course.unmatched
        + ' 个薄弱标签未匹配到知识图谱，已在下方列表中展示。';
    } else {
      note.hidden = true;
      note.textContent = '';
    }
  }

  function renderChart() {
    var course = courses[activeIndex];
    if (!course) return;
    renderNote(course);
    if (!chart) {
      chart = echarts.init(box, null, { renderer: 'canvas' });
      chart.on('graphRoam', function () { roamTouched = true; });
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
        tags: node.tags || [],
        symbolSize: Math.min(30, 15 + Math.sqrt(weight + 1) * 5),
        itemStyle: {
          color: status.color,
          borderColor: 'rgba(255,255,255,.9)',
          borderWidth: 1.4,
          shadowColor: status.color,
          shadowBlur: node.status === 'weak' ? 28 : 14
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
            node.statusLabel + ' · 错 ' + node.wrongCount + ' / 半对 '
              + node.partialCount + ' / 共 ' + node.totalCount + ' 题'];
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
    var zoom = Math.min(chart.getWidth() / width, chart.getHeight() / height) * 0.78;
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
