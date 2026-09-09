"""SQLite persistence for the shared Manhattan fresco.

Ready for future multi-user registration and multiple maps.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "fresque.db"

MANHATTAN_MAP_ID = "manhattan"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS maps (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS spots (
                id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL REFERENCES maps(id) ON DELETE CASCADE,
                user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                contributor_name TEXT,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                label TEXT NOT NULL,
                score REAL,
                stress REAL,
                intensity REAL,
                rms REAL,
                color TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_spots_map ON spots(map_id);
            CREATE INDEX IF NOT EXISTS idx_spots_created ON spots(created_at);
            """
        )
        row = conn.execute(
            "SELECT id FROM maps WHERE id = ?", (MANHATTAN_MAP_ID,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO maps (id, slug, title, created_at) VALUES (?, ?, ?, ?)",
                (MANHATTAN_MAP_ID, "manhattan", "Silence in NYC", utc_now()),
            )


def list_maps() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, slug, title, created_at FROM maps ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def list_spots(map_id: str = MANHATTAN_MAP_ID) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, map_id, user_id, contributor_name, lat, lng, label,
                   score, stress, intensity, rms, color, created_at
            FROM spots
            WHERE map_id = ?
            ORDER BY created_at ASC
            """,
            (map_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_spot(
    *,
    lat: float,
    lng: float,
    label: str,
    score: float | None = None,
    stress: float | None = None,
    intensity: float | None = None,
    rms: float | None = None,
    color: str | None = None,
    contributor_name: str | None = None,
    user_id: str | None = None,
    map_id: str = MANHATTAN_MAP_ID,
    spot_id: str | None = None,
) -> dict[str, Any]:
    sid = spot_id or str(uuid.uuid4())
    created = utc_now()
    with connect() as conn:
        m = conn.execute("SELECT id FROM maps WHERE id = ?", (map_id,)).fetchone()
        if not m:
            raise ValueError(f"Unknown map: {map_id}")
        conn.execute(
            """
            INSERT INTO spots (
                id, map_id, user_id, contributor_name, lat, lng, label,
                score, stress, intensity, rms, color, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                map_id,
                user_id,
                (contributor_name or "").strip() or None,
                lat,
                lng,
                label,
                score,
                stress,
                intensity,
                rms,
                color,
                created,
            ),
        )
    return {
        "id": sid,
        "map_id": map_id,
        "user_id": user_id,
        "contributor_name": contributor_name,
        "lat": lat,
        "lng": lng,
        "label": label,
        "score": score,
        "stress": stress,
        "intensity": intensity,
        "rms": rms,
        "color": color,
        "created_at": created,
    }


def import_spots(spots: list[dict[str, Any]], map_id: str = MANHATTAN_MAP_ID) -> int:
    """Bulk import (e.g. migrate from browser localStorage). Skips duplicate ids."""
    n = 0
    for s in spots:
        try:
            add_spot(
                spot_id=s.get("id"),
                lat=float(s["lat"]),
                lng=float(s["lng"]),
                label=str(s["label"]),
                score=s.get("score"),
                stress=s.get("stress"),
                intensity=s.get("intensity"),
                rms=s.get("rms"),
                color=s.get("color"),
                contributor_name=s.get("contributor_name"),
                map_id=map_id,
            )
            n += 1
        except sqlite3.IntegrityError:
            continue
    return n


def delete_spot(spot_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM spots WHERE id = ?", (spot_id,))
        return cur.rowcount > 0


def update_spot_analysis(
    spot_id: str,
    *,
    label: str,
    score: float | None,
    stress: float | None,
    intensity: float | None,
    rms: float | None,
    color: str | None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE spots
            SET label = ?, score = ?, stress = ?, intensity = ?, rms = ?, color = ?
            WHERE id = ?
            """,
            (label, score, stress, intensity, rms, color, spot_id),
        )


def spot_count(map_id: str = MANHATTAN_MAP_ID) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM spots WHERE map_id = ?", (map_id,)
        ).fetchone()
    return int(row["n"]) if row else 0
