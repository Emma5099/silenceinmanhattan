const fs = require("fs");
const path = require("path");
const { json, cors, initBlobs, store } = require("../lib/spots");

const NYC311_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json";
const NOISE_TYPES = {
  all: null,
  street: "Noise - Street/Sidewalk",
  vehicle: "Noise - Vehicle",
  helicopter: "Noise - Helicopter",
};
const NOISE_TYPES_ALL = Object.values(NOISE_TYPES).filter(Boolean);
const BBOX = { south: 40.698, north: 40.878, west: -74.022, east: -73.906 };
const CACHE_KEY = "311-noise-all";
const CACHE_TTL_MS = 30 * 60 * 1000;
const DEFAULT_DAYS = 90;
const DEFAULT_LIMIT = 6000;

async function fetchFromNyc(days, kind, limit) {
  const since = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10) + "T00:00:00";
  const clauses = [
    "latitude IS NOT NULL",
    "longitude IS NOT NULL",
    "borough='MANHATTAN'",
    `created_date >= '${since}'`,
    `latitude between ${BBOX.south} and ${BBOX.north}`,
    `longitude between ${BBOX.west} and ${BBOX.east}`,
  ];
  const exact = NOISE_TYPES[kind];
  if (exact) clauses.push(`complaint_type='${exact}'`);
  else clauses.push(`complaint_type in (${NOISE_TYPES_ALL.map((t) => `'${t}'`).join(", ")})`);

  const params = new URLSearchParams({
    $select: "unique_key,created_date,complaint_type,descriptor,latitude,longitude",
    $where: clauses.join(" AND "),
    $order: "created_date DESC",
    $limit: String(limit),
  });

  const res = await fetch(`${NYC311_URL}?${params}`, {
    headers: { Accept: "application/json", "User-Agent": "fresque-sonore/1.0" },
  });
  if (!res.ok) throw new Error(`NYC Open Data ${res.status}`);
  const rows = await res.json();
  if (!Array.isArray(rows)) throw new Error("NYC Open Data returned non-array");

  const points = [];
  for (const row of rows) {
    const lat = Number(row.latitude);
    const lng = Number(row.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    points.push({
      id: row.unique_key,
      lat,
      lng,
      type: row.complaint_type || "Noise",
      descriptor: row.descriptor || "",
      date: row.created_date || "",
    });
  }

  return {
    source: NYC311_URL,
    filters: { borough: "MANHATTAN", days, kind, limit },
    count: points.length,
    points,
    kinds: Object.keys(NOISE_TYPES),
    cached_at: new Date().toISOString(),
  };
}

function loadBundledSnapshot() {
  const candidates = [
    path.join(__dirname, "..", "..", "public", "data", "311_noise_cache.json"),
    path.join(__dirname, "311_noise_cache.json"),
  ];
  for (const file of candidates) {
    try {
      if (!fs.existsSync(file)) continue;
      const data = JSON.parse(fs.readFileSync(file, "utf8"));
      if (data && Array.isArray(data.points) && data.points.length) return data;
    } catch (_) {}
  }
  return null;
}

function filterPayload(payload, kind, limit, days) {
  let points = payload.points || [];
  if (kind !== "all") {
    const want = NOISE_TYPES[kind];
    points = points.filter((p) => p.type === want);
  }
  if (limit < points.length) points = points.slice(0, limit);
  return {
    ...payload,
    points,
    count: points.length,
    filters: {
      borough: "MANHATTAN",
      days: payload.filters?.days || days,
      kind,
      limit,
    },
  };
}

exports.handler = async (event) => {
  const preflight = cors(event);
  if (preflight) return preflight;
  if (event.httpMethod !== "GET") return json(405, { detail: "Method not allowed" });

  const params = event.queryStringParameters || {};
  const days = Math.min(365, Math.max(7, Number(params.days) || DEFAULT_DAYS));
  const kind = String(params.kind || "all").toLowerCase();
  const limit = Math.min(10000, Math.max(100, Number(params.limit) || DEFAULT_LIMIT));

  if (!(kind in NOISE_TYPES)) {
    return json(400, { detail: `Unknown kind. Use one of: ${Object.keys(NOISE_TYPES).join(", ")}` });
  }

  let payload = null;

  // 1) Netlify Blobs cache
  try {
    initBlobs(event);
    const blobStore = store(event);
    payload = await blobStore.get(CACHE_KEY, { type: "json" });
    const age = payload?.cached_at ? Date.now() - Date.parse(payload.cached_at) : Infinity;
    if (!payload || !Array.isArray(payload.points) || age > CACHE_TTL_MS) {
      try {
        payload = await fetchFromNyc(DEFAULT_DAYS, "all", DEFAULT_LIMIT);
        try {
          await blobStore.setJSON(CACHE_KEY, payload);
        } catch (_) {}
      } catch (fetchErr) {
        if (!(payload && Array.isArray(payload.points) && payload.points.length)) {
          throw fetchErr;
        }
      }
    }
  } catch (_) {
    payload = null;
  }

  // 2) Live NYC fetch without cache
  if (!(payload && Array.isArray(payload.points) && payload.points.length)) {
    try {
      payload = await fetchFromNyc(days, "all", Math.min(limit, DEFAULT_LIMIT));
    } catch (_) {
      payload = null;
    }
  }

  // 3) Bundled snapshot shipped with the site
  if (!(payload && Array.isArray(payload.points) && payload.points.length)) {
    payload = loadBundledSnapshot();
  }

  if (!(payload && Array.isArray(payload.points) && payload.points.length)) {
    return json(503, {
      detail: "311 dataset unavailable",
      points: [],
      count: 0,
      kinds: Object.keys(NOISE_TYPES),
    });
  }

  return json(200, filterPayload(payload, kind, limit, days));
};
