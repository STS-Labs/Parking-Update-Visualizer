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
