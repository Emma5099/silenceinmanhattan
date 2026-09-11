const { json, cors, readSpots } = require("../lib/spots");

exports.handler = async (event) => {
  const preflight = cors(event);
  if (preflight) return preflight;

  let spots = 0;
  try {
    spots = (await readSpots()).length;
  } catch (_) {
    // Blobs may be unavailable in some local contexts
  }

  return json(200, {
    ok: true,
    mode: "loudness+frequency",
    spots,
    platform: "netlify",
  });
};
