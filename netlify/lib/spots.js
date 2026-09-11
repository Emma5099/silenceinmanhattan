const { getStore } = require("@netlify/blobs");

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

async function readSpots() {
  const store = getStore(STORE_NAME);
  const data = await store.get(SPOTS_KEY, { type: "json" });
  if (!data || !Array.isArray(data.spots)) return [];
  return data.spots;
}

async function writeSpots(spots) {
  const store = getStore(STORE_NAME);
  await store.setJSON(SPOTS_KEY, { spots, updated_at: new Date().toISOString() });
}

module.exports = { json, cors, readSpots, writeSpots, getStore, STORE_NAME };
