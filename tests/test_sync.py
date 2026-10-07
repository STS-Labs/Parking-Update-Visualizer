import importlib.util
import json
import os
import shapefile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_sync(tmp_path):
    spec = importlib.util.spec_from_file_location("sync", os.path.join(HERE, "..", "scripts", "sync.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.DATA = str(tmp_path / "out")
    m.SESSIONS_DIR = str(tmp_path / "out" / "sessions")
    return m


def mk(root, rel, content=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        p.mkdir(exist_ok=True)
    elif isinstance(content, (dict, list)):
        p.write_text(json.dumps(content))
    else:
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    return p


def test_crawl_enters_container_and_root_wins(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    mk(d, "2026-09-30/session_a/checkpoint_001")
    mk(d, "2026-10-07/S1/chunks/001")
    mk(d, "_sts_test/2026-10-07/S1/chunks/001")
    mk(d, "_sts_test/2026-10-07/S2/chunks/001")
    mk(d, "_sts_test/notes.txt", "x")
    mk(d, "loose_session/final")
    found = list(sync.crawl(sync.LocalSource(str(d)), str(d)))
    got = sorted((date, s["name"], c) for date, s, c in found)
    assert got == [("", "loose_session", ""), ("2026-09-30", "session_a", ""),
                   ("2026-10-07", "S1", ""), ("2026-10-07", "S1", "_sts_test"),
                   ("2026-10-07", "S2", "_sts_test")]
    assert sync.dedupe_sessions(found) == [(date, s, c) for date, s, c in found
                                           if not (s["name"] == "S1" and c == "_sts_test")]


def points_shp(folder, stem, fixes):
    """fixes: [(time_str, lon, lat)] -> <stem>.shp/.shx/.dbf with a 'time' field."""
    folder.mkdir(parents=True, exist_ok=True)
    w = shapefile.Writer(str(folder / stem), shapeType=shapefile.POINT)
    w.field("time", "C", size=19)
    for t, lon, lat in fixes:
        w.point(lon, lat)
        w.record(t)
    w.close()


def geojson(points):
    """points: [(lon, lat, props)]"""
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat, 500.0]}, "properties": p}
        for lon, lat, p in points]}


FIXES = [(f"2026-10-07 10:00:{m:02d}", 44.80 + m * 1e-4, 41.70) for m in range(0, 10)]  # 1 s apart (gaps > 10 s break the line)


def test_legacy_checkpoint_session(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    cp = d / "2026-09-30" / "session_x" / "checkpoint_002"
    mk(d, "2026-09-30/session_x/checkpoint_001/session_x_checkpoint_001.geojson", geojson([]))
    mk(cp, "session_x_checkpoint_002.geojson",
       geojson([(44.8002, 41.70, {"labels": "7.4", "image": "session_x_checkpoint_002_photos/s0.jpg",
                                  "first_ts": 1, "last_ts": 2})]))
    mk(cp, "session_x_checkpoint_002.raw.jsonl", '{"ts_ns": 1, "label": "7.4", "confidence": 0.9}\n')
    mk(cp, "session_x_checkpoint_002_photos/s0.jpg", b"jpg")
    points_shp(cp, "session_x_checkpoint_002_gnss_track_points", FIXES)
    mk(cp, "session_x_checkpoint_002_progress.json", {"svo_frame": 50, "svo_frames_total": 100,
                                                      "written_at": "2026-09-30 12:00:00"})
    src = sync.LocalSource(str(d))
    [(date, s, c)] = list(sync.crawl(src, str(d)))
    r = sync.process_session(src, date, s, None)
    assert r["checkpoint"] == "checkpoint_002" and r["points"] == 1 and r["photos_missing"] == 0
    assert r["stamp"].startswith("checkpoint_002|2026-09-30 12:00:00|")
    feats = json.load(open(os.path.join(sync.SESSIONS_DIR, r["id"] + ".geojson")))["features"]
    sign = [f for f in feats if f["properties"]["kind"] == "sign"][0]["properties"]
    assert sign["labels"] == ["7.4"] and sign["photo_url"].endswith("s0.jpg")
    assert sign["label_stats"] == {"7.4": {"detections": 1, "mean_conf": 0.9, "max_conf": 0.9}}
    kinds = sorted(f["properties"]["kind"] for f in feats)
    assert kinds == ["sign", "track", "track_pending"]  # cut at svo_frame / svo_frames_total
