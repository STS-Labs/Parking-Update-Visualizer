#!/usr/bin/env python3
"""Sync detection outputs from the public Google Drive folder into data/ for the web map.

Drive layout:  <root>/<date>/<session>/<checkpoint_NNN | final>/...
Each checkpoint folder is cumulative, so only the newest one per session is used.

Usage:
    python scripts/sync.py                      # crawl the public Drive root
    python scripts/sync.py --local some/dir     # same layout on local disk (testing)
"""
import argparse
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

import shapefile  # pyshp

ROOT_FOLDER_ID = os.environ.get("DRIVE_ROOT_ID", "169aEHDyF3illYhWNkUE4ugvX8YXKpYgi")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")
SESSIONS_DIR = os.path.join(DATA, "sessions")

CHECKPOINT_RE = re.compile(r"^checkpoint_(\d+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FAR_FROM_TRACK_M = 50.0  # signs at least this far from the GNSS track are georeferencing errors
GNSS_GAP_S = 10.0        # no track segment (and no route line) across a longer gap between fixes


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- sources

class DriveSource:
    """Read-only access to a public ("anyone with the link") Drive folder tree, no credentials."""

    # one match per entry; folders link to /drive/folders/, files to /file/d/
    ENTRY_RE = re.compile(
        r'class="flip-entry" id="entry-([^"]+)"[^>]*>.*?href="([^"]*)".*?flip-entry-title">([^<]*)<', re.S)

    def _get(self, url, tries=4):
        for i in range(tries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 parking-sync"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.read()
            except Exception as e:  # noqa: BLE001
                if i == tries - 1:
                    raise
                log(f"  retry {url}: {e}")
                time.sleep(2 * (i + 1))

    def list(self, folder_id):
        """-> list of dicts {id, name, folder}"""
        page = self._get(f"https://drive.google.com/embeddedfolderview?id={folder_id}").decode("utf-8", "replace")
        out = []
        for m in self.ENTRY_RE.finditer(page):
            out.append({"id": m.group(1), "name": html.unescape(m.group(3)).strip(),
                        "folder": "/folders/" in m.group(2)})
        return out

    def read(self, entry):
        return self._get(f"https://drive.usercontent.google.com/download?id={entry['id']}&export=download&confirm=t")

    def photo_urls(self, entry):
        fid = entry["id"]
        return (f"https://drive.google.com/thumbnail?id={fid}&sz=w1200",
                f"https://drive.google.com/file/d/{fid}/view")

    def photo_urls_for_id(self, file_id):
        return self.photo_urls({"id": file_id})


class LocalSource:
    """Same interface as DriveSource but over a local directory (for testing)."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def list(self, folder_id):
        return [{"id": os.path.join(folder_id, n), "name": n, "folder": os.path.isdir(os.path.join(folder_id, n))}
                for n in sorted(os.listdir(folder_id))]

    def read(self, entry):
        with open(entry["id"], "rb") as f:
            return f.read()

    def photo_urls(self, entry):
        url = "file://" + entry["id"]
        return url, url

    def photo_urls_for_id(self, file_id):
        return None  # Drive file ids mean nothing locally; photos/ is listed instead


# --------------------------------------------------------------------------- helpers

def find(entries, suffix, exclude=()):
    """First non-folder entry whose name ends with suffix and doesn't end with any of exclude."""
    for e in entries:
        n = e["name"].lower()
        if not e["folder"] and n.endswith(suffix) and not any(n.endswith(x) for x in exclude):
            return e
    return None


def pick_latest_checkpoint(entries):
    final = [e for e in entries if e["folder"] and e["name"].lower() == "final"]
    if final:
        return final[0]
    cps = [(int(m.group(1)), e) for e in entries if e["folder"] and (m := CHECKPOINT_RE.match(e["name"]))]
    return max(cps, key=lambda t: t[0])[1] if cps else None


def checkpoint_candidates(entries):
    """final first, then checkpoint folders newest first. The uploader sends a
    checkpoint's progress.json last, so the first one that has it is complete."""
    final = [e for e in entries if e["folder"] and e["name"].lower() == "final"]
    cps = sorted(((int(m.group(1)), e) for e in entries if e["folder"] and (m := CHECKPOINT_RE.match(e["name"]))),
                 key=lambda t: -t[0])
    return final + [e for _, e in cps]


def slug(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")


def utm_to_lonlat(e, n, zone, northern=True):
    """WGS84 UTM -> (lon, lat). Used only if the track shapefile turns out to be projected."""
    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    x = e - 500000.0
    y = n if northern else n - 10000000.0
    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    p1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
          + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
          + (151 * e1 ** 3 / 96) * math.sin(6 * mu))
    c1 = ep2 * math.cos(p1) ** 2
    t1 = math.tan(p1) ** 2
    n1 = a / math.sqrt(1 - e2 * math.sin(p1) ** 2)
    r1 = a * (1 - e2) / (1 - e2 * math.sin(p1) ** 2) ** 1.5
    d = x / (n1 * k0)
    lat = p1 - (n1 * math.tan(p1) / r1) * (
        d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / math.cos(p1)
    return math.degrees(lon) + (zone - 1) * 6 - 180 + 3, math.degrees(lat)


def make_reprojector(prj_text):
    if not prj_text or not prj_text.lstrip().upper().startswith("PROJCS"):
        return None
    m = re.search(r"UTM[_ ]zone[_ ](\d+)([NS])?", prj_text, re.I)
    if not m:
        raise ValueError("unsupported projected CRS in .prj: " + prj_text[:120])
    zone, hemi = int(m.group(1)), (m.group(2) or "N").upper()
    return lambda x, y: utm_to_lonlat(x, y, zone, hemi == "N")


def read_track(src, entries):
    """Track line shapefile -> list of LineString coordinate lists ([lon, lat])."""
    shp = find(entries, "_gnss_track_line.shp")
    if not shp:
        return []
    base = shp["name"][:-4]
    shx = next((e for e in entries if e["name"] == base + ".shx"), None)
    dbf = next((e for e in entries if e["name"] == base + ".dbf"), None)
    prj = next((e for e in entries if e["name"] == base + ".prj"), None)
    reproject = make_reprojector(src.read(prj).decode("utf-8", "replace")) if prj else None
    reader = shapefile.Reader(shp=io.BytesIO(src.read(shp)),
                              shx=io.BytesIO(src.read(shx)) if shx else None,
                              dbf=io.BytesIO(src.read(dbf)) if dbf else None)
    lines = []
    for shape in reader.shapes():
        pts = shape.points
        parts = list(shape.parts) + [len(pts)]
        for i in range(len(parts) - 1):
            seg = pts[parts[i]:parts[i + 1]]
            if reproject:
                seg = [reproject(x, y) for x, y in seg]
            seg = [[round(x, 7), round(y, 7)] for x, y in seg]
            if len(seg) >= 2:
                lines.append(seg)
    return lines


def read_track_points(src, entries):
    """GNSS track points shapefile -> list of (datetime | None, [lon, lat]) in recording order."""
    shp = find(entries, "_gnss_track_points.shp")
    if not shp:
        return []
    base = shp["name"][:-4]
    shx = next((e for e in entries if e["name"] == base + ".shx"), None)
    dbf = next((e for e in entries if e["name"] == base + ".dbf"), None)
    prj = next((e for e in entries if e["name"] == base + ".prj"), None)
    reproject = make_reprojector(src.read(prj).decode("utf-8", "replace")) if prj else None
    reader = shapefile.Reader(shp=io.BytesIO(src.read(shp)),
                              shx=io.BytesIO(src.read(shx)) if shx else None,
                              dbf=io.BytesIO(src.read(dbf)) if dbf else None)
    names = [f[0] for f in reader.fields[1:]] if dbf else []
    ti = names.index("time") if "time" in names else None
    out = []
    for sr in reader.iterShapeRecords() if dbf else ((sh, None) for sh in reader.shapes()):
        shape, rec = (sr.shape, sr.record) if dbf else sr
        if not shape.points:
            continue
        x, y = shape.points[0]
        if reproject:
            x, y = reproject(x, y)
        t = None
        if ti is not None:
            try:
                t = datetime.strptime(str(rec[ti])[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        out.append((t, [round(x, 7), round(y, 7)]))
    return out


def thin(line, min_m=2.0):
    """Drop points closer than min_m to the previously kept one (keeps the files small)."""
    if len(line) < 3:
        return line
    out = [line[0]]
    for c in line[1:-1]:
        dx = math.radians(c[0] - out[-1][0]) * math.cos(math.radians(c[1])) * 6371000
        dy = math.radians(c[1] - out[-1][1]) * 6371000
        if math.hypot(dx, dy) >= min_m:
            out.append(c)
    out.append(line[-1])
    return out


def line_km(lines):
    km = 0.0
    for line in lines:
        for (x1, y1), (x2, y2) in zip(line, line[1:]):
            dx = math.radians(x2 - x1) * math.cos(math.radians((y1 + y2) / 2))
            km += 6371.0 * math.hypot(dx, math.radians(y2 - y1))
    return km


def split_runs(timed, max_gap_s=GNSS_GAP_S):
    """Split time-ordered (datetime, coord) fixes wherever no fix came for more than max_gap_s."""
    runs, run = [], []
    for t, c in timed:
        if run and (t - run[-1][0]).total_seconds() > max_gap_s:
            runs.append(run)
            run = []
        run.append((t, c))
    if run:
        runs.append(run)
    return runs


def split_track(src, entries, progress, pts):
    """-> (processed_lines, pending_lines).

    The GNSS track covers the whole recording, but detection only reaches svo_frame/svo_frames_total of it.
    The track's time span equals the video duration, so cut it at the same fraction of time.
    The line is broken where the GNSS log has gaps, instead of drawing a straight line across them.
    """
    timed = [p for p in pts if p[0] is not None]
    total = progress.get("svo_frames_total") or 0
    frac = 1.0 if progress.get("final") else (progress.get("svo_frame", 0) / total if total else None)
    if len(timed) >= 2 and frac is not None:
        t0, t1 = timed[0][0], timed[-1][0]
        cutoff = t0 + (t1 - t0) * max(0.0, min(1.0, frac))
        done_lines, pending_lines = [], []
        for run in split_runs(timed):
            done = [c for t, c in run if t <= cutoff]
            rest = [c for t, c in run if t >= cutoff]
            if done and rest:
                rest.insert(0, done[-1])  # connect the two parts
            for line, out in ((thin(done), done_lines), (thin(rest), pending_lines)):
                if len(line) >= 2:
                    out.append(line)
        return done_lines, pending_lines
    lines = read_track(src, entries) or ([thin([c for _, c in pts])] if len(pts) >= 2 else [])
    return lines, []


class TrackIndex:
    """Distance (m) from a point to the GNSS track: segments between consecutive fixes (none across a
    GNSS gap), bucketed in a coarse grid so thousands of signs stay fast."""

    CELL_M = 100.0

    def __init__(self, pts):
        timed = [p for p in pts if p[0] is not None]
        runs = split_runs(timed) if len(timed) >= 2 else [list(pts)]
        coords = [c for _, c in pts]
        self.lat0 = sum(c[1] for c in coords) / len(coords) if coords else 0.0
        self.kx = 111320.0 * math.cos(math.radians(self.lat0))
        self.grid = {}
        for run in runs:
            xy = [self._xy(c) for _, c in run]
            if len(xy) == 1:
                xy = xy * 2
            for a, b in zip(xy, xy[1:]):
                for cx in range(int(min(a[0], b[0]) // self.CELL_M), int(max(a[0], b[0]) // self.CELL_M) + 1):
                    for cy in range(int(min(a[1], b[1]) // self.CELL_M), int(max(a[1], b[1]) // self.CELL_M) + 1):
                        self.grid.setdefault((cx, cy), []).append((a, b))

    def _xy(self, c):
        return c[0] * self.kx, c[1] * 111320.0

    def distance(self, lon, lat):
        """Metres to the nearest track segment; inf if none within ~100 m."""
        px, py = self._xy([lon, lat])
        cx, cy = int(px // self.CELL_M), int(py // self.CELL_M)
        best = math.inf
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (ax, ay), (bx, by) in self.grid.get((cx + dx, cy + dy), ()):
                    vx, vy = bx - ax, by - ay
                    seg2 = vx * vx + vy * vy
                    u = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / seg2))
                    best = min(best, math.hypot(px - ax - u * vx, py - ay - u * vy))
        return best


def raw_stats(raw_bytes):
    """Parse raw.jsonl -> list of (ts_ns, label, confidence)."""
    rows = []
    for line in raw_bytes.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            rows.append((int(d["ts_ns"]), str(d["label"]), float(d["confidence"])))
        except (ValueError, KeyError, TypeError):
            continue
    rows.sort()
    return rows


def pole_detection_stats(rows, labels, first_ts, last_ts):
    import bisect
    lo = bisect.bisect_left(rows, (first_ts,))
    hi = bisect.bisect_right(rows, (last_ts, "￿"))
    out = {}
    for _, lab, conf in rows[lo:hi]:
        if lab not in labels:
            continue
        s = out.setdefault(lab, {"n": 0, "sum": 0.0, "max": 0.0})
        s["n"] += 1
        s["sum"] += conf
        s["max"] = max(s["max"], conf)
    return {lab: {"detections": s["n"], "mean_conf": round(s["sum"] / s["n"], 3), "max_conf": round(s["max"], 3)}
            for lab, s in out.items()}


# --------------------------------------------------------------------------- session processing

def process_session(src, date, session_entry, previous):
    name = session_entry["name"]
    sid = slug(f"{date}_{name}") if date else slug(name)
    children = src.list(session_entry["id"])
    cp, entries, prog_e = None, None, None
    for candidate in checkpoint_candidates(children):
        listing = src.list(candidate["id"])
        prog_e = find(listing, "progress.json")
        if prog_e:
            cp, entries = candidate, listing
            break
        log(f"  {sid}: {candidate['name']} still uploading (no progress.json yet), using an older one")
    if not cp:
        log(f"  {sid}: no complete checkpoint yet")
        return None
    progress = json.loads(src.read(prog_e))

    # Photos, newest layout first:
    #  1. <checkpoint>/*_photo_ids.json: photo_key -> Drive file id of <session>/photos/<photo_key>
    #  2. <session>/photos/ listed directly (local testing: ids mean nothing there)
    #  3. checkpoints up to 019: "<checkpoint>_photos/<file>.jpg" inside the checkpoint folder
    ids_e = find(entries, "_photo_ids.json")
    photo_ids = json.loads(src.read(ids_e)) if ids_e else {}
    shared = {}
    if not photo_ids:
        for e in children:
            if e["folder"] and e["name"] == "photos":
                shared = {p["name"]: p for p in src.list(e["id"]) if not p["folder"]}
    photos = {}
    for e in entries:
        if e["folder"] and e["name"].lower().endswith("_photos"):
            for p in src.list(e["id"]):
                if not p["folder"]:
                    photos[f"{e['name']}/{p['name']}"] = p
                    photos.setdefault(p["name"], p)

    stamp = f"{cp['name']}|{progress.get('written_at')}|{len(entries)}|{len(photos) + len(photo_ids) + len(shared)}"
    out_path = os.path.join(SESSIONS_DIR, sid + ".geojson")
    if previous and previous.get("stamp") == stamp and os.path.exists(out_path):
        log(f"  {sid}: unchanged ({cp['name']})")
        return previous

    log(f"  {sid}: processing {cp['name']}")
    geo_e = find(entries, ".geojson")
    if not geo_e:
        raise RuntimeError(f"no .geojson in {cp['name']}")
    points = json.loads(src.read(geo_e))

    raw_e = find(entries, ".raw.jsonl")
    rows = raw_stats(src.read(raw_e)) if raw_e else []

    track_pts = read_track_points(src, entries)
    track_index = TrackIndex(track_pts) if track_pts else None
    dropped_far = 0

    features = []
    for f in points.get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        p = dict(f.get("properties") or {})
        coords = g["coordinates"]
        # Points in GNSS gaps (gnss_gap) can't be checked against the track; they are shown as unverified.
        if (track_index and not p.get("gnss_gap")
                and track_index.distance(coords[0], coords[1]) >= FAR_FROM_TRACK_M):
            dropped_far += 1
            continue
        key = p.get("photo_key")
        urls = None
        if key and key in photo_ids:
            urls = src.photo_urls_for_id(photo_ids[key])
        elif key and key in shared:
            urls = src.photo_urls(shared[key])
        else:
            img = p.get("image") or ""
            ph = photos.get(img) or photos.get(os.path.basename(img))
            urls = src.photo_urls(ph) if ph else None
        if urls:
            p["photo_url"], p["photo_view"] = urls
        labels = p.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
            p["labels"] = labels
        if rows and p.get("first_ts") and p.get("last_ts"):
            p["label_stats"] = pole_detection_stats(rows, set(labels), int(p["first_ts"]), int(p["last_ts"]))
        p["alt"] = round(coords[2], 2) if len(coords) > 2 else None
        p["session"] = sid
        p["kind"] = "sign"
        features.append({"type": "Feature",
                         "geometry": {"type": "Point", "coordinates": [round(coords[0], 7), round(coords[1], 7)]},
                         "properties": p})

    if dropped_far:
        log(f"  {sid}: {dropped_far} sign(s) >= {FAR_FROM_TRACK_M:.0f} m from the GNSS track left out")
    track, pending = split_track(src, entries, progress, track_pts)
    if pending:
        features.append({"type": "Feature", "geometry": {"type": "MultiLineString", "coordinates": pending},
                         "properties": {"kind": "track_pending", "session": sid}})
    if track:
        features.append({"type": "Feature", "geometry": {"type": "MultiLineString", "coordinates": track},
                         "properties": {"kind": "track", "session": sid}})

    allc = [f["geometry"]["coordinates"] for f in features if f["geometry"]["type"] == "Point"]
    allc += [c for line in (track or pending) for c in line]
    bbox = ([min(c[0] for c in allc), min(c[1] for c in allc), max(c[0] for c in allc), max(c[1] for c in allc)]
            if allc else None)
    track_km = line_km(track)

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh, separators=(",", ":"))

    missing = sum(1 for f in features
                  if f["properties"].get("kind") == "sign" and f["properties"].get("image")
                  and "photo_url" not in f["properties"])
    if missing:
        log(f"  {sid}: {missing} photo(s) not uploaded yet; will retry next run")
        stamp += "|incomplete"  # never matches the next run's stamp, so the session is re-processed

    return {
        "id": sid,
        "date": date,
        "name": name,
        "checkpoint": cp["name"],
        "stamp": stamp,
        "file": f"data/sessions/{sid}.geojson",
        "points": sum(1 for f in features if f["properties"].get("kind") == "sign"),
        "unverified_points": sum(1 for f in features if f["properties"].get("gnss_gap")),
        "dropped_far": dropped_far,
        "photos_missing": missing,
        "track_km": round(track_km, 2),
        "recorded_km": round(track_km + line_km(pending), 2),
        "bbox": bbox,
        "progress": progress,
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def crawl(src, root_id):
    """Yield (date, session_entry). Accepts root = top folder, a date folder, or a session folder."""
    top = src.list(root_id)
    if pick_latest_checkpoint(top):  # root is itself a session
        yield "", {"id": root_id, "name": os.path.basename(str(root_id).rstrip("/")) or "session"}
        return
    for d in top:
        if not d["folder"]:
            continue
        if DATE_RE.match(d["name"]):
            for s in src.list(d["id"]):
                if s["folder"]:
                    yield d["name"], s
        else:  # session directly under root
            yield "", d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="read from a local folder instead of Drive")
    ap.add_argument("--root", default=ROOT_FOLDER_ID, help="Drive folder id of the top folder")
    args = ap.parse_args()

    src = LocalSource(args.local) if args.local else DriveSource()
    root = src.root if args.local else args.root

    index_path = os.path.join(DATA, "index.json")
    try:
        with open(index_path) as fh:
            old = {s["id"]: s for s in json.load(fh).get("sessions", [])}
    except (OSError, ValueError):
        old = {}

    sessions, errors = [], 0
    try:
        found = list(crawl(src, root))
    except Exception as e:  # noqa: BLE001
        log(f"ERROR listing Drive root: {e}; keeping existing data")
        return 1
    log(f"found {len(found)} session folder(s)")
    for date, s in found:
        sid = slug(f"{date}_{s['name']}") if date else slug(s["name"])
        try:
            r = process_session(src, date, s, old.get(sid))
        except Exception as e:  # noqa: BLE001
            errors += 1
            log(f"  WARNING {sid}: {e!r}; keeping previous data")
            r = old.get(sid)
        if r:
            sessions.append(r)

    # drop files of sessions that disappeared from Drive
    keep = {s["id"] + ".geojson" for s in sessions}
    if os.path.isdir(SESSIONS_DIR):
        for fn in os.listdir(SESSIONS_DIR):
            if fn not in keep:
                os.remove(os.path.join(SESSIONS_DIR, fn))
                log(f"  removed stale {fn}")

    sessions.sort(key=lambda s: (s["date"], s["name"]))
    os.makedirs(DATA, exist_ok=True)
    new_body = {"sessions": sessions}
    old_body = None
    try:
        with open(index_path) as fh:
            old_body = json.load(fh)
            old_body.pop("updated_at", None)
    except (OSError, ValueError):
        pass
    if old_body != new_body or not os.path.exists(index_path):
        new_body["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # keep key order readable
        out = {"updated_at": new_body.pop("updated_at"), **new_body}
        with open(index_path, "w") as fh:
            json.dump(out, fh, indent=1)
        log(f"wrote index with {len(sessions)} session(s)")
    else:
        log("no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
