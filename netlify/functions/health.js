const { json, cors, readSpots, initBlobs } = require("../lib/spots");

exports.handler = async (event) => {
  const preflight = cors(event);
  if (preflight) return preflight;

  let spots = 0;
  try {
    initBlobs(event);
    spots = (await readSpots(event)).length;
  } catch (_) {
    // Blobs may be unavailable; health should still succeed
  }

  return json(200, {
    ok: true,
    mode: "loudness+frequency",
    spots,
    platform: "netlify",
  });
};
