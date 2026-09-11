const { json, cors, readSpots, writeSpots } = require("../lib/spots");
const { randomUUID } = require("crypto");

exports.handler = async (event) => {
  const preflight = cors(event);
  if (preflight) return preflight;

  try {
    if (event.httpMethod === "GET") {
      const spots = await readSpots();
      return json(200, { spots, count: spots.length });
    }

    if (event.httpMethod === "POST") {
      const body = JSON.parse(event.body || "{}");
      if (
        typeof body.lat !== "number" ||
        typeof body.lng !== "number" ||
        typeof body.label !== "string"
      ) {
        return json(400, { detail: "lat, lng, and label are required" });
      }

      const spots = await readSpots();
      const saved = {
        id: body.id || randomUUID(),
        map_id: "manhattan",
        user_id: body.user_id || null,
        contributor_name: (body.contributor_name || "").trim() || null,
        lat: body.lat,
        lng: body.lng,
        label: body.label,
        score: body.score ?? null,
        stress: body.stress ?? null,
        intensity: body.intensity ?? null,
        rms: body.rms ?? null,
        color: body.color ?? null,
        created_at: new Date().toISOString(),
      };

      const idx = spots.findIndex((s) => s.id === saved.id);
      if (idx >= 0) spots[idx] = { ...spots[idx], ...saved };
      else spots.push(saved);

      await writeSpots(spots);
      return json(200, saved);
    }

    return json(405, { detail: "Method not allowed" });
  } catch (err) {
    return json(500, { detail: err.message || "Spots error" });
  }
};
