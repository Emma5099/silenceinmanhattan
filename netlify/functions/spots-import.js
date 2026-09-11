const { json, cors, readSpots, writeSpots } = require("../lib/spots");
const { randomUUID } = require("crypto");

exports.handler = async (event) => {
  const preflight = cors(event);
  if (preflight) return preflight;

  if (event.httpMethod !== "POST") {
    return json(405, { detail: "Method not allowed" });
  }

  try {
    const body = JSON.parse(event.body || "{}");
    const incoming = Array.isArray(body.spots) ? body.spots : [];
    const spots = await readSpots();
    const byId = new Map(spots.map((s) => [s.id, s]));
    let imported = 0;

    for (const s of incoming) {
      if (s == null || s.lat == null || s.lng == null || !s.label) continue;
      const id = s.id || randomUUID();
      if (byId.has(id)) continue;
      const saved = {
        id,
        map_id: "manhattan",
        user_id: s.user_id || null,
        contributor_name: (s.contributor_name || "").trim() || null,
        lat: Number(s.lat),
        lng: Number(s.lng),
        label: String(s.label),
        score: s.score ?? null,
        stress: s.stress ?? null,
        intensity: s.intensity ?? null,
        rms: s.rms ?? null,
        color: s.color ?? null,
        created_at: s.created_at || new Date().toISOString(),
      };
      byId.set(id, saved);
      imported += 1;
    }

    const next = Array.from(byId.values());
    await writeSpots(next);
    return json(200, { imported, count: next.length });
  } catch (err) {
    return json(500, { detail: err.message || "Import failed" });
  }
};
