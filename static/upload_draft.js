// 作业草稿箱：选图 / 拍照 → 预览、删除 → 确认后一次性提交。
// 适用于学生自己提交，以及老师代学生上传（表单加 data-draft-upload 即可）。
(function () {
  function humanSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function setup(form) {
    var picker = form.querySelector('[data-draft-picker]');
    var camera = form.querySelector('[data-draft-camera]');
    var finalInput = form.querySelector('[data-draft-final]');
    var box = form.querySelector('[data-draft-box]');
    var grid = form.querySelector('[data-draft-grid]');
    var count = form.querySelector('[data-draft-count]');
    var clearBtn = form.querySelector('[data-draft-clear]');
    if (!picker || !finalInput || !box || !grid) return;
    var files = [];

    Array.prototype.forEach.call(form.querySelectorAll('[data-draft-open]'), function (btn) {
      btn.addEventListener('click', function () {
        var target = btn.getAttribute('data-draft-open') === 'camera' ? camera : picker;
        if (target) target.click();
      });
    });

    function render() {
      grid.innerHTML = '';
      files.forEach(function (file, index) {
        var item = document.createElement('div');
        item.className = 'draft-item';
        if (/^image\//.test(file.type)) {
          var img = document.createElement('img');
          img.src = URL.createObjectURL(file);
          img.onload = function () { URL.revokeObjectURL(img.src); };
          item.appendChild(img);
        } else {
          var badge = document.createElement('div');
          badge.className = 'draft-file';
          badge.textContent = 'PDF';
          item.appendChild(badge);
        }
        var name = document.createElement('span');
        name.className = 'draft-name';
        name.textContent = file.name;
        item.appendChild(name);
        var remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'draft-remove';
        remove.title = '移除';
        remove.textContent = '×';
        remove.addEventListener('click', function () {
          files.splice(index, 1);
          render();
        });
        item.appendChild(remove);
        grid.appendChild(item);
      });
      box.hidden = files.length === 0;
      var total = files.reduce(function (sum, f) { return sum + f.size; }, 0);
      count.textContent = files.length ? files.length + ' 个文件 · ' + humanSize(total) : '';
    }

    Array.prototype.forEach.call([picker, camera], function (input) {
      if (!input) return;
      input.addEventListener('change', function () {
        var added = Array.prototype.slice.call(input.files);
        input.value = '';
        added.forEach(function (file) {
          if (files.length >= 20) {
            alert('一次最多提交 20 个文件');
            return;
          }
          files.push(file);
        });
        render();
      });
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        files = [];
        render();
      });
    }

    form.addEventListener('submit', function (e) {
      if (!files.length) {
        e.preventDefault();
        alert('请先选择要提交的图片或 PDF');
        return;
      }
      var dt = new DataTransfer();
      files.forEach(function (file) { dt.items.add(file); });
      finalInput.files = dt.files;
    });
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll('form[data-draft-upload]'), setup);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
