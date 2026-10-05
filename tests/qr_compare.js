/* 二维码交叉验证：把自制 app/web/qr.js 的输出与 npm qrcode 包逐模块比对。
 * 关键点：必须把参考实现也强制为 Byte 模式（否则它会自动分段优化，两边码字天然不同）。
 * 用法： node tests/qr_compare.js   （结果写入 tests/qr_compare_result.txt）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const LIB = path.join(__dirname, 'qrref', 'node_modules', 'qrcode', 'lib', 'core');
const Segments = require(path.join(LIB, 'segments'));
const Mode = require(path.join(LIB, 'mode'));
const QRCodeCore = require(path.join(LIB, 'qrcode'));
const EccLevel = require(path.join(LIB, 'error-correction-level'));

const src = fs.readFileSync(path.join(ROOT, 'app', 'web', 'qr.js'), 'utf8');
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const QR = sandbox.QR;

const samples = [
  'http://10.0.0.5:8099',
  'http://127.0.0.1:8099',
  'a',
  'https://example.com/very/long/path?with=query&and=more#frag',
  '\u4e2d\u6587\u6d4b\u8bd5\uff1a\u5c40\u57df\u7f51\u4f20\u8f93',
  'x'.repeat(180),
  '0123456789'.repeat(15),
  'FileShare / \u5c40\u57df\u7f51\u4f20\u8f93 2026',
];

const lines = [];
let checks = 0;
let failures = 0;
let versionChecks = 0;
let versionFailures = 0;
let autoMaskChecks = 0;
let autoMaskFailures = 0;
let skipped = 0;

function log(msg) { lines.push(msg); }

for (const level of ['L', 'M', 'Q', 'H']) {
  for (const text of samples) {
    const label = `level=${level} text=${JSON.stringify(text.slice(0, 26))}`;
    const segment = Segments.fromArray([{ data: text, mode: Mode.BYTE }]);
    const ecl = EccLevel.from(level);

    // 1) 版本选择（Byte 模式）应与参考实现一致
    const refAuto = QRCodeCore.create(segment, { errorCorrectionLevel: ecl });
    let mineAuto;
    try {
      mineAuto = QR.create(text, { level: level });
    } catch (err) {
      if (err.message.indexOf('1-10') >= 0) {
        skipped += 1;                       // 超出实现支持的版本范围（1-10），跳过
        continue;
      }
      failures += 1;
      log(`FAIL 编码异常 ${label}: ${err.message}`);
      continue;
    }
    versionChecks += 1;
    if (mineAuto.version !== refAuto.version) {
      versionFailures += 1;
      log(`FAIL 版本选择 ${label} 期望 ${refAuto.version} 实际 ${mineAuto.version}`);
      continue;
    }
    autoMaskChecks += 1;
    if (mineAuto.mask !== refAuto.maskPattern) {
      autoMaskFailures += 1;
      log(`FAIL 掩码选择 ${label} 期望 ${refAuto.maskPattern} 实际 ${mineAuto.mask}`);
    }

    // 2) 逐模块比对（固定版本 + 固定掩码，覆盖 8 种掩码）
    for (let mask = 0; mask < 8; mask++) {
      const ref = QRCodeCore.create(segment, {
        errorCorrectionLevel: ecl, version: mineAuto.version, maskPattern: mask,
      });
      let mine;
      try {
        mine = QR.create(text, { level: level, mask: mask });
      } catch (err) {
        failures += 1;
        log(`FAIL 编码异常 ${label} mask=${mask}: ${err.message}`);
        continue;
      }
      checks += 1;
      if (mine.size !== ref.modules.size) {
        failures += 1;
        log(`FAIL 尺寸不一致 ${label} mask=${mask} 期望 ${ref.modules.size} 实际 ${mine.size}`);
        continue;
      }
      let diff = 0;
      let first = '';
      for (let y = 0; y < ref.modules.size; y++) {
        for (let x = 0; x < ref.modules.size; x++) {
          const a = !!mine.modules[y][x];
          const b = !!ref.modules.get(y, x);
          if (a !== b) {
            diff += 1;
            if (!first) { first = `row=${y},col=${x}`; }
          }
        }
      }
      if (diff) {
        failures += 1;
        log(`FAIL 矩阵不一致 ${label} version=${mine.version} mask=${mask} 差异 ${diff} 首处 ${first}`);
      }
    }
  }
}

log('');
log(`固定掩码矩阵比对： ${checks - failures}/${checks} 通过`);
log(`版本选择比对：     ${versionChecks - versionFailures}/${versionChecks} 通过`);
log(`自动掩码比对：     ${autoMaskChecks - autoMaskFailures}/${autoMaskChecks} 通过`);
log(`超出容量跳过：     ${skipped} 项（本实现支持版本 1-10，仅需覆盖局域网 URL）`);
const text = lines.join('\n');
fs.writeFileSync(path.join(__dirname, 'qr_compare_result.txt'), text, 'utf8');
const ok = failures + versionFailures + autoMaskFailures === 0;
console.log(ok ? 'QR_COMPARE_OK' : 'QR_COMPARE_FAIL');
process.exit(ok ? 0 : 1);
