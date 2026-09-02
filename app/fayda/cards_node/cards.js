'use strict';

// ─── Server-5 QR + card bridge ────────────────────────────────────────────
// Reuses the Node bot's own modules so the output matches it exactly — same
// scanner tolerances, same card templates, same fonts.
//
// Two jobs, chosen by the "op" field on stdin:
//
//   op:"scan"   Read the user's Telebirr Fayda (National ID) screenshot, pull out the QR.
//               Returns the FAN plus the QR REGENERATED FROM THE SCANNED TEXT —
//               byte-identical payload, so it keeps the real signature and still
//               verifies. This is the whole point of scanning rather than
//               generating: a built QR carries a sample signature that no
//               verifier will ever accept.
//
//   op:"cards"  Draw the front/back cards. A scanned QR (`qrPngB64`) is used
//               as-is and keeps its real signature. Without one, the QR is built
//               from the identity data — that path is only reachable when an
//               admin enables typed-FAN for the bot, and its signature is a
//               sample value, so the card scans but will NOT verify.
//
// Protocol: one JSON object in, one JSON object out; images base64. stderr is
// diagnostics only. Individual failures come back as null rather than killing
// the call — a missing back card must never cost the user their PDF.

const { buildLegacyQr, makeQrThumbWebp } = require('./faydaQrBuilder');
const { generateCards } = require('./faydaCardGenerator');
const { scanFaydaQr } = require('./faydaQrScanner');

function readStdin() {
  return new Promise((resolve, reject) => {
    let buf = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => { buf += c; });
    process.stdin.on('end', () => resolve(buf));
    process.stdin.on('error', reject);
  });
}

const b64 = (v) => (Buffer.isBuffer(v) ? v.toString('base64') : (v || null));

async function doScan(input) {
  const image = Buffer.from(input.image || '', 'base64');
  const { scanBytes, isCoseSign1, fanFromCose } = require('./qrScanNew');
  const { decodeLegacyQr, isLegacyQrText, regenerateQrPng } = require('./faydaQrScanner');

  // ONE decode pass (zxing) then branch on the payload. zxing reads BOTH the dense
  // new-format COSE QR AND the legacy text QR, so this replaces the old two-scanner
  // sequence (jsQR then zxing) that was slow enough to risk the caller's timeout.
  let hit = null;
  try {
    hit = await scanBytes(image);
  } catch (_) { /* nothing decoded — handled below */ }

  if (hit && hit.bytes && hit.bytes.length) {
    // New format: COSE_Sign1 CWT. The 16-digit FAN is CWT claim 2; the QR is
    // rebuilt from the ORIGINAL bytes so its Fayda signature survives.
    if (isCoseSign1(hit.bytes)) {
      try {
        const fan = fanFromCose(hit.bytes);
        const QRCode = require('qrcode');
        const png = await QRCode.toBuffer([{ data: hit.bytes, mode: 'byte' }],
          { errorCorrectionLevel: 'L', type: 'png', margin: 2, scale: 4 });
        return { ok: true, qrType: 'new', fan, fanValid: true, signed: true,
                 via: hit.via, qr: png.toString('base64') };
      } catch (_) { /* not a valid CWT after all — try legacy text below */ }
    }
    // Legacy format: colon-separated text with :DLT: / :SIGN:.
    const text = hit.bytes.toString('latin1');
    if (isLegacyQrText(text)) {
      const dec = decodeLegacyQr(text);
      const png = await regenerateQrPng(dec.rawText);
      return { ok: true, qrType: 'old', fan: dec.fan, fanValid: dec.fanValid,
               fullName: dec.fullName, birthDate: dec.birthDate, gender: dec.gender,
               version: dec.version,
               signature: dec.signature ? dec.signature.slice(0, 24) : '',
               signed: Boolean(dec.signature) && !/INVALID_SIGNATURE_SAMPLE/.test(dec.signature),
               qr: png.toString('base64') };
    }
  }

  // Nothing usable. zxing already covers both formats (it decodes the legacy text
  // QR too, handled above), so there is no jsQR fallback — it only added ~12 slow
  // attempts to the failure path, which matters when a plain receipt photo reaches
  // here in QR mode: it now fails in a few seconds, not ~50.
  return { ok: false, error: 'No QR code could be decoded from the image.' };
}

async function doCards(data) {
  let qrText = data.qrText || null;
  let qrPng = data.qrPngB64 ? Buffer.from(data.qrPngB64, 'base64') : null;

  // No scanned QR — build one (typed-FAN path). Three admin-selectable kinds:
  //   qrGen = "data"  (default): a legacy QR with the person's data AND a sample
  //     signature — it scans, shows data, but a verifier rejects the signature.
  //   qrGen = "nosig": the same legacy QR with the person's data but an EMPTY
  //     signature (:SIGN: with nothing after it) — scans and shows data, no signature.
  //   qrGen = "blank": a QR that decodes to NOTHING and carries no signature.
  if (!qrPng) {
    const qrGen = (data.qrGen || 'data');
    const QRCode = require('qrcode');
    const opts = { errorCorrectionLevel: 'L', type: 'png', margin: 2, scale: 4 };
    try {
      if (qrGen === 'unscannable') {
        // A REAL QR at the SAME density as the actual new-format Fayda QR — a
        // ~1185-byte byte-mode payload at EC level L gives the same high version
        // (dense, ~version 27), with genuine finder patterns, timing and quiet zone.
        // Then ~40% of the DATA modules are flipped, far beyond error correction, so
        // no reader can decode it; the finder patterns are kept so it reads visually
        // as an authentic Fayda QR.
        const { createCanvas } = require('@napi-rs/canvas');
        const dummy = Buffer.alloc(1185);
        for (let i = 0; i < dummy.length; i += 1) dummy[i] = (i * 31 + 7) & 0xff;
        const qr = QRCode.create([{ data: dummy, mode: 'byte' }], { errorCorrectionLevel: 'L' });
        const size = qr.modules.size;
        const md = qr.modules;
        const inFinder = (r, c) =>
          (r < 8 && c < 8) || (r < 8 && c >= size - 8) || (r >= size - 8 && c < 8);
        for (let r = 0; r < size; r += 1) {
          for (let c = 0; c < size; c += 1) {
            if (inFinder(r, c)) continue;              // keep finder patterns intact
            if ((((r * 928371 + c * 123457) >>> 0) % 100) < 40) {
              const i = r * size + c;
              md.data[i] = md.data[i] ? 0 : 1;         // flip → breaks decoding
            }
          }
        }
        const margin = 4, box = 5, dim = (size + margin * 2) * box;
        const cv = createCanvas(dim, dim);
        const cx = cv.getContext('2d');
        cx.fillStyle = '#fff'; cx.fillRect(0, 0, dim, dim);
        cx.fillStyle = '#000';
        for (let r = 0; r < size; r += 1) {
          for (let c = 0; c < size; c += 1) {
            if (md.data[r * size + c]) cx.fillRect((c + margin) * box, (r + margin) * box, box, box);
          }
        }
        qrText = null;
        qrPng = await cv.encode('png');
      } else if (qrGen === 'blank') {
        // A legacy-format QR (has the :DLT: and :SIGN: markers the Fayda app looks
        // for) but with EMPTY data and no signature. The app recognises it as a
        // legacy QR and shows a blank result — no "not a COSE security Message"
        // error, because it is never treated as a new COSE credential.
        qrText = ':DLT::V:4:G::A::D::SIGN:';
        qrPng = await QRCode.toBuffer(qrText, opts);
      } else {
        const face = data.photo ? await makeQrThumbWebp(data.photo) : null;
        const built = await buildLegacyQr({
          face,
          fullName: data.fullName_eng || data.fullName || '',
          gender: data.sex_eng || data.gender || '',
          fan: data.fan || '',
          dob: data.dobGc || '',
        });
        qrText = built.qrText;
        qrPng = built.qrPngBuffer;
        if (qrGen === 'nosig') {
          // Keep the data with an EMPTY signature: ":SIGN:" stays but nothing follows.
          // The ":SIGN:" marker is what the Fayda app uses to recognise a legacy DATA
          // QR — remove it entirely and the app instead tries to parse the text as a
          // new COSE QR and errors ("too many bytes … CBOR") instead of showing data.
          const i = qrText.lastIndexOf(':SIGN:');
          if (i >= 0) qrText = qrText.slice(0, i + ':SIGN:'.length);
          qrPng = await QRCode.toBuffer(qrText, opts);
        }
      }
    } catch (e) {
      process.stderr.write(`qr build failed: ${e && e.message}\n`);
    }
  }

  let front = null, back = null;
  try {
    const cards = await generateCards({ ...data, qr: qrPng });
    front = cards.front;
    back = cards.back;
  } catch (e) {
    process.stderr.write(`cards failed: ${e && e.message}\n`);
  }

  return {
    ok: Boolean(qrPng || front || back),
    qrText,
    qrFromScan: Boolean(data.qrPngB64),
    qr: b64(qrPng),
    front: b64(front),
    back: b64(back),
  };
}

async function main() {
  const raw = await readStdin();
  if (!raw.trim()) throw new Error('no input on stdin');
  const input = JSON.parse(raw);
  const out = (input.op === 'scan') ? await doScan(input) : await doCards(input);
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  // A failed scan is an ORDINARY outcome — most photos are payment receipts, not ID
  // QRs. Report it as data and stay silent on stderr, otherwise every receipt a user
  // sends writes a stack trace to the logs and buries the real errors.
  process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
});
