// Exact port of faydapdf-railway/pdfGenerator.js — reads the verify/callback JSON on
// stdin and writes the finished PDF bytes to stdout (name on stderr as NAME:<b64>).
// Bundled with esbuild into render.bundle.js so the deploy needs only Node (no
// node_modules). Assets (template + fonts) live one dir up, in app/fayda/assets/.
const fs = require("fs");
const path = require("path");
const { PDFDocument, rgb } = require("pdf-lib");
const fontkit = require("@pdf-lib/fontkit");

const ASSETS = process.env.FAYDA_ASSETS_DIR || path.join(__dirname, "..", "assets");
const TEMPLATE_PATH = path.join(ASSETS, "template.pdf");
const ENGLISH_FONT_PATH = path.join(ASSETS, "barlow.ttf");
const AMHARIC_FONT_PATH = path.join(ASSETS, "nyala.ttf");

const VALUE_FONT_SIZE = 9;
const VALUE_COLOR = rgb(0.137, 0.364, 0.443);

const TEXT_LAYOUT = [
  { key: "dateOfBirth_et", font: "amharic", x: 59.6, y: 553.19 },
  { key: "dateOfBirth_eng", font: "english", x: 59.6, y: 544.49 },
  { key: "gender_amh", font: "amharic", x: 59.6, y: 517.99 },
  { key: "gender_eng", font: "english", x: 59.6, y: 508.59 },
  { key: "citizenship_amh", font: "amharic", x: 59.6, y: 487.29 },
  { key: "citizenship_Eng", font: "english", x: 59.6, y: 477.59 },
  { key: "phone", font: "english", x: 59.6, y: 455.29 },
  { key: "region_amh", font: "amharic", x: 203.2, y: 553.19 },
  { key: "region_eng", font: "english", x: 203.2, y: 544.49 },
  { key: "zone_amh", font: "amharic", x: 203.2, y: 517.99 },
  { key: "zone_eng", font: "english", x: 203.2, y: 508.59 },
  { key: "woreda_amh", font: "amharic", x: 203.2, y: 487.29 },
  { key: "woreda_eng", font: "english", x: 203.2, y: 477.59 },
  { key: "fcn", font: "english", x: 73.6, y: 605.99, format: formatFcn },
  { key: "fullName_amh", font: "amharic", x: 170.7, y: 615.99 },
  { key: "fullName_eng", font: "english", x: 170.7, y: 604.49 }
];

const IMAGE_LAYOUT = [
  { key: "photo", x: 53.8, y: 624.69, width: 85, height: 117.5 },
  { key: "QRCodes", x: 110, y: 268.89, width: 164, height: 162 },
  { key: "fronts", x: 397.1, y: 511.89, width: 156.6, height: 240 },
  { key: "backs", x: 397.1, y: 264.89, width: 156.6, height: 240 }
];

function stripHomepage(response) {
  if (!response || typeof response !== "object" || Array.isArray(response)) return response;
  const { homepage, ...rest } = response;
  return rest;
}
function pickFirst(source, keys) {
  for (const key of keys) {
    if (source && source[key] !== undefined && source[key] !== null && source[key] !== "") return source[key];
  }
  return "";
}
function normalizeBase64(value) {
  if (!value) return null;
  if (Buffer.isBuffer(value)) return value;
  const stringValue = String(value).trim();
  const stripped = stringValue.replace(/^data:[^;]+;base64,/, "").replace(/\s+/g, "");
  return Buffer.from(stripped, "base64");
}
function isPng(buffer) {
  return buffer.length >= 8 && buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
}
function isJpeg(buffer) {
  return buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff;
}
function formatFcn(value) {
  const raw = String(value || "").replace(/\s+/g, "");
  if (!raw) return "";
  return raw.replace(/(.{4})/g, "$1 ").trim();
}
function extractVerifyResponseData(response) {
  const cleanResponse = stripHomepage(response || {});
  return (
    cleanResponse?.user?.data ||
    cleanResponse?.data?.user?.data ||
    cleanResponse?.data?.data?.user?.data ||
    cleanResponse?.data ||
    {}
  );
}
function sanitizeVerifyResponse(response) {
  const cleanResponse = stripHomepage(response || {});
  const data = extractVerifyResponseData(cleanResponse);
  return {
    cleanResponse,
    pdfData: {
      fullName_eng: pickFirst(data, ["fullName_eng", "fullNameEng", "fullName"]),
      fullName_amh: pickFirst(data, ["fullName_amh", "fullNameAmh"]),
      dateOfBirth_eng: pickFirst(data, ["dateOfBirth_eng", "dateOfBirthEng", "birthdate"]),
      dateOfBirth_et: pickFirst(data, ["dateOfBirth_et", "dateOfBirthEt"]),
      gender_eng: pickFirst(data, ["gender_eng", "genderEng"]),
      gender_amh: pickFirst(data, ["gender_amh", "genderAmh"]),
      citizenship_Eng: pickFirst(data, ["citizenship_Eng", "citizenship_eng", "citizenshipEng"]),
      citizenship_amh: pickFirst(data, ["citizenship_amh", "citizenshipAmh"]),
      phone: pickFirst(data, ["phone"]),
      region_eng: pickFirst(data, ["region_eng", "regionEng"]),
      region_amh: pickFirst(data, ["region_amh", "regionAmh"]),
      zone_eng: pickFirst(data, ["zone_eng", "zoneEng"]),
      zone_amh: pickFirst(data, ["zone_amh", "zoneAmh"]),
      woreda_eng: pickFirst(data, ["woreda_eng", "woredaEng"]),
      woreda_amh: pickFirst(data, ["woreda_amh", "woredaAmh"]),
      fcn: pickFirst(data, ["vid", "VID", "fcn", "FCN"]),
      photo: pickFirst(data, ["photo"]),
      QRCodes: pickFirst(data, ["QRCodes", "qrCodes", "qrCode"]),
      fronts: pickFirst(data, ["fronts", "front"]),
      backs: pickFirst(data, ["backs", "back"])
    }
  };
}
async function embedImage(pdfDoc, rawBytes) {
  if (isPng(rawBytes)) return pdfDoc.embedPng(rawBytes);
  if (isJpeg(rawBytes)) return pdfDoc.embedJpg(rawBytes);
  throw new Error("Unsupported image format in verify-otp response");
}

async function generate(verifyResponse) {
  const { pdfData } = sanitizeVerifyResponse(verifyResponse);
  const [templateBytes, englishFontBytes, amharicFontBytes] = [
    fs.readFileSync(TEMPLATE_PATH),
    fs.readFileSync(ENGLISH_FONT_PATH),
    fs.readFileSync(AMHARIC_FONT_PATH)
  ];
  const pdfDoc = await PDFDocument.load(templateBytes);
  pdfDoc.registerFontkit(fontkit);
  const englishFont = await pdfDoc.embedFont(englishFontBytes);
  const amharicFont = await pdfDoc.embedFont(amharicFontBytes);
  const page = pdfDoc.getPage(0);

  for (const field of TEXT_LAYOUT) {
    const rawValue = pdfData[field.key];
    const value = field.format ? field.format(rawValue) : String(rawValue ?? "");
    if (!value.trim()) continue;
    page.drawText(value, {
      x: field.x, y: field.y, size: VALUE_FONT_SIZE, color: VALUE_COLOR,
      font: field.font === "amharic" ? amharicFont : englishFont
    });
  }
  for (const imageField of IMAGE_LAYOUT) {
    const imageBytes = normalizeBase64(pdfData[imageField.key]);
    if (!imageBytes) continue;
    let image;
    try { image = await embedImage(pdfDoc, imageBytes); } catch (_) { continue; }
    page.drawImage(image, { x: imageField.x, y: imageField.y, width: imageField.width, height: imageField.height });
  }
  const pdfBytes = await pdfDoc.save();
  return { pdfBytes, name: pdfData.fullName_eng || "fayda" };
}

async function main() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const verifyResponse = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  const { pdfBytes, name } = await generate(verifyResponse);
  process.stderr.write("NAME:" + Buffer.from(String(name), "utf8").toString("base64") + "\n");
  process.stdout.write(Buffer.from(pdfBytes));
}
main().catch((e) => { process.stderr.write("ERR:" + ((e && e.message) || String(e)) + "\n"); process.exit(1); });
