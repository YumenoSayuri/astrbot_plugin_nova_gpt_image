#!/usr/bin/env node
/**
 * Minimal non-CLI local test for New API: POST /v1/images/generations (JSON)
 *
 * How to run:
 *   node ./newapi-image-generate.local.mjs
 */

import fs from "node:fs/promises";
import path from "node:path";

// =========================
// Config: edit these values
// =========================
const CONFIG = {
  baseUrl: "https://senapi.fun",
  apiKey: "sk-xxx",
  endpoint: "/v1/images/generations",
  model: "gpt-image-2",
  background: "auto",
  output_format: "png",
  image: ["https://filesystem.site/cdn/20260423/2af9b185b31a8d9861fcbb1588b872.jpg"],
  prompt: "改成黑发",
  quality: "high",
  size: "2160x3840",
  n: 1,
  outputDir: "./.tmp/newapi-image-generate",
  verboseHeaders: true,
  dumpJson: true,
};

function stripTrailingSlash(s) {
  return s.replace(/\/+$/, "");
}

function nowTag() {
  const d = new Date();
  const pad2 = (n) => String(n).padStart(2, "0");
  return (
    String(d.getFullYear()) +
    pad2(d.getMonth() + 1) +
    pad2(d.getDate()) +
    "_" +
    pad2(d.getHours()) +
    pad2(d.getMinutes()) +
    pad2(d.getSeconds())
  );
}

async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

function printHeaders(headers) {
  for (const [k, v] of headers.entries()) {
    console.log(`h: ${k}: ${v}`);
  }
}

function extFromContentType(ct) {
  const s = String(ct || "").toLowerCase();
  if (s.includes("image/png")) return "png";
  if (s.includes("image/jpeg")) return "jpg";
  if (s.includes("image/webp")) return "webp";
  return "bin";
}

async function downloadToFile(url, outPath) {
  const res = await fetch(url, { method: "GET" });
  if (!res.ok) throw new Error(`Download failed: ${res.status} ${res.statusText}`);
  const buf = Buffer.from(await res.arrayBuffer());
  await fs.writeFile(outPath, buf);
  return { bytes: buf.length, contentType: res.headers.get("content-type") || "" };
}

async function main() {
  if (!CONFIG.apiKey || CONFIG.apiKey.includes("REPLACE_ME")) {
    throw new Error("Please set CONFIG.apiKey (or env NEWAPI_KEY).");
  }

  const baseUrl = stripTrailingSlash(CONFIG.baseUrl);
  const url = baseUrl + CONFIG.endpoint;

  /** @type {Record<string, any>} */
  const payload = {
    model: CONFIG.model,
    prompt: CONFIG.prompt,
    image: CONFIG.image,
    background: CONFIG.background,
    output_format: CONFIG.output_format,
  };
  if (CONFIG.quality) payload.quality = CONFIG.quality;
  if (CONFIG.size) payload.size = CONFIG.size;
  if (CONFIG.n && CONFIG.n !== 1) payload.n = CONFIG.n;

  console.log(`\n==> POST ${url}`);
  console.log(`payload: ${JSON.stringify(payload)}`);

  const startedAt = Date.now();
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${CONFIG.apiKey}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(payload),
  });
  const tookMs = Date.now() - startedAt;

  const contentType = res.headers.get("content-type") || "";
  const text = await res.text();

  console.log(`[${res.status}] ${res.statusText} (${tookMs}ms)`);
  console.log(`response content-type: ${contentType || "(none)"}`);
  if (CONFIG.verboseHeaders) printHeaders(res.headers);

  const looksJson = contentType.includes("application/json") || text.trim().startsWith("{");
  if (!looksJson) {
    console.log("\n--- body (text) ---");
    console.log(text);
    if (!res.ok) throw new Error(`Request failed: ${res.status}`);
    return;
  }

  /** @type {any} */
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    console.log("\n--- body (invalid json) ---");
    console.log(text);
    if (!res.ok) throw new Error(`Request failed: ${res.status}`);
    return;
  }

  console.log("\n--- body (json) ---");
  console.log(JSON.stringify(json));

  const outDir = path.resolve(CONFIG.outputDir);
  await ensureDir(outDir);
  const tag = nowTag();

  if (CONFIG.dumpJson) {
    const jsonPath = path.join(outDir, `response_${tag}.json`);
    await fs.writeFile(jsonPath, JSON.stringify(json, null, 2), "utf8");
    console.log(`json saved: ${jsonPath}`);
  }

  const data = json?.data;
  if (!Array.isArray(data) || data.length === 0) {
    if (!res.ok) throw new Error(`Request failed: ${res.status}`);
    console.log("(no data[] array in response)");
    return;
  }

  let wrote = 0;
  for (let i = 0; i < data.length; i++) {
    const item = data[i];
    if (item?.b64_json) {
      const buf = Buffer.from(String(item.b64_json), "base64");
      const outPath = path.join(outDir, `image_${tag}_${i + 1}.png`);
      await fs.writeFile(outPath, buf);
      wrote++;
      console.log(`image saved: ${outPath} (${buf.length} bytes)`);
      continue;
    }
    if (item?.url) {
      // Some implementations return pre-signed URLs; download to make debugging easier.
      const tmpPath = path.join(outDir, `image_${tag}_${i + 1}.bin`);
      const dl = await downloadToFile(String(item.url), tmpPath);
      const ext = extFromContentType(dl.contentType);
      const finalPath = path.join(outDir, `image_${tag}_${i + 1}.${ext}`);
      await fs.rename(tmpPath, finalPath);
      wrote++;
      console.log(`image downloaded: ${finalPath} (${dl.bytes} bytes, ${dl.contentType || "unknown"})`);
      continue;
    }
  }

  if (wrote === 0) {
    console.log("(no b64_json/url images in response)");
  }

  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
}

main().catch((err) => {
  console.error("\nFatal:", err?.stack || err);
  process.exit(1);
});

