"""Tests for the two upstream parsers and the JMA/USGS merge.

Run from the repo root:  python -m pytest backend -q

These are the cheapest insurance against the failure modes that actually bite:
sentinel values leaking into the UI, and one quake being counted twice because
two networks reported it.
"""
import json

import pytest

import main


# ---------------------------------------------------------------- fixtures
def p2p(**over):
    msg = {
        "code": 551, "id": "ABC123", "test": False,
        "earthquake": {
            "time": "2026/11/12 09:30:00",
            "maxScale": 45,
            "domesticTsunami": "None",
            "hypocenter": {
                "name": "Fukushima-oki", "latitude": 37.4, "longitude": 141.6,
                "depth": 50, "magnitude": 5.2,
            },
        },
        "issue": {"correct": "None"},
        "points": [
            {"addr": "Iwaki", "pref": "Fukushima", "scale": 45},
            {"addr": "Sendai", "pref": "Miyagi", "scale": 30},
        ],
    }
    msg.update(over)
    return msg


def usgs(**over):
    f = {
        "id": "us7000abcd",
        "geometry": {"coordinates": [141.6, 37.4, 50.0]},
        "properties": {
            "time": 1762903200000, "mag": 5.2, "place": "50 km E of Iwaki, Japan",
            "tsunami": 0, "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000abcd",
        },
    }
    f.update(over)
    return f


# ---------------------------------------------------------------- JMA parser
def test_time_is_converted_from_jst_to_utc():
    e = main.parse_p2p(p2p())
    assert e["time"] == "2026-11-12T00:30:00Z"
    assert e["ts"] == main.iso_to_ts("2026-11-12T00:30:00Z")


def test_fractional_seconds_are_truncated_to_microseconds():
    assert main.jma_time_to_iso("2026/11/12 09:30:00.1234567") == "2026-11-12T00:30:00Z"


def test_unknown_depth_sentinel_becomes_none():
    msg = p2p()
    msg["earthquake"]["hypocenter"]["depth"] = -200
    assert main.parse_p2p(msg)["depth"] is None


def test_negative_one_depth_becomes_none():
    msg = p2p()
    msg["earthquake"]["hypocenter"]["depth"] = -1
    assert main.parse_p2p(msg)["depth"] is None


def test_unknown_magnitude_sentinel_becomes_none():
    msg = p2p()
    msg["earthquake"]["hypocenter"]["magnitude"] = -1
    assert main.parse_p2p(msg)["mag"] is None


def test_unknown_epicentre_is_dropped():
    msg = p2p()
    msg["earthquake"]["hypocenter"]["latitude"] = -200
    assert main.parse_p2p(msg) is None


def test_missing_hypocenter_is_dropped():
    msg = p2p()
    del msg["earthquake"]["hypocenter"]
    assert main.parse_p2p(msg) is None


def test_non_numeric_coordinates_are_dropped():
    msg = p2p()
    msg["earthquake"]["hypocenter"]["latitude"] = "abc"
    assert main.parse_p2p(msg) is None


def test_shindo_and_prefectures_are_derived():
    e = main.parse_p2p(p2p())
    assert e["shindo"] == "5-"
    assert e["prefs"] == {"Fukushima": 45, "Miyagi": 30}
    assert e["pts"][0] == {"a": "Iwaki", "s": "5-"}


def test_corrected_issue_flag():
    msg = p2p()
    msg["issue"]["correct"] = "revised"
    assert main.parse_p2p(msg)["correct"] == 1


def test_test_messages_are_ignored():
    assert main.parse_p2p(p2p(test=True)) is None


def test_non_551_codes_are_ignored():
    assert main.parse_p2p(p2p(code=552)) is None


def test_id_falls_back_to_underscore_id():
    msg = p2p()
    del msg["id"]
    msg["_id"] = "MONGO1"
    assert main.parse_p2p(msg)["id"] == "p2p-MONGO1"


def test_missing_id_is_dropped():
    msg = p2p()
    del msg["id"]
    assert main.parse_p2p(msg) is None


# ---------------------------------------------------------------- USGS parser
def test_usgs_inside_box():
    e = main.parse_usgs(usgs())
    assert e["id"] == "usgs-us7000abcd"
    assert e["source"] == "USGS"


def test_usgs_outside_box_is_dropped():
    assert main.parse_usgs(usgs(geometry={"coordinates": [-70.0, -30.0, 10]})) is None


def test_usgs_null_magnitude_survives():
    f = usgs()
    f["properties"]["mag"] = None
    assert main.parse_usgs(f)["mag"] is None


def test_usgs_tsunami_flag():
    f = usgs()
    f["properties"]["tsunami"] = 1
    assert main.parse_usgs(f)["tsunami"] == "Warning"


def test_usgs_missing_geometry_is_dropped():
    assert main.parse_usgs({"id": "x", "properties": {}}) is None


def test_both_sources_parse_to_the_same_shape():
    a, b = main.parse_p2p(p2p()), main.parse_usgs(usgs())
    for key in ("id", "source", "time", "ts", "lat", "lon", "depth", "mag",
                "shindo", "place", "tsunami", "correct", "pts", "prefs"):
        assert key in a, key
        assert key in b, key


# ---------------------------------------------------------------- merge
@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "quakes.db")
    main.init_db()
    return tmp_path


def test_jma_and_usgs_collapse_into_one_quake(tmpdb):
    jma = main.parse_p2p(p2p())
    usg = main.parse_usgs(usgs())
    usg["ts"] = jma["ts"] + 20_000           # 20 s later, as the two networks report
    usg["time"] = main.ts_to_iso(usg["ts"])
    with main.db() as conn:
        main.upsert(conn, [jma])
        main.upsert(conn, [usg])
        assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 2
        q = conn.execute("SELECT * FROM quakes").fetchall()
        assert len(q) == 1
        assert json.loads(q[0]["sources"]) == ["JMA", "USGS"]
        assert q[0]["primary_source"] == "JMA"
        assert q[0]["mag"] == pytest.approx(5.2)


def test_two_distant_quakes_stay_separate(tmpdb):
    a = main.parse_p2p(p2p())
    b = main.parse_p2p(p2p(id="XYZ"))
    b["lat"], b["lon"] = 30.0, 130.0
    with main.db() as conn:
        main.upsert(conn, [a, b])
        assert conn.execute("SELECT COUNT(*) c FROM quakes").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 2


def test_reserved_quake_id_is_stable_across_reupsert(tmpdb):
    e = main.parse_p2p(p2p())
    with main.db() as conn:
        main.upsert(conn, [e])
        first = conn.execute("SELECT id FROM quakes").fetchone()["id"]
        main.upsert(conn, [e])                 # same message again
        again = conn.execute("SELECT id FROM quakes").fetchone()["id"]
        assert first == again
        assert conn.execute("SELECT COUNT(*) c FROM quakes").fetchone()["c"] == 1


def test_quake_prefs_are_merged(tmpdb):
    jma = main.parse_p2p(p2p())
    with main.db() as conn:
        main.upsert(conn, [jma])
        rows = conn.execute(
            "SELECT pref, scale FROM quake_prefs ORDER BY pref").fetchall()
        assert [r["pref"] for r in rows] == ["Fukushima", "Miyagi"]
        assert [r["scale"] for r in rows] == [45, 30]


def test_api_event_shape_matches_frontend_expectations(tmpdb):
    jma = main.parse_p2p(p2p())
    with main.db() as conn:
        main.upsert(conn, [jma])
        r = conn.execute("SELECT * FROM quakes").fetchone()
    ev = main.quake_to_event(r)
    # the frontend dedupes hydrated history against live events by this id,
    # so it must be the same p2p-<id> string the WebSocket sends
    assert ev["id"] == "p2p-ABC123"
    assert ev["source"] == "JMA"
    assert ev["shindo"] == "5-"
    assert ev["also"] == []
