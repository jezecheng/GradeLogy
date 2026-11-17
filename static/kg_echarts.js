(function () {
  'use strict';

  var shell = document.getElementById('kg-galaxy-shell');
  if (!shell) return;
  var container = document.getElementById('kg-galaxy');
  var loading = document.getElementById('kg-galaxy-loading');
  var errorBox = document.getElementById('kg-galaxy-error');
  var categoryBox = document.getElementById('kg-galaxy-categories');
  var drawer = document.getElementById('kg-drawer');
  var drawerBody = document.getElementById('kg-drawer-body');
  var drawerClose = document.getElementById('kg-drawer-close');

  if (!window.echarts) {
    showError();
    return;
  }

  var PALETTE = ['#38bdf8', '#a78bfa', '#34d399', '#f472b6', '#fbbf24',
    '#22d3ee', '#fb7185', '#a3e635', '#c084fc', '#60a5fa',
    '#f97316', '#2dd4bf', '#e879f9', '#facc15', '#4ade80', '#818cf8'];
  var LEGEND_LIMIT = 12;
  var TOP_LABELS = 50;
  var RELATION_LABEL = { prerequisite: '前置', contains: '包含', related: '相关' };

  var chart = null;
  var nodes = [];
  var nodeById = {};
  var dataIndexById = {};
  var edges = [];
  var rankedIds = [];
  var rankById = {};
  var labelSet = {};
  var selectedId = null;
  var resizeTimer = null;

  var overlay = null;
  var overlayCtx = null;
  var particles = [];
  var particleFrame = 0;
  var lastParticleTime = 0;
  var prefersReduced = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  fetch(shell.dataset.url, { headers: { Accept: 'application/json' } })
    .then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    })
    .then(render)
    .catch(function () { showError(); });

  function showError() {
    if (loading) loading.style.display = 'none';
    if (errorBox) errorBox.hidden = false;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function radiusFor(degree) {
    return Math.max(8, Math.min(32, 7 + Math.sqrt(degree || 0) * 5));
  }

  function relationStyle(relation) {
    if (relation === 'contains') {
      return { color: '#34d399', width: 1.1, type: 'dashed', opacity: 0.5 };
    }
    if (relation === 'related') {
      return { color: '#94a3b8', width: 0.8, type: 'solid', opacity: 0.18 };
    }
    return { color: '#38bdf8', width: 1.4, type: 'solid', opacity: 0.7 };
  }

  function render(payload) {
    var categories = payload.categories || [];
    var colorByName = {};
    categories.forEach(function (category, index) {
      colorByName[category.name] = PALETTE[index % PALETTE.length];
    });

    nodes = payload.nodes || [];
    edges = payload.edges || [];
    var top = nodes.slice().sort(function (a, b) {
      return b.degree - a.degree || String(a.id).localeCompare(String(b.id));
    });
    rankedIds = top.map(function (node) { return String(node.id); });
    nodes.forEach(function (node) { nodeById[String(node.id)] = node; });

    var hubCut = Math.max(3, Math.round(nodes.length * 0.02));
    rankById = {};
    rankedIds.forEach(function (id, index) { rankById[id] = index; });
    labelSet = {};
    rankedIds.slice(0, TOP_LABELS).forEach(function (id) { labelSet[id] = true; });

    var graphNodes = nodes.map(function (node, index) {
      var id = String(node.id);
      dataIndexById[id] = index;
      var color = colorByName[node.category] || PALETTE[0];
      var isHub = (rankById[id] || 0) < hubCut;
      return {
        id: id,
        name: node.name,
        value: node.degree,
        category: node.category,
        degree: node.degree,
        definition: node.definition,
        confidence: node.confidence,
        symbolSize: radiusFor(node.degree),
        itemStyle: {
          color: color,
          borderColor: isHub ? 'rgba(255,255,255,.85)' : 'rgba(255,255,255,.42)',
          borderWidth: isHub ? 1.6 : 0.6,
          shadowColor: color,
          shadowBlur: isHub ? 26 : 9
        }
      };
    });

    var graphEdges = edges.map(function (edge) {
      var style = relationStyle(edge.relation);
      var arrow = edge.relation !== 'related';
      return {
        source: String(edge.src_id),
        target: String(edge.dst_id),
        relation: edge.relation,
        evidence: edge.evidence,
        symbol: arrow ? ['none', 'arrow'] : ['none', 'none'],
        symbolSize: edge.relation === 'prerequisite' ? 6 : 5,
        lineStyle: {
          color: style.color,
          width: style.width,
          type: style.type,
          opacity: style.opacity,
          curveness: 0.2
        }
      };
    });

    var big = nodes.length > 600;
    chart = echarts.init(container, null, { renderer: 'canvas' });
    chart.setOption({
      backgroundColor: 'transparent',
      animationDuration: 260,
      tooltip: {
        confine: true,
        backgroundColor: 'rgba(9,14,26,.94)',
        borderColor: 'rgba(56,189,248,.35)',
        textStyle: { color: '#dbeafe', fontSize: 12 },
        formatter: function (params) {
          if (params.dataType !== 'node') return '';
          var data = params.data;
          return '<b>' + escapeHtml(data.name) + '</b><br>'
            + escapeHtml(data.category) + ' · 连接度 ' + data.degree
            + (data.definition
              ? '<br><span style="color:#94a3b8">'
                + escapeHtml(data.definition.slice(0, 90)) + '</span>'
              : '');
        }
      },
      series: [{
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        zoom: 1,
        scaleLimit: { min: 0.15, max: 8 },
        categories: categories.map(function (category) {
          return { name: category.name };
        }),
        data: graphNodes,
        edges: graphEdges,
        force: {
          initLayout: 'circular',
          repulsion: big ? 55 : 150,
          gravity: big ? 0.08 : 0.06,
          edgeLength: big ? [24, 70] : [50, 140],
          friction: 0.2,
          layoutAnimation: true
        },
        label: {
          show: true,
          position: 'right',
          distance: 4,
          color: '#cbd5e1',
          fontSize: 11,
          textBorderColor: 'rgba(5,9,20,.92)',
          textBorderWidth: 2.5,
          formatter: labelFormatter(labelSet)
        },
        labelLayout: { hideOverlap: true },
        lineStyle: { curveness: 0.2 },
        emphasis: {
          focus: 'adjacency',
          scale: false,
          label: { show: true, color: '#ffffff' },
          itemStyle: {
            borderColor: '#ffffff',
            borderWidth: 2,
            shadowBlur: 32,
            shadowColor: 'rgba(191,219,254,.9)'
          },
          lineStyle: { opacity: 0.95, width: 2 }
        },
        blur: {
          itemStyle: { opacity: 0.1 },
          label: { opacity: 0.08 },
          lineStyle: { opacity: 0.06 }
        }
      }]
    }, true);

    renderCategories(categories);
    bindEvents();
    if (loading) loading.style.display = 'none';
    buildOverlay();
    startParticles();
  }

  function labelFormatter(visible) {
    return function (params) {
      var id = params.data && String(params.data.id);
      return visible[id] ? (params.data.name || '') : '';
    };
  }

  function renderCategories(categories) {
    if (!categoryBox) return;
    var shown = categories.slice(0, LEGEND_LIMIT);
    var html = shown.map(function (category, index) {
      var color = PALETTE[index % PALETTE.length];
      return '<span class="kg-cluster-chip"><i style="background:'
        + color + '"></i>' + escapeHtml(category.name)
        + '<b>' + category.count + '</b></span>';
    }).join('');
    if (categories.length > shown.length) {
      html += '<span class="kg-cluster-more">+' + (categories.length - shown.length)
        + ' 个分类</span>';
    }
    categoryBox.innerHTML = html;
  }

  function bindEvents() {
    chart.on('click', function (params) {
      if (params.dataType !== 'node') return;
      var id = String(params.data.id);
      selectedId = id;
      highlight(id);
      openDrawer(id);
    });
    chart.getZr().on('click', function (event) {
      if (event.target) return;
      closeDrawer();
      chart.dispatchAction({ type: 'downplay', seriesIndex: 0 });
      selectedId = null;
      refreshLabels();
    });
    chart.on('graphRoam', function () {
      scheduleLabelRefresh();
    });
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        chart.resize();
        resizeOverlay();
      }, 120);
    });
    if (drawerClose) {
      drawerClose.addEventListener('click', closeDrawer);
    }
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closeDrawer();
    });
  }

  function highlight(id) {
    var dataIndex = dataIndexById[id];
    if (dataIndex == null) return;
    chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: dataIndex });
  }

  var labelTimer = null;

  function scheduleLabelRefresh() {
    clearTimeout(labelTimer);
    labelTimer = setTimeout(refreshLabels, 160);
  }

  function refreshLabels() {
    if (!chart) return;
    var series = chart.getModel().getSeriesByIndex(0);
    if (!series) return;
    var coord = series.coordinateSystem;
    var zoom = coord && coord.getZoom ? coord.getZoom() : 1;
    var visible = [];
    if (zoom >= 1.25) {
      var data = series.getData();
      var width = chart.getWidth();
      var height = chart.getHeight();
      for (var i = 0; i < data.count(); i += 1) {
        var layout = data.getItemLayout(i);
        if (!layout) continue;
        var point = null;
        try { point = chart.convertToPixel({ seriesIndex: 0 }, layout); } catch (error) { point = null; }
        if (!point || !isFinite(point[0]) || !isFinite(point[1])) continue;
        if (point[0] > -70 && point[0] < width + 70
          && point[1] > -50 && point[1] < height + 50) {
          visible.push(String(data.getId(i)));
        }
      }
    }
    // 视野内标签仍按连接度分层，避免一次铺满造成重叠
    var limit = zoom < 2 ? 120 : (zoom < 3 ? 260 : (zoom < 4.5 ? 520 : nodes.length));
    visible.sort(function (a, b) {
      return (rankById[a] || 0) - (rankById[b] || 0);
    });
    var next = {};
    rankedIds.slice(0, TOP_LABELS).forEach(function (id) { next[id] = true; });
    visible.slice(0, limit).forEach(function (id) { next[id] = true; });
    if (!visible.length && zoom >= 1.25) {
      rankedIds.slice(0, limit).forEach(function (id) { next[id] = true; });
    }
    if (selectedId) next[selectedId] = true;
    labelSet = next;
    chart.setOption({
      series: [{ label: { formatter: labelFormatter(labelSet) } }]
    });
  }

  function openDrawer(id) {
    if (!drawer || !drawerBody) return;
    var node = nodeById[id];
    drawerBody.innerHTML = renderDrawerShell(node);
    drawer.setAttribute('aria-hidden', 'false');
    drawer.classList.add('open');
    if (window.renderKatexIn) window.renderKatexIn(drawerBody);
    fetch(shell.dataset.neighborsUrl + '?node_id=' + encodeURIComponent(id),
      { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        drawerBody.innerHTML = renderDrawer(payload);
        if (window.renderKatexIn) window.renderKatexIn(drawerBody);
      })
      .catch(function () {
        var note = drawerBody.querySelector('.kg-drawer-loading');
        if (note) note.textContent = '关联知识加载失败，请稍后重试。';
      });
  }

  function renderDrawerShell(node) {
    if (!node) return '<p class="kg-drawer-loading">正在读取知识点…</p>';
    return '<p class="kg-drawer-kicker">知识点</p>'
      + '<h2 class="kg-drawer-title">' + escapeHtml(node.name) + '</h2>'
      + '<p class="kg-drawer-def">' + escapeHtml(node.definition || '暂无定义') + '</p>'
      + '<p class="kg-drawer-loading">正在加载上下游关系…</p>';
  }

  function renderDrawer(payload) {
    var node = payload.node || {};
    var html = '<p class="kg-drawer-kicker">'
      + escapeHtml(node.category || '未分类') + '</p>'
      + '<h2 class="kg-drawer-title">' + escapeHtml(node.name) + '</h2>'
      + '<p class="kg-drawer-def">' + escapeHtml(node.definition || '暂无标准定义') + '</p>'
      + '<div class="kg-drawer-meta">'
      + '<span>连接度 <b>' + (nodeById[String(node.id)] || {}).degree + '</b></span>'
      + '<span>置信度 <b>' + Math.round((node.confidence || 0) * 100) + '%</b></span>'
      + '</div>';
    html += drawerSection('前置知识', payload.prerequisites, '暂无记录的前置知识');
    html += drawerSection('延伸知识', payload.dependents, '暂无延伸知识');
    html += drawerSection('相关／包含', payload.others, '暂无相关概念');
    if (node.url) {
      html += '<a class="kg-drawer-page" href="' + escapeHtml(node.url)
        + '">查看完整概念页 →</a>';
    }
    return html;
  }

  function drawerSection(title, items, emptyText) {
    var list = (items || []).map(function (item) {
      return '<li><a href="' + escapeHtml(item.url || '#') + '">'
        + escapeHtml(item.name) + '</a>'
        + '<span class="kg-drawer-rel">'
        + escapeHtml(RELATION_LABEL[item.relation] || item.relation || '') + '</span>'
        + (item.evidence
          ? '<p class="kg-drawer-evidence">' + escapeHtml(item.evidence) + '</p>'
          : '')
        + '</li>';
    }).join('');
    return '<section class="kg-drawer-section"><h3>' + escapeHtml(title)
      + '<b>' + (items || []).length + '</b></h3>'
      + (list ? '<ul>' + list + '</ul>'
        : '<p class="kg-drawer-empty">' + escapeHtml(emptyText) + '</p>')
      + '</section>';
  }

  function closeDrawer() {
    if (!drawer) return;
    drawer.setAttribute('aria-hidden', 'true');
    drawer.classList.remove('open');
  }

  function buildOverlay() {
    if (prefersReduced || overlay) return;
    overlay = document.createElement('canvas');
    overlay.className = 'kg-galaxy-particles';
    container.appendChild(overlay);
    overlayCtx = overlay.getContext('2d');
    resizeOverlay();
  }

  function resizeOverlay() {
    if (!overlay || !overlayCtx) return;
    var ratio = window.devicePixelRatio || 1;
    var width = container.clientWidth;
    var height = container.clientHeight;
    overlay.width = Math.max(1, Math.round(width * ratio));
    overlay.height = Math.max(1, Math.round(height * ratio));
    overlay.style.width = width + 'px';
    overlay.style.height = height + 'px';
    overlayCtx.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function prepareParticles() {
    particles = [];
    var candidates = edges.filter(function (edge) {
      return edge.relation === 'prerequisite';
    }).map(function (edge) {
      var src = nodeById[String(edge.src_id)];
      var dst = nodeById[String(edge.dst_id)];
      return { edge: edge, weight: Math.min(src ? src.degree : 0, dst ? dst.degree : 0) };
    }).sort(function (a, b) { return b.weight - a.weight; }).slice(0, 180);
    candidates.forEach(function (item, index) {
      particles.push({
        source: String(item.edge.src_id),
        target: String(item.edge.dst_id),
        t: (index / candidates.length) % 1,
        speed: 0.0022 + (index % 5) * 0.00055
      });
    });
  }

  function screenPoint(series, index) {
    var layout = series.getData().getItemLayout(index);
    if (!layout) return null;
    try {
      var point = chart.convertToPixel({ seriesIndex: 0 }, layout);
      if (point && isFinite(point[0]) && isFinite(point[1])) return point;
    } catch (error) { /* 布局未就绪时跳过 */ }
    return null;
  }

  function drawParticles(time) {
    particleFrame = window.requestAnimationFrame(drawParticles);
    if (!overlayCtx || !chart) return;
    if (time - lastParticleTime < 33) return;
    lastParticleTime = time;
    var width = container.clientWidth;
    var height = container.clientHeight;
    overlayCtx.clearRect(0, 0, width, height);
    if (!particles.length) return;

    var series = chart.getModel().getSeriesByIndex(0);
    if (!series) return;
    var data = series.getData();
    var idToIndex = {};
    for (var i = 0; i < data.count(); i += 1) {
      idToIndex[String(data.getId(i))] = i;
    }
    var cache = {};
    function pointOf(id) {
      if (cache[id]) return cache[id];
      var index = idToIndex[id];
      var point = index == null ? null : screenPoint(series, index);
      cache[id] = point;
      return point;
    }

    overlayCtx.save();
    overlayCtx.globalCompositeOperation = 'lighter';
    particles.forEach(function (particle) {
      particle.t += particle.speed;
      if (particle.t > 1) particle.t -= 1;
      var source = pointOf(particle.source);
      var target = pointOf(particle.target);
      if (!source || !target) return;
      var controlX = (source[0] + target[0]) / 2 - (source[1] - target[1]) * 0.2;
      var controlY = (source[1] + target[1]) / 2 - (target[0] - source[0]) * 0.2;
      var t = particle.t;
      var mt = 1 - t;
      var x = mt * mt * source[0] + 2 * mt * t * controlX + t * t * target[0];
      var y = mt * mt * source[1] + 2 * mt * t * controlY + t * t * target[1];
      var glow = overlayCtx.createRadialGradient(x, y, 0, x, y, 5);
      glow.addColorStop(0, 'rgba(147,214,255,.95)');
      glow.addColorStop(1, 'rgba(56,189,248,0)');
      overlayCtx.fillStyle = glow;
      overlayCtx.beginPath();
      overlayCtx.arc(x, y, 5, 0, Math.PI * 2);
      overlayCtx.fill();
    });
    overlayCtx.restore();
  }

  function startParticles() {
    if (prefersReduced || !overlayCtx) return;
    prepareParticles();
    if (!particleFrame) particleFrame = window.requestAnimationFrame(drawParticles);
  }
}());
