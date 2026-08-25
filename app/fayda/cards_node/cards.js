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
  // Legacy first — it is the cheaper decoder and still the common case. The
  // National ID app now also issues a COSE_Sign1 QR that jsQR cannot read at all,
  // so fall through to the zxing/CBOR path rather than calling it unreadable.
  let legacyErr = null;
  try {
    const scan = await scanFaydaQr(image);
    return {
      ok: true,
      qrType: 'old',
      fan: scan.fan,
      fanValid: scan.fanValid,
      fullName: scan.fullName,
      birthDate: scan.birthDate,
      gender: scan.gender,
      version: scan.version,
      // Present and non-sample => the QR came off a real ID and will verify.
      signature: scan.signature ? scan.signature.slice(0, 24) : '',
      signed: Boolean(scan.signature) && !/INVALID_SIGNATURE_SAMPLE/.test(scan.signature),
      qr: scan.regeneratedQr,
    };
  } catch (e) {
    legacyErr = e;
  }
  const { scanNewFaydaQr } = require('./qrScanNew');
  try {
    const scan = await scanNewFaydaQr(image);
    return {
      ok: true,
      qrType: 'new',
      fan: scan.fan,
      fanValid: scan.fanValid,
      fullName: '',
      birthDate: '',
      gender: '',
      signed: true,
      via: scan.via,
      qr: scan.regeneratedQrBuffer.toString('base64'),
    };
  } catch (e) {
    // Report the legacy reason: for a photo that holds no QR at all it is the
    // more useful message, and the new-format attempt failed for the same cause.
    throw legacyErr || e;
  }
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
