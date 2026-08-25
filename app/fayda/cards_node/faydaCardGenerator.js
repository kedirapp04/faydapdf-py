'use strict';

// ─── Fayda front/back ID-card generator ──────────────────────────────────
// Renders the front and back of the Ethiopian Digital ID card by drawing a
// person's data onto prepared blank templates (res/cards/*.jpg). Used to
// supply the `fronts` / `backs` card images for the Server 2 flow, which
// does not return them.
//
// Output: 1968×3150 JPEG buffers — same dimensions as the real cards
// extracted from the official eFayda PDF.

const path = require('path');
const { createCanvas, loadImage, GlobalFonts } = require('@napi-rs/canvas');
const bwipjs = require('bwip-js');

// ─── Fonts ───────────────────────────────────────────────────────────────
const FONT_DIR = path.join(__dirname, 'res', 'raw');
const FONT_EN = 'FaydaCardEn';            // Barlow Semi Condensed SemiBold — values
const FONT_EN_LIGHT = 'FaydaCardEnLight'; // Barlow Semi Condensed Medium — barcode number, FIN
const FONT_AM = 'FaydaCardAm';            // Nyala — Amharic (regular weight only)

let _fontsReady = false;
function ensureFonts() {
  if (_fontsReady) return;
  // The real card's Latin values are semi-bold — use the SemiBold weight
  // directly. The barcode number and FIN are lighter, so they use the
  // Medium weight. Nyala has no bold weight, so Amharic gets a light
  // synthetic stroke (see drawAm).
  GlobalFonts.registerFromPath(
    path.join(FONT_DIR, 'assets_fonts_barlowsemicondensedsemibold.ttf'), FONT_EN);
  GlobalFonts.registerFromPath(
    path.join(FONT_DIR, 'assets_fonts_barlowsemicondensedmedium.ttf'), FONT_EN_LIGHT);
  GlobalFonts.registerFromPath(
    path.join(FONT_DIR, 'assets_fonts_nyala.ttf'), FONT_AM);
  _fontsReady = true;
}

// ─── Templates ───────────────────────────────────────────────────────────
const CARD_DIR = path.join(__dirname, 'res', 'cards');
const FRONT_TEMPLATE = path.join(CARD_DIR, 'front_template.jpg');
const BACK_TEMPLATE = path.join(CARD_DIR, 'back_template.jpg');

const VALUE_COLOR = '#161616';

// ─── Layout — coordinates in the 1968×3150 template space ────────────────
// y values are text BASELINES.
const FRONT = {
  photo:   { x: 525, y: 410, w: 850, h: 1095 }, // cover-fit, grayscale
  nameAmh: { x: 245, y: 1914, size: 82 },
  nameEng: { x: 245, y: 1994, size: 74 },
  dob:     { x: 245, y: 2192, size: 90 },
  sex:     { x: 245, y: 2340, size: 84 },
  expiry:  { x: 245, y: 2534, size: 90 },
  issue:   { x: 1818, y: 1520, size: 75, spacing: 13 }, // vertical text (bottom-up, letter-spaced)
  barcode: { x: 567, y: 2710, w: 722, h: 170 },     // Code128 bars box
  barcodeNum: { x: 928, y: 2700, size: 86, spacing: 7 }, // number above bars (centered, letter-spaced)
};

const BACK = {
  qr:      { x: 185, y: 351, size: 1585 },
  phone:   { x: 195, y: 2177, size: 74 },
  fin:     { x: 1262, y: 2122, size: 76 },
  nationality: { x: 195, y: 2401, size: 78 },
  addrAmh1: { x: 195, y: 2576, size: 74 },          // region   (Amharic)
  addrEng1: { x: 195, y: 2662, size: 66 },          // region   (English)
  addrAmh2: { x: 195, y: 2752, size: 74 },          // zone     (Amharic)
  addrEng2: { x: 195, y: 2838, size: 66 },          // zone     (English)
  addrAmh3: { x: 195, y: 2932, size: 74 },          // woreda   (Amharic)
  addrEng3: { x: 195, y: 3022, size: 66 },          // woreda   (English)
};

const GC_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// ─── Date helpers ────────────────────────────────────────────────────────
// Gregorian → Ethiopian (Amete-Mihret epoch, JDN 1723856).
function gregorianToEthiopian(gYear, gMonth, gDay) {
  const a = Math.floor((14 - gMonth) / 12);
  const y = gYear + 4800 - a;
  const m = gMonth + 12 * a - 3;
  const jdn = gDay + Math.floor((153 * m + 2) / 5) + 365 * y
    + Math.floor(y / 4) - Math.floor(y / 100) + Math.floor(y / 400) - 32045;
  const EPOCH = 1723856;
  const diff = jdn - EPOCH;
  const r = ((diff % 1461) + 1461) % 1461;
  const n = (r % 365) + 365 * Math.floor(r / 1460);
  return {
    year: 4 * Math.floor(diff / 1461) + Math.floor(r / 365) - Math.floor(r / 1460),
    month: Math.floor(n / 30) + 1,
    day: (n % 30) + 1,
  };
}

const pad2 = (n) => String(n).padStart(2, '0');

// Parse "YYYY/MM/DD" or "YYYY-MM-DD" → {y,m,d} or null.
function parseGcDate(value) {
  const match = String(value || '').trim().match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})$/);
  if (!match) return null;
  return { y: Number(match[1]), m: Number(match[2]), d: Number(match[3]) };
}

const fmtGcLong = (g) => `${g.y}/${GC_MONTHS[g.m - 1]}/${pad2(g.d)}`;          // 2001/Jan/13
const fmtEcDob = (e) => `${pad2(e.day)}/${pad2(e.month)}/${e.year}`;          // DD/MM/YYYY
const fmtEcYmd = (e) => `${e.year}/${pad2(e.month)}/${pad2(e.day)}`;          // YYYY/MM/DD

// Build the dual-calendar strings the card prints.
function buildDates(dobGc) {
  // Date of Birth — EC is DD/MM/YYYY, GC is YYYY/Mon/DD.
  const dob = parseGcDate(dobGc);
  const dobStr = dob
    ? `${fmtEcDob(gregorianToEthiopian(dob.y, dob.m, dob.d))} | ${fmtGcLong(dob)}`
    : '';

  // Date of Issue — today. EC is YYYY/MM/DD, GC is YYYY/Mon/DD.
  const today = new Date();
  const issG = { y: today.getFullYear(), m: today.getMonth() + 1, d: today.getDate() };
  const issueStr =
    `${fmtEcYmd(gregorianToEthiopian(issG.y, issG.m, issG.d))} | ${fmtGcLong(issG)}`;

  // Date of Expiry — issue + 8 years − 2 days.
  const exp = new Date(today.getFullYear() + 8, today.getMonth(), today.getDate() - 2);
  const expG = { y: exp.getFullYear(), m: exp.getMonth() + 1, d: exp.getDate() };
  const expiryStr =
    `${fmtEcYmd(gregorianToEthiopian(expG.y, expG.m, expG.d))} | ${fmtGcLong(expG)}`;

  return { dobStr, issueStr, expiryStr };
}

// ─── Drawing helpers ─────────────────────────────────────────────────────
function toBuffer(input) {
  if (Buffer.isBuffer(input)) return input;
  if (typeof input === 'string') {
    return Buffer.from(input.replace(/^data:[^;]+;base64,/i, ''), 'base64');
  }
  return null;
}

// Latin / digit text. The Barlow font registered for FONT_EN is the Bold
// weight, so a plain fill already renders bold — no synthetic thickening.
function drawEn(ctx, text, x, y) {
  ctx.fillText(text, x, y);
}

// Amharic text. Nyala ships only a regular weight, so add a thin uniform
// stroke (in the fill colour) to match the real card's bold Amharic.
function drawAm(ctx, text, x, y, size) {
  ctx.fillText(text, x, y);
  ctx.save();
  ctx.lineWidth = Math.max(1.5, size * 0.052);
  ctx.strokeStyle = ctx.fillStyle;
  ctx.lineJoin = 'round';
  ctx.strokeText(text, x, y);
  ctx.restore();
}

// Draw an "Amharic | English" value: Amharic in Nyala, the rest in Barlow.
function drawAmEn(ctx, amh, eng, x, y, size) {
  let cursor = x;
  if (amh) {
    ctx.font = `${size}px "${FONT_AM}"`;
    drawAm(ctx, amh, cursor, y, size);
    cursor += ctx.measureText(amh).width;
  }
  const tail = (amh && eng) ? ` | ${eng}` : (eng || '');
  if (tail) {
    ctx.font = `${size}px "${FONT_EN}"`;
    drawEn(ctx, tail, cursor, y);
  }
}

// Cover-fit an image into a box (scale to fill, center-crop the overflow).
function drawCover(ctx, img, box) {
  const scale = Math.max(box.w / img.width, box.h / img.height);
  const dw = img.width * scale;
  const dh = img.height * scale;
  const dx = box.x + (box.w - dw) / 2;
  const dy = box.y + (box.h - dh) / 2;
  ctx.save();
  ctx.beginPath();
  ctx.rect(box.x, box.y, box.w, box.h);
  ctx.clip();
  ctx.drawImage(img, dx, dy, dw, dh);
  ctx.restore();
}

// Desaturate a rectangular region (the card photo is rendered grayscale).
function grayscaleRegion(ctx, box) {
  const img = ctx.getImageData(box.x, box.y, box.w, box.h);
  const d = img.data;
  for (let i = 0; i < d.length; i += 4) {
    const g = Math.round(0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]);
    d[i] = d[i + 1] = d[i + 2] = g;
  }
  ctx.putImageData(img, box.x, box.y);
}

// Find the dark-module bounding box inside a QR image, so the surrounding
// quiet-zone (white border) can be excluded — the card's white box already
// supplies the quiet zone, and the real card's QR modules run edge-to-edge.
function qrContentBounds(img) {
  const c = createCanvas(img.width, img.height);
  const cx = c.getContext('2d');
  cx.drawImage(img, 0, 0);
  const d = cx.getImageData(0, 0, img.width, img.height).data;
  let minX = img.width, minY = img.height, maxX = -1, maxY = -1;
  for (let y = 0; y < img.height; y++) {
    for (let x = 0; x < img.width; x++) {
      if (d[(y * img.width + x) * 4] < 128) {           // dark module pixel
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < minX) return { sx: 0, sy: 0, sw: img.width, sh: img.height };
  return { sx: minX, sy: minY, sw: maxX - minX + 1, sh: maxY - minY + 1 };
}

async function makeBarcodePng(text) {
  return bwipjs.toBuffer({
    bcid: 'code128',
    text: String(text),
    scale: 4,
    height: 12,
    includetext: false,
    paddingwidth: 0,
    paddingheight: 0,
  });
}

// ─── Front card ──────────────────────────────────────────────────────────
async function generateFrontCard(data) {
  ensureFonts();
  const template = await loadImage(FRONT_TEMPLATE);
  const canvas = createCanvas(template.width, template.height);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(template, 0, 0);
  ctx.fillStyle = VALUE_COLOR;
  ctx.textBaseline = 'alphabetic';

  // Photo (grayscale, cover-fit).
  const photoBuf = toBuffer(data.photo);
  if (photoBuf) {
    const photo = await loadImage(photoBuf);
    drawCover(ctx, photo, FRONT.photo);
    grayscaleRegion(ctx, FRONT.photo);
  }

  const dates = buildDates(data.dobGc);

  // Full name — Amharic then English, two lines.
  if (data.fullName_amh) {
    ctx.font = `${FRONT.nameAmh.size}px "${FONT_AM}"`;
    drawAm(ctx, data.fullName_amh, FRONT.nameAmh.x, FRONT.nameAmh.y, FRONT.nameAmh.size);
  }
  if (data.fullName_eng) {
    ctx.font = `${FRONT.nameEng.size}px "${FONT_EN}"`;
    drawEn(ctx, data.fullName_eng, FRONT.nameEng.x, FRONT.nameEng.y);
  }

  // Date of Birth (digits only → Barlow).
  ctx.font = `${FRONT.dob.size}px "${FONT_EN}"`;
  drawEn(ctx, dates.dobStr, FRONT.dob.x, FRONT.dob.y);

  // Sex (Amharic | English).
  drawAmEn(ctx, data.sex_amh, data.sex_eng, FRONT.sex.x, FRONT.sex.y, FRONT.sex.size);

  // Date of Expiry.
  ctx.font = `${FRONT.expiry.size}px "${FONT_EN}"`;
  drawEn(ctx, dates.expiryStr, FRONT.expiry.x, FRONT.expiry.y);

  // Date of Issue — vertical text up the right edge: it reads bottom-to-top
  // (head tilted left), sitting above the template's "Date of Issue" label,
  // and is drawn with wide letter-spacing like the real card.
  ctx.save();
  ctx.translate(FRONT.issue.x, FRONT.issue.y);
  ctx.rotate(-Math.PI / 2);
  ctx.font = `${FRONT.issue.size}px "${FONT_EN}"`;
  ctx.letterSpacing = `${FRONT.issue.spacing}px`;
  drawEn(ctx, dates.issueStr, 0, 0);
  ctx.restore();

  // FAN — Code128 barcode + the number printed above it. The number is
  // NOT bold (Medium weight), letter-spaced to match the real card.
  if (data.fan) {
    ctx.save();
    ctx.font = `${FRONT.barcodeNum.size}px "${FONT_EN_LIGHT}"`;
    ctx.textAlign = 'center';
    ctx.letterSpacing = `${FRONT.barcodeNum.spacing}px`;
    drawEn(ctx, String(data.fan), FRONT.barcodeNum.x, FRONT.barcodeNum.y);
    ctx.restore();
    try {
      const barcode = await loadImage(await makeBarcodePng(data.fan));
      ctx.drawImage(barcode, FRONT.barcode.x, FRONT.barcode.y,
        FRONT.barcode.w, FRONT.barcode.h);
    } catch (err) {
      console.warn('[faydaCardGenerator] barcode failed:', err.message);
    }
  }

  return canvas.encode('jpeg', 75);
}

// ─── Back card ───────────────────────────────────────────────────────────
async function generateBackCard(data) {
  ensureFonts();
  const template = await loadImage(BACK_TEMPLATE);
  const canvas = createCanvas(template.width, template.height);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(template, 0, 0);
  ctx.fillStyle = VALUE_COLOR;
  ctx.textBaseline = 'alphabetic';

  // QR code — draw only the module area (quiet zone trimmed) so the
  // modules fill the box edge-to-edge, matching the real card.
  const qrBuf = toBuffer(data.qr);
  if (qrBuf) {
    const qr = await loadImage(qrBuf);
    const b = qrContentBounds(qr);
    ctx.drawImage(qr, b.sx, b.sy, b.sw, b.sh,
      BACK.qr.x, BACK.qr.y, BACK.qr.size, BACK.qr.size);
  }

  // Phone number.
  if (data.phone) {
    ctx.font = `${BACK.phone.size}px "${FONT_EN}"`;
    drawEn(ctx, String(data.phone), BACK.phone.x, BACK.phone.y);
  }

  // FIN — not bold (Medium weight), like the barcode number.
  if (data.fin) {
    ctx.font = `${BACK.fin.size}px "${FONT_EN_LIGHT}"`;
    drawEn(ctx, String(data.fin), BACK.fin.x, BACK.fin.y);
  }

  // Nationality (Amharic | English).
  drawAmEn(ctx, data.nationality_amh, data.nationality_eng,
    BACK.nationality.x, BACK.nationality.y, BACK.nationality.size);

  // Address — region / zone / woreda, Amharic line then English line.
  const drawAmh = (text, slot) => {
    if (!text) return;
    ctx.font = `${slot.size}px "${FONT_AM}"`;
    drawAm(ctx, text, slot.x, slot.y, slot.size);
  };
  const drawEng = (text, slot) => {
    if (!text) return;
    ctx.font = `${slot.size}px "${FONT_EN}"`;
    drawEn(ctx, text, slot.x, slot.y);
  };
  drawAmh(data.region_amh, BACK.addrAmh1);
  drawEng(data.region_eng, BACK.addrEng1);
  drawAmh(data.zone_amh, BACK.addrAmh2);
  drawEng(data.zone_eng, BACK.addrEng2);
  drawAmh(data.woreda_amh, BACK.addrAmh3);
  drawEng(data.woreda_eng, BACK.addrEng3);

  return canvas.encode('jpeg', 75);
}

// Generate both cards. Returns { front: Buffer, back: Buffer }.
async function generateCards(data) {
  const [front, back] = await Promise.all([
    generateFrontCard(data),
    generateBackCard(data),
  ]);
  return { front, back };
}

module.exports = {
  generateCards,
  generateFrontCard,
  generateBackCard,
  buildDates,
  gregorianToEthiopian,
};
