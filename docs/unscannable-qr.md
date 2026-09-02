# Unscannable QR — how it's generated

The **Unscannable** option (admin setting `s5_qr_gen = "unscannable"`) produces a QR
that **looks like a genuine, dense Fayda ID QR but cannot be decoded by any reader**.
It is one of the four generated-QR kinds used on the typed-FAN path (when there is no
real scanned Telebirr QR to embed).

Contrast with the other three:

| kind | scans? | shows |
|---|---|---|
| `data` | yes | identity data + a **sample** signature (fails verification) |
| `nosig` | yes | identity data, **no** signature |
| `blank` | yes | a legacy QR with empty fields → blank result, no error |
| **`unscannable`** | **no** | nothing — no reader can decode it |

Code: [app/fayda/cards_node/cards.js](../app/fayda/cards_node/cards.js), the
`qrGen === 'unscannable'` branch in `doCards()`.

---

## Use it in another project (self-contained)

You don't need any file from this repo. Copy the single function below into any Node
project — its **only** dependency is the `qrcode` npm package (`npm i qrcode`). It writes
the PNG itself with Node's built-in `zlib`, so there's **no `@napi-rs/canvas`** and no
other project file to carry along.

> A ready-to-drop copy also lives at
> [unscannable-qr.standalone.js](./unscannable-qr.standalone.js) — same code, with a tiny
> CLI (`node unscannable-qr.standalone.js out.png`).

```js
'use strict';
const zlib = require('zlib');
const QRCode = require('qrcode');            // npm i qrcode   ← the only dependency

// ── minimal 8-bit grayscale PNG writer (no canvas) ──────────────────────────
const CRC = (() => { const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0; } return t; })();
function crc32(b){ let c = 0xffffffff;
  for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0; }
function chunk(type, data){ const len = Buffer.alloc(4); len.writeUInt32BE(data.length, 0);
  const body = Buffer.concat([Buffer.from(type,'latin1'), data]);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body), 0);
  return Buffer.concat([len, body, crc]); }
function grayPng(size, px){                    // px: Uint8Array(size*size) of 0|255
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size,0); ihdr.writeUInt32BE(size,4);
  ihdr[8] = 8; ihdr[9] = 0;                     // 8-bit, grayscale
  const raw = Buffer.alloc((size+1)*size);
  for (let y = 0; y < size; y++){ raw[y*(size+1)] = 0;
    for (let x = 0; x < size; x++) raw[y*(size+1)+1+x] = px[y*size+x]; }
  return Buffer.concat([ Buffer.from([137,80,78,71,13,10,26,10]),
    chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw,{level:9})),
    chunk('IEND', Buffer.alloc(0)) ]); }

// ── the generator ───────────────────────────────────────────────────────────
function makeUnscannableQr(opts = {}){
  const { payloadBytes = 1185, ecLevel = 'L', corruptPct = 40, box = 5, margin = 4 } = opts;

  // 1. real, dense QR grid from a deterministic ~1 KB payload
  const dummy = Buffer.alloc(payloadBytes);
  for (let i = 0; i < dummy.length; i++) dummy[i] = (i*31 + 7) & 0xff;
  const qr = QRCode.create([{ data: dummy, mode: 'byte' }], { errorCorrectionLevel: ecLevel });
  const size = qr.modules.size, md = qr.modules;

  // 2. flip ~corruptPct% of data modules; KEEP the three finder corners intact
  const inFinder = (r,c) => (r<8&&c<8) || (r<8&&c>=size-8) || (r>=size-8&&c<8);
  for (let r = 0; r < size; r++) for (let c = 0; c < size; c++){
    if (inFinder(r,c)) continue;
    if ((((r*928371 + c*123457) >>> 0) % 100) < corruptPct){
      const i = r*size + c; md.data[i] = md.data[i] ? 0 : 1; } }

  // 3. rasterise to grayscale PNG
  const dim = (size + margin*2) * box;
  const px = new Uint8Array(dim*dim).fill(255);
  for (let r = 0; r < size; r++) for (let c = 0; c < size; c++){
    if (!md.data[r*size + c]) continue;
    const y0 = (r+margin)*box, x0 = (c+margin)*box;
    for (let dy = 0; dy < box; dy++){ const row = (y0+dy)*dim;
      for (let dx = 0; dx < box; dx++) px[row + x0 + dx] = 0; } }
  return grayPng(dim, px);
}

module.exports = { makeUnscannableQr };
```

**Usage:**

```js
const { makeUnscannableQr } = require('./unscannable-qr.standalone');
const png = makeUnscannableQr();                       // Buffer of PNG bytes (625×625)
require('fs').writeFileSync('fake.png', png);

// tune density / corruption if you like:
makeUnscannableQr({ payloadBytes: 1500, corruptPct: 45, box: 6, margin: 4 });
```

Defaults reproduce exactly what the bot embeds: **version 25, 117×117 modules, 625×625 px,
~40 % of data modules flipped**. Output is deterministic (no randomness) and contains no
real data.

**Not on Node?** The same three steps port directly — any QR library that exposes the raw
module matrix works:
- **Python:** `qrcode` (matrix via `qr.get_matrix()`, use `border=0`) or `segno`, then flip
  the same modules and save with Pillow / a PNG writer.
- **Go / Rust / etc.:** any library that gives you the boolean module grid before rendering.

The only requirements are: (1) a real payload so the grid is authentically dense, (2) skip
the three 8×8 finder corners, (3) flip well above the EC level's repair limit (~7 % for L).

---

## The idea

A real Fayda new-format QR is a **dense, high-version** QR (a ~1 KB COSE credential).
To look authentic, our fake has to be the same density — random black-and-white noise
of the wrong size reads as an obvious fake ("multiple squared boxes"). So instead of
inventing a pattern, we:

1. Encode a **real ~1 KB payload** so the QR library lays out an authentic, high-version
   grid (correct size, finder patterns, timing lines, quiet zone).
2. **Corrupt ~40 % of the data modules** — far beyond what error correction can repair —
   so no scanner can ever decode it.
3. **Keep the three finder patterns intact** so the human eye (and a scanner's *locator*)
   still reads it as "a QR code," even though the payload is unrecoverable.

The result is visually indistinguishable from a real dense Fayda QR but is undecodable.

---

## Step by step (with the actual numbers)

### 1. Build a dense, real QR grid

```js
const dummy = Buffer.alloc(1185);
for (let i = 0; i < dummy.length; i += 1) dummy[i] = (i * 31 + 7) & 0xff;
const qr = QRCode.create([{ data: dummy, mode: 'byte' }], { errorCorrectionLevel: 'L' });
```

- **1185-byte** byte-mode payload, filled with a deterministic ramp `(i*31 + 7) & 0xff`
  (no randomness — the same every run, which is required: `Math.random` isn't used).
- **Error-correction level L** — the lowest, matching the real Fayda QR, which maximises
  data density for a given size.
- This yields **QR version 25 → a 117 × 117 module grid (13 689 modules)** — the same
  density class as a genuine new-format Fayda credential QR.

### 2. Flip ~40 % of the data modules, keep the finders

```js
const inFinder = (r, c) =>
  (r < 8 && c < 8) || (r < 8 && c >= size - 8) || (r >= size - 8 && c < 8);

for (let r = 0; r < size; r += 1) {
  for (let c = 0; c < size; c += 1) {
    if (inFinder(r, c)) continue;                       // keep finder patterns intact
    if ((((r * 928371 + c * 123457) >>> 0) % 100) < 40) {
      const i = r * size + c;
      md.data[i] = md.data[i] ? 0 : 1;                  // flip → breaks decoding
    }
  }
}
```

- `inFinder` protects the three **8 × 8 finder-pattern corners** (top-left, top-right,
  bottom-left) — **192 modules kept**. These are what a scanner uses to *locate* a QR, so
  keeping them means the image still registers as a QR code visually.
- Every other module is flipped when a **position hash** `(r*928371 + c*123457) mod 100`
  is `< 40` — i.e. a deterministic ~40 % of the **13 497 non-finder modules → ~5 399
  modules flipped**.
- **Why it's undecodable:** EC level L recovers only about **7 %** corruption. Flipping
  ~40 % is roughly **5.7× past** what error correction can fix, so decoding fails with
  certainty — and deterministically, since the flip pattern is a fixed hash, not random.

> Note: the flip also hits format/timing/alignment modules (only the finders are
> excluded), which independently defeats decoding — belt and suspenders.

### 3. Render to PNG

```js
const margin = 4, box = 5, dim = (size + margin * 2) * box;   // (117 + 8) * 5 = 625
const cv = createCanvas(dim, dim);
const cx = cv.getContext('2d');
cx.fillStyle = '#fff'; cx.fillRect(0, 0, dim, dim);
cx.fillStyle = '#000';
for (let r = 0; r < size; r += 1) {
  for (let c = 0; c < size; c += 1) {
    if (md.data[r * size + c]) cx.fillRect((c + margin) * box, (r + margin) * box, box, box);
  }
}
qrText = null;                 // no decodable text — nothing to embed downstream
qrPng = await cv.encode('png');
```

- Drawn with **`@napi-rs/canvas`** (not `QRCode.toBuffer`, because we hand-render the
  *corrupted* module matrix, not a valid one).
- **box = 5 px** per module, **quiet zone = 4 modules**, giving a **625 × 625 px** PNG
  (`(117 + 4×2) × 5`).
- `qrText` is set to `null` — there is no meaningful text payload, so nothing tries to
  parse or re-embed it downstream.

---

## Properties

- **Deterministic** — identical bytes every time (fixed payload + hash-based flips), so
  it's reproducible and testable; contains no real identity data.
- **Undecodable by design** — zxing, zxing-cpp, pyzbar, phone camera apps, and the Fayda
  app all fail to read it. Because the finder patterns survive, a scanner will *find* the
  QR and then fail on the payload, rather than not seeing a QR at all.
- **Authentic density** — version 25 / 117×117 matches a real dense Fayda QR, so it
  passes a glance test where sparse or noise-only images do not.

## Tuning knobs

| what | where | effect |
|---|---|---|
| density / version | `Buffer.alloc(1185)` size | larger → higher version → denser grid |
| corruption level | `... % 100) < 40` | raise for more corruption; keep well above ~7 % (EC-L limit) |
| protected regions | `inFinder(...)` | widen to also protect timing/alignment if a reader ever partially locks on |
| pixel size | `box = 5` | px per module → final image size |
| quiet zone | `margin = 4` | white border in modules |
