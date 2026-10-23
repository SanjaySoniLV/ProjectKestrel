"""Quality feedback: frame selection, payload, crop re-encoding, upload client, bridge."""

from __future__ import annotations

import io
import json
import os
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge  # noqa: E402
import quality_feedback_client as qfc  # noqa: E402
from kestrel_analyzer import quality_feedback as qf  # noqa: E402

pytestmark = pytest.mark.unit

IID = "qfi_" + "a" * 32


def row(name, q, s=None, l=None, crop=True, culled=""):
    return {
        "filename": name,
        "crop_path": f".kestrel/crop/{name}_crop_0.jpg" if crop else "",
        "quality": q,
        "quality_siglip": -1.0 if s is None else s,
        "quality_legacy": -1.0 if l is None else l,
        "culled": culled,
    }


# ── Selector ───────────────────────────────────────────────────────────────


class TestSelectFeedbackFrames:
    def test_disagreement_pair_picked_first(self):
        # B and C: SigLIP says B > C, legacy says C > B (strongly). The rest agree.
        rows = [
            row("A", 0.90, 0.90, 0.90),
            row("B", 0.60, 0.60, 0.20),
            row("C", 0.50, 0.50, 0.80),
            row("D", 0.30, 0.30, 0.30),
            row("E", 0.20, 0.20, 0.19),
            row("F", 0.10, 0.10, 0.05),
            row("G", 0.05, 0.05, 0.02),
        ]
        sel = qf.select_feedback_frames(rows, k=3, rng=random.Random(1))
        names = {f["filename"] for f in sel["frames"]}
        assert {"B", "C"} <= names
        by_name = {f["filename"]: f["index"] for f in sel["frames"]}
        assert sel["reasons"][by_name["B"]] == "disagreement"
        assert sel["reasons"][by_name["C"]] == "disagreement"
        assert len(sel["frames"]) == 3
        # One slot is kept for the spread when the burst is larger than k.
        assert "spread" in sel["reasons"].values()

    def test_strongest_disagreement_wins(self):
        rows = [
            row("A", 0.80, 0.80, 0.70),
            row("B", 0.78, 0.78, 0.72),   # weak disagreement with A
            row("C", 0.40, 0.40, 0.10),
            row("D", 0.30, 0.30, 0.90),   # strong disagreement with C
            row("E", 0.10, 0.10, 0.10),
        ]
        sel = qf.select_feedback_frames(rows, k=2, rng=random.Random(0))
        assert {f["filename"] for f in sel["frames"]} == {"C", "D"}

    def test_close_scores_then_culled(self):
        rows = [
            row("A", 0.90), row("B", 0.51), row("C", 0.50),
            row("D", 0.20, culled="accept"), row("E", 0.05, culled="reject"),
            row("F", 0.70), row("G", 0.30),
        ]
        sel = qf.select_feedback_frames(rows, k=5, rng=random.Random(0))
        reasons = {f["filename"]: sel["reasons"][f["index"]] for f in sel["frames"]}
        assert reasons["B"] == reasons["C"] == "close"
        assert reasons["D"] == reasons["E"] == "culled"
        assert list(reasons.values()).count("spread") == 1

    def test_frames_are_in_kestrel_order_and_shown_shuffled(self):
        rows = [row(n, q, q, 1 - q) for n, q in zip("ABCDE", (0.9, 0.7, 0.5, 0.3, 0.1))]
        for seed in range(20):
            sel = qf.select_feedback_frames(rows, k=5, rng=random.Random(seed))
            qs = [f["quality"] for f in sel["frames"]]
            assert qs == sorted(qs, reverse=True)
            assert [f["index"] for f in sel["frames"]] == list(range(5))
            assert sorted(sel["shown_order"]) == list(range(5))
            assert sel["shown_order"] != list(range(5))  # never Kestrel's order

    def test_seeded_shuffle_is_deterministic(self):
        rows = [row(n, q) for n, q in zip("ABCDE", (0.9, 0.7, 0.5, 0.3, 0.1))]
        a = qf.select_feedback_frames(rows, rng=random.Random(42))["shown_order"]
        b = qf.select_feedback_frames(rows, rng=random.Random(42))["shown_order"]
        assert a == b

    def test_two_image_scene(self):
        rows = [row("A", 0.4, 0.4, 0.6), row("B", 0.6, 0.6, 0.4)]
        sel = qf.select_feedback_frames(rows, rng=random.Random(3))
        assert [f["filename"] for f in sel["frames"]] == ["B", "A"]
        assert sel["shown_order"] == [1, 0]

    def test_missing_legacy_scores(self):
        rows = [row("A", 0.8, 0.8, None), row("B", 0.4, 0.4, None), row("C", 0.1, 0.1, None)]
        sel = qf.select_feedback_frames(rows, k=5, rng=random.Random(0))
        assert len(sel["frames"]) == 3
        assert all(f["quality_legacy"] is None for f in sel["frames"])
        assert "disagreement" not in sel["reasons"].values()

    def test_rows_without_crops_or_scores_excluded(self):
        rows = [
            row("A", 0.8), row("B", 0.4, crop=False), row("C", -1.0),
            {"filename": "D", "crop_path": float("nan"), "quality": 0.5},
            row("E", 0.2),
        ]
        sel = qf.select_feedback_frames(rows, rng=random.Random(0))
        assert {f["filename"] for f in sel["frames"]} == {"A", "E"}

    def test_effective_falls_back_to_raw_scores(self):
        rows = [row("A", -1, 0.7, 0.2), row("B", -1, 0.3, 0.9)]
        sel = qf.select_feedback_frames(rows, active_scorer="legacy", rng=random.Random(0))
        assert [f["filename"] for f in sel["frames"]] == ["B", "A"]

    def test_fewer_than_two_frames(self):
        assert qf.select_feedback_frames([row("A", 0.5)])["frames"] == []
        assert qf.select_feedback_frames([])["frames"] == []

    def test_caps_at_five(self):
        rows = [row(f"I{i}", i / 20, i / 20, (20 - i) / 20) for i in range(12)]
        sel = qf.select_feedback_frames(rows, k=9, rng=random.Random(0))
        assert len(sel["frames"]) == 5


# ── Payload ────────────────────────────────────────────────────────────────


def _frames(n):
    return [
        {"index": i, "quality": 0.9 - i * 0.1, "quality_siglip": 0.9 - i * 0.1, "quality_legacy": None}
        for i in range(n)
    ]


def _payload(n=4, ranking=None, **kw):
    args = dict(
        frames=_frames(n),
        shown_order=list(reversed(range(n))),
        ranking=ranking if ranking is not None else [[i] for i in range(n)],
        thumbs="down",
        duration_ms=14200.4,
        install_id=IID,
        crop_sizes=[1000 + i for i in range(n)],
        active_scorer="siglip",
        species=["Roseate Spoonbill", "Unknown", "Roseate Spoonbill"],
        family=["Threskiornithidae"],
        app_version="Ruddy Turnstone",
        pipeline_version=3,
        platform="windows",
    )
    args.update(kw)
    return qf.build_submission_payload(**args)


class TestPayload:
    def test_contract_shape(self):
        p = _payload(ranking=[[0], [2, 3], [1]])
        assert p["install_id"] == IID
        assert p["client"] == {"app_version": "Ruddy Turnstone", "pipeline_version": 3, "platform": "windows"}
        assert p["thumbs"] == "down" and p["active_scorer"] == "siglip"
        assert p["shown_order"] == [3, 2, 1, 0]
        assert p["ranking"] == [[0], [2, 3], [1]]
        assert p["duration_ms"] == 14200
        assert p["species"] == ["Roseate Spoonbill"]
        assert p["family"] == ["Threskiornithidae"]
        assert p["crops"][0] == {"index": 0, "bytes": 1000, "quality": 0.9,
                                 "quality_siglip": 0.9, "quality_legacy": None}
        json.dumps(p)  # serialisable
        assert len(json.dumps(p)) < 32 * 1024

    def test_ranking_must_cover_each_index_once(self):
        for bad in ([[0], [1], [2]], [[0], [1], [2], [2], [3]], [[0, 1], [2], [5]], [], [[0], [], [1, 2, 3]],
                    [[0], [1], [2], [True]]):
            with pytest.raises(ValueError):
                _payload(ranking=bad)

    def test_ties(self):
        assert _payload(n=2, ranking=[[0, 1]])["ranking"] == [[0, 1]]
        assert _payload(n=3, ranking=[[1, 0, 2]])["ranking"] == [[1, 0, 2]]
        assert qf.normalize_ranking([1, [0, 2]], 3) == [[1], [0, 2]]

    def test_validate_rejects_contract_violations(self):
        good = _payload()
        for mutate in (
            lambda p: p.update(install_id="qfi_xyz"),
            lambda p: p.update(thumbs="meh"),
            lambda p: p.update(active_scorer="clip"),
            lambda p: p.update(shown_order=[0, 0, 1, 2]),
            lambda p: p["crops"].pop(),
            lambda p: p["crops"][0].update(bytes=0),
            lambda p: p["crops"][0].update(bytes=1_500_001),
            lambda p: p["crops"][1].update(quality=1.5),
            lambda p: p.update(species=["x" * 81]),
            lambda p: p.update(species=[str(i) for i in range(11)]),
        ):
            p = json.loads(json.dumps(good))
            mutate(p)
            with pytest.raises(ValueError):
                qf.validate_payload(p)

    def test_crop_count_bounds(self):
        with pytest.raises(ValueError):
            _payload(n=1, ranking=[[0]])
        with pytest.raises(ValueError):
            _payload(n=6)


# ── Crop re-encoding ──────────────────────────────────────────────────────


def _jpeg_with_metadata(size=(1024, 1024)) -> bytes:
    im = Image.new("RGB", size, (120, 160, 90))
    exif = Image.Exif()
    exif[0x010F] = "Canon"            # Make
    exif[0x0110] = "EOS R5"           # Model
    exif[0xA431] = "123456789"        # BodySerialNumber
    exif[0x8825] = {1: "N", 2: (40.0, 26.0, 46.0)}  # GPS IFD
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes(), icc_profile=b"\0" * 128, comment=b"secret")
    return buf.getvalue()


class TestReencode:
    def test_strips_metadata_and_caps_size(self, tmp_path):
        src = tmp_path / "crop.jpg"
        src.write_bytes(_jpeg_with_metadata())
        with Image.open(src) as before:
            assert before.getexif() and before.info.get("icc_profile")
        out = qf.reencode_crop(str(src))
        with Image.open(io.BytesIO(out)) as im:
            assert im.format == "JPEG"
            assert max(im.size) == 768
            assert not im.getexif()
            assert "exif" not in im.info and "icc_profile" not in im.info and "comment" not in im.info
        assert out[:3] == b"\xff\xd8\xff"
        assert b"Canon" not in out and b"123456789" not in out

    def test_small_crop_not_upscaled(self):
        out = qf.reencode_crop(_jpeg_with_metadata((300, 200)))
        with Image.open(io.BytesIO(out)) as im:
            assert im.size == (300, 200)

    def test_rgba_png_source(self):
        buf = io.BytesIO()
        Image.new("RGBA", (900, 400), (1, 2, 3, 128)).save(buf, format="PNG")
        with Image.open(io.BytesIO(qf.reencode_crop(buf.getvalue()))) as im:
            assert im.mode == "RGB" and im.size == (768, 341)


# ── Client against a fake server ───────────────────────────────────────────


class FakeServer:
    """Speaks the quality-feedback API; behaviour knobs per test."""

    def __init__(self):
        self.calls = []
        self.objects = {}
        self.fail_put_times = {}     # index -> how many PUTs to fail
        self.drop_put_once = set()   # index -> accept the PUT but don't store it (first time)
        self.create_status = 201
        server = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                data = json.dumps(body or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n)

            def do_POST(self):
                body = self._body()
                server.calls.append(("POST", self.path))
                if self.path == "/v1/feedback/quality":
                    p = json.loads(body)
                    server.last_create = p
                    if server.create_status != 201:
                        return self._send(server.create_status, {"error": "rate_limited", "retry_after": 60})
                    host = f"http://127.0.0.1:{server.port}"
                    return self._send(201, {
                        "submission_id": "qfs_" + "b" * 32,
                        "uploads": [{"index": c["index"], "url": f"{host}/r2/{c['index']}",
                                     "headers": {"Content-Type": "image/jpeg"}} for c in p["crops"]],
                        "expires_in": 900,
                    })
                if self.path.endswith("/commit"):
                    n = len(server.last_create["crops"])
                    missing = [i for i in range(n) if i not in server.objects]
                    if missing:
                        return self._send(409, {"error": "upload_incomplete", "missing": missing})
                    return self._send(200, {"ok": True})
                self._send(404)

            def do_PUT(self):
                body = self._body()
                i = int(self.path.rsplit("/", 1)[1])
                server.calls.append(("PUT", i))
                assert self.headers.get("Content-Type") == "image/jpeg"
                if server.fail_put_times.get(i, 0) > 0:
                    server.fail_put_times[i] -= 1
                    return self._send(500)
                if i in server.drop_put_once:
                    server.drop_put_once.discard(i)
                    return self._send(200)
                server.objects[i] = body
                self._send(200)

            def do_DELETE(self):
                server.calls.append(("DELETE", self.path))
                self._send(200, {"deleted": 3})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


CROPS = [b"\xff\xd8\xff" + bytes([i]) * 50 for i in range(3)]


class TestClient:
    def test_create_put_commit(self, server):
        p = _payload(n=3, ranking=[[0], [1, 2]])
        res = qfc.submit(p, CROPS, base=server.base)
        assert res == {"submission_id": "qfs_" + "b" * 32}
        assert server.objects == {i: CROPS[i] for i in range(3)}
        assert server.calls[0] == ("POST", "/v1/feedback/quality")
        assert [c for c in server.calls if c[0] == "PUT"] == [("PUT", 0), ("PUT", 1), ("PUT", 2)]
        assert server.calls[-1] == ("POST", f"/v1/feedback/quality/qfs_{'b' * 32}/commit")
        assert server.last_create["install_id"] == IID

    def test_failed_put_is_retried_once(self, server):
        server.fail_put_times = {1: 1}
        qfc.submit(_payload(n=3), CROPS, base=server.base)
        assert [c for c in server.calls if c == ("PUT", 1)] == [("PUT", 1)] * 2
        assert 1 in server.objects

    def test_409_reputs_missing_then_commits(self, server):
        server.drop_put_once = {2}
        qfc.submit(_payload(n=3), CROPS, base=server.base)
        commits = [c for c in server.calls if c[0] == "POST" and c[1].endswith("/commit")]
        assert len(commits) == 2
        assert [c for c in server.calls if c == ("PUT", 2)] == [("PUT", 2)] * 2
        assert server.objects[2] == CROPS[2]

    def test_persistent_put_failure_raises(self, server):
        server.fail_put_times = {0: 99}
        with pytest.raises(qfc.QualityFeedbackError) as ei:
            qfc.submit(_payload(n=3), CROPS, base=server.base)
        assert ei.value.code == "upload_incomplete"

    def test_rate_limited(self, server):
        server.create_status = 429
        with pytest.raises(qfc.QualityFeedbackError) as ei:
            qfc.submit(_payload(n=3), CROPS, base=server.base)
        assert ei.value.code == "rate_limited"

    def test_network_failure(self):
        with pytest.raises(qfc.QualityFeedbackError) as ei:
            qfc.submit(_payload(n=3), CROPS, base="http://127.0.0.1:9")
        assert ei.value.code == "network"

    def test_forget(self, server):
        assert qfc.forget(IID, base=server.base) == 3
        assert server.calls == [("DELETE", f"/v1/feedback/quality/installs/{IID}")]

    def test_api_base_env_override(self, monkeypatch):
        monkeypatch.delenv(qfc.API_BASE_ENV, raising=False)
        assert qfc.api_base() == "https://perchapi.projectkestrel.org"
        monkeypatch.setenv(qfc.API_BASE_ENV, "http://127.0.0.1:8787/")
        assert qfc.api_base() == "http://127.0.0.1:8787"

    def test_install_id_shape(self):
        a, b = qfc.new_install_id(), qfc.new_install_id()
        assert qf.INSTALL_ID_RE.match(a) and a != b

    def test_log_roundtrip_has_no_paths(self, tmp_path):
        path = str(tmp_path / "log.jsonl")
        p = _payload(n=3, ranking=[[0], [1, 2]])
        qfc.append_log(qfc.log_entry("qfs_1", p), path)
        qfc.append_log(qfc.log_entry("qfs_2", p), path)
        entries = qfc.read_log(path)
        assert [e["submission_id"] for e in entries] == ["qfs_2", "qfs_1"]
        assert entries[0]["ranking"] == [[0], [1, 2]] and entries[0]["n"] == 3
        text = Path(path).read_text()
        assert IID not in text and "crop" not in text and "/" not in text.replace("\\/", "")
        qfc.clear_log(path)
        assert qfc.read_log(path) == []


# ── Bridge ─────────────────────────────────────────────────────────────────


def _make_folder(tmp_path) -> Path:
    root = tmp_path / "photos"
    crop_dir = root / ".kestrel" / "crop"
    crop_dir.mkdir(parents=True)
    recs = []
    for name, q, s, l in (("IMG_1.CR3", 0.8, 0.8, 0.3), ("IMG_2.CR3", 0.6, 0.6, 0.7),
                          ("IMG_3.CR3", 0.2, 0.2, 0.1), ("IMG_4.CR3", 0.5, 0.5, 0.5)):
        rel = f".kestrel/crop/{name[:-4]}_crop_0.jpg"
        (root / rel).write_bytes(_jpeg_with_metadata((400, 400)))
        recs.append({"filename": name, "species": "Roseate Spoonbill", "family": "Ibis/Spoonbill",
                     "quality": q, "crop_path": rel, "scene_count": 1 if name != "IMG_4.CR3" else 2,
                     "quality_siglip": s, "quality_legacy": l})
    pd.DataFrame(recs).to_csv(root / ".kestrel" / "kestrel_database.csv", index=False)
    return root


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    store = {"quality_model": "siglip"}
    monkeypatch.setattr(api_bridge, "load_persisted_settings", lambda: dict(store))
    monkeypatch.setattr(api_bridge, "save_persisted_settings", lambda d: (store.clear(), store.update(d)))
    log = str(tmp_path / "qf_log.jsonl")
    monkeypatch.setattr(qfc, "default_log_path", lambda: log)
    api = api_bridge.Api()
    notes = []
    api._qf_notify = notes.append
    return api, store, log, notes


class TestBridge:
    def test_candidates_hide_scores_and_paths(self, bridge, tmp_path):
        api, store, _, _ = bridge
        root = _make_folder(tmp_path)
        res = api.get_quality_feedback_candidates(str(root), "1")
        assert res["success"] and res["n"] == 3
        assert [f["index"] for f in res["frames"]] == res["shown_order"]
        blob = json.dumps(res)
        assert "quality" not in blob and ".kestrel" not in blob and "IMG_" not in blob
        assert res["frames"][0]["data_url"].startswith("data:image/jpeg;base64,")

    def test_candidates_by_filenames_and_jail(self, bridge, tmp_path):
        api, *_ = bridge
        root = _make_folder(tmp_path)
        res = api.get_quality_feedback_candidates(str(root), "x", ["IMG_1.CR3", "IMG_4.CR3"])
        assert res["success"] and res["n"] == 2
        # A crop path escaping the root is ignored.
        df = pd.read_csv(root / ".kestrel" / "kestrel_database.csv")
        df.loc[0, "crop_path"] = "../../etc/passwd"
        df.to_csv(root / ".kestrel" / "kestrel_database.csv", index=False)
        res = api.get_quality_feedback_candidates(str(root), "x", ["IMG_1.CR3", "IMG_4.CR3"])
        assert not res["success"] and res["error"] == "not_enough_frames"

    def _submit(self, api, root, **extra):
        cand = api.get_quality_feedback_candidates(str(root), "1")
        payload = {"token": cand["token"], "ranking": [[0], [1, 2]], "thumbs": "down",
                   "duration_ms": 5000, "consent": True}
        payload.update(extra)
        return api.submit_quality_feedback(payload)

    def test_submit_end_to_end(self, bridge, tmp_path, server, monkeypatch):
        api, store, log, notes = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, server.base)
        root = _make_folder(tmp_path)
        r = self._submit(api, root)
        assert r == {"success": True, "started": True}
        api._qf_last_thread.join(10)
        assert notes == [{"ok": True, "submission_id": "qfs_" + "b" * 32}]
        assert qf.INSTALL_ID_RE.match(store["quality_feedback_install_id"])
        sent = server.last_create
        assert sent["install_id"] == store["quality_feedback_install_id"]
        assert sent["active_scorer"] == "siglip" and sent["species"] == ["Roseate Spoonbill"]
        assert [c["quality_siglip"] for c in sent["crops"]] == [0.8, 0.6, 0.2]
        for i, c in enumerate(sent["crops"]):
            assert c["bytes"] == len(server.objects[i])
            with Image.open(io.BytesIO(server.objects[i])) as im:
                assert not im.getexif()
        entries = qfc.read_log(log)
        assert len(entries) == 1 and entries[0]["species"] == ["Roseate Spoonbill"]
        assert str(root) not in Path(log).read_text()

    def test_pre_v3_folder_reports_legacy_scores(self, bridge, tmp_path, server, monkeypatch):
        # A folder analysed before pipeline 3 has only ``quality`` (= the legacy
        # CNN); the submission must say so instead of claiming SigLIP / v3.
        api, store, log, notes = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, server.base)
        root = _make_folder(tmp_path)
        df = pd.read_csv(root / ".kestrel" / "kestrel_database.csv")
        df.drop(columns=["quality_siglip", "quality_legacy"]).to_csv(
            root / ".kestrel" / "kestrel_database.csv", index=False)
        assert self._submit(api, root)["success"]
        api._qf_last_thread.join(10)
        assert notes and notes[0]["ok"]
        sent = server.last_create
        assert sent["active_scorer"] == "legacy"
        assert sent["client"]["pipeline_version"] == 2
        assert [c["quality_legacy"] for c in sent["crops"]] == [c["quality"] for c in sent["crops"]]
        assert all(c["quality_siglip"] is None for c in sent["crops"])

    def test_v3_folder_reports_pipeline_major(self, bridge, tmp_path, server, monkeypatch):
        api, store, log, notes = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, server.base)
        root = _make_folder(tmp_path)
        (root / ".kestrel" / "kestrel_metadata.json").write_text(json.dumps({"pipeline_major": 3}))
        assert self._submit(api, root)["success"]
        api._qf_last_thread.join(10)
        assert server.last_create["client"]["pipeline_version"] == 3
        assert server.last_create["active_scorer"] == "siglip"

    def test_network_failure_leaves_no_log(self, bridge, tmp_path, monkeypatch):
        api, store, log, notes = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, "http://127.0.0.1:9")
        root = _make_folder(tmp_path)
        assert self._submit(api, root)["success"]
        api._qf_last_thread.join(10)
        assert notes == [{"ok": False, "error": "network"}]
        assert not os.path.exists(log)

    def test_consent_required_in_ask_mode(self, bridge, tmp_path, server, monkeypatch):
        api, store, log, _ = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, server.base)
        root = _make_folder(tmp_path)
        assert self._submit(api, root, consent=False) == {"success": False, "error": "consent_required"}
        assert server.calls == [] and "quality_feedback_install_id" not in store

    def test_always_mode_needs_no_per_submit_consent(self, bridge, tmp_path, server, monkeypatch):
        api, store, *_ = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, server.base)
        store["quality_feedback_mode"] = "always"
        root = _make_folder(tmp_path)
        assert self._submit(api, root, consent=False)["success"]
        api._qf_last_thread.join(10)
        assert server.objects

    def test_never_mode_uploads_nothing(self, bridge, tmp_path, server, monkeypatch):
        api, store, *_ = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, server.base)
        root = _make_folder(tmp_path)
        cand = api.get_quality_feedback_candidates(str(root), "1")
        store["quality_feedback_mode"] = "never"
        r = api.submit_quality_feedback({"token": cand["token"], "ranking": [[0], [1], [2]],
                                         "thumbs": "down", "duration_ms": 1, "consent": True})
        assert r == {"success": False, "error": "feedback_disabled"}
        assert api.get_quality_feedback_candidates(str(root), "1")["error"] == "feedback_disabled"
        assert server.calls == []

    def test_bad_ranking_and_token(self, bridge, tmp_path):
        api, *_ = bridge
        root = _make_folder(tmp_path)
        assert self._submit(api, root, ranking=[[0], [1]])["error"] == "invalid_ranking"
        assert api.submit_quality_feedback({"token": "nope", "ranking": [[0], [1]], "thumbs": "up",
                                            "consent": True})["error"] == "unknown_token"

    def test_forget_regenerates_install_id(self, bridge, tmp_path, server, monkeypatch):
        api, store, log, _ = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, server.base)
        store["quality_feedback_install_id"] = IID
        qfc.append_log({"submission_id": "qfs_x"}, log)
        assert api.forget_quality_feedback() == {"success": True, "deleted": 3}
        assert ("DELETE", f"/v1/feedback/quality/installs/{IID}") in server.calls
        new = store["quality_feedback_install_id"]
        assert qf.INSTALL_ID_RE.match(new) and new != IID
        assert qfc.read_log(log) == []

    def test_forget_failure_keeps_id(self, bridge, monkeypatch):
        api, store, *_ = bridge
        monkeypatch.setenv(qfc.API_BASE_ENV, "http://127.0.0.1:9")
        store["quality_feedback_install_id"] = IID
        assert api.forget_quality_feedback() == {"success": False, "error": "network"}
        assert store["quality_feedback_install_id"] == IID

    def test_install_id_never_round_trips_through_js(self, bridge):
        api, store, *_ = bridge
        store["quality_feedback_install_id"] = IID
        assert "quality_feedback_install_id" not in api.get_settings()["settings"]
        api.save_settings_data({"quality_feedback_install_id": "qfi_" + "c" * 32, "editor": "gimp"})
        assert store["quality_feedback_install_id"] == IID and store["editor"] == "gimp"

    def test_set_mode(self, bridge):
        api, store, *_ = bridge
        assert api.get_quality_feedback_mode()["mode"] == "ask"
        assert api.set_quality_feedback_mode("never")["success"]
        assert store["quality_feedback_mode"] == "never"
        assert not api.set_quality_feedback_mode("sometimes")["success"]


def test_settings_sanitizer():
    import settings_utils as su
    out = su._sanitize_settings_payload({
        "quality_feedback_mode": "bogus",
        "quality_feedback_install_id": "qfi_NOTHEX",
    })
    assert out["quality_feedback_mode"] == "ask"
    assert out["quality_feedback_install_id"] == ""
    out = su._sanitize_settings_payload({"quality_feedback_mode": "always", "quality_feedback_install_id": IID})
    assert out == {"quality_feedback_mode": "always", "quality_feedback_install_id": IID}
