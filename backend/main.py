"""Japan Quake Monitor — backend v2.

FastAPI + SQLite service that:
  * collects JMA earthquake reports (P2PQuake JSON API v2, /history) and
    USGS events into a local SQLite database (history that outlives the
    browser's localStorage),
  * serves the dashboard (index.html) and a JSON API for history,
    statistics and CSV export,
  * stays well inside the documented rate limits (P2PQuake /history:
    60 req/min per IP; USGS: 1 req/s is fine) — 45 s JMA + 60 s USGS polling.

Run:  DATA_DIR=./data python main.py   →  http://localhost:8000
"""
import asyncio
import csv
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

# ---------------------------------------------------------------- settings
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", "static"))
DB_PATH = DATA_DIR / "quakes.db"
P2P_HISTORY = "https://api.p2pquake.net/v2/history?codes=551&limit=100"
USGS_DAY = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
USGS_HOUR = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
P2P_INTERVAL = float(os.environ.get("P2P_INTERVAL_S", "45"))
USGS_INTERVAL = float(os.environ.get("USGS_INTERVAL_S", "60"))
USGS_DAY_INTERVAL = float(os.environ.get("USGS_DAY_INTERVAL_S", "600"))
HTTP_TIMEOUT = 15.0

BOUNDS = dict(minLat=24, maxLat=46, minLon=122, maxLon=154)
SHINDO = {10: "1", 20: "2", 30: "3", 40: "4", 45: "5-", 50: "5+", 55: "6-", 60: "6+", 70: "7"}
JST = timezone(timedelta(hours=9))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jqm")

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id          TEXT PRIMARY KEY,
  source      TEXT NOT NULL,
  time        TEXT NOT NULL,
  lat         REAL, lon REAL, depth REAL,
  mag         REAL,
  max_scale   INTEGER,
  shindo      TEXT,
  place       TEXT,
  tsunami     TEXT,
  correct     INTEGER NOT NULL DEFAULT 0,
  url         TEXT,
  pts         TEXT NOT NULL DEFAULT '[]',
  prefs       TEXT NOT NULL DEFAULT '{}',
  raw         TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(time);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)


def set_meta(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )


def get_meta(key: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None


# ---------------------------------------------------------------- parsers
def jma_time_to_iso(t: str) -> str | None:
    m = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", t or "")
    if not m:
        return None
    y, mo, d, h, mi, s, frac = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=JST)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_p2p(msg: dict) -> dict | None:
    if not isinstance(msg, dict) or msg.get("code") != 551 or msg.get("test"):
        return None
    eq = msg.get("earthquake") or {}
    h = eq.get("hypocenter") or {}
    if not h or h.get("latitude") == -200 or h.get("longitude") == -200:
        return None
    iso = jma_time_to_iso(eq.get("time"))
    if not iso:
        return None
    pts = sorted(
        (p for p in (msg.get("points") or []) if p.get("addr") and p.get("scale")),
        key=lambda p: p["scale"], reverse=True,
    )[:40]
    prefs: dict[str, int] = {}
    for p in pts:
        pref = p.get("pref") or "?"
        prefs[pref] = max(prefs.get(pref, 0), p["scale"])
    issue = msg.get("issue") or {}
    return {
        "id": "p2p-" + str(msg.get("id")),
        "source": "JMA",
        "time": iso,
        "lat": h.get("latitude"),
        "lon": h.get("longitude"),
        "depth": h.get("depth"),
        "mag": h.get("magnitude") if isinstance(h.get("magnitude"), (int, float)) else None,
        "max_scale": eq.get("maxScale"),
        "shindo": SHINDO.get(eq.get("maxScale")),
        "place": h.get("name") or "—",
        "tsunami": eq.get("domesticTsunami") or "Unknown",
        "correct": 1 if str(issue.get("correct", "None")) != "None" else 0,
        "url": None,
        "pts": [{"a": p["addr"], "s": SHINDO.get(p["scale"], "?")} for p in pts],
        "prefs": prefs,
        "raw": msg,
    }


def parse_usgs(f: dict) -> dict | None:
    try:
        lon, lat, depth = f["geometry"]["coordinates"]
    except (KeyError, TypeError, ValueError):
        return None
    if not (BOUNDS["minLat"] <= lat <= BOUNDS["maxLat"] and BOUNDS["minLon"] <= lon <= BOUNDS["maxLon"]):
        return None
    p = f.get("properties") or {}
    try:
        iso = datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (KeyError, TypeError, ValueError):
        return None
    mag = p.get("mag") if isinstance(p.get("mag"), (int, float)) else None
    return {
        "id": "usgs-" + str(f.get("id")),
        "source": "USGS",
        "time": iso,
        "lat": lat, "lon": lon, "depth": depth,
        "mag": mag,
        "max_scale": None,
        "shindo": None,
        "place": p.get("place") or "—",
        "tsunami": "Warning" if p.get("tsunami") == 1 else None,
        "correct": 0,
        "url": p.get("url"),
        "pts": [],
        "prefs": {},
        "raw": f,
    }


def upsert(events: list[dict]) -> tuple[int, int]:
    """Insert new rows / refresh richer fields on known ids. Returns (new, updated)."""
    new = upd = 0
    with db() as conn:
        for e in events:
            cur = conn.execute("SELECT id FROM events WHERE id=?", (e["id"],))
            conn.execute(
                """INSERT INTO events(id, source, time, lat, lon, depth, mag, max_scale, shindo,
                                      place, tsunami, correct, url, pts, prefs, raw, ingested_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     mag=excluded.mag, max_scale=excluded.max_scale, shindo=excluded.shindo,
                     place=excluded.place, tsunami=excluded.tsunami, correct=excluded.correct,
                     pts=excluded.pts, prefs=excluded.prefs, raw=excluded.raw""",
                (e["id"], e["source"], e["time"], e["lat"], e["lon"], e["depth"], e["mag"],
                 e["max_scale"], e["shindo"], e["place"], e["tsunami"], e["correct"], e["url"],
                 json.dumps(e["pts"], ensure_ascii=False), json.dumps(e["prefs"], ensure_ascii=False),
                 json.dumps(e["raw"], ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()),
            )
            if cur.fetchone():
                upd += 1
            else:
                new += 1
    return new, upd


# ---------------------------------------------------------------- collector
async def fetch_json(client: httpx.AsyncClient, url: str):
    r = await client.get(url)
    r.raise_for_status()
    return r.json()


async def collect_p2p(client: httpx.AsyncClient) -> None:
    arr = await fetch_json(client, P2P_HISTORY)
    events = [e for e in (parse_p2p(m) for m in (arr if isinstance(arr, list) else [])) if e]
    new, upd = upsert(events)
    set_meta("last_p2p_ok", datetime.now(timezone.utc).isoformat())
    log.info("P2PQuake: %d message(s) → %d new, %d refreshed", len(events), new, upd)


async def collect_usgs(client: httpx.AsyncClient, url: str, label: str) -> None:
    j = await fetch_json(client, url)
    events = [e for e in (parse_usgs(f) for f in (j.get("features") or [])) if e]
    new, upd = upsert(events)
    set_meta("last_usgs_ok", datetime.now(timezone.utc).isoformat())
    log.info("USGS %s: %d event(s) in Japan box → %d new, %d refreshed", label, len(events), new, upd)


async def loop_p2p(client: httpx.AsyncClient) -> None:
    while True:
        try:
            await collect_p2p(client)
        except Exception as exc:
            log.warning("P2PQuake collect failed: %s", exc)
        await asyncio.sleep(P2P_INTERVAL)


async def loop_usgs(client: httpx.AsyncClient, url: str, label: str, interval: float) -> None:
    while True:
        try:
            await collect_usgs(client, url, label)
        except Exception as exc:
            log.warning("USGS %s collect failed: %s", label, exc)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- API rows
def row_to_event(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "source": r["source"],
        "time": r["time"],
        "lat": r["lat"], "lon": r["lon"], "depth": r["depth"],
        "mag": r["mag"],
        "place": r["place"],
        "shindo": r["shindo"],
        "tsunami": r["tsunami"],
        "correct": bool(r["correct"]),
        "url": r["url"],
        "pts": json.loads(r["pts"]),
        "maxScale": (r["max_scale"] or 0) or None,
    }


def query_events(days: float, limit: int, minmag: float, source: str, q: str, offset: int):
    where, args = [], []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    where.append("time >= ?")
    args.append(since)
    if minmag > 0:
        where.append("mag IS NOT NULL AND mag >= ?")
        args.append(minmag)
    if source in ("JMA", "USGS"):
        where.append("source = ?")
        args.append(source)
    if q:
        where.append("place LIKE ?")
        args.append(f"%{q}%")
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM events" + cond, args).fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM events" + cond + " ORDER BY time DESC, id LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
    return total, rows


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": "jqm-backend/2.0"}) as client:
        tasks = [
            asyncio.create_task(loop_p2p(client)),
            asyncio.create_task(loop_usgs(client, USGS_HOUR, "hour", USGS_INTERVAL)),
            asyncio.create_task(loop_usgs(client, USGS_DAY, "day", USGS_DAY_INTERVAL)),
        ]
        yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Japan Quake Monitor API", version="2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def index():
    f = STATIC_DIR / "index.html"
    if f.is_file():
        return FileResponse(f, headers={"Cache-Control": "no-cache"})
    return Response("dashboard not bundled — serve index.html separately", status_code=404)


@app.get("/api/healthz")
def healthz():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        last = conn.execute("SELECT time FROM events ORDER BY time DESC LIMIT 1").fetchone()
        first = conn.execute("SELECT time FROM events ORDER BY time ASC LIMIT 1").fetchone()
    return {
        "ok": True,
        "service": "japan-quake-monitor-backend",
        "events_total": total,
        "history_since": first["time"] if first else None,
        "last_event": last["time"] if last else None,
        "last_ingest_p2p": get_meta("last_p2p_ok"),
        "last_ingest_usgs": get_meta("last_usgs_ok"),
        "db_size_mb": round(DB_PATH.stat().st_size / 1e6, 2) if DB_PATH.exists() else 0,
    }


@app.get("/api/events")
def events(
    days: float = Query(30, gt=0, le=3650),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    minmag: float = Query(0, ge=0, le=10),
    source: str = Query("all", pattern="^(all|JMA|USGS)$"),
    q: str = Query("", max_length=100),
):
    total, rows = query_events(days, limit, minmag, source, q, offset)
    return {"total": total, "count": len(rows), "events": [row_to_event(r) for r in rows]}


@app.get("/api/stats")
def stats(days: float = Query(30, gt=0, le=3650), minmag: float = Query(0, ge=0, le=10)):
    _, rows = query_events(days, limit=100000, minmag=minmag, source="all", q="", offset=0)
    by_day: dict[str, dict] = {}
    by_scale: dict[str, int] = {}
    by_pref: dict[str, dict] = {}
    strongest = None
    for r in rows:
        day = datetime.fromisoformat(r["time"].replace("Z", "+00:00")).astimezone(JST).date().isoformat()
        d = by_day.setdefault(day, {"date": day, "count": 0, "max_mag": None})
        d["count"] += 1
        if r["mag"] is not None and (d["max_mag"] is None or r["mag"] > d["max_mag"]):
            d["max_mag"] = r["mag"]
        if r["shindo"]:
            by_scale[r["shindo"]] = by_scale.get(r["shindo"], 0) + 1
        if r["mag"] is not None and (strongest is None or r["mag"] > strongest["mag"]):
            strongest = row_to_event(r)
        for pref, scale in json.loads(r["prefs"]).items():
            bp = by_pref.setdefault(pref, {"pref": pref, "count": 0, "max_scale": 0})
            bp["count"] += 1
            bp["max_scale"] = max(bp["max_scale"], scale)
    return {
        "days": days,
        "total": len(rows),
        "by_day": sorted(by_day.values(), key=lambda x: x["date"]),
        "by_scale": dict(sorted(by_scale.items(), key=lambda kv: -int(kv[0][0] if kv[0][0].isdigit() else 0))),
        "by_pref": sorted(by_pref.values(), key=lambda x: -x["count"])[:10],
        "strongest": strongest,
    }


@app.get("/api/export.csv")
def export_csv(days: float = Query(365, gt=0, le=3650), minmag: float = Query(0, ge=0, le=10)):
    _, rows = query_events(days, limit=100000, minmag=minmag, source="all", q="", offset=0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "time_jst", "source", "magnitude", "max_scale", "shindo",
                "latitude", "longitude", "depth_km", "place", "tsunami", "corrected", "url"])
    for r in rows:
        t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
        w.writerow([r["time"], t.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST"), r["source"],
                    r["mag"], r["max_scale"], r["shindo"], r["lat"], r["lon"], r["depth"],
                    r["place"], r["tsunami"], bool(r["correct"]), r["url"]])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="japan-quakes-{stamp}.csv"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
