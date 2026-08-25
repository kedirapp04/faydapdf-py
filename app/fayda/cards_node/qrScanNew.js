'use strict';

// ─── New-format Fayda QR (COSE_Sign1 / CWT) ───────────────────────────────
// The Node bot's scanner only understands the LEGACY QR — plain text with
// :DLT: and :SIGN: markers, decoded with jsQR. The National ID app now issues a
// different QR entirely, and it fails there twice over:
//
//   1. jsQR cannot decode it at all. It is a very dense (high-version) code and
//      jsQR's binarizer gives up on it at every scale. zxing reads it.
//   2. Even decoded, the payload is BINARY, not text — so the legacy parser
//      rejects it for missing its markers.
//
// Structure, confirmed byte-for-byte against a real ID:
//
//   d2                     CBOR tag 18  -> COSE_Sign1
//   84                     array(4): protected, unprotected, payload, signature
//   43 a1 01 26            protected = {1: -7}  -> ES256
//   payload = CBOR CWT claims:
//        2  -> subject, the 16-digit FAN     <- what we need
//        4  -> expiry
//        7  -> CWT id (uuid)
//      169  -> map of identity attributes
//
// We only read the FAN. The signature is left alone and never re-encoded from
// parsed parts: the QR image handed on is rebuilt from the ORIGINAL bytes, so it
// still verifies.

const { Jimp } = require('jimp');
const cbor = require('cbor');
const QRCode = require('qrcode');
const {
  MultiFormatReader, BarcodeFormat, RGBLuminanceSource, BinaryBitmap,
  HybridBinarizer, DecodeHintType, ResultMetadataType,
} = require('@zxing/library');

const COSE_SIGN1_TAG = 18;
const CLAIM_SUBJECT = 2;

function luminance(img) {
  const { data, width, height } = img.bitmap;
  const lum = new Uint8ClampedArray(width * height);
  for (let i = 0, j = 0; i < data.length; i += 4, j++) {
    lum[j] = (data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114) | 0;
  }
  return new RGBLuminanceSource(lum, width, height);
}

function readerHints() {
  const hints = new Map();
  hints.set(DecodeHintType.POSSIBLE_FORMATS, [BarcodeFormat.QR_CODE]);
  hints.set(DecodeHintType.TRY_HARDER, true);
  return hints;
}

// Returns the raw BYTE payload (not getText(), which mangles binary through a
// charset conversion), or null.
function decodeBytes(img) {
  const reader = new MultiFormatReader();
  reader.setHints(readerHints());
  const result = reader.decode(new BinaryBitmap(new HybridBinarizer(luminance(img))));
  const segments = result.getResultMetadata().get(ResultMetadataType.BYTE_SEGMENTS);
  if (segments && segments.length) {
    return Buffer.concat(segments.map((s) => Buffer.from(s)));
  }
  return Buffer.from(result.getText(), 'latin1');
}

// Native resolution often fails while 2x succeeds — the dense modules need more
// pixels for the binarizer to separate them. Try a spread, cheapest first.
async function scanBytes(buffer) {
  const base = await Jimp.read(buffer);
  const W = base.bitmap.width;
  const H = base.bitmap.height;
  const attempts = [
    ['x2', (b) => b.resize({ w: W * 2 })],
    ['native', (b) => b],
    ['x3', (b) => b.resize({ w: W * 3 })],
    ['x2 grey', (b) => b.greyscale().resize({ w: W * 2 })],
    // The National ID screen puts the QR in the middle band, under the photo.
    ['band x2', (b) => b.crop({ x: 0, y: Math.round(H * 0.30), w: W, h: Math.round(H * 0.55) })
      .resize({ w: W * 2 })],
  ];
  for (const [name, prep] of attempts) {
    try {
      const bytes = decodeBytes(prep(base.clone()));
      if (bytes && bytes.length) return { bytes, via: name };
    } catch (_) { /* this variant didn't decode — try the next */ }
  }
  return null;
}

function isCoseSign1(buf) {
  return Boolean(buf) && buf.length > 4 && buf[0] === 0xd2 && buf[1] === 0x84;
}

// COSE_Sign1 -> the 16-digit FAN from CWT claim 2.
function fanFromCose(buf) {
  const decoded = cbor.decodeFirstSync(buf);
  const arr = decoded && decoded.value ? decoded.value : decoded;
  if (!Array.isArray(arr) || arr.length !== 4) throw new Error('not a COSE_Sign1 array');
  const claims = cbor.decodeFirstSync(arr[2]);
  const sub = claims instanceof Map ? claims.get(CLAIM_SUBJECT) : claims[CLAIM_SUBJECT];
  const fan = String(sub == null ? '' : sub).trim();
  if (!/^\d{16}$/.test(fan)) throw new Error('no 16-digit subject in the CWT claims');
  return fan;
}

// Rebuild the QR from the ORIGINAL bytes, so the signature survives untouched and
// the card's QR still verifies. Byte mode — never re-encode as text.
async function requantiseQr(bytes) {
  return QRCode.toBuffer([{ data: bytes, mode: 'byte' }], {
    errorCorrectionLevel: 'L',
    type: 'png',
    margin: 2,
    scale: 4,
  });
}

// Scan a new-format Fayda QR. Throws if the image holds no QR, or one that isn't
// this format — the caller falls back to the legacy scanner.
async function scanNewFaydaQr(image) {
  const hit = await scanBytes(image);
  if (!hit) throw new Error('No QR code could be decoded from the image.');
  if (!isCoseSign1(hit.bytes)) throw new Error('QR decoded but is not a COSE_Sign1 Fayda QR.');
  const fan = fanFromCose(hit.bytes);
  return {
    qrType: 'new',
    fan,
    fanValid: true,
    fullName: '',            // the new QR carries attributes by numeric id only;
    birthDate: '',           // the identity call supplies these anyway
    gender: '',
    signed: true,            // COSE_Sign1 is signed by construction
    via: hit.via,
    regeneratedQrBuffer: await requantiseQr(hit.bytes),
  };
}

module.exports = { scanNewFaydaQr, scanBytes, isCoseSign1, fanFromCose };
