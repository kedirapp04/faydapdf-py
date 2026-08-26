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

  // No scanned QR — build one from the identity data (typed-FAN path, which an
  // admin has to enable per bot). It renders and scans, but its signature is a
  // fixed sample, so a verifier app rejects it. The caller is told which kind it
  // got via `qrFromScan`, and the user is warned before the download starts.
  if (!qrPng) {
    try {
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
