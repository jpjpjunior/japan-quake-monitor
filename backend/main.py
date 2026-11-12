"""Japan Quake Monitor — backend v3.

FastAPI + SQLite archive for JMA earthquake reports (relayed by P2PQuake)
and USGS events inside the Japan bounding box.

v3 changes vs v2:
  * connections are closed, not leaked (contextmanager; one per cycle/request)
  * timestamps stored as epoch-millis INTEGER — no string-comparison pitfalls
  * events (one row per upstream message) are linked to quakes (one row per
    physical quake), so JMA+USGS duplicates no longer inflate statistics
  * /api/events and /api/stats aggregate in SQL instead of loading 100k rows
  * JMA sentinels (-200 depth, -1 magnitude) become NULL, never rendered
  * raw upstream payloads kept 30 days in a compressed side table, then pruned
  * collector heartbeat lets /api/healthz report a dead poller

Run:  DATA_DIR=./data python main.py   ->  http://localhost:8000
"""
import asyncio
import csv
import io
import json
import logging
import os
import re
import sqlite3
import zlib
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
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
RAW_RETENTION_DAYS = float(os.environ.get("RAW_RETENTION_DAYS", "30"))
HTTP_TIMEOUT = 15.0

BOUNDS = dict(minLat=24, maxLat=46, minLon=122, maxLon=154)
SHINDO = {10: "1", 20: "2", 30: "3", 40: "4", 45: "5-", 50: "5+",
          55: "6-", 60: "6+", 70: "7"}
SHINDO_ORDER = {"1": 1, "2": 2, "3": 3, "4": 4, "5-": 5, "5+": 6,
                "6-": 7, "6+": 8, "7": 9}
JST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 3

# merge tolerances, mirrored from the dashboard
TWIN_WINDOW_MS = 30_000     # JMA quick report vs detailed report
TWIN_KM = 30
FRIEND_WINDOW_MS = 120_000  # JMA report vs the same quake from USGS
FRIEND_KM = 60

STARTED_AT = datetime.now(timezone.utc)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jqm")

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id          TEXT PRIMARY KEY,
  source      TEXT NOT NULL,
  time        TEXT NOT NULL,
  ts          INTEGER NOT NULL,
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
  quake_id    INTEGER,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_quake ON events(quake_id);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);

CREATE TABLE IF NOT EXISTS quakes(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  time             TEXT NOT NULL,
  ts               INTEGER NOT NULL,
  lat REAL, lon REAL, depth REAL, mag REAL,
  shindo           TEXT,
  max_scale        INTEGER,
  place            TEXT,
  tsunami          TEXT,
  correct          INTEGER NOT NULL DEFAULT 0,
  url              TEXT,
  primary_event_id TEXT NOT NULL,
  primary_source   TEXT NOT NULL,
  sources          TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_quakes_ts ON quakes(ts);

CREATE TABLE IF NOT EXISTS quake_prefs(
  quake_id INTEGER NOT NULL,
  pref     TEXT NOT NULL,
  scale    INTEGER NOT NULL,
  PRIMARY KEY (quake_id, pref)
);
CREATE INDEX IF NOT EXISTS idx_quake_prefs_pref ON quake_prefs(pref);

CREATE TABLE IF NOT EXISTS raw_messages(
  id     TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  body   BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_messages(ts);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def db():
    """Open a connection, commit on success, always close.

    Note: `with sqlite3.connect(...)` does NOT close the handle - it only
    commits. v2 leaked one file descriptor per helper call because of that.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        # migrate BEFORE the schema: SCHEMA builds indexes on events(ts) and
        # events(quake_id), which do not exist in a v2 archive yet
        migrate(conn)
        conn.executescript(SCHEMA)
        link_orphans(conn)


def migrate(conn: sqlite3.Connection) -> None:
    """v2 -> v3: add ts/quake_id to an existing archive and backfill.

    Runs BEFORE the schema is created, so a legacy archive gets its columns
    added before SCHEMA builds indexes on them. A fresh database has no
    events table yet — skip straight to stamping the version; SCHEMA will
    create everything in v3 form on the next line of init_db().
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "events" in tables:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
        if "ts" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN ts INTEGER")
            # stored times end in 'Z'; SQLite < 3.38 cannot parse that suffix, so
            # strip it (bare ISO strings are treated as UTC by date functions)
            conn.execute(
                "UPDATE events SET ts = CAST(strftime('%s', REPLACE(time, 'Z', '')) AS INTEGER) * 1000 "
                "WHERE ts IS NULL"
            )
        if "quake_id" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN quake_id INTEGER")
        log.info("migrated archive to schema v%d", SCHEMA_VERSION)
    conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )


def get_meta(conn, key: str):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else None


# ---------------------------------------------------------------- geo / time
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))


def iso_to_ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def ts_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jma_time_to_iso(t: str):
    m = re.fullmatch(
        r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", t or ""
    )
    if not m:
        return None
    y, mo, d, h, mi, s, frac = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro, tzinfo=JST)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- parsers
def parse_p2p(msg: dict):
    if not isinstance(msg, dict) or msg.get("code") != 551 or msg.get("test"):
        return None
    mid = msg.get("id") or msg.get("_id")
    if not mid:
        return None
    eq = msg.get("earthquake") or {}
    h = eq.get("hypocenter") or {}
    lat, lon = h.get("latitude"), h.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if lat == -200 or lon == -200:          # JMA "unknown epicentre"
        return None
    iso = jma_time_to_iso(eq.get("time"))
    if not iso:
        return None

    depth = h.get("depth")
    if not isinstance(depth, (int, float)) or depth < 0:   # -1 / -200 = unknown
        depth = None
    mag = h.get("magnitude")
    if not isinstance(mag, (int, float)) or mag <= 0:       # -1 = unknown
        mag = None

    pts = sorted(
        (p for p in (msg.get("points") or []) if p.get("addr") and p.get("scale")),
        key=lambda p: p["scale"], reverse=True,
    )[:40]
    prefs = {}
    for p in pts:
        pref = p.get("pref") or "?"
        prefs[pref] = max(prefs.get(pref, 0), p["scale"])

    issue = msg.get("issue") or {}
    corrected = issue.get("correct") not in (None, "None")
    return {
        "id": "p2p-" + str(mid),
        "source": "JMA",
        "time": iso,
        "ts": iso_to_ts(iso),
        "lat": lat, "lon": lon, "depth": depth, "mag": mag,
        "max_scale": eq.get("maxScale"),
        "shindo": SHINDO.get(eq.get("maxScale")),
        "place": h.get("name") or "\u2014",
        "tsunami": eq.get("domesticTsunami") or "Unknown",
        "correct": 1 if corrected else 0,
        "url": None,
        "pts": [{"a": p["addr"], "s": SHINDO.get(p["scale"], "?")} for p in pts],
        "prefs": prefs,
        "raw": msg,
    }


def parse_usgs(f: dict):
    try:
        lon, lat, depth = f["geometry"]["coordinates"]
    except (KeyError, TypeError, ValueError):
        return None
    if not (BOUNDS["minLat"] <= lat <= BOUNDS["maxLat"]
            and BOUNDS["minLon"] <= lon <= BOUNDS["maxLon"]):
        return None
    if not isinstance(depth, (int, float)) or depth < 0:
        depth = None
    p = f.get("properties") or {}
    try:
        ts = int(p["time"])
    except (KeyError, TypeError, ValueError):
        return None
    mag = p.get("mag") if isinstance(p.get("mag"), (int, float)) else None
    return {
        "id": "usgs-" + str(f.get("id")),
        "source": "USGS",
        "time": ts_to_iso(ts),
        "ts": ts,
        "lat": lat, "lon": lon, "depth": depth, "mag": mag,
        "max_scale": None,
        "shindo": None,
        "place": p.get("place") or "\u2014",
        "tsunami": "Warning" if p.get("tsunami") == 1 else None,
        "correct": 0,
        "url": p.get("url"),
        "pts": [],
        "prefs": {},
        "raw": f,
    }


# ---------------------------------------------------------------- merging
def find_quake(conn, e: dict):
    """Nearest quake within tolerance - nearest, not first."""
    if e.get("lat") is None or e.get("lon") is None:
        return None
    rows = conn.execute(
        "SELECT id, lat, lon FROM quakes WHERE ts BETWEEN ? AND ?",
        (e["ts"] - FRIEND_WINDOW_MS, e["ts"] + FRIEND_WINDOW_MS),
    ).fetchall()
    best, best_d = None, None
    for r in rows:
        if r["lat"] is None or r["lon"] is None:
            continue
        d = haversine_km(e["lat"], e["lon"], r["lat"], r["lon"])
        if d <= FRIEND_KM and (best_d is None or d < best_d):
            best, best_d = r["id"], d
    return best


def refresh_quake(conn, qid: int) -> None:
    """Recompute the merged view from its member events (JMA wins)."""
    rows = conn.execute(
        "SELECT * FROM events WHERE quake_id=? ORDER BY (source='JMA') DESC, ts DESC",
        (qid,),
    ).fetchall()
    if not rows:
        conn.execute("DELETE FROM quakes WHERE id=?", (qid,))
        conn.execute("DELETE FROM quake_prefs WHERE quake_id=?", (qid,))
        return

    def pick(field):
        for r in rows:
            if r[field] not in (None, ""):
                return r[field]
        return None

    primary = rows[0]
    sources = sorted({r["source"] for r in rows})
    conn.execute(
        """UPDATE quakes SET time=?, ts=?, lat=?, lon=?, depth=?, mag=?, shindo=?,
                             max_scale=?, place=?, tsunami=?, correct=?, url=?,
                             primary_event_id=?, primary_source=?, sources=?
           WHERE id=?""",
        (primary["time"], primary["ts"], pick("lat"), pick("lon"), pick("depth"),
         pick("mag"), pick("shindo"), pick("max_scale"), pick("place"),
         pick("tsunami"), max(r["correct"] for r in rows), pick("url"),
         primary["id"], primary["source"], json.dumps(sources), qid),
    )

    prefs = {}
    for r in rows:
        if r["source"] != "JMA":
            continue
        for k, v in json.loads(r["prefs"]).items():
            prefs[k] = max(prefs.get(k, 0), v)
    conn.execute("DELETE FROM quake_prefs WHERE quake_id=?", (qid,))
    conn.executemany(
        "INSERT INTO quake_prefs(quake_id, pref, scale) VALUES(?,?,?)",
        [(qid, k, v) for k, v in prefs.items()],
    )


def upsert(conn, events: list) -> tuple:
    """Insert/refresh events, link them to quakes, refresh those quakes."""
    if not events:
        return 0, 0
    now = datetime.now(timezone.utc).isoformat()
    new = upd = 0
    touched = set()

    for e in events:
        qid = find_quake(conn, e)
        if qid is None:
            # lat/lon must be present NOW: find_quake skips quake rows with NULL
            # coordinates, so a bare INSERT would break same-batch twin merging
            conn.execute(
                "INSERT INTO quakes(time, ts, lat, lon, primary_event_id,"
                " primary_source, sources) VALUES(?,?,?,?,?,?,'[]')",
                (e["time"], e["ts"], e["lat"], e["lon"], e["id"], e["source"]),
            )
            qid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        existed = conn.execute("SELECT 1 FROM events WHERE id=?", (e["id"],)).fetchone()
        conn.execute(
            """INSERT INTO events(id, source, time, ts, lat, lon, depth, mag, max_scale,
                                  shindo, place, tsunami, correct, url, pts, prefs,
                                  quake_id, ingested_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 mag=excluded.mag, max_scale=excluded.max_scale, shindo=excluded.shindo,
                 depth=excluded.depth, place=excluded.place, tsunami=excluded.tsunami,
                 correct=excluded.correct, pts=excluded.pts, prefs=excluded.prefs,
                 quake_id=excluded.quake_id""",
            (e["id"], e["source"], e["time"], e["ts"], e["lat"], e["lon"], e["depth"],
             e["mag"], e["max_scale"], e["shindo"], e["place"], e["tsunami"],
             e["correct"], e["url"], json.dumps(e["pts"], ensure_ascii=False),
             json.dumps(e["prefs"], ensure_ascii=False), qid, now),
        )
        if existed:
            upd += 1
        else:
            new += 1

        conn.execute(
            "INSERT OR REPLACE INTO raw_messages(id, source, ts, body) VALUES(?,?,?,?)",
            (e["id"], e["source"], e["ts"],
             zlib.compress(json.dumps(e["raw"], ensure_ascii=False).encode("utf-8"), 6)),
        )
        touched.add(qid)

    for qid in touched:
        refresh_quake(conn, qid)
    return new, upd


def link_orphans(conn) -> None:
    """One-time: attach pre-v3 events to quakes."""
    rows = conn.execute(
        "SELECT * FROM events WHERE quake_id IS NULL ORDER BY ts ASC"
    ).fetchall()
    if not rows:
        return
    touched = set()
    for r in rows:
        e = dict(r)
        qid = find_quake(conn, e)
        if qid is None:
            # same as upsert(): coordinates must be present during the linking
            # pass, or later orphans of the same physical quake never merge
            conn.execute(
                "INSERT INTO quakes(time, ts, lat, lon, primary_event_id,"
                " primary_source, sources) VALUES(?,?,?,?,?,?,'[]')",
                (e["time"], e["ts"], e["lat"], e["lon"], e["id"], e["source"]),
            )
            qid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute("UPDATE events SET quake_id=? WHERE id=?", (qid, e["id"]))
        touched.add(qid)
    for qid in touched:
        refresh_quake(conn, qid)
    log.info("linked %d orphan event(s) into %d quake(s)", len(rows), len(touched))


def prune_raw(conn) -> None:
    cutoff = int((datetime.now(timezone.utc).timestamp() - RAW_RETENTION_DAYS * 86400) * 1000)
    cur = conn.execute("DELETE FROM raw_messages WHERE ts < ?", (cutoff,))
    if cur.rowcount:
        log.info("pruned %d raw message(s) older than %g days",
                 cur.rowcount, RAW_RETENTION_DAYS)


# ---------------------------------------------------------------- collector
def collect_p2p() -> None:
    with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": "jqm-backend/3.0"}) as c:
        r = c.get(P2P_HISTORY)
        r.raise_for_status()
        msgs = r.json()
    events = [e for e in (parse_p2p(m) for m in (msgs if isinstance(msgs, list) else [])) if e]
    with db() as conn:
        new, upd = upsert(conn, events)
        stamp = datetime.now(timezone.utc).isoformat()
        set_meta(conn, "last_p2p_ok", stamp)
        set_meta(conn, "last_p2p_beat", stamp)
    log.info("P2PQuake: %d message(s) -> %d new, %d refreshed", len(events), new, upd)


def collect_usgs(url: str, label: str, beat: str) -> None:
    with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": "jqm-backend/3.0"}) as c:
        r = c.get(url)
        r.raise_for_status()
        payload = r.json()
    events = [e for e in (parse_usgs(f) for f in (payload.get("features") or [])) if e]
    with db() as conn:
        new, upd = upsert(conn, events)
        stamp = datetime.now(timezone.utc).isoformat()
        set_meta(conn, "last_usgs_ok", stamp)
        set_meta(conn, "last_%s_beat" % beat, stamp)
    log.info("USGS %s: %d event(s) in Japan box -> %d new, %d refreshed",
             label, len(events), new, upd)


async def loop(name: str, fn, interval: float) -> None:
    """Run a blocking collector off the event loop; heartbeat every cycle."""
    while True:
        try:
            await asyncio.to_thread(fn)
        except Exception as exc:
            log.warning("%s collect failed: %s", name, exc)
            try:
                def _beat():
                    with db() as conn:
                        set_meta(conn, "last_%s_beat" % name,
                                 datetime.now(timezone.utc).isoformat())
                await asyncio.to_thread(_beat)
            except Exception:
                pass
        await asyncio.sleep(interval)


def prune_raw_conn() -> None:
    with db() as conn:
        prune_raw(conn)


async def loop_prune() -> None:
    while True:
        try:
            await asyncio.to_thread(prune_raw_conn)
        except Exception as exc:
            log.warning("prune failed: %s", exc)
        await asyncio.sleep(3600)


# ---------------------------------------------------------------- API shaping
def quake_to_event(r) -> dict:
    """Dashboard-shaped event, keyed by the primary upstream id so the
    browser can dedupe it against the live feed."""
    sources = json.loads(r["sources"] or "[]")
    others = [s for s in sources if s != r["primary_source"]]
    keys = r.keys()
    pts_raw = r["primary_pts"] if "primary_pts" in keys else None
    return {
        "id": r["primary_event_id"],
        "source": r["primary_source"],
        "time": r["time"],
        "lat": r["lat"], "lon": r["lon"], "depth": r["depth"],
        "mag": r["mag"],
        "place": r["place"],
        "shindo": r["shindo"],
        "maxScale": r["max_scale"],
        "tsunami": r["tsunami"],
        "correct": bool(r["correct"]),
        "url": r["url"],
        "also": others,
        "pts": json.loads(pts_raw)[:40] if pts_raw else [],
    }


def since_ms(days: float) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)


def build_where(days: float, minmag: float, source: str, q: str, table: str = "quakes"):
    """WHERE clause over the quakes table (or an alias of it).

    `table` is interpolated, never user input: pass the alias you joined with.
    """
    where = ["%s.ts >= ?" % table]
    args = [since_ms(days)]
    if minmag > 0:
        where.append("%s.mag IS NOT NULL AND %s.mag >= ?" % (table, table))
        args.append(minmag)
    if source in ("JMA", "USGS"):
        where.append(
            "EXISTS (SELECT 1 FROM events e WHERE e.quake_id = %s.id AND e.source = ?)"
            % table
        )
        args.append(source)
    if q:
        where.append("%s.place LIKE ?" % table)
        args.append("%%%s%%" % q)
    return " WHERE " + " AND ".join(where), args


def query_quakes(days: float, limit: int, minmag: float, source: str,
                 q: str, offset: int):
    # alias quakes as q in both statements: the LEFT JOIN aliases events as e,
    # and build_where's source-filter subquery reuses that alias safely
    cond, args = build_where(days, minmag, source, q, table="q")
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM quakes q" + cond, args).fetchone()["c"]
        # LEFT JOIN restores the 震度 point list of the primary event (v2 parity)
        rows = conn.execute(
            "SELECT q.*, e.pts AS primary_pts FROM quakes q"
            " LEFT JOIN events e ON e.id = q.primary_event_id" + cond +
            " ORDER BY q.ts DESC, q.id LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
    return total, rows


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(init_db)
    tasks = [
        asyncio.create_task(loop("p2p", collect_p2p, P2P_INTERVAL)),
        asyncio.create_task(
            loop("usgs", lambda: collect_usgs(USGS_HOUR, "hour", "usgs"), USGS_INTERVAL)),
        asyncio.create_task(
            loop("usgs_day", lambda: collect_usgs(USGS_DAY, "day", "usgs_day"),
                 USGS_DAY_INTERVAL)),
        asyncio.create_task(loop_prune()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)   # wait before shutdown


app = FastAPI(title="Japan Quake Monitor API", version="3.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
                   allow_headers=["*"])


@app.get("/")
def index():
    f = STATIC_DIR / "index.html"
    if f.is_file():
        return FileResponse(f, headers={"Cache-Control": "no-cache"})
    return Response("dashboard not bundled - serve index.html separately", status_code=404)


_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@app.get("/assets/{name}")
def asset(name: str):
    """Static assets (the alert chime) for backend-served dashboards; the
    nginx image serves /assets/ from its own docroot."""
    if not _ASSET_NAME.match(name):
        raise HTTPException(status_code=404)
    f = STATIC_DIR / "assets" / name
    if not f.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(f, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/livez")
def livez():
    """Liveness: no database access, so a transient lock cannot kill the pod."""
    return {"ok": True}


@app.get("/api/healthz")
def healthz():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM quakes").fetchone()["c"]
        rows = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        last = conn.execute("SELECT MAX(ts) t FROM quakes").fetchone()["t"]
        first = conn.execute("SELECT MIN(ts) t FROM quakes").fetchone()["t"]
        beats = {k: get_meta(conn, "last_%s_beat" % k)
                 for k in ("p2p", "usgs", "usgs_day")}
        last_ok = {"p2p": get_meta(conn, "last_p2p_ok"),
                   "usgs": get_meta(conn, "last_usgs_ok")}

    # per-collector staleness: a collector is stale after missing 5 of ITS OWN
    # intervals (usgs_day beats every 600s, so a single global window would
    # flag a perfectly healthy collector every 5th minute)
    intervals = {"p2p": P2P_INTERVAL, "usgs": USGS_INTERVAL, "usgs_day": USGS_DAY_INTERVAL}
    now = datetime.now(timezone.utc)
    uptime = (now - STARTED_AT).total_seconds()
    stale = {}
    for k, v in beats.items():
        stale_after = 5 * intervals.get(k, USGS_INTERVAL)
        if not v:
            # no beat yet: not stale while the first cycles are still due
            stale[k] = uptime > stale_after
            continue
        age = (now - datetime.fromisoformat(v)).total_seconds()
        stale[k] = age > stale_after

    return {
        "ok": not any(stale.values()),
        "service": "japan-quake-monitor-backend",
        "schema_version": SCHEMA_VERSION,
        "quakes_total": total,
        "events_total": rows,
        "history_since": ts_to_iso(first) if first else None,
        "last_event": ts_to_iso(last) if last else None,
        "last_ingest_p2p": last_ok["p2p"],
        "last_ingest_usgs": last_ok["usgs"],
        "collector_beats": beats,
        "collector_stale": stale,
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
    total, rows = query_quakes(days, limit, minmag, source, q, offset)
    return {"total": total, "count": len(rows), "events": [quake_to_event(r) for r in rows]}


@app.get("/api/stats")
def stats(days: float = Query(30, gt=0, le=3650),
          minmag: float = Query(0, ge=0, le=10)):
    cond, args = build_where(days, minmag, "all", "")
    with db() as conn:
        by_day = [
            {"date": r["day"], "count": r["c"], "max_mag": r["mx"]}
            for r in conn.execute(
                "SELECT date(ts/1000, 'unixepoch', '+9 hours') AS day,"
                " COUNT(*) c, MAX(mag) mx FROM quakes" + cond +
                " GROUP BY day ORDER BY day", args)
        ]
        scale_rows = conn.execute(
            "SELECT shindo, COUNT(*) c FROM quakes" + cond +
            " AND shindo IS NOT NULL GROUP BY shindo", args).fetchall()
        by_scale = {r["shindo"]: r["c"] for r in sorted(
            scale_rows, key=lambda r: SHINDO_ORDER.get(r["shindo"], 0))}
        pcond, pargs = build_where(days, minmag, "all", "", table="q")
        by_pref = [
            {"pref": r["pref"], "count": r["c"], "max_scale": r["s"]}
            for r in conn.execute(
                "SELECT p.pref, COUNT(*) c, MAX(p.scale) s FROM quake_prefs p"
                " JOIN quakes q ON q.id = p.quake_id" + pcond +
                " GROUP BY p.pref ORDER BY c DESC LIMIT 10", pargs)
        ]
        strongest = conn.execute(
            "SELECT * FROM quakes" + cond +
            " AND mag IS NOT NULL ORDER BY mag DESC LIMIT 1", args).fetchone()
        total = conn.execute("SELECT COUNT(*) c FROM quakes" + cond, args).fetchone()["c"]
    return {
        "days": days,
        "total": total,
        "by_day": by_day,
        "by_scale": by_scale,
        "by_pref": by_pref,
        "strongest": quake_to_event(strongest) if strongest else None,
    }


@app.get("/api/export.csv")
def export_csv(days: float = Query(365, gt=0, le=3650),
               minmag: float = Query(0, ge=0, le=10)):
    cond, args = build_where(days, minmag, "all", "")
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM quakes" + cond + " ORDER BY ts DESC", args).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "time_jst", "sources", "magnitude", "max_scale", "shindo",
                "latitude", "longitude", "depth_km", "place", "tsunami",
                "corrected", "url"])
    for r in rows:
        jst = datetime.fromtimestamp(r["ts"] / 1000, tz=JST).strftime("%Y-%m-%d %H:%M:%S")
        w.writerow([ts_to_iso(r["ts"]), jst + " JST",
                    "+".join(json.loads(r["sources"] or "[]")),
                    r["mag"], r["max_scale"], r["shindo"], r["lat"], r["lon"],
                    r["depth"], r["place"], r["tsunami"], bool(r["correct"]), r["url"]])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="japan-quakes-%s.csv"' % stamp},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True)
