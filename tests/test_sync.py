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


def sts_chunk(sess, n, signs, photos=True, fixes=FIXES, frames=100, footage=10.0):
    c = sess / "chunks" / f"{n:03d}"
    p = f"S1_c{n:04d}"
    mk(c, f"{p}.geojson", geojson([(lon, lat, {"labels": [lab], "image": f"{p}_photos/sign_{i}.jpg",
                                               "first_ts": 1, "last_ts": 2})
                                   for i, (lon, lat, lab) in enumerate(signs)]))
    mk(c, f"{p}.raw.jsonl", "".join('{"ts_ns": 1, "label": "%s", "confidence": 0.8}\n' % s[2] for s in signs))
    if photos:
        for i in range(len(signs)):
            mk(c, f"{p}_photos/sign_{i}.jpg", b"jpg")
    points_shp(c, "gnss_track_points", fixes)
    mk(c, "run_stats.json", {"frames": frames, "footage_s": footage, "raw_detections": len(signs),
                             "started_at": 1791368000.0 + n, "wall_s": 5.0})
    mk(sess, f"logs/manifest_{n:03d}.json", {"frames": {"grabbed": frames}, "time": {"duration_s": footage}})
    return c


def run_one(sync, d):
    src = sync.LocalSource(str(d))
    [(date, s, c)] = sync.dedupe_sessions(list(sync.crawl(src, str(d))))
    return sync.process_session(src, date, s, None)


def signs_of(sync, r):
    feats = json.load(open(os.path.join(sync.SESSIONS_DIR, r["id"] + ".geojson")))["features"]
    return [f["properties"] for f in feats if f["properties"]["kind"] == "sign"]


def test_ststcc_combines_complete_chunks(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sess = d / "_sts_test" / "2026-10-07" / "S1"
    sts_chunk(sess, 1, [(44.8001, 41.70, "7.4")])
    sts_chunk(sess, 2, [(44.8003, 41.70, "5.1"), (44.8005, 41.70, "7.4")])
    mk(sess, "chunks/003/run_stats.json", {"frames": 100})            # still uploading: no .geojson
    mk(sess, "logs/manifest_003.json", {"frames": {"grabbed": 100}, "time": {"duration_s": 10.0}})
    mk(sess, "chunks/_superseded/x.geojson", geojson([(44.9, 41.9, {"labels": ["x"]})]))
    mk(sess, "final_cc_r1/S1.geojson", geojson([(44.9, 41.9, {"labels": ["x"]})]))
    r = run_one(sync, d)
    assert r["id"] == "2026-10-07_S1" and r["checkpoint"] == "2 chunks" and r["points"] == 3
    assert r["photos_missing"] == 0 and r["track_km"] > 0
    pr = r["progress"]
    assert pr["final"] is False and pr["chunks"] == 2
    assert (pr["frames_done"], pr["frames_expected"]) == (200, 300)
    assert (pr["video_time_reached"], pr["video_duration"]) == ("0:00:20", "0:00:30")
    assert pr["raw_detections"] == 3
    assert all(p["photo_url"].endswith(".jpg") for p in signs_of(sync, r))


def test_ststcc_photos_still_uploading_retry(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sts_chunk(d / "2026-10-07" / "S1", 1, [(44.8001, 41.70, "7.4")], photos=False)
    r = run_one(sync, d)
    assert r["points"] == 1 and r["photos_missing"] == 1 and r["stamp"].endswith("|incomplete")


def test_ststcc_final_only_when_complete(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sess = d / "2026-10-07" / "S1"
    sts_chunk(sess, 1, [(44.8001, 41.70, "7.4")])
    sts_chunk(sess, 2, [(44.8003, 41.70, "5.1")])
    mk(sess, "final/S1.geojson", geojson([(44.8002, 41.70, {"labels": ["7.4"]})]))
    points_shp(sess / "final", "gnss_track_points", FIXES)
    for status, want in (({"state": "final_provisional"}, "2 chunks"), ("{not json", "2 chunks"),
                         ({"state": "final_complete"}, "final")):
        mk(sess, "_status.json", status)
        r = run_one(sync, d)
        assert r["checkpoint"] == want
    assert r["points"] == 1 and r["progress"]["final"] is True


def test_ststcc_corrupt_chunk_left_out(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sess = d / "2026-10-07" / "S1"
    sts_chunk(sess, 1, [(44.8001, 41.70, "7.4")])
    c2 = sts_chunk(sess, 2, [(44.8003, 41.70, "5.1")])
    (c2 / "S1_c0002.geojson").write_text("{broken")
    r = run_one(sync, d)
    assert r["points"] == 1


# --- review fixes -------------------------------------------------------------------------------

def test_ststcc_corrupt_final_is_not_cached_as_empty(tmp_path):
    import pytest
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sess = d / "2026-10-07" / "S1"
    sts_chunk(sess, 1, [(44.8001, 41.70, "7.4")])
    mk(sess, "final/S1.geojson", "{truncated")
    mk(sess, "_status.json", {"state": "final_complete", "updated_at": 1.0})
    with pytest.raises(Exception):  # main() keeps the previous data for this session
        run_one(sync, d)


def test_ststcc_skipped_chunk_is_retried_next_run(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sess = d / "2026-10-07" / "S1"
    sts_chunk(sess, 1, [(44.8001, 41.70, "7.4")])
    c2 = sts_chunk(sess, 2, [(44.8003, 41.70, "5.1")])
    good = (c2 / "S1_c0002.geojson").read_text()
    (c2 / "S1_c0002.geojson").write_text("{broken")
    r1 = run_one(sync, d)
    assert r1["points"] == 1 and r1["stamp"].endswith("|incomplete")
    (c2 / "S1_c0002.geojson").write_text(good)
    src = sync.LocalSource(str(d))
    [(date, s, c)] = list(sync.crawl(src, str(d)))
    assert sync.process_session(src, date, s, r1)["points"] == 2


def test_ststcc_reprocessed_chunk_with_same_files_is_rerendered(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sess = d / "2026-10-07" / "S1"
    c1 = sts_chunk(sess, 1, [(44.8001, 41.70, "7.4")])
    r1 = run_one(sync, d)
    mk(c1, "S1_c0001.geojson", geojson([(44.8001, 41.70, {"labels": ["5.1"], "image": "S1_c0001_photos/sign_0.jpg"})]))
    mk(c1, "run_stats.json", {"frames": 100, "footage_s": 10.0, "started_at": 1791379999.0, "wall_s": 5.0})
    src = sync.LocalSource(str(d))
    [(date, s, c)] = list(sync.crawl(src, str(d)))
    r2 = sync.process_session(src, date, s, r1)
    assert r2["stamp"] != r1["stamp"] and signs_of(sync, r2)[0]["labels"] == ["5.1"]


def test_ststcc_final_session_reads_only_final(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sess = d / "2026-10-07" / "S1"
    sts_chunk(sess, 1, [(44.8001, 41.70, "7.4")])
    mk(sess, "final/S1.geojson", geojson([(44.8001, 41.70, {"labels": ["7.4"]})]))
    mk(sess, "_status.json", {"state": "final_complete", "updated_at": 1791380000.0, "chunks": 1})
    src = sync.LocalSource(str(d))
    [(date, s, c)] = list(sync.crawl(src, str(d)))
    r1 = sync.process_session(src, date, s, None)
    assert r1["checkpoint"] == "final" and r1["progress"]["final"] is True and r1["progress"]["chunks"] == 1
    listed = []
    real_list = src.list
    src.list = lambda fid: (listed.append(fid), real_list(fid))[1]
    assert sync.process_session(src, date, s, r1) is r1  # unchanged
    assert not any(os.sep + "chunks" in str(f) or str(f).endswith("logs") for f in listed), listed


def test_ststcc_frames_done_from_manifest_not_resumed_run_stats(tmp_path):
    sync = load_sync(tmp_path)
    d = tmp_path / "drive"
    sess = d / "2026-10-07" / "S1"
    c1 = sts_chunk(sess, 1, [(44.8001, 41.70, "7.4")], frames=100)
    mk(c1, "run_stats.json", {"frames": 15, "footage_s": 10.0, "started_at": 1.0})  # resumed: last segment only
    r = run_one(sync, d)
    assert (r["progress"]["frames_done"], r["progress"]["frames_expected"]) == (100, 100)
