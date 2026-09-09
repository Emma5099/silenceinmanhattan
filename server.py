#!/usr/bin/env python3
"""Analyze urban sound clips (loudness + harshness) and serve the Manhattan map."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import db

ROOT = Path(__file__).resolve().parent

_env_path = ROOT / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

SILENCE_RMS = 0.006
TARGET_SR = 16000
# Bands for "harsh" street noise (brakes, horns, sirens, chatter)
MID_FREQ_HZ = 800.0
HIGH_FREQ_HZ = 2200.0

# Quiet → loud watercolor stops (same idea as the client legend)
INTENSITY_STOPS = [
    (0.0, (255, 236, 150)),
    (0.22, (255, 214, 100)),
    (0.45, (255, 170, 50)),
    (0.65, (255, 110, 30)),
    (0.82, (230, 50, 40)),
    (1.0, (200, 20, 20)),
]

LABEL_BY_INTENSITY = [
    (0.12, "silence"),
    (0.28, "quiet"),
    (0.45, "soft"),
    (0.62, "lively"),
    (0.80, "noisy"),
    (1.01, "harsh"),
]

app = FastAPI(title="Silence in NYC")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


DATA_DIR = ROOT / "data"

# Filename (stem) → lat/lng. Slightly offset duplicate streets so both read on the map.
DATA_LOCATIONS = {
    "400–498 E 75th St": (40.76956, -73.95437),
    "400–498 E 75th St 2": (40.76972, -73.95395),
    "Madison Square Garden": (40.75051, -73.99352),
}


@app.on_event("startup")
def _startup():
    db.init_db()
    seeded = seed_data_folder()
    if seeded:
        print(f"Seeded {seeded} sound(s) from data/")
    print(f"Database ready: {db.DB_PATH} ({db.spot_count()} sounds)")
    bootstrap_311_cache()


def seed_data_folder() -> int:
    """Analyze audio files in data/ and insert/refresh spots (stable ids)."""
    if not DATA_DIR.is_dir():
        return 0
    existing = {s["id"]: s for s in db.list_spots()}
    n = 0
    for path in sorted(DATA_DIR.iterdir()):
        if path.suffix.lower() not in {".m4a", ".wav", ".mp3", ".webm", ".ogg", ".aac"}:
            continue
        stem = path.stem
        coords = DATA_LOCATIONS.get(stem)
        if not coords:
            print(f"Skip {path.name}: no location mapping")
            continue
        lat, lng = coords
        spot_id = f"data-{stem}"
        try:
            raw = path.read_bytes()
            audio, rms = load_audio_mono_16k(raw, filename=path.name)
            result = analyze_clip(audio, rms)
            if spot_id in existing:
                db.update_spot_analysis(
                    spot_id,
                    label=result["label"],
                    score=result["score"],
                    stress=result["stress"],
                    intensity=result["intensity"],
                    rms=result["rms"],
                    color=result["color"],
                )
            else:
                db.add_spot(
                    spot_id=spot_id,
                    lat=lat,
                    lng=lng,
                    label=result["label"],
                    score=result["score"],
                    stress=result["stress"],
                    intensity=result["intensity"],
                    rms=result["rms"],
                    color=result["color"],
                    contributor_name=stem,
                )
            n += 1
            print(f"  + {stem}: {result['label']} ({result['intensity']:.2f}) {result['color']}")
        except Exception as exc:
            print(f"  ! {path.name}: {exc}")
    return n


def load_audio_mono_16k(raw: bytes, filename: str = "") -> tuple[np.ndarray, float]:
    """Decode uploaded audio to mono float32 @ 16 kHz."""
    suffix = Path(filename).suffix.lower() if filename else ".audio"

    try:
        audio, sr = sf.read(io.BytesIO(raw), always_2d=False)
    except Exception:
        audio, sr = None, None

    if audio is None:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".audio", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            audio, sr = librosa.load(tmp_path, sr=TARGET_SR, mono=True)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        audio = np.asarray(audio, dtype=np.float32)
        rms = float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
        return audio, rms

    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != TARGET_SR:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=TARGET_SR)
    rms = float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
    return audio, rms


def spectral_profile(audio: np.ndarray, sr: int = TARGET_SR) -> tuple[float, float, float]:
    """Return (centroid_hz, mid_band_ratio, high_band_ratio)."""
    if audio.size < 32:
        return 0.0, 0.0, 0.0
    windowed = audio * np.hanning(audio.size)
    spectrum = np.abs(np.fft.rfft(windowed))
    power = spectrum**2
    freqs = np.fft.rfftfreq(audio.size, d=1.0 / sr)
    total = float(power.sum()) + 1e-12
    centroid = float((freqs * power).sum() / total)
    mid = float(power[(freqs >= MID_FREQ_HZ) & (freqs < HIGH_FREQ_HZ)].sum() / total)
    high = float(power[freqs >= HIGH_FREQ_HZ].sum() / total)
    return centroid, mid, high


def peak_window_rms(audio: np.ndarray, sr: int = TARGET_SR, win_s: float = 0.25) -> float:
    """Loudest short window — catches horns/sirens in otherwise quiet clips."""
    win = max(32, int(sr * win_s))
    if audio.size <= win:
        return float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
    # hop half-window
    hop = win // 2
    best = 0.0
    for i in range(0, audio.size - win + 1, hop):
        chunk = audio[i : i + win]
        best = max(best, float(np.sqrt(np.mean(chunk**2))))
    return best


def loudness_01(rms: float, peak_rms: float | None = None) -> float:
    """Log-ish loudness so street clips spread across yellow→orange→red."""
    peak = peak_rms or rms
    # Blend overall level with short peaks (horns) without maxing out every clip
    level = 0.65 * rms + 0.35 * peak
    if level <= SILENCE_RMS:
        return 0.0
    # ~0.03 quiet, ~0.08 soft, ~0.14 lively, ~0.22+ noisy
    lo, hi = np.log10(0.025), np.log10(0.32)
    return float(np.clip((np.log10(max(level, 1e-6)) - lo) / (hi - lo), 0.0, 1.0))


def harshness_01(centroid_hz: float, mid: float, high: float) -> float:
    """Brightness of the spectrum — mid/high bands pull toward red."""
    # Ambient rumble ~300–600 Hz; stressed street noise piles into mid band
    bright = float(np.clip((centroid_hz - 300.0) / 1800.0, 0.0, 1.0))
    return float(np.clip(0.25 * bright + 0.55 * mid + 0.50 * high, 0.0, 1.0))


def analyze_clip(audio: np.ndarray, rms: float) -> dict:
    """Combine loudness + spectral harshness into paint intensity."""
    if rms < SILENCE_RMS:
        intensity = 0.04
        harshness = 0.0
        loud = 0.0
    else:
        peak = peak_window_rms(audio)
        loud = loudness_01(rms, peak)
        centroid, mid, high = spectral_profile(audio)
        harshness = harshness_01(centroid, mid, high)
        # Independent axes so similar volumes can still diverge by tone
        intensity = float(np.clip(0.48 * loud + 0.52 * harshness, 0.0, 1.0))

    label = next(name for thresh, name in LABEL_BY_INTENSITY if intensity < thresh)
    color = intensity_to_hex(intensity)
    stress = intensity
    score = 1.0

    return {
        "label": label,
        "score": score,
        "color": color,
        "identified": True,
        "rms": float(rms),
        "stress": float(stress),
        "intensity": float(intensity),
        "harshness": float(harshness),
        "loudness": float(loud),
        "top": [
            {"label": label, "score": score},
            {"label": "loudness", "score": float(loud)},
            {"label": "harshness", "score": float(harshness)},
        ],
    }


def intensity_to_hex(t: float) -> str:
    t = float(np.clip(t, 0.0, 1.0))
    for i in range(len(INTENSITY_STOPS) - 1):
        t0, c0 = INTENSITY_STOPS[i]
        t1, c1 = INTENSITY_STOPS[i + 1]
        if t0 <= t <= t1:
            u = (t - t0) / (t1 - t0 or 1.0)
            r = int(c0[0] + (c1[0] - c0[0]) * u)
            g = int(c0[1] + (c1[1] - c0[1]) * u)
            b = int(c0[2] + (c1[2] - c0[2]) * u)
            return f"#{r:02X}{g:02X}{b:02X}"
    r, g, b = INTENSITY_STOPS[-1][1]
    return f"#{r:02X}{g:02X}{b:02X}"


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mode": "loudness+frequency",
        "spots": db.spot_count(),
    }


@app.get("/api/config")
def config():
    """Public client config. CARTO key is required in tile URLs by the basemap CDN."""
    return {
        "cartoApiKey": os.environ.get("CARTO_API_KEY") or os.environ.get("CARTO_KEY") or "",
    }


# NYC Open Data 311 — outdoor / transit noise in Manhattan
# https://data.cityofnewyork.us/Social-Services/311-Service-Requests-from-2020-to-Present/erm2-nwe9
NYC311_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
NOISE_TYPES = {
    "all": None,  # street + vehicle + helicopter
    "street": "Noise - Street/Sidewalk",
    "vehicle": "Noise - Vehicle",
    "helicopter": "Noise - Helicopter",
}
NOISE_TYPES_ALL = tuple(v for k, v in NOISE_TYPES.items() if v)
_manhattan_bbox = {
    "south": 40.698,
    "north": 40.878,
    "west": -74.022,
    "east": -73.906,
}

CACHE_311_PATH = ROOT / "data" / "311_noise_cache.json"
DEFAULT_311_DAYS = 90
DEFAULT_311_LIMIT = 6000
_311_REFRESH_SEC = 30 * 60
_311_lock = threading.Lock()
_311_payload: dict | None = None
_311_fetched_at: float = 0.0


def _fetch_311_from_nyc(days: int, kind: str, limit: int) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    bb = _manhattan_bbox
    clauses = [
        "latitude IS NOT NULL",
        "longitude IS NOT NULL",
        "borough='MANHATTAN'",
        f"created_date >= '{since}'",
        f"latitude between {bb['south']} and {bb['north']}",
        f"longitude between {bb['west']} and {bb['east']}",
    ]
    exact = NOISE_TYPES.get(kind)
    if exact:
        clauses.append(f"complaint_type='{exact}'")
    else:
        listed = ", ".join(f"'{t}'" for t in NOISE_TYPES_ALL)
        clauses.append(f"complaint_type in ({listed})")

    params = {
        "$select": "unique_key,created_date,complaint_type,descriptor,latitude,longitude",
        "$where": " AND ".join(clauses),
        "$order": "created_date DESC",
        "$limit": str(limit),
    }
    url = NYC311_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"User-Agent": "fresque-sonore/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        rows = json.loads(resp.read().decode())

    points = []
    for row in rows:
        try:
            lat = float(row["latitude"])
            lng = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        points.append(
            {
                "id": row.get("unique_key"),
                "lat": lat,
                "lng": lng,
                "type": row.get("complaint_type") or "Noise",
                "descriptor": row.get("descriptor") or "",
                "date": row.get("created_date") or "",
            }
        )

    return {
        "source": "https://data.cityofnewyork.us/resource/erm2-nwe9.json",
        "filters": {"borough": "MANHATTAN", "days": days, "kind": kind, "limit": limit},
        "count": len(points),
        "points": points,
        "kinds": list(NOISE_TYPES.keys()),
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_311_disk(payload: dict) -> None:
    try:
        CACHE_311_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_311_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(CACHE_311_PATH)
    except Exception as exc:
        print(f"311 disk cache write skipped: {exc}")


def _load_311_disk() -> dict | None:
    if not CACHE_311_PATH.exists():
        return None
    try:
        return json.loads(CACHE_311_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"311 disk cache read failed: {exc}")
        return None


def _set_311_payload(payload: dict) -> None:
    global _311_payload, _311_fetched_at
    with _311_lock:
        _311_payload = payload
        _311_fetched_at = time.time()


def refresh_311_cache(force: bool = False) -> dict | None:
    """Fetch from NYC Open Data; update memory + disk. Never called on the request path."""
    if (
        not force
        and _311_payload is not None
        and (time.time() - _311_fetched_at) < _311_REFRESH_SEC
    ):
        return _311_payload
    try:
        print("Refreshing 311 noise cache from NYC Open Data…")
        payload = _fetch_311_from_nyc(DEFAULT_311_DAYS, "all", DEFAULT_311_LIMIT)
        _set_311_payload(payload)
        _save_311_disk(payload)
        print(f"311 cache ready: {payload['count']} complaints")
        return payload
    except Exception as exc:
        print(f"311 refresh failed: {exc}")
        return _311_payload


def _311_refresh_loop() -> None:
    while True:
        time.sleep(_311_REFRESH_SEC)
        refresh_311_cache(force=True)


def bootstrap_311_cache() -> None:
    """Serve instantly from disk; refresh NYC data in the background forever."""
    disk = _load_311_disk()
    if disk and isinstance(disk.get("points"), list):
        _set_311_payload(disk)
        print(f"311 cache loaded from disk: {disk.get('count', len(disk['points']))} complaints")
    threading.Thread(
        target=lambda: refresh_311_cache(force=True), daemon=True, name="311-warm"
    ).start()
    threading.Thread(target=_311_refresh_loop, daemon=True, name="311-refresh").start()


@app.get("/api/311/noise")
def noise_311(
    days: int = Query(DEFAULT_311_DAYS, ge=7, le=365),
    kind: str = Query("all"),
    limit: int = Query(DEFAULT_311_LIMIT, ge=100, le=10000),
):
    """Return preloaded Manhattan street / vehicle / helicopter noise complaints."""
    kind_key = (kind or "all").lower().strip()
    if kind_key not in NOISE_TYPES:
        raise HTTPException(400, f"Unknown kind. Use one of: {', '.join(NOISE_TYPES)}")

    with _311_lock:
        payload = _311_payload

    if payload is None:
        threading.Thread(
            target=lambda: refresh_311_cache(force=True), daemon=True
        ).start()
        return {
            "source": NYC311_URL,
            "filters": {
                "borough": "MANHATTAN",
                "days": days,
                "kind": kind_key,
                "limit": limit,
            },
            "count": 0,
            "points": [],
            "kinds": list(NOISE_TYPES.keys()),
            "warming": True,
        }

    points = list(payload.get("points") or [])
    if kind_key != "all":
        want = NOISE_TYPES[kind_key]
        points = [p for p in points if p.get("type") == want]
    if limit < len(points):
        points = points[:limit]

    return {
        **payload,
        "points": points,
        "count": len(points),
        "filters": {
            "borough": "MANHATTAN",
            "days": payload.get("filters", {}).get("days", days),
            "kind": kind_key,
            "limit": limit,
        },
    }


class SpotIn(BaseModel):
    lat: float
    lng: float
    label: str
    score: float | None = None
    stress: float | None = None
    intensity: float | None = None
    rms: float | None = None
    color: str | None = None
    contributor_name: str | None = None
    id: str | None = None


class SpotBatchIn(BaseModel):
    spots: list[SpotIn] = Field(default_factory=list)


@app.get("/api/spots")
def get_spots():
    return {"spots": db.list_spots(), "count": db.spot_count()}


@app.post("/api/spots")
def create_spot(spot: SpotIn):
    try:
        saved = db.add_spot(
            spot_id=spot.id,
            lat=spot.lat,
            lng=spot.lng,
            label=spot.label,
            score=spot.score,
            stress=spot.stress,
            intensity=spot.intensity,
            rms=spot.rms,
            color=spot.color,
            contributor_name=spot.contributor_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Could not save spot: {exc}") from exc
    return saved


@app.post("/api/spots/import")
def import_spots(batch: SpotBatchIn):
    """Migrate browser localStorage spots into the shared database."""
    payload = [s.model_dump() for s in batch.spots]
    n = db.import_spots(payload)
    return {"imported": n, "count": db.spot_count()}


@app.get("/api/palette")
def palette():
    return {
        "mode": "loudness+frequency",
        "labels": [name for _, name in LABEL_BY_INTENSITY],
        "high_freq_hz": HIGH_FREQ_HZ,
    }


@app.post("/api/classify")
async def classify(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty audio file")

    try:
        audio, rms = load_audio_mono_16k(raw, filename=file.filename or "")
    except Exception as exc:
        raise HTTPException(400, f"Could not decode audio: {exc}") from exc

    if audio.size < 1600:  # < 0.1 s
        raise HTTPException(400, "Audio too short")

    try:
        return analyze_clip(audio, rms)
    except Exception as exc:
        raise HTTPException(500, f"Analysis failed: {exc}") from exc


@app.get("/")
def index():
    """Serve the map with the CARTO key embedded so tiles never load without it."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    key = os.environ.get("CARTO_API_KEY") or os.environ.get("CARTO_KEY") or ""
    inject = f"<script>window.__CARTO_API_KEY__={key!r};</script>"
    if "</head>" in html:
        html = html.replace("</head>", inject + "\n</head>", 1)
    else:
        html = inject + html
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8765, reload=True)
