'use strict';
// ─── Standalone "unscannable" QR generator ────────────────────────────────────
// Produces a PNG that LOOKS like a dense, genuine QR code but cannot be decoded
// by any reader. Drop this one file into any Node project.
//
// Dependencies: ONLY the `qrcode` npm package  ( npm i qrcode )
//   — no @napi-rs/canvas, no other files. PNG is written here with Node's zlib.
//
//   const { makeUnscannableQr } = require('./unscannable-qr.standalone');
//   const png = makeUnscannableQr();              // Buffer (PNG bytes)
//   require('fs').writeFileSync('fake.png', png);
//
// How it works: encode a real ~1 KB payload so the library lays out an authentic
// high-version grid (finders, timing, quiet zone), then flip ~40% of the data
// modules — far past what error correction can repair — while KEEPING the three
// finder-pattern corners, so scanners still see "a QR" but can never decode it.

const zlib = require('zlib');
const QRCode = require('qrcode');

// ── minimal PNG writer (8-bit grayscale) ─────────────────────────────────────
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i += 1) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function pngChunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length, 0);
  const body = Buffer.concat([Buffer.from(type, 'latin1'), data]);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body), 0);
  return Buffer.concat([len, body, crc]);
}
function grayPng(size, pixels) {          // pixels: Uint8Array(size*size) of 0|255
  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);            // width
  ihdr.writeUInt32BE(size, 4);            // height
  ihdr[8] = 8;                            // bit depth
  ihdr[9] = 0;                            // colour type 0 = grayscale
  // 10,11,12 = compression / filter / interlace = 0
  const raw = Buffer.alloc((size + 1) * size);
  for (let y = 0; y < size; y += 1) {
    raw[y * (size + 1)] = 0;              // filter byte: none
    for (let x = 0; x < size; x += 1) raw[y * (size + 1) + 1 + x] = pixels[y * size + x];
  }
  const idat = zlib.deflateSync(raw, { level: 9 });
  return Buffer.concat([
    sig,
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', idat),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

// ── the generator ────────────────────────────────────────────────────────────
/**
 * @param {object} [opts]
 * @param {number} [opts.payloadBytes=1185] size of the dummy payload → density/version
 * @param {'L'|'M'|'Q'|'H'} [opts.ecLevel='L'] error-correction level (L = densest)
 * @param {number} [opts.corruptPct=40] % of non-finder modules to flip (keep well above ~7)
 * @param {number} [opts.box=5]    pixels per module
 * @param {number} [opts.margin=4] quiet-zone width in modules
 * @returns {Buffer} PNG bytes
 */
function makeUnscannableQr(opts = {}) {
  const {
    payloadBytes = 1185,
    ecLevel = 'L',
    corruptPct = 40,
    box = 5,
    margin = 4,
  } = opts;
  const DARK = 0;
  const LIGHT = 255;

  // 1. real, dense QR grid from a deterministic ~1 KB payload
  const dummy = Buffer.alloc(payloadBytes);
  for (let i = 0; i < dummy.length; i += 1) dummy[i] = (i * 31 + 7) & 0xff;
  const qr = QRCode.create([{ data: dummy, mode: 'byte' }], { errorCorrectionLevel: ecLevel });
  const size = qr.modules.size;
  const md = qr.modules;

  // 2. flip ~corruptPct% of data modules; keep the three finder corners intact
  const inFinder = (r, c) =>
    (r < 8 && c < 8) || (r < 8 && c >= size - 8) || (r >= size - 8 && c < 8);
  for (let r = 0; r < size; r += 1) {
    for (let c = 0; c < size; c += 1) {
      if (inFinder(r, c)) continue;
      if ((((r * 928371 + c * 123457) >>> 0) % 100) < corruptPct) {
        const i = r * size + c;
        md.data[i] = md.data[i] ? 0 : 1;
      }
    }
  }

  // 3. rasterise the corrupted matrix to a grayscale PNG
  const dim = (size + margin * 2) * box;
  const px = new Uint8Array(dim * dim).fill(LIGHT);
  for (let r = 0; r < size; r += 1) {
    for (let c = 0; c < size; c += 1) {
      if (!md.data[r * size + c]) continue;
      const y0 = (r + margin) * box;
      const x0 = (c + margin) * box;
      for (let dy = 0; dy < box; dy += 1) {
        const row = (y0 + dy) * dim;
        for (let dx = 0; dx < box; dx += 1) px[row + x0 + dx] = DARK;
      }
    }
  }
  return grayPng(dim, px);
}

module.exports = { makeUnscannableQr };

// CLI: `node unscannable-qr.standalone.js out.png`
if (require.main === module) {
  const fs = require('fs');
  const out = process.argv[2] || 'unscannable.png';
  fs.writeFileSync(out, makeUnscannableQr());
  process.stdout.write(`wrote ${out}\n`);
}
