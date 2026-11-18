(function () {
  'use strict';
  var cards = Array.from(document.querySelectorAll('.review-item[data-qid]'));
  if (!cards.length) return;
  var filter = 'pending';
  function show() {
    var count = 0;
    cards.forEach(function (card) {
      var pending = card.querySelector('.reviewed-badge').dataset.reviewed !== '1';
      var verdict = card.querySelector('select[name^="verdict_"]');
      card.hidden = filter === 'pending' ? !pending : filter === 'unknown' ? !verdict || verdict.value !== '无法判定' : false;
      if (!card.hidden) count++;
    });
    document.getElementById('review-filter-status').textContent = count ? count + ' 题' : '没有匹配题目，可切换到全部';
    document.querySelectorAll('[data-review-filter]').forEach(function (b) { b.setAttribute('aria-pressed', b.dataset.reviewFilter === filter); });
  }
  document.querySelectorAll('[data-review-filter]').forEach(function (b) { b.addEventListener('click', function () { filter = b.dataset.reviewFilter; show(); }); });
  document.addEventListener('review-progress', show);
  document.getElementById('review-jump').addEventListener('change', function (e) {
    filter = 'all'; show();
    var card = cards.find(function (c) { return c.dataset.qid === e.target.value; });
    card.open = true; card.scrollIntoView({block: 'start', behavior: 'smooth'});
    card.querySelector('summary').focus();
  });
  document.querySelectorAll('[data-review-pane]').forEach(function (b) {
    b.addEventListener('click', function () {
      var card = b.closest('.review-item'); card.querySelector('.review-row').dataset.pane = b.dataset.reviewPane;
      card.querySelectorAll('[data-review-pane]').forEach(function (other) { other.setAttribute('aria-pressed', other === b); });
    });
  });
  var width = document.getElementById('pdf-width');
  if (width) width.addEventListener('input', function () { document.body.style.setProperty('--pdf-width', width.value + 'vw'); });
  if (!cards.some(function (c) { return c.querySelector('.reviewed-badge').dataset.reviewed !== '1'; })) filter = 'all';
  show();
  var first = cards.find(function (c) { return !c.hidden; }); if (first) first.open = true;
})();
