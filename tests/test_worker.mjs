/**
 * Worker tests -- runs the real worker/worker.js against a fake R2 binding.
 *
 *   node tests/test_worker.mjs
 *
 * Node 18+ is required (Web Crypto, Request/Response, R2-style list()).
 */

import { webcrypto } from "node:crypto";
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const ROOT = new URL("..", import.meta.url).pathname;
const WORKER_SRC = join(ROOT, "worker", "worker.js");
const TOKEN = "unit-test-token";

let passed = 0;
let failed = 0;

function check(name, condition, detail = "") {
  if (condition) {
    passed += 1;
    console.log(`  ok   ${name}`);
  } else {
    failed += 1;
    console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ""}`);
  }
}

// ---------------------------------------------------------------------------
// a fake R2 bucket with the bits the Worker touches
// ---------------------------------------------------------------------------

const OBJECTS = new Map();
const KEYS = [
  ["djmax/By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/AI[UE]OON.ogg", 1000],
  ["djmax/By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/4B.pt", 200],
  ["djmax/By_DLC/Arcaea/Gears/gear_4b.png", 300],
  ["djmax/By_DLC/Deemo/Songs/Myosotis [224]/Chart and OGG/Myosotis.ogg", 400],
  ["secret/do-not-share.txt", 10],
];
for (const [key, size] of KEYS) OBJECTS.set(key, size);

function fakeBucket() {
  return {
    async list({ prefix = "", delimiter, cursor, limit = 1000 } = {}) {
      const matching = [...OBJECTS.keys()].filter((k) => k.startsWith(prefix)).sort();
      const prefixes = new Set();
      const files = [];
      for (const key of matching) {
        const rest = key.slice(prefix.length);
        if (delimiter && rest.includes(delimiter)) {
          prefixes.add(prefix + rest.slice(0, rest.indexOf(delimiter) + 1));
        } else {
          files.push({ key, size: OBJECTS.get(key), etag: `etag-${key.length}`, uploaded: new Date(0) });
        }
      }
      const offset = cursor ? Number(cursor) : 0;
      const page = files.slice(offset, offset + limit);
      const truncated = offset + limit < files.length;
      return {
        objects: page,
        delimitedPrefixes: [...prefixes].sort(),
        truncated,
        cursor: truncated ? String(offset + limit) : undefined,
      };
    },
    async get(key, options = {}) {
      if (!OBJECTS.has(key)) return null;
      const size = OBJECTS.get(key);
      const body = new Uint8Array(size).map((_, i) => i % 251);
      let range;
      if (options.range) {
        if (options.range.suffix) {
          const length = Math.min(size, options.range.suffix);
          range = { offset: size - length, length };
        } else {
          const offset = options.range.offset || 0;
          const length = Math.min(options.range.length ?? size - offset, size - offset);
          range = { offset, length };
        }
      }
      const start = range ? range.offset : 0;
      const length = range ? range.length : size;
      return {
        key,
        size,
        etag: `etag-${size}`,
        httpEtag: `"etag-${size}"`,
        uploaded: new Date(0),
        range,
        body: body.slice(start, start + length),
        writeHttpMetadata(headers) {
          headers.set("Content-Type", "application/octet-stream");
        },
      };
    },
    async delete(keys) {
      for (const key of keys) OBJECTS.delete(key);
    },
  };
}

async function call(worker, path, { method = "GET", token = TOKEN, headers = {}, env = {} } = {}) {
  const allHeaders = new Headers(headers);
  if (token) allHeaders.set("Authorization", `Bearer ${token}`);
  const request = new Request(`https://stash.example.workers.dev${path}`, {
    method,
    headers: allHeaders,
  });
  return worker.fetch(request, {
    BUCKET: fakeBucket(),
    APP_TOKEN: TOKEN,
    ALLOWED_PREFIX: "djmax/",
    ...env,
  });
}

// ---------------------------------------------------------------------------
// load the worker (copy to .mjs so Node treats it as a module)
// ---------------------------------------------------------------------------

const dir = mkdtempSync(join(tmpdir(), "stash-worker-"));
const modulePath = join(dir, "worker.mjs");
writeFileSync(modulePath, readFileSync(WORKER_SRC, "utf8"));
const { default: worker } = await import(pathToFileURL(modulePath).href);

console.log("worker auth");
{
  const noToken = await call(worker, "/api/ping", { token: "" });
  check("missing token -> 401", noToken.status === 401, `got ${noToken.status}`);

  const wrong = await call(worker, "/api/list?prefix=djmax/", { token: "nope" });
  check("wrong token -> 401", wrong.status === 401, `got ${wrong.status}`);

  const good = await call(worker, "/api/ping");
  const body = await good.json();
  check("valid token -> 200", good.status === 200, `got ${good.status}`);
  check("ping reports the scope", body.scope === "djmax/", JSON.stringify(body));
  check("ping reports delete is off", body.allow_delete === false);
}

console.log("worker listing");
{
  const top = await (await call(worker, "/api/list?prefix=djmax/&delimiter=/")).json();
  check("delimiter lists DLC folders", JSON.stringify(top.prefixes) === JSON.stringify(["djmax/By_DLC/"]),
    JSON.stringify(top.prefixes));

  const dlcs = await (await call(worker, "/api/list?prefix=djmax/By_DLC/&delimiter=/")).json();
  check("lists both DLCs", dlcs.prefixes.length === 2, JSON.stringify(dlcs.prefixes));

  const all = await (await call(worker, "/api/list?prefix=djmax/By_DLC/Arcaea/")).json();
  check("recursive listing returns 3 objects", all.keys.length === 3, JSON.stringify(all.keys));

  const outOfScope = await call(worker, "/api/list?prefix=secret/&delimiter=/");
  check("prefix outside scope -> 403", outOfScope.status === 403, `got ${outOfScope.status}`);

  const traversal = await call(worker, "/api/list?prefix=djmax/../../etc/");
  check("traversal prefix -> 403", traversal.status === 403, `got ${traversal.status}`);
}

console.log("worker stats");
{
  const stats = await (await call(worker, "/api/stats?prefix=djmax/By_DLC/Arcaea/")).json();
  check("stats count files", stats.files === 3, JSON.stringify(stats));
  check("stats sum bytes", stats.bytes === 1500, JSON.stringify(stats));
}

console.log("worker download");
{
  const ok = await call(worker, "/api/file?key=djmax/By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/AI[UE]OON.ogg");
  check("file -> 200", ok.status === 200, `got ${ok.status}`);
  check("file reports content length", ok.headers.get("Content-Length") === "1000",
    ok.headers.get("Content-Length"));
  check("file advertises ranges", ok.headers.get("Accept-Ranges") === "bytes");
  const bytes = new Uint8Array(await ok.arrayBuffer());
  check("file body is complete", bytes.length === 1000, `got ${bytes.length}`);

  const ranged = await call(worker, "/api/file?key=djmax/By_DLC/Arcaea/Gears/gear_4b.png",
    { headers: { Range: "bytes=100-199" } });
  check("range -> 206", ranged.status === 206, `got ${ranged.status}`);
  check("range content-range header", ranged.headers.get("Content-Range") === "bytes 100-199/300",
    ranged.headers.get("Content-Range"));
  check("range length", (await ranged.arrayBuffer()).byteLength === 100);

  const suffix = await call(worker, "/api/file?key=djmax/By_DLC/Arcaea/Gears/gear_4b.png",
    { headers: { Range: "bytes=-50" } });
  check("suffix range -> 206 with 50 bytes",
    suffix.status === 206 && (await suffix.arrayBuffer()).byteLength === 50);

  const missing = await call(worker, "/api/file?key=djmax/By_DLC/Arcaea/Nope.ogg");
  check("missing object -> 404", missing.status === 404, `got ${missing.status}`);

  const forbidden = await call(worker, "/api/file?key=secret/do-not-share.txt");
  check("object outside scope -> 403", forbidden.status === 403, `got ${forbidden.status}`);

  const sneaky = await call(worker, "/api/file?key=djmax/%2e%2e/secret/do-not-share.txt");
  check("encoded traversal -> 403", sneaky.status === 403, `got ${sneaky.status}`);

  const head = await call(worker, "/api/file?key=djmax/By_DLC/Arcaea/Gears/gear_4b.png",
    { method: "HEAD" });
  check("HEAD works and has no body", head.status === 200
    && (await head.arrayBuffer()).byteLength === 0);
}

console.log("worker delete gating");
{
  const denied = await call(worker, "/api/delete", { method: "POST" });
  check("delete disabled by default -> 403", denied.status === 403, `got ${denied.status}`);

  const enabled = await call(worker, "/api/delete", {
    method: "POST", env: { ALLOW_DELETE: "true" }, headers: { "Content-Type": "application/json" },
  });
  check("delete still needs a key list", enabled.status === 400, `got ${enabled.status}`);
}

console.log("worker misc");
{
  const unknown = await call(worker, "/api/nope");
  check("unknown route -> 404", unknown.status === 404, `got ${unknown.status}`);

  const noBinding = await worker.fetch(
    new Request("https://x/api/ping", { headers: { Authorization: `Bearer ${TOKEN}` } }), {});
  check("missing R2 binding -> 500", noBinding.status === 500, `got ${noBinding.status}`);

  const preflight = await call(worker, "/api/list", { method: "OPTIONS", token: "" });
  check("OPTIONS -> 204", preflight.status === 204, `got ${preflight.status}`);
}

rmSync(dir, { recursive: true, force: true });

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
