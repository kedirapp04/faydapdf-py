'use strict';

// ─── Fayda QR scanner component (self-contained, no external service) ────
// Give it a National ID app screenshot; it returns the decoded fields plus
// a regenerated, pixel-perfect QR code. Everything runs in-process:
//
//   * jimp   — load + rescale the screenshot         (already a bot dep)
//   * jsqr   — locate & decode the QR from the image
//   * qrcode — rebuild a pixel-perfect QR            (already a bot dep)
//
// Only the legacy ("old") Fayda QR is supported. A new-format QR throws.
//
// Usage:
//   const { scanFaydaQr } = require('./faydaQrScanner');
//
//   // image may be a Buffer, a file path, or a base64 / data-URI string
//   const result = await scanFaydaQr(imageBuffer);
//   //  → {
//   //      fan, fanValid, fullName, birthDate, gender, version, signature,
//   //      qrType,
//   //      regeneratedQr        (base64 PNG string),
//   //      regeneratedQrBuffer  (Buffer, ready for bot.sendPhoto)
//   //    }
//
//   await bot.sendPhoto(chatId, result.regeneratedQrBuffer);

const fs = require('fs');
const { Jimp } = require('jimp');
const jsQR = require('jsqr');
const QRCode = require('qrcode');

const SIGN_MARKER = ':SIGN:';
const DLT_MARKER = ':DLT:';

// ─── image input ────────────────────────────────────────────────────────
// Accept a Buffer, a file path, or a base64 / data-URI string.
function resolveImageBuffer(image) {
  if (Buffer.isBuffer(image)) return image;

  if (typeof image === 'string') {
    if (/^data:image\//i.test(image)) {
      return Buffer.from(
        image.replace(/^data:image\/[^;]+;base64,/i, ''),
        'base64'
      );
    }
    try {
      if (fs.existsSync(image)) return fs.readFileSync(image);
    } catch (_) {
      // fall through — treat as raw base64
    }
    return Buffer.from(image, 'base64');
  }

  throw new Error(
    'Unsupported image input — pass a Buffer, a file path, or a base64 string.'
  );
}

// ─── QR location + decode ───────────────────────────────────────────────
function _jsqrDecode(img) {
  const { data, width, height } = img.bitmap;
  const rgba = new Uint8ClampedArray(data.buffer, data.byteOffset, data.byteLength);
  return jsQR(rgba, width, height, { inversionAttempts: 'attemptBoth' });
}

// jsQR is sensitive to the QR's pixels-per-module — the dense legacy Fayda
// QR fails at some resolutions and decodes at others, non-monotonically.
// So we try a spread of whole-image scales, then a device-agnostic
// center-band crop as a fallback. First successful decode wins.
async function _scanQrText(buffer) {
  const base = await Jimp.read(buffer);
  const origW = base.bitmap.width;
  const origH = base.bitmap.height;

  const widths = [1500, 1200, 2000, origW, 900, 2400];
  const seen = new Set();

  for (const w of widths) {
    if (!w || w < 300 || seen.has(w)) continue;
    seen.add(w);
    const img = w === origW ? base.clone() : base.clone().resize({ w });

    let hit = _jsqrDecode(img);
    if (hit && hit.data) return hit.data;

    hit = _jsqrDecode(img.greyscale());
    if (hit && hit.data) return hit.data;
  }

  // Fallback — crop the central band where the National ID app always
  // renders the QR (below the name, above the buttons), then upscale.
  for (const w of [1800, 2400]) {
    const crop = base.clone().crop({
      x: Math.round(origW * 0.02),
      y: Math.round(origH * 0.18),
      w: Math.round(origW * 0.96),
      h: Math.round(origH * 0.64),
    });
    const hit = _jsqrDecode(crop.resize({ w }));
    if (hit && hit.data) return hit.data;
  }

  throw new Error('No QR code could be decoded from the image.');
}

// ─── legacy Fayda QR payload decode ─────────────────────────────────────
// The OLD Fayda QR is plain text:
//   <faceBase64>:DLT:<fullName>:V:<ver>:G:<gender>:A:<FAN>:D:<dob>:SIGN:<sig>
// Field keys:  A = FAN (16-digit FCN), D = DOB, G = gender, V = version.
function isLegacyQrText(text) {
  return text.includes(DLT_MARKER) && text.includes(SIGN_MARKER);
}

function parseDltPayload(dlt) {
  const tokens = dlt.split(':');
  const fullName = (tokens[0] || '').trim();
  const fields = {};
  let i = 1;
  while (i < tokens.length) {
    const token = tokens[i];
    if (/^[A-Z]{1,5}$/.test(token) && i + 1 < tokens.length) {
      fields[token] = tokens[i + 1];
      i += 2;
      continue;
    }
    i += 1;
  }
  return { fullName, fields };
}

function _compactDateToIso(value) {
  if (/^\d{8}$/.test(value)) {
    return `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
  }
  return value;
}

function _slashDateToIso(value) {
  const parts = value.split('/');
  if (parts.length === 3 && parts.every((p) => /^\d+$/.test(p)) && parts[0].length === 4) {
    return `${parts[0]}-${parts[1].padStart(2, '0')}-${parts[2].padStart(2, '0')}`;
  }
  return value;
}

function normalizeBirthDate(value) {
  if (!value) return '';
  return _slashDateToIso(_compactDateToIso(String(value).trim()));
}

function normalizeGender(value) {
  const token = String(value || '').trim().toUpperCase();
  if (token === 'M' || token === 'MALE') return 'Male';
  if (token === 'F' || token === 'FEMALE') return 'Female';
  return value ? String(value).trim() : '';
}

function decodeLegacyQr(text) {
  if (!text || !isLegacyQrText(text)) {
    throw new Error(
      "This QR is not a legacy ('old') Fayda QR. " +
      'Only old-format Fayda QR codes are supported.'
    );
  }

  const signIndex = text.lastIndexOf(SIGN_MARKER);
  const preSign = text.slice(0, signIndex);
  const signature = text.slice(signIndex + SIGN_MARKER.length).trim();

  // Everything before :DLT: is the embedded face thumbnail — ignored.
  const dltIndex = preSign.indexOf(DLT_MARKER);
  const dltPayload = dltIndex >= 0
    ? preSign.slice(dltIndex + DLT_MARKER.length)
    : preSign;

  const { fullName, fields } = parseDltPayload(dltPayload);
  const fan = String(fields.A || '').trim();

  return {
    qrType: 'old',
    fan,
    fanValid: /^\d{16}$/.test(fan),
    fullName,
    birthDate: normalizeBirthDate(fields.D || ''),
    gender: normalizeGender(fields.G || ''),
    version: fields.V || '',
    fields,
    signature,
    rawText: text,
  };
}

// ─── QR regeneration ────────────────────────────────────────────────────
// Rebuild a pixel-perfect QR from the decoded payload text. The output
// decodes back to exactly the same string.
async function regenerateQrPng(rawText) {
  if (!rawText) throw new Error('Cannot regenerate a QR from empty payload text.');
  return QRCode.toBuffer(rawText, {
    errorCorrectionLevel: 'L',
    type: 'png',
    margin: 2,                       // legacy Fayda uses a 2-module quiet zone
    scale: 4,                        // pixels per module — matches reference PDF output (~661×661)
  });
}

// ─── public entry point ─────────────────────────────────────────────────
// Scan a National ID screenshot → decoded fields + regenerated QR.
//
//   image   Buffer | file path | base64 / data-URI string
async function scanFaydaQr(image) {
  const buffer = resolveImageBuffer(image);
  if (!buffer || !buffer.length) {
    throw new Error('Empty image — nothing to scan.');
  }

  const text = await _scanQrText(buffer);
  const decoded = decodeLegacyQr(text);
  const regeneratedQrBuffer = await regenerateQrPng(decoded.rawText);

  return {
    qrType: decoded.qrType,
    fan: decoded.fan,
    fanValid: decoded.fanValid,
    fullName: decoded.fullName,
    birthDate: decoded.birthDate,
    gender: decoded.gender,
    version: decoded.version,
    signature: decoded.signature,
    regeneratedQr: regeneratedQrBuffer.toString('base64'),  // base64 PNG
    regeneratedQrBuffer,                                     // Buffer (PNG)
  };
}

module.exports = {
  scanFaydaQr,
  decodeLegacyQr,
  regenerateQrPng,
  resolveImageBuffer,
};
