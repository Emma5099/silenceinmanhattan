const { connectLambda, getStore } = require("@netlify/blobs");

const STORE_NAME = "fresque";
const SPOTS_KEY = "manhattan-spots";

function json(statusCode, body) {
  return {
    statusCode,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    },
    body: JSON.stringify(body),
  };
}

function cors(event) {
  if (event.httpMethod === "OPTIONS") {
    return json(204, {});
  }
  return null;
}

/** Classic `exports.handler` needs this before getStore(). */
function initBlobs(event) {
  if (event) connectLambda(event);
}

function store(event) {
  initBlobs(event);
  return getStore(STORE_NAME);
}

async function readSpots(event) {
  const data = await store(event).get(SPOTS_KEY, { type: "json" });
  if (!data || !Array.isArray(data.spots)) return [];
  return data.spots;
}

async function writeSpots(event, spots) {
  await store(event).setJSON(SPOTS_KEY, {
    spots,
    updated_at: new Date().toISOString(),
  });
}

module.exports = {
  json,
  cors,
  initBlobs,
  store,
  readSpots,
  writeSpots,
  getStore,
  STORE_NAME,
};
