'use strict';

// ─── Legacy Fayda QR builder ──────────────────────────────────────────────
// Builds a "legacy" Fayda QR code (string + PNG) used by the typed-FAN
// flow — when the user types a 16-digit FAN instead of sending a Telebirr
// QR screenshot. The resulting QR scans cleanly and carries the user's
// verified data (name, gender, FAN, DOB) but its SIGN is a fixed sample
// value that will never validate, so a verifier app correctly rejects it.
// The user is shown an "unverified" notice on the PDF caption.
//
// Legacy format (single colon-separated string):
//   <face-webp-base64>:DLT:<fullName>:V:<ver>:G:<M|F>:A:<fan>:D:<dob>:SIGN:<jwt>

const fs = require('fs');
const path = require('path');
const QRCode = require('qrcode');
const { createCanvas, loadImage } = require('@napi-rs/canvas');

const SAMPLE_FACE_PATH = path.join(__dirname, 'res', 'raw', 'sample_face.webp');

// Loaded once at module load. URL-safe base64, no padding — matches the
// encoding the original legacy QR uses for the face thumbnail.
const SAMPLE_FACE_B64 = (() => {
  try {
    return fs.readFileSync(SAMPLE_FACE_PATH).toString('base64url').replace(/=+$/, '');
  } catch (_) {
    return '';
  }
})();

// Constant placeholder JWT-shaped signature. The header is a real RS256
// header so the JWT structure parses, the payload is empty (".." middle),
// and the "signature" is a fixed marker. Verification fails by design.
const SAMPLE_SIGN =
  'eyJhbGciOiJSUzI1NiJ9..' +
  'AAAA_INVALID_SIGNATURE_SAMPLE_ONLY_DO_NOT_TRUST_PDF_WITHOUT_TELEBIRR_QR_VERIFICATION_AAAA';

const VERSION = '4';

// 78 × 100 — the embedded-face dimensions the original legacy QR uses.
// Encoding at Q40 keeps each thumbnail near ~500–700 bytes, matching the
// weight of a real legacy QR's face segment.
const FACE_W = 78;
const FACE_H = 100;
const FACE_WEBP_QUALITY = 40;

function genderLetter(input) {
  const s = String(input || '').trim().toLowerCase();
  if (s.startsWith('f')) return 'F';
  if (s.startsWith('m')) return 'M';
  // Amharic fallback
  if (/^ሴት|^ሴ/.test(s)) return 'F';
  if (/^ወንድ|^ወ/.test(s)) return 'M';
  return 'M';
}

// Downsample a JPEG/PNG photo buffer to 78×100 WebP @ Q40, matching the
// legacy QR's embedded face dimensions and weight. Returns URL-safe base64
// without padding — the same encoding the legacy QR carries. Falls back
// to SAMPLE_FACE_B64 if the input is missing or canvas fails.
async function makeQrThumbWebp(imageBufferOrBase64) {
  try {
    const buf = Buffer.isBuffer(imageBufferOrBase64)
      ? imageBufferOrBase64
      : (typeof imageBufferOrBase64 === 'string' && imageBufferOrBase64
        ? Buffer.from(imageBufferOrBase64.replace(/^data:[^;]+;base64,/i, ''), 'base64')
        : null);
    if (!buf || !buf.length) return SAMPLE_FACE_B64;
    const img = await loadImage(buf);
    const c = createCanvas(FACE_W, FACE_H);
    c.getContext('2d').drawImage(img, 0, 0, FACE_W, FACE_H);
    const webp = await c.encode('webp', FACE_WEBP_QUALITY);
    return webp.toString('base64url').replace(/=+$/, '');
  } catch (_) {
    return SAMPLE_FACE_B64;
  }
}

// Build the colon-separated legacy QR text from its parts.
function buildLegacyQrString({ face, fullName, gender, fan, dob, sign }) {
  const faceB64 = face || SAMPLE_FACE_B64;
  const signStr = sign || SAMPLE_SIGN;
  // The original samples carry a trailing space on the name field after
  // some upstream concatenation; preserve whatever is passed in verbatim.
  return `${faceB64}:DLT:${fullName || ''}:V:${VERSION}:G:${genderLetter(gender)}:A:${fan || ''}:D:${dob || ''}:SIGN:${signStr}`;
}

// Render the legacy QR string to a PNG. Returns
//   { qrText, qrPngBuffer, qrPngBase64 }
// matching the shape of faydaQrScanner's scan output so the downstream
// PDF + card pipeline can consume either source identically.
async function buildLegacyQr(parts) {
  const qrText = buildLegacyQrString(parts || {});
  const qrPngBuffer = await QRCode.toBuffer(qrText, {
    errorCorrectionLevel: 'L',
    margin: 2,
    scale: 4,
  });
  return {
    qrText,
    qrPngBuffer,
    qrPngBase64: qrPngBuffer.toString('base64'),
  };
}

module.exports = {
  SAMPLE_FACE_B64,
  SAMPLE_SIGN,
  VERSION,
  FACE_W,
  FACE_H,
  genderLetter,
  makeQrThumbWebp,
  buildLegacyQrString,
  buildLegacyQr,
};
