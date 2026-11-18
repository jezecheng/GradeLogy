(function () {
  'use strict';

  var shell = document.getElementById('kg-galaxy-shell');
  if (!shell) return;
  var container = document.getElementById('kg-galaxy');
  var loading = document.getElementById('kg-galaxy-loading');
  var errorBox = document.getElementById('kg-galaxy-error');
  var categoryBox = document.getElementById('kg-galaxy-categories');
  var crumbsBox = document.getElementById('kg-galaxy-crumbs');
  var backBtn = document.getElementById('kg-galaxy-back');
  var hintBox = shell.querySelector('.kg-galaxy-hint');
  var nodeTable = document.getElementById('kg-node-table');
  var nodeBody = document.getElementById('kg-node-body');
  var nodeEmpty = document.getElementById('kg-node-empty');
  var nodeSummary = document.getElementById('kg-node-summary');
  var edgeTable = document.getElementById('kg-edge-table');
  var edgeBody = document.getElementById('kg-edge-body');
  var edgeEmpty = document.getElementById('kg-edge-empty');
  var edgeSummary = document.getElementById('kg-edge-summary');
  var drawer = document.getElementById('kg-drawer');
  var drawerBody = document.getElementById('kg-drawer-body');
  var drawerClose = document.getElementById('kg-drawer-close');

  var available = !!window.echarts;

  var PALETTE = ['#38bdf8', '#a78bfa', '#34d399', '#f472b6', '#fbbf24',
    '#22d3ee', '#fb7185', '#a3e635', '#c084fc', '#60a5fa',
    '#f97316', '#2dd4bf', '#e879f9', '#facc15', '#4ade80', '#818cf8'];
  var LEGEND_LIMIT = 12;
  var TOP_LABELS = 50;
  var MAX_CLUSTERS = 24;
  var NODE_LIMIT = 150;
  var RELATION_LABEL = { prerequisite: '前置', contains: '包含', related: '相关' };

  var chart = null;
  var nodes = [];
  var edges = [];
  var nodeById = {};
  var colorByName = {};
  var tree = null;

  var viewStack = [];
  var viewKind = '';
  var currentBuckets = [];
  var currentViewEdges = [];
  var currentRanked = [];
  var dataIndexById = {};
  var currentRank = {};
  var labelSet = {};
  var selectedId = null;
  var resizeTimer = null;
  var labelTimer = null;
  var fitTimers = [];
  var roamTouched = false;
  var scopeRequest = 0;
  var drawerRequest = 0;
  var drawerFocus = null;

  var overlay = null;
  var overlayCtx = null;
  var particles = [];
  var particleFrame = 0;
  var lastParticleTime = 0;
  var glowSprite = null;
  var prefersReduced = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  fetch(shell.dataset.url, { headers: { Accept: 'application/json' } })
    .then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    })
    .then(prepare)
    .then(function () {
      viewStack = [rootView()];
      if (available) renderView(); else showError();
    })
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

  function clusterRadius(count) {
    return Math.max(16, Math.min(54, 13 + Math.sqrt(count || 1) * 2.4));
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

  // ---- 层级树：按 category 的 “/” 路径逐层聚合 ----

  function categorySegments(node) {
    var segments = String(node.category || '未分类').split('/')
      .map(function (part) { return part.trim(); })
      .filter(function (part) { return part.length > 0; });
    return segments.length ? segments : ['未分类'];
  }

  function createGroup(name, path, depth) {
    return { name: name, path: path, depth: depth, children: {}, direct: [], count: 0 };
  }

  function buildTree(nodeList) {
    var root = createGroup('全部', '', 0);
    nodeList.forEach(function (node) {
      root.count += 1;
      var group = root;
      categorySegments(node).forEach(function (segment) {
        if (!group.children[segment]) {
          group.children[segment] = createGroup(
            segment,
            group.path ? group.path + '/' + segment : segment,
            group.depth + 1);
        }
        group = group.children[segment];
        group.count += 1;
      });
      group.direct.push(node);
    });
    return root;
  }

  function sortedChildren(group) {
    return Object.keys(group.children).map(function (key) {
      return group.children[key];
    }).sort(function (a, b) {
      return b.count - a.count || a.name.localeCompare(b.name);
    });
  }

  function collectNodes(group, bucket) {
    bucket = bucket || [];
    group.direct.forEach(function (node) { bucket.push(node); });
    sortedChildren(group).forEach(function (child) { collectNodes(child, bucket); });
    return bucket;
  }

  function prepare(payload) {
    var categories = payload.categories || [];
    categories.forEach(function (category, index) {
      colorByName[category.name] = PALETTE[index % PALETTE.length];
    });
    nodes = payload.nodes || [];
    edges = payload.edges || [];
    nodes.forEach(function (node) { nodeById[String(node.id)] = node; });
    tree = buildTree(nodes);
    initializeSearch();
  }

  function initializeSearch() {
    var input = document.getElementById('graph-search');
    var results = document.getElementById('graph-search-results');
    var status = document.getElementById('graph-search-status');
    if (!input || !results) return;
    function search() {
      var query = input.value.trim().toLocaleLowerCase();
      var matches = query ? nodes.filter(function (n) {
        return [n.name, n.category, JSON.stringify(n.aliases || [])].join(' ').toLocaleLowerCase().includes(query);
      }) : [];
      results.replaceChildren();
      status.textContent = query ? '找到 ' + matches.length + ' 个概念' + (matches.length > 40 ? '，先显示 40 项，请缩小关键词' : '') : '可搜索全部 ' + nodes.length + ' 个概念；点击分类逐层浏览。';
      matches.slice(0, 40).forEach(function (n) {
        var link = document.createElement('a'); link.className = 'graph-search-result'; link.href = n.url;
        link.textContent = n.name;
        var category = document.createElement('small'); category.textContent = n.category || '未分类'; link.appendChild(category);
        results.appendChild(link);
      });
    }
    input.addEventListener('input', search); search();
  }

  function rootView() {
    if (tree.count <= NODE_LIMIT) {
      return { kind: 'nodes', nodes: collectNodes(tree), title: '全部' };
    }
    return { kind: 'clusters', group: tree, title: '全部' };
  }

  function viewForGroup(group, title) {
    if (group.count <= NODE_LIMIT) {
      return { kind: 'nodes', nodes: collectNodes(group), title: title || group.name };
    }
    return { kind: 'clusters', group: group, title: title || group.name };
  }

  // ---- 视图切换 ----

  function renderView() {
    var view = viewStack[viewStack.length - 1];
    if (!view) return;
    viewKind = view.kind;
    closeDrawer();
    resetChart();
    if (view.kind === 'clusters') {
      renderClusterView(view);
    } else {
      renderNodeView(view);
    }
    renderCrumbs();
    updateHint();
    syncTables(view);
    scheduleFit();
    if (loading) loading.style.display = 'none';
  }

  // 力导向不自动适配画布，布局稳定后按包围盒缩放一次，避免星团或节点被裁掉
  function scheduleFit() {
    roamTouched = false;
    [700, 2200].forEach(function (delay) {
      fitTimers.push(setTimeout(function () {
        if (!roamTouched) fitToView();
      }, delay));
    });
  }

  function clearFit() {
    fitTimers.forEach(function (timer) { clearTimeout(timer); });
    fitTimers = [];
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
    var zoom = Math.min(chart.getWidth() / width, chart.getHeight() / height) * 0.82;
    var maxZoom = viewKind === 'clusters' ? 1.3 : 1.6;
    zoom = Math.min(Math.max(zoom, 0.2), maxZoom);
    chart.setOption({
      series: [{
        center: [(minX + maxX) / 2, (minY + maxY) / 2],
        zoom: zoom
      }]
    });
    if (viewKind === 'nodes') refreshLabels(true);
  }

  function resetChart() {
    stopParticles();
    clearFit();
    selectedId = null;
    labelSet = {};
    currentBuckets = [];
    dataIndexById = {};
    currentRank = {};
    if (chart) {
      chart.dispose();
      chart = null;
    }
    container.innerHTML = '';
    overlay = null;
    overlayCtx = null;
    particleFrame = 0;
    lastParticleTime = 0;
    chart = echarts.init(container, null, { renderer: 'canvas' });
    bindChartEvents();
  }

  function baseOption(extra) {
    var option = {
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
          if (data.kind === 'cluster') {
            return '<b>' + escapeHtml(data.name) + '</b><br>'
              + data.count + ' 个概念 · 点击进入模块';
          }
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
        categories: [],
        data: [],
        edges: [],
        force: {
          initLayout: 'circular',
          repulsion: 150,
          gravity: 0.06,
          edgeLength: [50, 140],
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
          textBorderWidth: 2.5
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
    };
    Object.keys(extra || {}).forEach(function (key) {
      option.series[0][key] = extra[key];
    });
    return option;
  }

  // ---- 星团视图 ----

  function buildBuckets(group) {
    var children = sortedChildren(group);
    var slots = MAX_CLUSTERS;
    var childLimit = slots - 1;
    if (group.direct.length) childLimit -= 1;
    childLimit = Math.max(1, childLimit);
    var shown = children.slice(0, childLimit);
    var rest = children.slice(shown.length);
    var buckets = shown.map(function (child) {
      return { kind: 'group', group: child, label: child.name, count: child.count };
    });
    if (group.direct.length) {
      buckets.push({
        kind: 'nodes', nodes: group.direct.slice(),
        label: '直属概念', count: group.direct.length
      });
    }
    if (rest.length) {
      var restGroup = createGroup('其他', group.path, group.depth);
      rest.forEach(function (child) { restGroup.children[child.name] = child; });
      restGroup.count = rest.reduce(function (sum, child) { return sum + child.count; }, 0);
      buckets.push({
        kind: 'rest', group: restGroup,
        label: '其他 ' + rest.length + ' 类', count: restGroup.count
      });
    }
    return buckets;
  }

  function renderClusterView(view) {
    var group = view.group;
    var buckets = buildBuckets(group);
    currentBuckets = buckets;

    var childToBucket = {};
    var directIndex = null;
    buckets.forEach(function (bucket, index) {
      if (bucket.kind === 'group') {
        childToBucket[bucket.label] = index;
      } else if (bucket.kind === 'rest') {
        Object.keys(bucket.group.children).forEach(function (name) {
          childToBucket[name] = index;
        });
      } else if (bucket.kind === 'nodes') {
        directIndex = index;
      }
    });

    var nodeToBucket = {};
    nodes.forEach(function (node) {
      var segments = categorySegments(node);
      var index = null;
      if (segments.length === group.depth) {
        index = directIndex;
      } else {
        index = childToBucket[segments[group.depth]];
      }
      if (index != null) nodeToBucket[String(node.id)] = index;
    });

    var grouped = {};
    edges.forEach(function (edge) {
      var source = nodeToBucket[String(edge.src_id)];
      var target = nodeToBucket[String(edge.dst_id)];
      if (source == null || target == null || source === target) return;
      var key = source + '|' + target + '|' + edge.relation;
      if (!grouped[key]) {
        grouped[key] = { source: source, target: target, relation: edge.relation, count: 0 };
      }
      grouped[key].count += 1;
    });

    var graphNodes = buckets.map(function (bucket, index) {
      var color = PALETTE[index % PALETTE.length];
      return {
        id: 'cluster-' + index,
        name: bucket.label,
        labelText: bucket.label + '  ' + bucket.count,
        value: bucket.count,
        count: bucket.count,
        kind: 'cluster',
        bucketIndex: index,
        symbolSize: clusterRadius(bucket.count),
        itemStyle: {
          color: color,
          borderColor: 'rgba(255,255,255,.78)',
          borderWidth: 1.2,
          shadowColor: color,
          shadowBlur: 22
        }
      };
    });

    var graphEdges = Object.keys(grouped).map(function (key) {
      var item = grouped[key];
      var style = relationStyle(item.relation);
      var width = Math.min(5, 0.7 + Math.log(item.count + 1) / Math.LN2 * 0.9);
      return {
        source: 'cluster-' + item.source,
        target: 'cluster-' + item.target,
        relation: item.relation,
        count: item.count,
        symbol: item.relation === 'prerequisite' ? ['none', 'arrow'] : ['none', 'none'],
        symbolSize: 6,
        lineStyle: {
          color: style.color,
          width: width,
          type: style.type,
          opacity: style.opacity,
          curveness: 0.2
        }
      };
    });

    var option = baseOption({
      data: graphNodes,
      edges: graphEdges,
      force: {
        initLayout: 'circular',
        repulsion: 1600,
        gravity: 0.05,
        edgeLength: [120, 260],
        friction: 0.2,
        layoutAnimation: true
      },
      label: {
        show: true,
        position: 'right',
        distance: 6,
        color: '#e2e8f0',
        fontSize: 12,
        formatter: function (params) {
          return params.data && params.data.labelText ? params.data.labelText : '';
        }
      }
    });
    chart.setOption(option, true);
    if (categoryBox) categoryBox.innerHTML = '';
  }

  // ---- 节点视图（叶子层） ----

  function renderNodeView(view) {
    var viewNodes = view.nodes || [];
    var viewSet = {};
    viewNodes.forEach(function (node) { viewSet[String(node.id)] = true; });

    var sorted = viewNodes.slice().sort(function (a, b) {
      return b.degree - a.degree || String(a.id).localeCompare(String(b.id));
    });
    currentRanked = sorted.map(function (node) { return String(node.id); });
    currentRanked.forEach(function (id, index) { currentRank[id] = index; });
    var topLabels = Math.min(TOP_LABELS, currentRanked.length);
    currentRanked.slice(0, topLabels).forEach(function (id) { labelSet[id] = true; });

    var categoryCount = {};
    viewNodes.forEach(function (node) {
      var name = node.category || '未分类';
      categoryCount[name] = (categoryCount[name] || 0) + 1;
    });
    var viewCategories = Object.keys(categoryCount).map(function (name) {
      return { name: name, count: categoryCount[name] };
    }).sort(function (a, b) {
      return b.count - a.count || a.name.localeCompare(b.name);
    });

    var hubCut = Math.max(3, Math.round(viewNodes.length * 0.05));
    var graphNodes = viewNodes.map(function (node, index) {
      var id = String(node.id);
      dataIndexById[id] = index;
      var color = colorByName[node.category] || PALETTE[0];
      var isHub = (currentRank[id] || 0) < hubCut;
      return {
        id: id,
        name: node.name,
        value: node.degree,
        kind: 'node',
        category: node.category || '未分类',
        degree: node.degree,
        definition: node.definition,
        confidence: node.confidence,
        symbolSize: radiusFor(node.degree),
        itemStyle: {
          color: color,
          borderColor: isHub ? 'rgba(255,255,255,.85)' : 'rgba(255,255,255,.42)',
          borderWidth: isHub ? 1.6 : 0.6,
          shadowColor: color,
          shadowBlur: isHub ? 24 : 8
        }
      };
    });

    var viewEdges = edges.filter(function (edge) {
      return viewSet[String(edge.src_id)] && viewSet[String(edge.dst_id)];
    });

    var graphEdges = viewEdges.map(function (edge) {
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

    var option = baseOption({
      categories: viewCategories.map(function (category) {
        return { name: category.name };
      }),
      data: graphNodes,
      edges: graphEdges,
      force: {
        initLayout: 'circular',
        repulsion: 190,
        gravity: 0.06,
        edgeLength: [55, 150],
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
      }
    });
    chart.setOption(option, true);
    renderCategories(viewCategories);
    currentViewEdges = viewEdges;
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
    if (!categories.length) {
      categoryBox.innerHTML = '';
      return;
    }
    var shown = categories.slice(0, LEGEND_LIMIT);
    var html = shown.map(function (category, index) {
      var color = colorByName[category.name] || PALETTE[index % PALETTE.length];
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

  // ---- 面包屑与提示 ----

  function renderCrumbs() {
    if (backBtn) backBtn.disabled = viewStack.length <= 1;
    if (!crumbsBox) return;
    var html = viewStack.map(function (view, index) {
      var current = index === viewStack.length - 1;
      return '<button type="button" class="kg-galaxy-crumb" data-index="' + index + '"'
        + (current ? ' aria-current="true"' : '') + '>'
        + escapeHtml(view.title) + '</button>';
    }).join('<span class="kg-galaxy-crumb-sep">›</span>');
    crumbsBox.innerHTML = html;
    Array.prototype.forEach.call(
      crumbsBox.querySelectorAll('.kg-galaxy-crumb'), function (button) {
        button.addEventListener('click', function () {
          var index = Number(button.dataset.index);
          if (!isFinite(index) || index >= viewStack.length - 1) return;
          viewStack = viewStack.slice(0, index + 1);
          renderView();
        });
      });
  }

  function updateHint() {
    if (!hintBox) return;
    hintBox.textContent = viewKind === 'clusters'
      ? '点击星团逐层下钻 · 滚轮缩放 · 拖拽平移'
      : '滚轮缩放 · 拖拽平移 · 前 50 个枢纽显示标签';
  }

  function goUp() {
    if (viewStack.length <= 1) return;
    viewStack = viewStack.slice(0, -1);
    renderView();
  }

  if (backBtn) backBtn.addEventListener('click', goUp);

  // ---- 交互 ----

  function bindChartEvents() {
    chart.on('click', function (params) {
      if (params.dataType !== 'node' || !params.data) return;
      if (viewKind === 'clusters') {
        drillBucket(params.data.bucketIndex);
        return;
      }
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
      refreshLabels(true);
    });
    chart.on('graphRoam', function () {
      roamTouched = true;
      if (viewKind !== 'nodes') return;
      clearTimeout(labelTimer);
      labelTimer = setTimeout(function () { refreshLabels(false); }, 180);
    });
  }

  function drillBucket(index) {
    var bucket = currentBuckets[index];
    if (!bucket) return;
    if (bucket.kind === 'group') {
      viewStack.push(viewForGroup(bucket.group, bucket.label));
    } else if (bucket.kind === 'nodes') {
      viewStack.push({ kind: 'nodes', nodes: bucket.nodes.slice(), title: bucket.label });
    } else if (bucket.kind === 'rest') {
      viewStack.push(viewForGroup(bucket.group, bucket.label));
    }
    renderView();
  }

  function highlight(id) {
    var dataIndex = dataIndexById[id];
    if (dataIndex == null) return;
    chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: dataIndex });
  }

  function refreshLabels(force) {
    if (!chart || viewKind !== 'nodes') return;
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
    var limit = zoom < 2 ? 120 : (zoom < 3 ? 260 : (zoom < 4.5 ? 520 : nodes.length));
    visible.sort(function (a, b) {
      return (currentRank[a] || 0) - (currentRank[b] || 0);
    });

    var next = {};
    currentRanked.slice(0, TOP_LABELS).forEach(function (id) { next[id] = true; });
    visible.slice(0, limit).forEach(function (id) { next[id] = true; });
    if (!visible.length && zoom >= 1.25) {
      currentRanked.slice(0, limit).forEach(function (id) { next[id] = true; });
    }
    if (selectedId) next[selectedId] = true;
    if (!force && sameKeys(next, labelSet)) return;
    labelSet = next;
    chart.setOption({
      series: [{ label: { formatter: labelFormatter(labelSet) } }]
    });
  }

  function sameKeys(a, b) {
    var left = Object.keys(a);
    var right = Object.keys(b);
    if (left.length !== right.length) return false;
    for (var i = 0; i < left.length; i += 1) {
      if (!b[left[i]]) return false;
    }
    return true;
  }

  // ---- 概念/关系表跟随当前下钻层级 ----

  function syncTables(view) {
    if (!nodeTable || !nodeBody || !edgeTable || !edgeBody) return;
    if (view.kind !== 'nodes') {
      scopeRequest += 1;
      nodeTable.hidden = true;
      edgeTable.hidden = true;
      nodeEmpty.hidden = false;
      edgeEmpty.hidden = false;
      nodeEmpty.textContent = '当前是星团概览：点击星团下钻到概念层后，这里只显示该层的概念。';
      edgeEmpty.textContent = '当前是星团概览：下钻到概念层后，这里只显示该层概念之间的关系。';
      nodeSummary.textContent = '（等待选择星团）';
      edgeSummary.textContent = '（等待选择星团）';
      return;
    }
    var ids = view.nodes.map(function (node) { return String(node.id); });
    var token = ++scopeRequest;
    fetch(shell.dataset.scopeUrl + '?ids=' + encodeURIComponent(ids.join(',')),
      { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (token !== scopeRequest) return;
        renderNodeRows(payload);
        renderEdgeRows(payload);
      })
      .catch(function () {
        if (token !== scopeRequest) return;
        nodeTable.hidden = true;
        nodeEmpty.hidden = false;
        nodeEmpty.textContent = '当前层级的概念加载失败，请稍后重试。';
      });
  }

  function renderNodeRows(payload) {
    var total = payload.nodes.length;
    nodeBody.innerHTML = payload.nodes.map(function (node) {
      return '<tr>'
        + '<td class="kg-name"><a href="' + node.url + '">' + escapeHtml(node.name) + '</a></td>'
        + '<td>' + escapeHtml(node.category || '—') + '</td>'
        + '<td>' + escapeHtml(node.definition || '—') + '</td>'
        + '<td class="kg-note">' + escapeHtml(node.aliases || '—') + '</td>'
        + '<td>' + node.confidence + '%</td>'
        + '</tr>';
    }).join('');
    nodeTable.hidden = total === 0;
    nodeEmpty.hidden = total > 0;
    if (!total) nodeEmpty.textContent = '当前分类下还没有概念。';
    nodeSummary.textContent = '（本层 ' + total + ' 个）';
  }

  function renderEdgeRows(payload) {
    var csrf = payload.csrf || '';
    edgeBody.innerHTML = payload.edges.map(function (edge) {
      var actions = '';
      if (payload.can_edit) {
        actions = '<td class="kg-edge-actions">';
        if (edge.reverse_url) {
          actions += '<form method="post" action="' + edge.reverse_url + '"'
            + ' onsubmit="return confirm(\'把这条前置关系反向？系统会先检查是否会成环。\')">'
            + '<input type="hidden" name="_csrf" value="' + escapeHtml(csrf) + '">'
            + '<button class="ghost small">反向</button></form>';
        }
        actions += '<form method="post" action="' + edge.delete_url + '"'
          + ' onsubmit="return confirm(\'删除这条关系？\')">'
          + '<input type="hidden" name="_csrf" value="' + escapeHtml(csrf) + '">'
          + '<button class="ghost small">删除</button></form></td>';
      }
      return '<tr>'
        + '<td><span class="kg-name">' + escapeHtml(edge.src_name) + '</span>'
        + '<span class="kg-arrow">—' + escapeHtml(edge.relation_label) + '→</span>'
        + '<span class="kg-name">' + escapeHtml(edge.dst_name) + '</span></td>'
        + '<td class="kg-note">' + escapeHtml(edge.evidence || '—') + '</td>'
        + '<td>' + edge.confidence + '%</td>' + actions
        + '</tr>';
    }).join('');
    edgeTable.hidden = payload.edges.length === 0;
    edgeEmpty.hidden = payload.edges.length > 0;
    if (!payload.edges.length) edgeEmpty.textContent = '当前层级的概念之间还没有关系。';
    edgeSummary.textContent = '（本层 ' + payload.edge_total + ' 条'
      + (payload.edge_total > payload.edges.length ? '，最多展示 ' + payload.edges.length : '')
      + '）';
  }

  // ---- 抽屉 ----

  function openDrawer(id) {
    if (!drawer || !drawerBody) return;
    var node = nodeById[id];
    var request = ++drawerRequest;
    drawerFocus = document.activeElement;
    drawer.inert = false;
    drawerBody.innerHTML = renderDrawerShell(node);
    drawer.setAttribute('aria-hidden', 'false');
    drawer.classList.add('open');
    if (drawerClose) drawerClose.focus({preventScroll: true});
    if (window.renderKatexIn) window.renderKatexIn(drawerBody);
    fetch(shell.dataset.neighborsUrl + '?node_id=' + encodeURIComponent(id),
      { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (request !== drawerRequest) return;
        drawerBody.innerHTML = renderDrawer(payload);
        if (window.renderKatexIn) window.renderKatexIn(drawerBody);
      })
      .catch(function () {
        if (request !== drawerRequest) return;
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
    ++drawerRequest;
    var wasOpen = drawer.classList.contains('open');
    drawer.inert = true;
    drawer.setAttribute('aria-hidden', 'true');
    drawer.classList.remove('open');
    if (wasOpen && drawerFocus && drawerFocus.isConnected) drawerFocus.focus({preventScroll: true});
  }

  // ---- 粒子叠层（仅叶子节点视图） ----

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
    var candidates = currentViewEdges.filter(function (edge) {
      return edge.relation === 'prerequisite';
    }).map(function (edge) {
      var src = nodeById[String(edge.src_id)];
      var dst = nodeById[String(edge.dst_id)];
      return { edge: edge, weight: Math.min(src ? src.degree : 0, dst ? dst.degree : 0) };
    }).sort(function (a, b) { return b.weight - a.weight; }).slice(0, 120);
    candidates.forEach(function (item, index) {
      particles.push({
        source: String(item.edge.src_id),
        target: String(item.edge.dst_id),
        t: (index / candidates.length) % 1,
        speed: 0.0022 + (index % 5) * 0.00055
      });
    });
  }

  function sprite() {
    if (glowSprite) return glowSprite;
    var size = 16;
    var canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext('2d');
    var gradient = ctx.createRadialGradient(
      size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gradient.addColorStop(0, 'rgba(180,225,255,.95)');
    gradient.addColorStop(0.45, 'rgba(96,180,255,.55)');
    gradient.addColorStop(1, 'rgba(56,189,248,0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, size, size);
    glowSprite = canvas;
    return glowSprite;
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
    if (!overlayCtx || !chart || viewKind !== 'nodes') return;
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

    var spriteImage = sprite();
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
      overlayCtx.drawImage(spriteImage, x - 8, y - 8, 16, 16);
    });
    overlayCtx.restore();
  }

  function startParticles() {
    if (prefersReduced || !overlayCtx) return;
    prepareParticles();
    if (!particleFrame) particleFrame = window.requestAnimationFrame(drawParticles);
  }

  function stopParticles() {
    if (particleFrame) {
      window.cancelAnimationFrame(particleFrame);
      particleFrame = 0;
    }
    particles = [];
    if (overlayCtx && overlay) {
      overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    }
  }

  // ---- 全局事件 ----

  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closeDrawer();
      if (chart) chart.dispatchAction({ type: 'downplay', seriesIndex: 0 });
    }
  });
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (chart) chart.resize();
      resizeOverlay();
    }, 120);
  });
}());
