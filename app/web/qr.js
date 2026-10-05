/*!
 * qr.js —— 极简二维码编码器（Byte 模式，纠错级别 L/M/Q/H，版本 1-10）
 * 纯 JS、零依赖，仅用于在本机界面显示"手机访问地址"二维码。
 * 实现依据 ISO/IEC 18004（本文件由本项目自行编写，未复制第三方代码）。
 */
(function (global) {
  'use strict';

  // 版本 -> 纠错级别 -> [每块纠错码字数, [[块数, 数据码字数], ...]]
  var RS_BLOCKS = {
    1: { L: [7, [[1, 19]]], M: [10, [[1, 16]]], Q: [13, [[1, 13]]], H: [17, [[1, 9]]] },
    2: { L: [10, [[1, 34]]], M: [16, [[1, 28]]], Q: [22, [[1, 22]]], H: [28, [[1, 16]]] },
    3: { L: [15, [[1, 55]]], M: [26, [[1, 44]]], Q: [18, [[2, 17]]], H: [22, [[2, 13]]] },
    4: { L: [20, [[1, 80]]], M: [18, [[2, 32]]], Q: [26, [[2, 24]]], H: [16, [[4, 9]]] },
    5: { L: [26, [[1, 108]]], M: [24, [[2, 43]]], Q: [18, [[2, 15], [2, 16]]], H: [22, [[2, 11], [2, 12]]] },
    6: { L: [18, [[2, 68]]], M: [16, [[4, 27]]], Q: [24, [[4, 19]]], H: [28, [[4, 15]]] },
    7: { L: [20, [[2, 78]]], M: [18, [[4, 31]]], Q: [18, [[2, 14], [4, 15]]], H: [26, [[4, 13], [1, 14]]] },
    8: { L: [24, [[2, 97]]], M: [22, [[2, 38], [2, 39]]], Q: [22, [[4, 18], [2, 19]]], H: [26, [[4, 14], [2, 15]]] },
    9: { L: [30, [[2, 116]]], M: [22, [[3, 36], [2, 37]]], Q: [20, [[4, 16], [4, 17]]], H: [24, [[4, 12], [4, 13]]] },
    10: { L: [18, [[2, 68], [2, 69]]], M: [26, [[4, 43], [1, 44]]], Q: [24, [[6, 19], [2, 20]]], H: [28, [[6, 15], [2, 16]]] }
  };

  var ALIGN_POS = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]
  };

  var FORMAT_BITS = { L: 1, M: 0, Q: 3, H: 2 };

  // ---------------------------------------------------------------- GF(256)
  var EXP = new Uint8Array(512);
  var LOG = new Uint8Array(256);
  (function () {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      EXP[i] = x;
      LOG[x] = i;
      x <<= 1;
      if (x & 0x100) { x ^= 0x11d; }
    }
    for (var j = 255; j < 512; j++) { EXP[j] = EXP[j - 255]; }
  })();

  function gfMul(a, b) {
    if (a === 0 || b === 0) { return 0; }
    return EXP[LOG[a] + LOG[b]];
  }

  function rsGenerator(degree) {
    var poly = [1];
    for (var i = 0; i < degree; i++) {
      var next = new Array(poly.length + 1);
      for (var k = 0; k < next.length; k++) { next[k] = 0; }
      for (var j = 0; j < poly.length; j++) {
        next[j] ^= poly[j];
        next[j + 1] ^= gfMul(poly[j], EXP[i]);
      }
      poly = next;
    }
    return poly;
  }

  function rsEncode(data, ecLen) {
    var gen = rsGenerator(ecLen);
    var buf = data.slice();
    for (var z = 0; z < ecLen; z++) { buf.push(0); }
    for (var i = 0; i < data.length; i++) {
      var coef = buf[i];
      if (coef !== 0) {
        for (var j = 0; j < gen.length; j++) {
          buf[i + j] ^= gfMul(gen[j], coef);
        }
      }
    }
    return buf.slice(data.length);
  }

  function utf8Bytes(text) {
    if (typeof TextEncoder !== 'undefined') {
      return Array.prototype.slice.call(new TextEncoder().encode(text));
    }
    var out = [];
    var encoded = unescape(encodeURIComponent(text));
    for (var i = 0; i < encoded.length; i++) { out.push(encoded.charCodeAt(i) & 0xff); }
    return out;
  }

  // ------------------------------------------------------- 数据码字 + 纠错
  function buildCodewords(bytes, level) {
    var version = 0;
    var info = null;
    for (var v = 1; v <= 10; v++) {
      var entry = RS_BLOCKS[v][level];
      if (!entry) { continue; }
      var totalData = 0;
      for (var g = 0; g < entry[1].length; g++) {
        totalData += entry[1][g][0] * entry[1][g][1];
      }
      var ccBits = v <= 9 ? 8 : 16;
      if (4 + ccBits + bytes.length * 8 <= totalData * 8) {
        version = v;
        info = entry;
        break;
      }
    }
    if (!version) {
      throw new Error('内容过长：本实现仅支持版本 1-10（最多约 270 字节）');
    }

    var capacityBits = 0;
    for (var g2 = 0; g2 < info[1].length; g2++) {
      capacityBits += info[1][g2][0] * info[1][g2][1] * 8;
    }

    var bits = [];
    function pushBits(value, len) {
      for (var i = len - 1; i >= 0; i--) { bits.push((value >>> i) & 1); }
    }
    pushBits(4, 4);                                    // 字节模式
    pushBits(bytes.length, version <= 9 ? 8 : 16);      // 字符计数
    for (var b = 0; b < bytes.length; b++) { pushBits(bytes[b], 8); }
    for (var t = 0; t < 4 && bits.length < capacityBits; t++) { bits.push(0); }
    while (bits.length % 8 !== 0) { bits.push(0); }

    var dataCW = [];
    for (var i = 0; i < bits.length; i += 8) {
      var value = 0;
      for (var j = 0; j < 8; j++) { value = (value << 1) | bits[i + j]; }
      dataCW.push(value);
    }
    var pad = [0xec, 0x11];
    for (var p = 0; dataCW.length < capacityBits / 8; p++) { dataCW.push(pad[p % 2]); }

    var ecPerBlock = info[0];
    var blocks = [];
    var offset = 0;
    for (var gi = 0; gi < info[1].length; gi++) {
      var count = info[1][gi][0];
      var dataLen = info[1][gi][1];
      for (var c = 0; c < count; c++) {
        var chunk = dataCW.slice(offset, offset + dataLen);
        offset += dataLen;
        blocks.push({ data: chunk, ec: rsEncode(chunk, ecPerBlock) });
      }
    }

    var out = [];
    var maxData = 0;
    for (var bi = 0; bi < blocks.length; bi++) {
      if (blocks[bi].data.length > maxData) { maxData = blocks[bi].data.length; }
    }
    for (var k = 0; k < maxData; k++) {
      for (var bj = 0; bj < blocks.length; bj++) {
        if (k < blocks[bj].data.length) { out.push(blocks[bj].data[k]); }
      }
    }
    for (var e = 0; e < ecPerBlock; e++) {
      for (var bm = 0; bm < blocks.length; bm++) { out.push(blocks[bm].ec[e]); }
    }
    return { version: version, codewords: out };
  }

  function getBit(value, index) {
    return ((value >>> index) & 1) !== 0;
  }

  var PENALTY_A = [true, false, true, true, true, false, true, false, false, false, false];
  var PENALTY_B = [false, false, false, false, true, false, true, true, true, false, true];

  function buildMatrix(version, codewords, level, mask) {
    var size = version * 4 + 17;
    var modules = [];
    var isFunction = [];
    var y, x;
    for (y = 0; y < size; y++) {
      modules.push(new Array(size).fill(false));
      isFunction.push(new Array(size).fill(false));
    }
    function setFn(cx, cy, dark) {
      modules[cy][cx] = !!dark;
      isFunction[cy][cx] = true;
    }
    function drawFinder(cx, cy) {
      for (var dy = -4; dy <= 4; dy++) {
        for (var dx = -4; dx <= 4; dx++) {
          var xx = cx + dx, yy = cy + dy;
          if (xx < 0 || xx >= size || yy < 0 || yy >= size) { continue; }
          var dist = Math.max(Math.abs(dx), Math.abs(dy));
          setFn(xx, yy, dist !== 2 && dist !== 4);
        }
      }
    }
    function drawAlign(cx, cy) {
      for (var dy = -2; dy <= 2; dy++) {
        for (var dx = -2; dx <= 2; dx++) {
          var xx = cx + dx, yy = cy + dy;
          if (xx < 0 || xx >= size || yy < 0 || yy >= size) { continue; }
          setFn(xx, yy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
        }
      }
    }

    for (var i = 0; i < size; i++) {
      setFn(6, i, i % 2 === 0);
      setFn(i, 6, i % 2 === 0);
    }
    drawFinder(3, 3);
    drawFinder(size - 4, 3);
    drawFinder(3, size - 4);
    var ap = ALIGN_POS[version] || [];
    for (var a = 0; a < ap.length; a++) {
      for (var b = 0; b < ap.length; b++) {
        var corner = (a === 0 && b === 0) || (a === 0 && b === ap.length - 1) ||
                     (a === ap.length - 1 && b === 0);
        if (!corner) { drawAlign(ap[a], ap[b]); }
      }
    }

    function drawFormat(maskPattern) {
      var data = (FORMAT_BITS[level] << 3) | maskPattern;
      var rem = data;
      for (var k = 0; k < 10; k++) { rem = (rem << 1) ^ ((rem >>> 9) * 0x537); }
      var bits = ((data << 10) | rem) ^ 0x5412;
      for (var j = 0; j <= 5; j++) { setFn(8, j, getBit(bits, j)); }
      setFn(8, 7, getBit(bits, 6));
      setFn(8, 8, getBit(bits, 7));
      setFn(7, 8, getBit(bits, 8));
      for (var m = 9; m < 15; m++) { setFn(14 - m, 8, getBit(bits, m)); }
      for (var n = 0; n < 8; n++) { setFn(size - 1 - n, 8, getBit(bits, n)); }
      for (var p = 8; p < 15; p++) { setFn(8, size - 15 + p, getBit(bits, p)); }
      setFn(8, size - 8, true);
    }
    function drawVersion() {
      if (version < 7) { return; }
      var rem = version;
      for (var k = 0; k < 12; k++) { rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25); }
      var bits = (version << 12) | rem;
      for (var i2 = 0; i2 < 18; i2++) {
        var bit = getBit(bits, i2);
        var ax = size - 11 + (i2 % 3), ay = Math.floor(i2 / 3);
        setFn(ax, ay, bit);
        setFn(ay, ax, bit);
      }
    }

    drawFormat(0);              // 先占位（标记功能模块），最后按选定掩码重画
    drawVersion();

    // 数据按"右侧两列一组、上下蛇形"放置
    var bitIndex = 0;
    var totalBits = codewords.length * 8;
    for (var right = size - 1; right >= 1; right -= 2) {
      if (right === 6) { right = 5; }
      for (var vert = 0; vert < size; vert++) {
        for (var col = 0; col < 2; col++) {
          var cx = right - col;
          var upward = ((right + 1) & 2) === 0;
          var cy = upward ? size - 1 - vert : vert;
          if (!isFunction[cy][cx] && bitIndex < totalBits) {
            modules[cy][cx] = getBit(codewords[bitIndex >> 3], 7 - (bitIndex & 7));
            bitIndex++;
          }
        }
      }
    }

    function maskCondition(maskPattern, mx, my) {
      switch (maskPattern) {
        case 0: return (mx + my) % 2 === 0;
        case 1: return my % 2 === 0;
        case 2: return mx % 3 === 0;
        case 3: return (mx + my) % 3 === 0;
        case 4: return (Math.floor(my / 2) + Math.floor(mx / 3)) % 2 === 0;
        case 5: return ((mx * my) % 2) + ((mx * my) % 3) === 0;
        case 6: return (((mx * my) % 2) + ((mx * my) % 3)) % 2 === 0;
        default: return (((mx + my) % 2) + ((mx * my) % 3)) % 2 === 0;
      }
    }
    function applyMask(maskPattern) {
      for (var my = 0; my < size; my++) {
        for (var mx = 0; mx < size; mx++) {
          if (!isFunction[my][mx] && maskCondition(maskPattern, mx, my)) {
            modules[my][mx] = !modules[my][mx];
          }
        }
      }
    }

    function penaltyScore() {
      var score = 0, mx, my, run;
      for (my = 0; my < size; my++) {                     // 规则 1：行
        run = 0;
        for (mx = 0; mx < size; mx++) {
          if (mx === 0 || modules[my][mx] !== modules[my][mx - 1]) {
            if (run >= 5) { score += 3 + (run - 5); }
            run = 1;
          } else { run++; }
        }
        if (run >= 5) { score += 3 + (run - 5); }
      }
      for (mx = 0; mx < size; mx++) {                     // 规则 1：列
        run = 0;
        for (my = 0; my < size; my++) {
          if (my === 0 || modules[my][mx] !== modules[my - 1][mx]) {
            if (run >= 5) { score += 3 + (run - 5); }
            run = 1;
          } else { run++; }
        }
        if (run >= 5) { score += 3 + (run - 5); }
      }
      for (my = 0; my < size - 1; my++) {                 // 规则 2：2x2 同色块
        for (mx = 0; mx < size - 1; mx++) {
          var c = modules[my][mx];
          if (c === modules[my][mx + 1] && c === modules[my + 1][mx] && c === modules[my + 1][mx + 1]) {
            score += 3;
          }
        }
      }
      function patternCount(line) {                       // 规则 3：类定位图案
        var hits = 0;
        for (var s = 0; s + 11 <= line.length; s++) {
          var hitA = true, hitB = true;
          for (var t = 0; t < 11; t++) {
            if (line[s + t] !== PENALTY_A[t]) { hitA = false; }
            if (line[s + t] !== PENALTY_B[t]) { hitB = false; }
          }
          if (hitA) { hits++; }
          if (hitB) { hits++; }
        }
        return hits;
      }
      for (my = 0; my < size; my++) {
        score += 40 * patternCount(modules[my]);
        var column = [];
        for (mx = 0; mx < size; mx++) { column.push(modules[mx][my]); }
        score += 40 * patternCount(column);
      }
      var dark = 0;                                       // 规则 4：黑白比例
      for (my = 0; my < size; my++) {
        for (mx = 0; mx < size; mx++) { if (modules[my][mx]) { dark++; } }
      }
      var ratio = Math.abs(dark * 100 / (size * size) - 50);
      score += Math.floor(ratio / 5) * 10;
      return score;
    }

    var chosen = mask;
    if (chosen === null || chosen === undefined || chosen < 0 || chosen > 7) {
      var best = 0, bestScore = Infinity;
      for (var mk = 0; mk < 8; mk++) {
        applyMask(mk);
        drawFormat(mk);
        var sc = penaltyScore();
        applyMask(mk);                                    // 撤销掩码
        if (sc < bestScore) { bestScore = sc; best = mk; }
      }
      chosen = best;
    }
    applyMask(chosen);
    drawFormat(chosen);
    return { version: version, size: size, modules: modules, mask: chosen };
  }

  function create(text, options) {
    options = options || {};
    var level = options.level || 'M';
    var mask = (options.mask === undefined || options.mask === null) ? null : options.mask;
    var built = buildCodewords(utf8Bytes(text), level);
    return buildMatrix(built.version, built.codewords, level, mask);
  }

  function draw(canvas, text, options) {
    options = options || {};
    var scale = options.scale || 4;
    var quiet = options.quiet === undefined ? 4 : options.quiet;
    var qr = create(text, options);
    var side = (qr.size + quiet * 2) * scale;
    canvas.width = side;
    canvas.height = side;
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, side, side);
    ctx.fillStyle = options.dark || '#000000';
    for (var y = 0; y < qr.size; y++) {
      for (var x = 0; x < qr.size; x++) {
        if (qr.modules[y][x]) {
          ctx.fillRect((x + quiet) * scale, (y + quiet) * scale, scale, scale);
        }
      }
    }
    return qr;
  }

  global.QR = global.QR || {};
  global.QR.create = create;
  global.QR.draw = draw;
  global.QR.buildMatrix = buildMatrix;
  global.QR.buildCodewords = buildCodewords;
})(typeof window !== 'undefined' ? window : this);
