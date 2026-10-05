/* 探测参考库可用 API：输出到 tests/api_probe.json */
const fs = require('fs');
const path = require('path');
const base = path.join(__dirname, 'qrref', 'node_modules', 'qrcode', 'lib');
const out = {};
for (const name of ['core/segments', 'core/mode', 'core/qrcode', 'core/rs-block', 'core/error-correction-level']) {
  try {
    const mod = require(path.join(base, name));
    out[name] = Object.keys(mod);
  } catch (err) {
    out[name] = 'ERROR: ' + err.message;
  }
}
try {
  const Segments = require(path.join(base, 'core/segments'));
  const Mode = require(path.join(base, 'core/mode'));
  out.fromString = typeof Segments.fromString;
  out.fromArray = typeof Segments.fromArray;
  out.modeByte = Mode.BYTE ? Mode.BYTE.id : null;
} catch (err) {
  out.segmentError = err.message;
}
fs.writeFileSync(path.join(__dirname, 'api_probe.json'), JSON.stringify(out, null, 2), 'utf8');
console.log('probe written');
