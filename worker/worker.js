/**
 * DJMAX Stash - Cloudflare Worker API
 * -----------------------------------
 * Sits in front of an R2 bucket.  Friends authenticate with an app token and can
 * do exactly two things: LIST objects under ALLOWED_PREFIX and DOWNLOAD them.
 * They never see R2 keys, and delete/write is off unless you explicitly enable
 * it with the ALLOW_DELETE variable.
 *
 * Deploy:  see worker/wrangler.toml.example and the README.
 * Runtime: Workers (ES modules), needs an R2 binding named BUCKET.
 *
 * Environment / vars
 *   BUCKET            (R2 binding)  required
 *   APP_TOKEN         (secret)      required - what the GUI sends
 *   ALLOWED_PREFIX    (var)         default "djmax/" - nothing outside this is reachable
 *   PUBLIC_PREFIXES   (var)         optional comma list of extra read-only prefixes
 *   ALLOW_DELETE      (var)         default "false"
 *   ALLOWED_ORIGINS   (var)         default "*"
 *   APP_VERSION       (var)         default "1.0.0"
 */

const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
};

const DEFAULTS = {
  allowedPrefix: "djmax/",
  allowDelete: false,
  allowedOrigins: "*",
  version: "1.0.0",
  maxLimit: 1000,
  statsMaxObjects: 20000,
};

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { ...JSON_HEADERS, ...extra },
  });
}

function fail(status, message, extra = {}) {
  return json({ error: message, status }, status, extra);
}

function normalisePrefix(value) {
  let prefix = String(value ?? "").trim();
  if (prefix.startsWith("/")) prefix = prefix.slice(1);
  if (prefix && !prefix.endsWith("/")) prefix += "/";
  return prefix;
}

function configOf(env) {
  const rawPrefix = env.ALLOWED_PREFIX && String(env.ALLOWED_PREFIX).trim()
    ? String(env.ALLOWED_PREFIX)
    : DEFAULTS.allowedPrefix;
  const prefixes = [normalisePrefix(rawPrefix)];
  if (env.PUBLIC_PREFIXES) {
    for (const piece of String(env.PUBLIC_PREFIXES).split(",")) {
      const p = normalisePrefix(piece);
      if (p && !prefixes.includes(p)) prefixes.push(p);
    }
  }
  return {
    prefixes,
    primaryPrefix: prefixes[0],
    allowDelete: String(env.ALLOW_DELETE ?? "").toLowerCase() === "true",
    allowedOrigins: env.ALLOWED_ORIGINS || DEFAULTS.allowedOrigins,
    version: env.APP_VERSION || DEFAULTS.version,
    maxLimit: Number(env.MAX_LIST_LIMIT) > 0 ? Number(env.MAX_LIST_LIMIT) : DEFAULTS.maxLimit,
    statsMaxObjects:
      Number(env.STATS_MAX_OBJECTS) > 0 ? Number(env.STATS_MAX_OBJECTS) : DEFAULTS.statsMaxObjects,
  };
}

/** Constant-time-ish token comparison. */
async function tokenMatches(provided, expected) {
  if (!expected) return false;
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(provided || "")),
    crypto.subtle.digest("SHA-256", enc.encode(String(expected))),
  ]);
  const av = new Uint8Array(a);
  const bv = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < av.length; i += 1) diff |= av[i] ^ bv[i];
  return diff === 0;
}

function extractToken(request) {
  const auth = request.headers.get("Authorization") || "";
  if (auth.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim();
  return request.headers.get("X-Stash-Token") || new URL(request.url).searchParams.get("token") || "";
}

function corsHeaders(request, cfg) {
  const origin = request.headers.get("Origin") || "";
  const allowed = cfg.allowedOrigins === "*" || cfg.allowedOrigins
    .split(",")
    .map((o) => o.trim())
    .includes(origin);
  return {
    "Access-Control-Allow-Origin": allowed ? (cfg.allowedOrigins === "*" ? "*" : origin) : "null",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, X-Stash-Token, Range, Content-Type",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, ETag, Accept-Ranges",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function isSafeObjectKey(key, cfg) {
  if (!key || typeof key !== "string") return false;
  if (key.includes("..") || key.includes("\\") || key.startsWith("/")) return false;
  if (key.length > 1024) return false;
  return cfg.prefixes.some((prefix) => prefix === "" || key.startsWith(prefix));
}

function isSafePrefix(prefix, cfg) {
  if (prefix.includes("..") || prefix.includes("\\")) return false;
  return cfg.prefixes.some((allowed) => allowed === "" || prefix === "" ||
    prefix.startsWith(allowed) || allowed.startsWith(prefix));
}

function objectSummary(object) {
  return {
    key: object.key,
    size: object.size,
    etag: (object.httpEtag || object.etag || "").replace(/"/g, ""),
    uploaded: object.uploaded ? new Date(object.uploaded).toISOString() : "",
  };
}

async function readJson(request) {
  try {
    return await request.json();
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// endpoints
// ---------------------------------------------------------------------------

async function handlePing(request, env, cfg) {
  const listed = await env.BUCKET.list({ prefix: cfg.primaryPrefix, limit: 1 });
  return json({
    ok: true,
    app: "djmax-stash-worker",
    version: cfg.version,
    bucket_ok: true,
    scope: cfg.primaryPrefix,
    scope_all: cfg.prefixes,
    allow_delete: cfg.allowDelete,
    sample_keys: (listed.objects || []).map((o) => o.key),
  });
}

async function handleList(request, env, cfg) {
  const url = new URL(request.url);
  const prefix = normalisePrefix(url.searchParams.get("prefix") ?? cfg.primaryPrefix);
  const delimiter = url.searchParams.get("delimiter");
  const cursor = url.searchParams.get("cursor") || undefined;
  const limitParam = Number(url.searchParams.get("limit"));
  const limit = Math.min(Math.max(1, limitParam > 0 ? limitParam : 1000), cfg.maxLimit);

  if (!isSafePrefix(prefix, cfg)) {
    return fail(403, `Prefix "${prefix}" is outside this token's scope (${cfg.prefixes.join(", ")})`);
  }

  const options = { prefix, limit };
  if (delimiter) options.delimiter = delimiter;
  if (cursor) options.cursor = cursor;

  const listed = await env.BUCKET.list(options);
  return json({
    prefix,
    delimiter: delimiter || null,
    keys: (listed.objects || []).map(objectSummary),
    prefixes: (listed.delimitedPrefixes || []),
    cursor: listed.truncated ? listed.cursor : null,
    truncated: Boolean(listed.truncated),
  });
}

/**
 * Walk everything under a prefix and return totals.
 * Uses the parallel-HEAD trick: R2's list() already gives sizes, and DELIMITER-less
 * listing is enough, so we only count.  One indexed listing beats thousands of HEADs.
 */
async function handleStats(request, env, cfg) {
  const url = new URL(request.url);
  const prefix = normalisePrefix(url.searchParams.get("prefix") ?? cfg.primaryPrefix);
  if (!isSafePrefix(prefix, cfg)) return fail(403, "Prefix outside token scope");

  let cursor;
  let files = 0;
  let bytes = 0;
  let scanned = 0;
  let capped = false;
  do {
    const options = { prefix, limit: 1000 };
    if (cursor) options.cursor = cursor;
    const listed = await env.BUCKET.list(options);
    for (const object of listed.objects || []) {
      files += 1;
      bytes += object.size || 0;
      scanned += 1;
    }
    cursor = listed.truncated ? listed.cursor : undefined;
    if (scanned >= cfg.statsMaxObjects && cursor) {
      capped = true;
      break;
    }
  } while (cursor);

  return json({ prefix, files, bytes, scanned, capped });
}

/** Rough name search across the whole tree (structural listing only, no object bodies). */
async function handleSearch(request, env, cfg) {
  const url = new URL(request.url);
  const query = (url.searchParams.get("q") || "").trim().toLowerCase();
  const scope = normalisePrefix(url.searchParams.get("prefix") ?? cfg.primaryPrefix);
  const depth = Math.min(Math.max(1, Number(url.searchParams.get("depth")) || 3), 4);
  if (!isSafePrefix(scope, cfg)) return fail(403, "Prefix outside token scope");
  if (query.length < 2) return fail(400, "Query must be at least 2 characters");

  const results = [];
  const walk = async (prefix, level) => {
    let cursor;
    do {
      const options = { prefix, delimiter: "/", limit: 1000 };
      if (cursor) options.cursor = cursor;
      const listed = await env.BUCKET.list(options);
      for (const full of listed.delimitedPrefixes || []) {
        const name = full.slice(prefix.length).replace(/\/$/, "");
        if (name.toLowerCase().includes(query)) {
          results.push({ path: full, name, level });
        }
        if (level + 1 < depth) await walk(full, level + 1);
      }
      for (const object of listed.objects || []) {
        if (object.key.toLowerCase().includes(query)) {
          results.push({ path: object.key, name: object.key.split("/").pop(), level, size: object.size });
        }
      }
      cursor = listed.truncated ? listed.cursor : undefined;
      if (results.length > 500) return;
    } while (cursor);
  };
  await walk(scope, 0);
  return json({ query, scope, results: results.slice(0, 500) });
}

/** Stream an object, honouring Range requests so the GUI can resume. */
async function handleFile(request, env, cfg) {
  const url = new URL(request.url);
  const key = url.searchParams.get("key") || "";
  if (!isSafeObjectKey(key, cfg)) {
    return fail(403, "Object key missing or outside this token's scope");
  }

  const isHead = request.method === "HEAD";
  const rangeHeader = request.headers.get("Range");
  const options = {};
  if (rangeHeader) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader.trim());
    if (match) {
      const [, startRaw, endRaw] = match;
      if (startRaw === "" && endRaw !== "") {
        options.range = { suffix: Number(endRaw) };
      } else if (startRaw !== "") {
        const offset = Number(startRaw);
        const end = endRaw !== "" ? Number(endRaw) : undefined;
        options.range = end === undefined
          ? { offset }
          : { offset, length: Math.max(1, end - offset + 1) };
      }
    }
  }

  const object = await env.BUCKET.get(key, options);
  if (object === null) return fail(404, `No such object: ${key}`);

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("ETag", object.httpEtag || "");
  headers.set("Accept-Ranges", "bytes");
  headers.set("Cache-Control", "public, max-age=3600");
  headers.set("X-Content-Type-Options", "nosniff");

  let status = 200;
  if (object.range) {
    const offset = object.range.offset ?? Math.max(0, object.size - (object.range.length || 0));
    const length = object.range.length ?? (object.size - offset);
    headers.set("Content-Range", `bytes ${offset}-${offset + length - 1}/${object.size}`);
    headers.set("Content-Length", String(length));
    status = 206;
  } else {
    headers.set("Content-Length", String(object.size));
  }

  if (isHead) {
    return new Response(null, { status, headers });
  }
  return new Response(object.body, { status, headers });
}

/** Delete is off by default; it exists so you can prune duplicates deliberately. */
async function handleDelete(request, env, cfg) {
  if (!cfg.allowDelete) return fail(403, "Delete is disabled on this Worker (ALLOW_DELETE=false)");
  const body = await readJson(request);
  const keys = body && Array.isArray(body.keys) ? body.keys : [];
  if (!keys.length) return fail(400, "Send {keys: [...]}");
  for (const key of keys) {
    if (!isSafeObjectKey(key, cfg)) return fail(403, `Key outside scope: ${key}`);
  }
  await env.BUCKET.delete(keys);
  return json({ deleted: keys });
}

// ---------------------------------------------------------------------------
// router
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env) {
    const cfg = configOf(env);
    const cors = corsHeaders(request, cfg);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    if (!env.BUCKET) {
      return fail(500, "R2 binding BUCKET is missing - check wrangler.toml");
    }

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    // /api/ping is intentionally reachable without a bucket read so the GUI can
    // distinguish "wrong URL" from "wrong token" while setting up.
    if (path === "/" || path === "/api" || path === "/api/ping") {
      const tokenOk = await tokenMatches(extractToken(request), env.APP_TOKEN);
      if (!tokenOk) {
        return fail(401, "Bad or missing token", cors);
      }
      try {
        return await handlePing(request, env, cfg);
      } catch (error) {
        return fail(502, `Bucket unreachable: ${error.message}`);
      }
    }

    const tokenOk = await tokenMatches(extractToken(request), env.APP_TOKEN);
    if (!tokenOk) return fail(401, "Bad or missing token", cors);

    try {
      switch (path) {
        case "/api/list":
          return await handleList(request, env, cfg);
        case "/api/stats":
          return await handleStats(request, env, cfg);
        case "/api/search":
          return await handleSearch(request, env, cfg);
        case "/api/file":
          return await handleFile(request, env, cfg);
        case "/api/delete":
          if (request.method !== "POST") return fail(405, "Use POST");
          return await handleDelete(request, env, cfg);
        default:
          return fail(404, `Unknown route ${path}`);
      }
    } catch (error) {
      return fail(500, `Worker error: ${error.message}`);
    }
  },
};
