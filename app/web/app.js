/*!
 * FileShare 前端（原生 JS，无框架）
 *
 * 关键实现点：
 *  - 资源管理器 -> 左面板：DataTransfer.files（含 webkitGetAsEntry 递归目录）+ XHR 进度上传
 *  - 右面板 -> 资源管理器：Chromium 专有 DownloadURL（mime:文件名:URL），URL 指向本机后端 /api/peer/raw，
 *    因此拖出时会同时进入传输队列并在本机留副本；跨域不受限，也不泄露对方地址。
 *  - 页面内互拖：自定义 MIME application/x-fileshare-item
 */
'use strict';

var TOKEN = document.body.getAttribute('data-token') || '';
var VERSION = document.body.getAttribute('data-version') || '';
var ITEM_MIME = 'application/x-fileshare-item';
var POLL_MS = 1500;

var state = {
  info: null,
  peers: [],
  transfers: [],
  pending: [],
  device: '',
  left: { root: 'share', path: '', entries: [] },
  right: { path: '', entries: [], kind: '', error: '' },
  scanning: false,
  uploads: 0,
};

var $ = function (id) { return document.getElementById(id); };

// ------------------------------------------------------------------ 工具
function fmtSize(bytes) {
  var value = Number(bytes) || 0;
  if (value < 1024) { return value + ' B'; }
  var units = ['KB', 'MB', 'GB', 'TB'], i = -1;
  do { value /= 1024; i++; } while (value >= 1024 && i < units.length - 1);
  return value.toFixed(value >= 100 ? 0 : 1) + ' ' + units[i];
}

function fmtTime(sec) {
  var n = Math.floor(Number(sec) || 0);
  if (n <= 0) { return '0 秒'; }
  if (n < 60) { return n + ' 秒'; }
  if (n < 3600) { return Math.floor(n / 60) + ' 分 ' + (n % 60) + ' 秒'; }
  return Math.floor(n / 3600) + ' 时 ' + Math.floor((n % 3600) / 60) + ' 分';
}

function esc(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* DownloadURL 的格式是 mime:文件名:url，文件名里不能有冒号，否则三段会被截断 */
function dragSafeName(name) {
  return String(name || 'file').replace(/[:\r\n\t]/g, '_');
}

function toast(message, kind, ms) {
  var box = document.createElement('div');
  box.className = 'toast ' + (kind || '');
  box.textContent = message;
  $('toasts').appendChild(box);
  setTimeout(function () {
    box.style.opacity = '0';
    setTimeout(function () { if (box.parentNode) { box.parentNode.removeChild(box); } }, 200);
  }, ms || 3600);
}

function isSecure() { return window.isSecureContext === true; }

// ------------------------------------------------------------------ 请求
function apiGet(url) {
  return fetch(url, { cache: 'no-store' }).then(function (resp) {
    return resp.text().then(function (text) {
      var data = {};
      try { data = text ? JSON.parse(text) : {}; } catch (err) { data = { error: text }; }
      if (!resp.ok) { throw new Error(data.error || ('HTTP ' + resp.status)); }
      return data;
    });
  });
}

function apiPost(url, payload) {
  var body = payload === undefined ? '{}' : JSON.stringify(payload);
  return fetch(url, {
    method: 'POST',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-FS-Token': TOKEN },
    body: body,
  }).then(function (resp) {
    return resp.text().then(function (text) {
      var data = {};
      try { data = text ? JSON.parse(text) : {}; } catch (err) { data = { error: text }; }
      if (!resp.ok) { throw new Error(data.error || ('HTTP ' + resp.status)); }
      return data;
    });
  });
}

/* 裸 body 上传（浏览器自动带 Content-Length），带进度回调 */
function uploadBlob(blob, options, onProgress) {
  var opts = options || {};
  var query = 'root=' + encodeURIComponent(opts.root || 'share') +
              '&path=' + encodeURIComponent(opts.path || '') +
              '&name=' + encodeURIComponent(opts.name || 'file');
  var url = '/api/fs/upload?' + query;
  return new Promise(function (resolve, reject) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.setRequestHeader('Content-Type', blob.type || 'application/octet-stream');
    xhr.setRequestHeader('X-FS-Token', TOKEN);
    if (opts.mtime) { xhr.setRequestHeader('X-File-Mtime', String(Math.floor(opts.mtime))); }
    if (onProgress && xhr.upload) {
      xhr.upload.onprogress = function (event) {
        if (event.lengthComputable) { onProgress(event.loaded, event.total); }
      };
    }
    xhr.onload = function () {
      var data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch (err) { data = {}; }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(data); }
      else { reject(new Error(data.error || ('HTTP ' + xhr.status))); }
    };
    xhr.onerror = function () { reject(new Error('网络错误或服务已停止')); };
    xhr.onabort = function () { reject(new Error('已取消')); };
    xhr.send(blob);
  });
}

/* 推送给对方：裸 body 中继（浏览器 -> 本机后端 -> 对方） */
function pushBlob(blob, device, name, onProgress) {
  var url = '/api/peer/push?device=' + encodeURIComponent(device) +
            '&name=' + encodeURIComponent(name);
  return new Promise(function (resolve, reject) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.setRequestHeader('Content-Type', blob.type || 'application/octet-stream');
    xhr.setRequestHeader('X-FS-Token', TOKEN);
    if (onProgress && xhr.upload) {
      xhr.upload.onprogress = function (event) {
        if (event.lengthComputable) { onProgress(event.loaded, event.total); }
      };
    }
    xhr.onload = function () {
      var data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch (err) { data = {}; }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(data); }
      else { reject(new Error(data.error || ('HTTP ' + xhr.status))); }
    };
    xhr.onerror = function () { reject(new Error('网络错误或服务已停止')); };
    xhr.send(blob);
  });
}

/* 递归收集拖入的目录：webkitGetAsEntry 是 Chromium/Safari 支持的标准扩展 */
function collectEntries(items) {
  var roots = [];
  var index;
  for (index = 0; index < items.length; index++) {
    var item = items[index];
    if (item.kind !== 'file') { continue; }
    if (typeof item.webkitGetAsEntry === 'function') {
      var entry = item.webkitGetAsEntry();
      if (entry) { roots.push(entry); continue; }
    }
    var file = item.getAsFile();
    if (file) { roots.push({ isFile: true, isDirectory: false, name: file.name, __file: file }); }
  }
  return roots;
}

function readAllEntries(reader) {
  return new Promise(function (resolve) {
    var all = [];
    function step() {
      reader.readEntries(function (batch) {
        if (!batch.length) { resolve(all); return; }
        all = all.concat(Array.prototype.slice.call(batch));
        step();
      }, function () { resolve(all); });
    }
    step();
  });
}

/* 返回 [{file, relDir}]，relDir 是相对拖入根目录的子路径（'' 表示根） */
function walkEntry(entry, relDir) {
  if (entry.__file) { return Promise.resolve([{ file: entry.__file, relDir: relDir }]); }
  if (entry.isFile) {
    return new Promise(function (resolve) {
      entry.file(function (file) { resolve([{ file: file, relDir: relDir }]); },
                 function () { resolve([]); });
    });
  }
  if (entry.isDirectory) {
    return readAllEntries(entry.createReader()).then(function (children) {
      var jobs = children.map(function (child) {
        var next = relDir ? (relDir + '/' + child.name) : child.name;
        if (child.isDirectory) { return walkEntry(child, next); }
        return walkEntry(child, relDir);
      });
      return Promise.all(jobs).then(function (groups) {
        var out = [];
        groups.forEach(function (group) { out = out.concat(group); });
        return out;
      });
    });
  }
  return Promise.resolve([]);
}

function collectFiles(dataTransfer) {
  var items = dataTransfer.items;
  if (!items || !items.length) {
    var files = [];
    var list = dataTransfer.files || [];
    for (var i = 0; i < list.length; i++) { files.push({ file: list[i], relDir: '' }); }
    return Promise.resolve(files);
  }
  var roots = collectEntries(items);
  if (!roots.length) {
    var fallback = [];
    var f2 = dataTransfer.files || [];
    for (var j = 0; j < f2.length; j++) { fallback.push({ file: f2[j], relDir: '' }); }
    return Promise.resolve(fallback);
  }
  return Promise.all(roots.map(function (entry) { return walkEntry(entry, ''); }))
    .then(function (groups) {
      var out = [];
      groups.forEach(function (group) { out = out.concat(group); });
      return out;
    });
}

/* 上传一批文件到共享目录（保持目录结构），带整体进度提示 */
function uploadBatch(pairs, destPath, label) {
  if (!pairs.length) { return Promise.resolve(0); }
  var total = pairs.reduce(function (sum, pair) { return sum + (pair.file.size || 0); }, 0);
  var loaded = 0;
  var done = 0;
  var failed = 0;
  var folders = {};
  var chain = Promise.resolve();

  function ensureFolder(rel) {
    if (!rel) { return Promise.resolve(); }
    if (folders[rel]) { return folders[rel]; }
    var full = destPath ? (destPath + '/' + rel) : rel;
    var parts = full.split('/');
    var promise = Promise.resolve();
    for (var i = 1; i <= parts.length; i++) {
      (function (sub) {
        promise = promise.then(function () {
          return apiPost('/api/fs/mkdir?root=share&path=' + encodeURIComponent(sub))
            .catch(function () { /* 已存在即忽略 */ });
        });
      })(parts.slice(0, i).join('/'));
    }
    folders[rel] = promise;
    return promise;
  }

  pairs.forEach(function (pair) {
    chain = chain.then(function () {
      return ensureFolder(pair.relDir).then(function () {
        return uploadBlob(pair.file, {
          root: 'share',
          path: destPath ? (pair.relDir ? destPath + '/' + pair.relDir : destPath) : pair.relDir,
          name: pair.file.name,
          mtime: pair.file.lastModified ? pair.file.lastModified / 1000 : 0,
        }, function (part, size) {
          setProgressLabel(label, loaded + part, total, size);
        });
      }).then(function () {
        loaded += pair.file.size || 0;
        done += 1;
        setProgressLabel(label, loaded, total, 0);
      }).catch(function (err) {
        failed += 1;
        loaded += pair.file.size || 0;
        toast('上传失败：' + pair.file.name + ' —— ' + err.message, 'err', 5000);
      });
    });
  });

  return chain.then(function () {
    if (done) { toast('已共享 ' + done + ' 个文件到左侧目录', 'ok'); }
    if (failed) { toast(failed + ' 个文件上传失败', 'err', 5000); }
    refreshLeft();
    return done;
  });
}

function setProgressLabel(label, loaded, total, currentSize) {
  if (!label) { return; }
  var percent = total ? Math.floor(loaded * 100 / total) : 0;
  label.textContent = '上传中… ' + percent + '%（' + fmtSize(loaded) + ' / ' + fmtSize(total) + '）';
}

// ------------------------------------------------------------------ 左面板
function iconFor(entry) {
  if (entry.is_dir) { return '📁'; }
  var mime = entry.mime || '';
  var name = (entry.name || '').toLowerCase();
  if (mime.indexOf('image/') === 0) { return '🖼️'; }
  if (mime.indexOf('video/') === 0) { return '🎬'; }
  if (mime.indexOf('audio/') === 0) { return '🎵'; }
  if (/\.(zip|rar|7z|tar|gz)$/.test(name)) { return '🗜️'; }
  if (/\.(exe|msi|bat|cmd)$/.test(name)) { return '⚙️'; }
  if (/\.(doc|docx|pdf|txt|md|xls|xlsx|ppt|pptx)$/.test(name)) { return '📄'; }
  return '📦';
}

function renderLeft() {
  var list = $('leftList');
  var data = state.left;
  $('leftCrumb').textContent = '/ share' + (data.path ? ('/' + data.path) : '');
  $('leftUp').disabled = !data.path;
  if (!data.entries.length) {
    list.innerHTML = '<div class="empty">这里还是空的。<br>把文件从资源管理器拖进来，' +
      '另一台电脑就能在右侧看到它。</div>';
    return;
  }
  list.innerHTML = data.entries.map(function (entry) {
    return '<div class="row" data-rel="' + esc(entry.rel) + '" data-dir="' + (entry.is_dir ? '1' : '0') +
      '" data-name="' + esc(entry.name) + '" data-size="' + entry.size + '" data-mime="' + esc(entry.mime || '') +
      '" draggable="true">' +
      '<span class="ico">' + iconFor(entry) + '</span>' +
      '<span class="nm" title="' + esc(entry.name) + '">' + esc(entry.name) + '</span>' +
      '<span class="sz">' + (entry.is_dir ? '' : fmtSize(entry.size)) + '</span>' +
      '<span class="ops">' +
      (entry.is_dir ? '' : '<button data-op="open">打开</button>') +
      '<button data-op="rename">改名</button>' +
      '<button data-op="delete">删除</button>' +
      '</span></div>';
  }).join('');
}

function refreshLeft() {
  var query = '?root=share&path=' + encodeURIComponent(state.left.path);
  return apiGet('/api/fs/list' + query).then(function (data) {
    state.left.entries = data.entries || [];
    renderLeft();
    return data;
  }).catch(function (err) {
    state.left.entries = [];
    renderLeft();
    toast('读取共享目录失败：' + err.message, 'err');
  });
}

function enterLeft(path) {
  state.left.path = path || '';
  refreshLeft();
}

function leftRowInfo(row) {
  return {
    rel: row.getAttribute('data-rel'),
    name: row.getAttribute('data-name'),
    isDir: row.getAttribute('data-dir') === '1',
    size: Number(row.getAttribute('data-size')) || 0,
    mime: row.getAttribute('data-mime') || '',
  };
}

function handleLeftRowOp(row, op) {
  var info = leftRowInfo(row);
  if (op === 'open') {
    var url = location.origin + '/api/fs/raw?root=share&path=' + encodeURIComponent(info.rel) +
      '&name=' + encodeURIComponent(info.name);
    window.open(url, '_blank');
    return;
  }
  if (op === 'rename') {
    var next = window.prompt('新的名称', info.name);
    if (!next || next === info.name) { return; }
    apiPost('/api/fs/rename?root=share&path=' + encodeURIComponent(info.rel) +
            '&new_name=' + encodeURIComponent(next))
      .then(function () { toast('已改名', 'ok'); refreshLeft(); })
      .catch(function (err) { toast('改名失败：' + err.message, 'err'); });
    return;
  }
  if (op === 'delete') {
    if (!window.confirm('确定删除「' + info.name + '」？' + (info.isDir ? '（含目录内全部内容）' : ''))) { return; }
    apiPost('/api/fs/delete?root=share&path=' + encodeURIComponent(info.rel) + '&recursive=1')
      .then(function () { toast('已删除', 'ok'); refreshLeft(); })
      .catch(function (err) { toast('删除失败：' + err.message, 'err'); });
  }
}

/* Chromium 专有拖出格式：mime:文件名:URL —— 拖到资源管理器会真正落成文件 */
function setDownloadUrl(dataTransfer, mime, name, url) {
  try {
    dataTransfer.setData('DownloadURL', mime + ':' + dragSafeName(name) + ':' + url);
  } catch (err) { /* 非 Chromium 忽略 */ }
  try { dataTransfer.setData('text/uri-list', url); } catch (err2) { /* 忽略 */ }
}

/* 左面板行的拖拽与点击 */
function bindLeftRows() {
  var list = $('leftList');

  list.addEventListener('click', function (event) {
    var row = event.target.closest ? event.target.closest('.row') : null;
    if (!row) { return; }
    var button = event.target.closest ? event.target.closest('button[data-op]') : null;
    if (button) {
      event.stopPropagation();
      handleLeftRowOp(row, button.getAttribute('data-op'));
      return;
    }
    var info = leftRowInfo(row);
    if (info.isDir) { enterLeft(info.rel); return; }
    list.querySelectorAll('.row.selected').forEach(function (node) { node.classList.remove('selected'); });
    row.classList.add('selected');
  });

  list.addEventListener('dblclick', function (event) {
    var row = event.target.closest ? event.target.closest('.row') : null;
    if (row && leftRowInfo(row).isDir) { enterLeft(leftRowInfo(row).rel); }
  });

  list.addEventListener('dragstart', function (event) {
    var row = event.target.closest ? event.target.closest('.row') : null;
    if (!row) { return; }
    var info = leftRowInfo(row);
    event.dataTransfer.setData(ITEM_MIME, JSON.stringify({
      side: 'left', root: 'share', rel: info.rel, name: info.name,
      isDir: info.isDir, size: info.size, mime: info.mime,
    }));
    event.dataTransfer.setData('text/plain', info.name);
    event.dataTransfer.effectAllowed = 'copy';
    if (!info.isDir) {
      var url = location.origin + '/api/fs/raw?root=share&path=' + encodeURIComponent(info.rel) +
        '&name=' + encodeURIComponent(info.name);
      setDownloadUrl(event.dataTransfer, info.mime || 'application/octet-stream', info.name, url);
    }
    list.querySelectorAll('.row.selected').forEach(function (node) { node.classList.remove('selected'); });
    row.classList.add('selected');
  });
}

// ------------------------------------------------------------------ 拖放区
function readItemPayload(dataTransfer) {
  try {
    var raw = dataTransfer.getData(ITEM_MIME);
    if (!raw) { return null; }
    var parsed = JSON.parse(raw);
    return parsed && parsed.side ? parsed : null;
  } catch (err) { return null; }
}

function hasFiles(dataTransfer) {
  var types = dataTransfer.types || [];
  for (var i = 0; i < types.length; i++) {
    if (types[i] === 'Files') { return true; }
  }
  return (dataTransfer.files && dataTransfer.files.length > 0);
}

function highlight(element, on) {
  var panel = element.closest ? element.closest('.panel') : null;
  element.classList.toggle('over', on);
  if (panel) { panel.classList.toggle('dropActive', on); }
}

/* 左面板放：资源管理器文件 -> 共享；右边文件 -> 下载进本机目录 */
function bindLeftDrop() {
  var zone = $('leftDrop');
  var panel = $('panelLeft');

  function onDragOver(event) {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    highlight(zone, true);
  }
  ['dragenter', 'dragover'].forEach(function (name) { panel.addEventListener(name, onDragOver); });
  ['dragleave', 'drop'].forEach(function (name) {
    panel.addEventListener(name, function (event) {
      if (name === 'dragleave' && event.relatedTarget && panel.contains(event.relatedTarget)) { return; }
      highlight(zone, false);
    });
  });

  panel.addEventListener('drop', function (event) {
    event.preventDefault();
    highlight(zone, false);
    var payload = readItemPayload(event.dataTransfer);
    if (payload && payload.side === 'right') {
      pullTo('share', payload.rel, payload.name);
      return;
    }
    if (hasFiles(event.dataTransfer)) {
      var label = zone.querySelector('.dzText');
      collectFiles(event.dataTransfer).then(function (pairs) {
        var directories = pairs.some(function (pair) { return !!pair.relDir; });
        var count = pairs.length + (directories ? 0 : 0);
        if (!pairs.length) { toast('没有识别到文件', 'warn'); return; }
        var hint = '上传 ' + count + ' 个文件…';
        if (label) { label.textContent = hint; }
        return uploadBatch(pairs, state.left.path, label).then(function () {
          if (label) { label.textContent = '拖拽文件 / 文件夹到这里'; }
        });
      });
      return;
    }
    if (payload && payload.side === 'left') { toast('这个文件已经在本机共享目录里', 'warn'); }
  });
}

/* 右面板放：左边文件 -> 推送；资源管理器文件 -> 中继推送 */
function bindRightDrop() {
  var zone = $('peerDrop');
  var panel = $('panelRight');

  ['dragenter', 'dragover'].forEach(function (name) {
    panel.addEventListener(name, function (event) {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
      highlight(zone, true);
    });
  });
  ['dragleave', 'drop'].forEach(function (name) {
    panel.addEventListener(name, function (event) {
      if (name === 'dragleave' && event.relatedTarget && panel.contains(event.relatedTarget)) { return; }
      highlight(zone, false);
    });
  });

  panel.addEventListener('drop', function (event) {
    event.preventDefault();
    highlight(zone, false);
    var peer = currentPeer();
    if (!peer) { toast('请先选择一台设备', 'warn'); return; }
    var payload = readItemPayload(event.dataTransfer);

    if (payload && payload.side === 'left') {
      if (payload.isDir) {
        toast('暂不支持推送整个目录，请进入目录后逐个推送文件', 'warn', 5000);
        return;
      }
      pushLocalFile(peer, payload);
      return;
    }
    if (payload && payload.side === 'right') { toast('这是对方自己的文件，无需推送', 'warn'); return; }
    if (hasFiles(event.dataTransfer)) {
      var label = zone.querySelector('.dzText');
      collectFiles(event.dataTransfer).then(function (pairs) {
        if (!pairs.length) { toast('没有识别到文件', 'warn'); return; }
        var total = pairs.length;
        var done = 0;
        var chain = Promise.resolve();
        pairs.forEach(function (pair) {
          chain = chain.then(function () {
            var text = '推送给 ' + (peer.alias || peer.ip) + '… ' + (done + 1) + '/' + total;
            if (label) { label.textContent = text; }
            return pushBlob(pair.file, peer.fingerprint, pair.file.name, function (part, size) {
              if (label) {
                label.textContent = text + '（' + Math.floor(part * 100 / (size || 1)) + '%）';
              }
            }).then(function () {
              done += 1;
              toast('已推送：' + pair.file.name, 'ok');
            }).catch(function (err) {
              toast('推送失败：' + pair.file.name + ' —— ' + err.message, 'err', 5000);
            });
          });
        });
        return chain.then(function () {
          if (label) { label.textContent = '把左边文件拖到这里 = 推送给对方'; }
          pollState();
        });
      });
    }
  });
}

function pushLocalFile(peer, payload) {
  return apiPost('/api/peer/push?device=' + encodeURIComponent(peer.fingerprint), {
    root: payload.root || 'share', path: payload.rel,
  }).then(function (data) {
    toast('正在推送：' + (data.transfer ? data.transfer.name : payload.name), 'ok');
    pollState();
  }).catch(function (err) {
    toast('推送失败：' + err.message, 'err', 5000);
  });
}

// ------------------------------------------------------------------ 队列
var DIR_TEXT = {
  upload: '上传', download: '下载', push: '推送', receive: '接收',
};
var STATUS_TEXT = {
  active: '进行中', done: '完成', error: '失败', cancelled: '已取消',
};
var speedCache = {};

function renderQueue() {
  var list = $('queueList');
  var items = state.transfers || [];
  $('activeCount').textContent = items.filter(function (t) { return t.status === 'active'; }).length + ' 进行中';
  if (!items.length) {
    list.innerHTML = '<div class="empty">还没有传输记录。</div>';
    return;
  }
  list.innerHTML = items.slice(0, 40).map(function (item) {
    var percent = item.size ? Math.min(100, Math.floor(item.done * 100 / item.size)) : (item.status === 'done' ? 100 : 0);
    var now = Date.now() / 1000;
    var prev = speedCache[item.id];
    var speed = '';
    if (item.status === 'active') {
      if (prev) {
        var deltaBytes = item.done - prev.done;
        var deltaTime = now - prev.t;
        if (deltaTime > 0.4 && deltaBytes >= 0) { speed = ' · ' + fmtSize(deltaBytes / deltaTime) + '/s'; }
      }
      speedCache[item.id] = { done: item.done, t: now };
    } else {
      delete speedCache[item.id];
    }
    var stat = STATUS_TEXT[item.status] || item.status;
    var detail = item.status === 'active'
      ? (item.size ? (fmtSize(item.done) + ' / ' + fmtSize(item.size) + ' · ' + percent + '%') : fmtSize(item.done)) + speed
      : (item.status === 'done' ? fmtSize(item.size || item.done || 0) : (item.error || ''));
    var ops = '';
    if (item.status === 'active') {
      ops = '<button data-op="cancel">取消</button>';
    } else if (item.status === 'error' || item.status === 'cancelled') {
      ops = '<button data-op="retry">重试</button>';
    }
    return '<div class="qItem" data-id="' + esc(item.id) + '" data-dir="' + esc(item.direction) +
      '" data-name="' + esc(item.name) + '" data-status="' + esc(item.status) + '">' +
      '<div class="qTop">' +
      '<span class="qDir">' + (DIR_TEXT[item.direction] || item.direction) + '</span>' +
      '<span class="qName" title="' + esc(item.name) + '">' + esc(item.name) + '</span>' +
      '<span class="qStat ' + esc(item.status) + '">' + esc(stat) + '</span>' +
      '<span class="qOps">' + ops + '</span>' +
      '</div>' +
      '<div class="qBar"><div class="qBarFill ' + esc(item.status) + '" style="width:' + percent + '%"></div></div>' +
      '<div class="qTop"><span class="qName" style="color:var(--muted)">' +
      esc(item.device || '') + '</span><span class="qStat">' + esc(detail) + '</span></div>' +
      '</div>';
  }).join('');
}

function bindQueue() {
  $('queueList').addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('button[data-op]') : null;
    if (!button) { return; }
    var item = event.target.closest('.qItem');
    if (!item) { return; }
    var id = item.getAttribute('data-id');
    var op = button.getAttribute('data-op');
    if (op === 'cancel') {
      apiPost('/api/transfer/cancel', { id: id })
        .then(function () { toast('已取消', 'ok'); pollState(); })
        .catch(function (err) { toast('取消失败：' + err.message, 'err'); });
    }
    if (op === 'retry') {
      var dir = item.getAttribute('data-dir');
      if (dir === 'push' || dir === 'receive') {
        toast('请重新拖拽一次以重试（源信息已过期）', 'warn');
      } else {
        toast('请重新拖拽或点击下载以重试', 'warn');
      }
    }
  });
}

// ------------------------------------------------------------------ 状态轮询
function pollState() {
  return apiGet('/api/state').then(function (data) {
    var previousPeers = state.peers.map(function (p) { return p.fingerprint; }).join(',');
    state.info = data;
    state.peers = data.peers || [];
    state.transfers = data.transfers || [];
    state.pending = data.pending || [];

    $('myInfo').textContent = data.alias + ' · ' + (data.ips[0] || '127.0.0.1') + ':' + data.port +
      ' · 共享 ' + data.share_dir;
    document.title = 'FileShare · ' + data.alias;

    var changed = previousPeers !== state.peers.map(function (p) { return p.fingerprint; }).join(',');
    renderPeerSelect();
    renderQueue();
    renderPending();
    if (changed) {
      refreshRight();
      var count = state.peers.length;
      if (count) { toast('发现 ' + count + ' 台设备', 'ok', 2200); }
      var clash = state.peers.filter(function (p) { return p.same_fingerprint; });
      if (clash.length && !state.warnedClash) {
        state.warnedClash = true;
        toast('发现设备 ' + clash[0].ip + ' 与本机指纹相同：它是另一台电脑，但 data 目录是从本机复制过去的。' +
              '建议在对方机器上删掉 data\\fingerprint.txt 与 data\\machine.txt 后重启（会自动生成新指纹）。',
              'warn', 12000);
      }
    }
    return data;
  }).catch(function (err) {
    $('myInfo').textContent = '服务连接失败：' + err.message;
    throw err;
  });
}

function renderPending() {
  var items = state.pending || [];
  var modal = $('pendingModal');
  if (!items.length) { modal.hidden = true; return; }
  var item = items[0];
  modal.hidden = false;
  var names = (item.files || []).slice(0, 8).join('、');
  if ((item.files || []).length > 8) { names += ' 等'; }
  $('pendingBody').innerHTML =
    '<div style="font-size:13px;line-height:1.7">' +
    '<div>来自：<b>' + esc((item.info && item.info.alias) || item.peer_ip) + '</b>' +
    '（' + esc(item.peer_ip) + '）</div>' +
    '<div>文件：' + (item.files || []).length + ' 个，共 ' + fmtSize(item.total) + '</div>' +
    '<div style="color:var(--muted);word-break:break-all">' + esc(names) + '</div></div>';
  var left = 60 - Math.floor(Date.now() / 1000 - (item.created || 0));
  $('pendingCountdown').textContent = left > 0 ? (left + ' 秒后自动拒绝') : '即将自动拒绝';
}

function decidePending(accept) {
  var items = state.pending || [];
  if (!items.length) { return; }
  var id = items[0].sessionId;
  apiPost('/api/pending/decide', { sessionId: id, accept: !!accept })
    .then(function () { toast(accept ? '已接受，正在接收' : '已拒绝', accept ? 'ok' : 'warn'); pollState(); })
    .catch(function (err) { toast('操作失败：' + err.message, 'err'); });
}

// ------------------------------------------------------------------ 设备/设置/二维码
/* 预填本机所在的 /24 网段（如 192.168.1.），便于只补最后一段 IP。
   动态推导而不是写死网段：换 WiFi/换网络时自动跟着变，也避免把作者的网段写进代码。 */
function localSubnetHint() {
  var info = state.info || {};
  var ip = (info.ips && info.ips[0]) || '';
  var parts = String(ip).split('.');
  if (parts.length === 4) { return parts.slice(0, 3).join('.') + '.'; }
  return '192.168.1.';
}

function addDevice() {
  var addr = window.prompt('对方 IP 或 IP:端口', localSubnetHint());
  if (!addr) { return; }
  apiPost('/api/peer/add', { addr: addr.trim() })
    .then(function (data) {
      toast('已添加：' + ((data.peer && data.peer.alias) || addr), 'ok');
      pollState().then(refreshRight);
    })
    .catch(function (err) { toast('添加失败：' + err.message, 'err', 5000); });
}

function scanDevices() {
  if (state.scanning) { toast('正在扫描中…', 'warn'); return; }
  state.scanning = true;
  $('btnScan').disabled = true;
  toast('正在扫描本网段设备（约 3~8 秒）…', 'warn', 4000);
  apiPost('/api/peer/scan', {})
    .then(function () { pollState(); })
    .catch(function (err) { toast('扫描失败：' + err.message, 'err'); })
    .then(function () {
      setTimeout(function () {
        state.scanning = false;
        $('btnScan').disabled = false;
        pollState().then(function (data) {
          toast('扫描结束，当前发现 ' + (data.peers || []).length + ' 台设备', 'ok');
        });
      }, 9000);
    });
}

function openSettings() {
  var info = state.info || {};
  $('setAlias').value = info.alias || '';
  $('setShare').value = info.share_dir || '';
  $('setInbox').value = info.inbox_dir || '';
  $('setDownloads').value = info.download_dir || '';
  $('setAutoAccept').checked = !!info.auto_accept;
  $('setKeepCopy').checked = !!info.keep_copy_on_dragout;
  $('setAllowDelete').checked = !!info.allow_delete;
  $('setPin').value = '';
  $('setPin').placeholder = info.pin_set ? '（已设置，留空表示不修改）' : '留空表示不启用';
  $('setFingerprint').value = info.fingerprint || '';
  $('settingsModal').hidden = false;
}

function saveSettings() {
  var payload = {
    alias: $('setAlias').value.trim(),
    share_dir: $('setShare').value.trim(),
    inbox_dir: $('setInbox').value.trim(),
    download_dir: $('setDownloads').value.trim(),
    auto_accept: $('setAutoAccept').checked,
    keep_copy_on_dragout: $('setKeepCopy').checked,
    allow_delete: $('setAllowDelete').checked,
  };
  var pin = $('setPin').value.trim();
  if (pin) { payload.pin = pin; }
  apiPost('/api/settings', payload)
    .then(function () {
      toast('设置已保存', 'ok');
      $('settingsModal').hidden = true;
      pollState().then(function () { refreshLeft(); refreshRight(); });
    })
    .catch(function (err) { toast('保存失败：' + err.message, 'err', 5000); });
}

function openQr() {
  var info = state.info || {};
  var url = info.url || ('http://' + ((info.ips || [])[0] || '127.0.0.1') + ':' + (info.port || 8099));
  $('qrUrl').textContent = url + '  （用手机浏览器打开即可下载共享文件）';
  $('qrModal').hidden = false;
  var canvas = $('qrCanvas');
  try {
    QR.draw(canvas, url, { level: 'M', scale: 5, quiet: 3 });
  } catch (err) {
    $('qrUrl').textContent = '二维码生成失败（' + err.message + '），请手动输入：' + url;
  }
}

/* Ctrl+V 粘贴即共享：截图/文本直接落进共享目录 */
function bindPaste() {
  document.addEventListener('paste', function (event) {
    var items = (event.clipboardData && event.clipboardData.items) || [];
    var jobs = [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === 'file') {
        var file = items[i].getAsFile();
        if (file) {
          var ext = (file.type.split('/')[1] || 'png').replace('quicktime', 'mov');
          var name = file.name && file.name !== 'image.png'
            ? file.name
            : ('paste_' + new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19) + '.' + ext);
          jobs.push({ file: file, relDir: '', name: name });
        }
      }
    }
    if (!jobs.length) {
      var text = event.clipboardData && event.clipboardData.getData('text/plain');
      if (text && text.trim()) {
        var blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        jobs.push({
          file: blob, relDir: '',
          name: 'paste_' + new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19) + '.txt',
        });
      }
    }
    if (!jobs.length) { return; }
    event.preventDefault();
    var label = $('leftDrop').querySelector('.dzText');
    var pairs = jobs.map(function (job) { return { file: job.file, relDir: '', __name: job.name }; });
    var chain = Promise.resolve();
    var done = 0;
    pairs.forEach(function (pair) {
      chain = chain.then(function () {
        var total = pair.file.size || 0;
        return uploadBlob(pair.file, {
          root: 'share', path: state.left.path, name: pair.__name,
          mtime: (pair.file.lastModified || Date.now()) / 1000,
        }, function (part) { setProgressLabel(label, part, total); }).then(function () {
          done += 1;
        }).catch(function (err) { toast('粘贴失败：' + err.message, 'err'); });
      });
    });
    chain.then(function () {
      if (label) { label.textContent = '拖拽文件 / 文件夹到这里'; }
      if (done) { toast('已粘贴 ' + done + ' 项到共享目录', 'ok'); }
      refreshLeft();
    });
  });
}

// ------------------------------------------------------------------ 初始化
function bindUi() {
  $('leftUp').addEventListener('click', function () {
    var path = state.left.path;
    enterLeft(path.indexOf('/') >= 0 ? path.replace(/\/[^/]*$/, '') : '');
  });
  $('leftRefresh').addEventListener('click', function () { refreshLeft(); });
  $('leftMkdir').addEventListener('click', function () {
    var name = window.prompt('新建文件夹名称', '新文件夹');
    if (!name) { return; }
    var full = state.left.path ? (state.left.path + '/' + name) : name;
    apiPost('/api/fs/mkdir?root=share&path=' + encodeURIComponent(full))
      .then(function () { toast('已新建文件夹', 'ok'); refreshLeft(); })
      .catch(function (err) { toast('新建失败：' + err.message, 'err'); });
  });
  $('btnPick').addEventListener('click', function () { $('filePicker').click(); });
  $('filePicker').addEventListener('change', function (event) {
    var files = event.target.files || [];
    var pairs = [];
    for (var i = 0; i < files.length; i++) { pairs.push({ file: files[i], relDir: '' }); }
    if (pairs.length) { uploadBatch(pairs, state.left.path, $('leftDrop').querySelector('.dzText')); }
    event.target.value = '';
  });

  $('peerSelect').addEventListener('change', function () {
    state.device = $('peerSelect').value;
    state.right.path = '';
    refreshRight();
  });
  $('peerUp').addEventListener('click', function () {
    var path = state.right.path;
    state.right.path = path.indexOf('/') >= 0 ? path.replace(/\/[^/]*$/, '') : '';
    refreshRight();
  });
  $('peerRefresh').addEventListener('click', function () { refreshRight(); });

  $('btnScan').addEventListener('click', scanDevices);
  $('btnAdd').addEventListener('click', addDevice);
  $('btnSettings').addEventListener('click', openSettings);
  $('btnQr').addEventListener('click', openQr);
  $('btnHelp').addEventListener('click', function () { $('helpModal').hidden = false; });
  $('btnQrClose').addEventListener('click', function () { $('qrModal').hidden = true; });
  $('btnHelpClose').addEventListener('click', function () { $('helpModal').hidden = true; });
  $('btnSettingsClose').addEventListener('click', function () { $('settingsModal').hidden = true; });
  $('btnSettingsSave').addEventListener('click', saveSettings);
  $('btnAccept').addEventListener('click', function () { decidePending(true); });
  $('btnReject').addEventListener('click', function () { decidePending(false); });
  $('btnRefreshState').addEventListener('click', function () { pollState(); });
  $('btnCancelAll').addEventListener('click', function () {
    apiPost('/api/transfer/cancel', { id: '*' })
      .then(function (data) { toast('已取消 ' + (data.cancelled || 0) + ' 项', 'ok'); pollState(); })
      .catch(function (err) { toast('取消失败：' + err.message, 'err'); });
  });

  // 弹窗点击遮罩关闭（接受/拒绝弹窗除外，必须明确选择）
  ['qrModal', 'helpModal', 'settingsModal'].forEach(function (id) {
    $(id).addEventListener('click', function (event) {
      if (event.target === $(id)) { $(id).hidden = true; }
    });
  });

  // 快捷键：F5 刷新、退格返回上级
  document.addEventListener('keydown', function (event) {
    if (event.key === 'F5') { event.preventDefault(); refreshLeft(); refreshRight(); pollState(); return; }
    if (event.key === 'Backspace' && !/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) {
      event.preventDefault();
      var path = state.left.path;
      enterLeft(path.indexOf('/') >= 0 ? path.replace(/\/[^/]*$/, '') : '');
    }
    if (event.key === 'Escape') {
      ['qrModal', 'helpModal', 'settingsModal'].forEach(function (id) { $(id).hidden = true; });
    }
  });
}

function init() {
  bindLeftRows();
  bindRightRows();
  bindLeftDrop();
  bindRightDrop();
  bindQueue();
  bindPaste();
  bindUi();

  pollState().then(function () {
    refreshLeft();
    refreshRight();
  }).catch(function () { /* 首屏失败仍显示界面 */ });

  setInterval(function () { pollState().catch(function () { /* 忽略瞬时失败 */ }); }, POLL_MS);
  setInterval(function () {
    if (!document.hidden) {
      refreshLeft();
      var peer = currentPeer();
      if (peer && peer.kind === 'app') { refreshRight(); }
    }
  }, 4000);

  if (!window.isSecureContext) {
    // 局域网 IP 打开时是非安全上下文：剪贴板 API 不可用，改用 paste 事件（已实现）
    toast('当前为非安全上下文，已自动使用兼容模式（拖拽、粘贴均可用）', 'warn', 5200);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
function currentPeer() {
  for (var i = 0; i < state.peers.length; i++) {
    if (state.peers[i].fingerprint === state.device) { return state.peers[i]; }
  }
  return null;
}

function renderPeerSelect() {
  var select = $('peerSelect');
  if (!state.peers.length) {
    select.innerHTML = '<option value="">（未发现设备）</option>';
    state.device = '';
    return;
  }
  var html = state.peers.map(function (peer) {
    var tag = peer.kind === 'app' ? '可浏览' : (peer.kind === 'localsend' ? '仅推送' : '检测中');
    var alive = (Date.now() / 1000 - (peer.last_seen || 0)) < 90;
    var warn = peer.same_fingerprint ? ' ⚠指纹与本机相同' : '';
    return '<option value="' + esc(peer.fingerprint) + '">' +
      esc(peer.alias || peer.ip) + ' · ' + peer.ip + ' · ' + tag + (alive ? '' : ' · 离线') + warn +
      '</option>';
  }).join('');
  select.innerHTML = html;
  var exists = state.peers.some(function (peer) { return peer.fingerprint === state.device; });
  if (!exists) { state.device = state.peers[0].fingerprint; }
  select.value = state.device;
}

function renderRight(message) {
  var list = $('peerList');
  var peer = currentPeer();
  var head = $('peerDropText');
  var sub = $('peerDropSub');

  if (!peer) {
    $('peerCrumb').textContent = '—';
    list.innerHTML = '<div class="empty">还没有发现其他电脑。<br>' +
      '点上方 <b>扫描设备</b>，或用 <b>添加设备</b> 手动填对方 IP。</div>';
    head.textContent = '先把左面板的文件拖到这里 = 推送给对方';
    sub.textContent = '（提示：需要先选中一台设备）';
    return;
  }
  if (peer.kind === 'localsend') {
    $('peerCrumb').textContent = peer.alias + '（官方 LocalSend）';
    list.innerHTML = '<div class="empty">对方是<b>官方 LocalSend</b>（如手机 App）。<br>' +
      '它的协议不支持浏览文件列表，但你可以把左边文件拖到下方区域<b>推送</b>给它，' +
      '或让对方推送给你。</div>';
    head.textContent = '把左边文件拖到这里 = 推送给 ' + peer.alias;
    sub.textContent = '对方手机上会弹出接受提示';
    return;
  }
  if (message) {
    list.innerHTML = '<div class="empty">' + esc(message) + '</div>';
  } else if (!state.right.entries.length) {
    list.innerHTML = '<div class="empty">对方共享目录是空的，或该层没有文件。</div>';
  } else {
    list.innerHTML = state.right.entries.map(function (entry) {
      return '<div class="row" data-rel="' + esc(entry.rel) + '" data-dir="' + (entry.is_dir ? '1' : '0') +
        '" data-name="' + esc(entry.name) + '" data-size="' + entry.size + '" data-mime="' + esc(entry.mime || '') +
        '" draggable="' + (entry.is_dir ? 'false' : 'true') + '">' +
        '<span class="ico">' + iconFor(entry) + '</span>' +
        '<span class="nm" title="' + esc(entry.name) + '">' + esc(entry.name) + '</span>' +
        '<span class="sz">' + (entry.is_dir ? '' : fmtSize(entry.size)) + '</span>' +
        '<span class="ops">' +
        (entry.is_dir ? '' : '<button data-op="download">下载</button><button data-op="copy">复制链接</button>') +
        '</span></div>';
    }).join('');
  }
  $('peerCrumb').textContent = peer.alias + ':/' + (state.right.path || '');
  head.textContent = '把左边文件拖到这里 = 推送给 ' + peer.alias;
  sub.textContent = '把右边的文件拖到桌面/资源管理器 = 下载';
}

function refreshRight() {
  var peer = currentPeer();
  if (!peer) { renderRight(''); return Promise.resolve(); }
  if (peer.kind === 'localsend') { renderRight(''); return Promise.resolve(); }
  var query = '?device=' + encodeURIComponent(peer.fingerprint) +
              '&root=share&path=' + encodeURIComponent(state.right.path);
  return apiGet('/api/peer/list' + query).then(function (data) {
    state.right.entries = data.entries || [];
    state.right.kind = peer.kind;
    renderRight('');
    if (state.right.path && !data.count) { /* 空目录保持显示 */ }
  }).catch(function (err) {
    state.right.entries = [];
    renderRight('读取失败：' + err.message);
  });
}

function rightRowInfo(row) {
  return {
    rel: row.getAttribute('data-rel'),
    name: row.getAttribute('data-name'),
    isDir: row.getAttribute('data-dir') === '1',
    size: Number(row.getAttribute('data-size')) || 0,
    mime: row.getAttribute('data-mime') || '',
  };
}

function bindRightRows() {
  var list = $('peerList');

  list.addEventListener('click', function (event) {
    var row = event.target.closest ? event.target.closest('.row') : null;
    if (!row) { return; }
    var info = rightRowInfo(row);
    var button = event.target.closest ? event.target.closest('button[data-op]') : null;
    if (button) {
      event.stopPropagation();
      var op = button.getAttribute('data-op');
      if (op === 'download') { pullTo('downloads', info.rel, info.name); }
      if (op === 'copy') {
        var peer = currentPeer();
        var url = location.origin + '/api/peer/raw?device=' + encodeURIComponent(peer.fingerprint) +
          '&root=share&path=' + encodeURIComponent(info.rel) + '&name=' + encodeURIComponent(info.name);
        window.prompt('下载链接（可粘贴到浏览器或下载工具）', url);
      }
      return;
    }
    if (info.isDir) { state.right.path = info.rel; refreshRight(); return; }
    list.querySelectorAll('.row.selected').forEach(function (node) { node.classList.remove('selected'); });
    row.classList.add('selected');
  });

  list.addEventListener('dblclick', function (event) {
    var row = event.target.closest ? event.target.closest('.row') : null;
    if (row && rightRowInfo(row).isDir) {
      state.right.path = rightRowInfo(row).rel;
      refreshRight();
    }
  });

  list.addEventListener('dragstart', function (event) {
    var row = event.target.closest ? event.target.closest('.row') : null;
    if (!row) { return; }
    var info = rightRowInfo(row);
    if (info.isDir) { event.preventDefault(); return; }
    var peer = currentPeer();
    if (!peer) { event.preventDefault(); return; }
    event.dataTransfer.setData(ITEM_MIME, JSON.stringify({
      side: 'right', device: peer.fingerprint, root: 'share', rel: info.rel,
      name: info.name, size: info.size, mime: info.mime,
    }));
    event.dataTransfer.setData('text/plain', info.name);
    event.dataTransfer.effectAllowed = 'copy';
    // 关键：拖出到资源管理器时由本机后端代理下载（可带进度、可留副本、不受 CORS 限制）
    var url = location.origin + '/api/peer/raw?device=' + encodeURIComponent(peer.fingerprint) +
      '&root=share&path=' + encodeURIComponent(info.rel) + '&name=' + encodeURIComponent(info.name);
    setDownloadUrl(event.dataTransfer, info.mime || 'application/octet-stream', info.name, url);
    list.querySelectorAll('.row.selected').forEach(function (node) { node.classList.remove('selected'); });
    row.classList.add('selected');
  });
}

/* 页内拖拽下载：把对方文件拉到本机 share / inbox / downloads */
function pullTo(target, rel, name) {
  var peer = currentPeer();
  if (!peer) { toast('请先选择设备', 'warn'); return Promise.resolve(); }
  if (!rel) { toast('请先选中一个文件', 'warn'); return Promise.resolve(); }
  return apiPost('/api/peer/pull', {
    device: peer.fingerprint, root: 'share', path: rel, name: name || rel.split('/').pop(),
    target: target,
  }).then(function (data) {
    toast('已加入下载队列：' + (data.transfer ? data.transfer.name : ''), 'ok');
    pollState();
  }).catch(function (err) {
    toast('下载失败：' + err.message, 'err');
  });
}