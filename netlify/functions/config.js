const { json, cors } = require("../lib/spots");

exports.handler = async (event) => {
  const preflight = cors(event);
  if (preflight) return preflight;

  return json(200, {
    cartoApiKey: process.env.CARTO_API_KEY || process.env.CARTO_KEY || "",
  });
};
