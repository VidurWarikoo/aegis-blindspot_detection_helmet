"""
Aegis, hosted archive and review service for a helmet that detects and alerts fully
on its own.

This used to be the cloud brain in a distributed architecture: the helmet streamed
raw frames here and waited for a verdict before it could warn the rider. That put a
safety-critical alert behind a network round trip, which is no longer acceptable, so
detection, tracking, and threat scoring (threat_engine.py) now run locally on the Pi
(see helmet_local.py), and the LEDs/motor fire before any request to this service is
even made. This service's job is now purely archival: it receives whatever the Pi
durably queued and uploaded via POST /api/ingest, and makes it browsable at /history.

Two things still reach this service:
  1. Async, best-effort uploads from a real helmet, via POST /api/ingest, whenever a
     connection happens to be available. Never on the safety-critical path.
  2. The legacy live path (WS /ws/helmet, WS /ws/dashboard, POST /simulate_stream),
     kept only to power the optional /demo page so the detection pipeline can be
     shown running without the physical helmet in the room. No real ride ever goes
     through this path anymore.

Any alert at medium or high severity is logged to a local SQLite database and is
queryable at GET /api/alerts for the /history page's charts and table. HIGH events
carry a near-miss clip, recorded and encoded entirely on the Pi, browsable via
GET /api/clips and GET /api/clips/<filename>.

The original request/response endpoints from the first version of this service are
kept as is, since they are documented in SKILL.md as the interface an agent can call
directly for a one shot threat read on a single image or video file:
  GET  /health            liveness check
  POST /analyze           single image in, single frame threat read out
  POST /analyze_video     full video in, full temporal pipeline out

Hardening notes:
  - Model weights are bundled in the repo instead of downloaded at runtime, so a cold
    start never depends on an external download completing.
  - Every helmet or simulated stream gets its own YOLO model instance and its own
    ThreatTracker, so concurrent streams never share tracking state and never produce
    cross contaminated track IDs.
  - Every error path returns JSON with a real HTTP status code.
  - Uploads are capped in size and video analysis is capped in frame count.
"""

import base64
import json
import os
import queue
import random
import sqlite3
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta

import cv2
import gevent
import numpy as np
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, send_from_directory
from flask_sock import Sock
from ultralytics import YOLO

import near_miss
from threat_engine import ThreatTracker, VEHICLE_CLASSES, draw_annotations, encode_jpeg

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 30 MB upload cap
# Needed for Flask's session cookie (the login gate below). This is a demo-flow
# login, not real account security, so a dev default is fine here, but set a real
# SECRET_KEY env var on Railway if this ever needs to resist cookie tampering.
app.secret_key = os.environ.get("SECRET_KEY", "aegis-dev-insecure-secret-change-me")
# NOTE: deliberately no ping_interval here. flask-sock/simple_websocket spawns its
# own background thread per connection to send that keepalive ping, and under the
# gevent worker that thread's socket write can land at the exact same moment our own
# code (broadcast_to_dashboards / helmet_ws.send) is writing to the same socket.
# gevent's socket wrapper only tolerates one writer at a time and raises
# ConcurrentObjectUseError when two greenlets/threads hit it together, which was
# observed in production killing that ping thread and, worse, occasionally taking our
# own send() down with it, silently evicting a live dashboard client from
# dashboard_clients with no error surfaced anywhere. Both clients already handle
# reconnects (helmet_client.py pings client-side via websocket-client's own
# ping_interval, and the dashboard JS auto-reconnects on close), so we don't need the
# server-initiated keepalive badly enough to justify the race it introduces.
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": None}
sock = Sock(app)

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv"}
MAX_VIDEO_FRAMES = 900  # about 30s at 30fps, enough for a demo clip or a simulate run
ALERT_LOG_COOLDOWN_SECONDS = 3.0  # minimum gap before the same track can log again

MODEL_PATH = os.environ.get("MODEL_PATH", os.path.join(os.path.dirname(__file__), "yolov8n.pt"))
# defaults to living next to app.py, which is wiped on every redeploy since that is
# the container's own disk. Set DB_PATH to a path on a mounted Railway Volume (for
# example /data/aegis.db) to make alert history survive across deploys.
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "aegis.db"))

model = YOLO(MODEL_PATH)  # shared instance, only used by /analyze which never tracks

dashboard_clients = set()
dashboard_lock = threading.Lock()
# A real helmet and a /simulate_stream run (or two helmets) can broadcast to the same
# dashboard viewer at the same time, each on its own greenlet. gevent's socket only
# tolerates one writer at a time, so every client gets its own send lock to keep
# concurrent broadcasts from colliding on the same underlying socket.
dashboard_send_locks = {}

# The rider's personal sync code. Two things both key off this single value now:
# the /sync step asked once right after a fresh login (see sync_code() below)
# before the account can view any ride data, and the "Helmet sync" panel on the
# /demo page, which just displays it for reference. They're intentionally the same
# code, not two separate ones, so there's nothing to keep in sync between them.
# Overridable via env var so it isn't hardcoded in a real deployment; defaults to
# the value used during development.
SYNC_ACCESS_CODE = os.environ.get("SYNC_ACCESS_CODE", "123456")
SYNC_CODE = SYNC_ACCESS_CODE
pairing_lock = threading.Lock()
helmet_paired = False


def mark_helmet_paired():
    global helmet_paired
    with pairing_lock:
        helmet_paired = True


def new_model():
    """A fresh YOLO instance per stream, so persist=True tracking state never bleeds
    across concurrent helmet or simulated sessions."""
    return YOLO(MODEL_PATH)


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            class TEXT NOT NULL,
            side TEXT NOT NULL,
            score REAL NOT NULL,
            level TEXT NOT NULL,
            source TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def log_alert(alert):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO alerts (timestamp, class, side, score, level, source) VALUES (?, ?, ?, ?, ?, ?)",
        (alert["timestamp"], alert["class"], alert["side"], alert["score"], alert["level"], alert["source"])
    )
    conn.commit()
    conn.close()


def get_alerts(days=30, limit=500):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM alerts WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?", (cutoff, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


init_db()


def _ext(filename):
    return os.path.splitext(filename or "")[1].lower()


def _error(message, status=400):
    return jsonify({"error": message}), status


def broadcast_to_dashboards(message):
    with dashboard_lock:
        clients_snapshot = list(dashboard_clients)
    dead = []
    for client in clients_snapshot:
        lock = dashboard_send_locks.get(client)
        if lock is None:
            continue
        with lock:
            try:
                client.send(message)
            except Exception:
                dead.append(client)
    if dead:
        with dashboard_lock:
            for d in dead:
                dashboard_clients.discard(d)
                dashboard_send_locks.pop(d, None)


def process_and_broadcast(frame, tracker, model_instance, source, session_id, helmet_ws=None):
    """Runs one frame through detection and scoring, annotates it, broadcasts the
    result to every connected dashboard, and logs plus pushes an alert if the threat
    level is medium or high. This is the single processing path shared by a live
    helmet stream and a simulated upload, so both produce an identical dashboard
    experience."""
    h, w = frame.shape[:2]
    infer_start = time.time()
    results = model_instance.track(frame, persist=True, classes=VEHICLE_CLASSES, conf=0.4, verbose=False)
    infer_ms = (time.time() - infer_start) * 1000
    if infer_ms > 500:
        # A single YOLO forward pass on this frame took over half a second. If this
        # keeps showing up, the feed's real-time lag is coming from raw inference
        # time on the host's CPU, not from queueing or network overhead, and the fix
        # is a smaller model, a lower input resolution, or more CPU on the host, not
        # more concurrency plumbing.
        print(f"[aegis] slow inference: {infer_ms:.0f}ms source={source} session={session_id}")

    detections = []
    if results[0].boxes.id is not None:
        for box in results[0].boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            detections.append({
                "track_id": int(box.id[0]),
                "cls_id": int(box.cls[0]),
                "x1": x1, "y1": y1, "x2": x2, "y2": y2
            })

    worst, level, all_objects = tracker.update(detections, w, h)
    annotated = draw_annotations(frame, all_objects, worst, level)
    near_miss.record_frame(session_id, annotated)
    jpeg = encode_jpeg(annotated)
    if jpeg is None:
        return

    frame_msg = json.dumps({
        "type": "frame",
        "source": source,
        "session_id": session_id,
        "image": base64.b64encode(jpeg).decode("ascii"),
        "level": level,
        "worst": worst
    })
    broadcast_to_dashboards(frame_msg)

    if worst and level in ("medium", "high"):
        alert = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "class": worst["class"],
            "side": worst["side"],
            "score": worst["score"],
            "level": level,
            "source": source
        }

        # A sustained threat scores medium/high on every frame it is visible, which
        # at eight frames a second would write a near duplicate row every 125ms. Only
        # write to history when this is a different object than the one we last
        # logged, or enough time has passed that it is reasonable to treat the same
        # object as a fresh event worth recording again.
        now_ts = time.time()
        is_new_event = (
            worst["track_id"] != tracker.last_logged_track_id
            or (now_ts - tracker.last_logged_at) > ALERT_LOG_COOLDOWN_SECONDS
        )
        if is_new_event:
            tracker.last_logged_track_id = worst["track_id"]
            tracker.last_logged_at = now_ts
            log_alert(alert)

        if level == "high":
            # Near-miss clips are for the genuinely dangerous moments, not every
            # "elevated proximity" medium alert, so only HIGH triggers a recording.
            near_miss.maybe_start_clip(session_id, alert)

        broadcast_to_dashboards(json.dumps({"type": "alert", **alert}))
        if helmet_ws is not None:
            try:
                helmet_ws.send(json.dumps({"type": "alert", "level": level, "side": worst["side"]}))
            except Exception:
                pass


@app.errorhandler(413)
def too_large(_e):
    return _error("upload too large (30 MB max)", 413)


@app.errorhandler(500)
def server_error(_e):
    return _error("internal error processing the request", 500)


@app.before_request
def require_login_for_pages():
    """Gates only the two personal, ride-data pages behind the demo login, plus a
    second sync-code step. The marketing/product home page is public on purpose -
    it's what a visitor should land on with no account, and everything else (the
    WebSockets, /simulate_stream, /api/*, /health, /analyze*) also stays open,
    because the physical helmet has no browser and carries no session cookie -
    gating those too would just break the real hardware."""
    if request.endpoint in ("demo", "history"):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        if not session.get("sync_verified"):
            return redirect(url_for("sync_code", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    """Demo-flow login: this is not real account security, there is no user store
    and no password check, any non-empty email/password is accepted. It exists so
    the product can be demoed with a believable 'sign in, then check your ride
    history' flow. Every logged-in browser still sees the exact same shared
    history underneath."""
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if not email or not password:
            return render_template("login.html", error="Enter an email and password to continue.")
        session["logged_in"] = True
        session["email"] = email
        # A fresh sign-in always has to re-clear the sync-code step below, even if
        # this same browser verified it earlier - the flag only survives inside an
        # active session, and logout() wipes the whole session anyway, but setting
        # it explicitly here makes that guarantee obvious at the point of login
        # rather than relying on it implicitly.
        session["sync_verified"] = False
        next_path = request.args.get("next") or url_for("history")
        return redirect(url_for("sync_code", next=next_path))
    return render_template("login.html", error=None)


@app.route("/sync", methods=["GET", "POST"])
def sync_code():
    """Second step after a fresh login: the rider enters their helmet's personal
    sync code before the account can see any ride data. This only ever gets asked
    once per session - require_login_for_pages() only routes here when
    sync_verified is missing, and a successful submission sets it for the rest of
    the session, so navigating between /history and /demo afterward never asks
    again. Logging out clears the whole session, so it's asked again on the next
    fresh sign-in."""
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    next_path = request.args.get("next") or url_for("history")

    if session.get("sync_verified"):
        return redirect(next_path)

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if code == SYNC_ACCESS_CODE:
            session["sync_verified"] = True
            return redirect(next_path)
        return render_template("sync.html", error="Incorrect code, try again.", next=next_path)

    return render_template("sync.html", error=None, next=next_path)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/pairing", methods=["GET"])
def api_pairing():
    with pairing_lock:
        paired = helmet_paired
    return jsonify({"code": SYNC_CODE, "paired": paired})


@app.route("/api/clips", methods=["GET"])
def api_clips():
    return jsonify(near_miss.list_clips())


@app.route("/api/clips/<path:filename>", methods=["GET"])
def api_clip_file(filename):
    return send_from_directory(near_miss.CLIPS_DIR, filename)


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """
    Receives one alert from a helmet that is doing its own detection and scoring
    locally (see helmet_local.py) and firing its LEDs/motor before this request is
    even sent. This endpoint is never on the safety-critical path, it only exists so
    the alert (and, for HIGH events, the near-miss clip the Pi already recorded and
    encoded on its own) becomes browsable on the /history page. Uploads are async and
    best-effort from the Pi's side (see outbox.py), so this can arrive anywhere from
    seconds to hours after the alert actually fired.

    Expects multipart/form-data with fields timestamp, class, side, score, level, and
    optionally source (defaults to "helmet"), plus an optional file field "clip" with
    an already-encoded mp4 for HIGH severity events.
    """
    required = ("timestamp", "class", "side", "score", "level")
    missing = [f for f in required if f not in request.form]
    if missing:
        return _error(f"missing required field(s): {', '.join(missing)}")

    try:
        score = float(request.form["score"])
    except ValueError:
        return _error("score must be a number")

    alert = {
        "timestamp": request.form["timestamp"],
        "class": request.form["class"],
        "side": request.form["side"],
        "score": score,
        "level": request.form["level"],
        "source": request.form.get("source", "helmet"),
    }
    log_alert(alert)
    mark_helmet_paired()

    clip_saved = False
    if "clip" in request.files and request.files["clip"].filename:
        near_miss.save_incoming_clip(request.files["clip"], alert)
        clip_saved = True

    return jsonify({"status": "ok", "logged": True, "clip_saved": clip_saved})


@app.route("/", methods=["GET"])
def index():
    # Public product/marketing page - what the helmet is, how it works, and photos
    # of the build. Deliberately not gated behind login, unlike /history and /demo,
    # since this is the page a visitor with no account should land on. The old
    # behavior of redirecting straight to /history moved to the "View ride history"
    # button on this page instead.
    return render_template("home.html", user_email=session.get("email"))


@app.route("/demo", methods=["GET"])
def demo():
    return render_template("dashboard.html", user_email=session.get("email"))


@app.route("/history", methods=["GET"])
def history():
    return render_template("history.html", user_email=session.get("email"))


@app.route("/api/info", methods=["GET"])
def info():
    return jsonify({
        "service": "aegis",
        "status": "ok",
        "endpoints": {
            "GET /health": "liveness check",
            "POST /analyze": "single image -> single frame threat assessment",
            "POST /analyze_video": "video clip -> full temporal threat analysis",
            "WS /ws/helmet": "live frame ingest from a physical helmet",
            "WS /ws/dashboard": "live annotated frames and alerts, powers the dashboard",
            "POST /simulate_stream": "replay an uploaded clip through the live pipeline",
            "GET /api/alerts": "alert history for the dashboard's charts"
        }
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "aegis", "model_loaded": model is not None})


@app.route("/api/alerts", methods=["GET"])
def api_alerts():
    days = int(request.args.get("days", 30))
    limit = int(request.args.get("limit", 500))
    return jsonify(get_alerts(days, limit))


DEMO_CLASSES = ["car", "truck", "bus", "motorcycle", "bicycle", "person"]
DEMO_CLASS_WEIGHTS = [5, 3, 2, 2, 2, 1]
DEMO_SIDES = ["LEFT", "RIGHT"]


@app.route("/api/seed_demo_data", methods=["POST"])
def seed_demo_data():
    """
    Inserts a batch of realistic looking alert history spanning the past week, tagged
    with source 'demo' so it is always identifiable and always safe to clear later.
    This exists purely so the history page's charts and table have something worth
    looking at before enough genuine usage has accumulated to populate them on their
    own. Call this once against the live deployment, it never touches or replaces
    real alerts, it only adds rows. Safe to call more than once if you want more data.
    """
    now = datetime.utcnow()
    inserted = 0
    for days_ago in range(6, -1, -1):
        day = now - timedelta(days=days_ago)
        count = random.randint(2, 9)
        for _ in range(count):
            cls = random.choices(DEMO_CLASSES, weights=DEMO_CLASS_WEIGHTS)[0]
            level = random.choices(["medium", "high"], weights=[7, 3])[0]
            score = round(random.uniform(0.13, 0.27), 4) if level == "medium" else round(random.uniform(0.29, 0.55), 4)
            ts = day.replace(
                hour=random.randint(6, 21),
                minute=random.randint(0, 59),
                second=random.randint(0, 59),
                microsecond=random.randint(0, 999999)
            )
            log_alert({
                "timestamp": ts.isoformat() + "Z",
                "class": cls,
                "side": random.choice(DEMO_SIDES),
                "score": score,
                "level": level,
                "source": "demo"
            })
            inserted += 1
    return jsonify({"status": "ok", "inserted": inserted})


@app.route("/api/clear_demo_data", methods=["POST"])
def clear_demo_data():
    """Removes every alert tagged source 'demo', leaving genuine helmet and simulated
    alerts untouched. Run this before real judging or before recording the final demo
    video with real footage, so the history page reflects only real activity."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM alerts WHERE source = 'demo'")
    conn.commit()
    removed = cur.rowcount
    conn.close()
    return jsonify({"status": "ok", "removed": removed})


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    Single image in, single threat assessment out. There is no motion history from
    one frame, so this scores proximity only, no approaching credit. Good for a quick
    sanity check of the detector. For the full pipeline, use /analyze_video or stream
    through /ws/helmet.
    """
    if "file" not in request.files:
        return _error("send an image as multipart/form-data field 'file'")

    file = request.files["file"]
    if not file.filename:
        return _error("empty filename")

    ext = _ext(file.filename)
    if ext not in ALLOWED_IMAGE_EXT:
        return _error(f"unsupported image type '{ext}', allowed: {sorted(ALLOWED_IMAGE_EXT)}")

    tmp_path = tempfile.mktemp(suffix=ext)
    try:
        file.save(tmp_path)
        frame = cv2.imread(tmp_path)
        if frame is None:
            return _error("could not decode image, file may be corrupt")

        h, w = frame.shape[:2]
        results = model.predict(frame, classes=VEHICLE_CLASSES, conf=0.4, verbose=False)

        detections = []
        for i, box in enumerate(results[0].boxes):
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            detections.append({
                "track_id": i,
                "cls_id": int(box.cls[0]),
                "x1": x1, "y1": y1, "x2": x2, "y2": y2
            })

        tracker = ThreatTracker()
        worst, level, all_objects = tracker.update(detections, w, h)

        return jsonify({
            "threat_level": level if worst else "none",
            "worst": worst,
            "detections": all_objects
        })
    except Exception:
        traceback.print_exc()
        return _error("failed to process image", 500)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.route("/analyze_video", methods=["POST"])
def analyze_video():
    """
    Full video in, full temporal pipeline out, persistent tracking, trend smoothing,
    convergence checking, and hysteresis, exactly like a live stream. Returns every
    medium or high alert event fired during the clip. This does not push to the
    dashboard, it is the one shot request and response version, for that use
    POST /simulate_stream instead.
    """
    if "file" not in request.files:
        return _error("send a video as multipart/form-data field 'file'")

    file = request.files["file"]
    if not file.filename:
        return _error("empty filename")

    ext = _ext(file.filename)
    if ext not in ALLOWED_VIDEO_EXT:
        return _error(f"unsupported video type '{ext}', allowed: {sorted(ALLOWED_VIDEO_EXT)}")

    tmp_path = tempfile.mktemp(suffix=ext)
    cap = None
    try:
        file.save(tmp_path)
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return _error("could not open video, file may be corrupt or an unsupported codec")

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w == 0 or h == 0:
            return _error("video has no readable frames")

        video_model = new_model()
        tracker = ThreatTracker()
        events = []
        frame_num = 0
        truncated = False

        while True:
            if frame_num >= MAX_VIDEO_FRAMES:
                truncated = True
                break

            ret, frame = cap.read()
            if not ret:
                break
            frame_num += 1

            results = video_model.track(frame, persist=True, classes=VEHICLE_CLASSES, conf=0.4, verbose=False)
            detections = []
            if results[0].boxes.id is not None:
                for box in results[0].boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    detections.append({
                        "track_id": int(box.id[0]),
                        "cls_id": int(box.cls[0]),
                        "x1": x1, "y1": y1, "x2": x2, "y2": y2
                    })

            worst, level, _ = tracker.update(detections, w, h)
            if worst and level in ("medium", "high"):
                events.append({
                    "frame": frame_num,
                    "class": worst["class"],
                    "side": worst["side"],
                    "score": worst["score"],
                    "level": level
                })

        peak = max(events, key=lambda e: e["score"]) if events else None

        return jsonify({
            "frames_analyzed": frame_num,
            "truncated": truncated,
            "alert_count": len(events),
            "peak_threat": peak,
            "events": events
        })
    except Exception:
        traceback.print_exc()
        return _error("failed to process video", 500)
    finally:
        if cap is not None:
            cap.release()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.route("/simulate_stream", methods=["POST"])
def simulate_stream():
    """
    Accepts a pre recorded clip and replays it through the exact same processing path
    as a live helmet, at the clip's own frame rate, broadcasting each annotated frame
    to every connected dashboard. This exists so the full pipeline can be demoed
    convincingly even when the physical helmet is not connected. Returns immediately,
    the actual streaming happens in a background thread.
    """
    if "file" not in request.files:
        return _error("send a video as multipart/form-data field 'file'")

    file = request.files["file"]
    if not file.filename:
        return _error("empty filename")

    ext = _ext(file.filename)
    if ext not in ALLOWED_VIDEO_EXT:
        return _error(f"unsupported video type '{ext}', allowed: {sorted(ALLOWED_VIDEO_EXT)}")

    tmp_path = tempfile.mktemp(suffix=ext)
    file.save(tmp_path)

    def run():
        cap = cv2.VideoCapture(tmp_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 24
        delay = 1.0 / fps if fps > 0 else 1.0 / 24
        tracker = ThreatTracker()
        sim_model = new_model()
        session_id = "sim-" + str(uuid.uuid4())[:8]
        frame_num = 0
        thread_pool = gevent.get_hub().threadpool
        # Lets the pairing panel show "connected" during a demo even without the
        # physical helmet, since a simulated stream is standing in for one.
        mark_helmet_paired()
        try:
            while frame_num < MAX_VIDEO_FRAMES:
                ret, frame = cap.read()
                if not ret:
                    break
                frame_num += 1
                # Same reasoning as /ws/helmet: keep the CPU-bound inference call off
                # the main greenlet so a running simulation never stalls dashboard
                # websocket traffic for other viewers while a frame is processed.
                thread_pool.spawn(
                    process_and_broadcast,
                    frame, tracker, sim_model,
                    source="simulated", session_id=session_id
                ).get()
                time.sleep(delay)
        except Exception:
            traceback.print_exc()
        finally:
            cap.release()
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started", "message": "simulation is now streaming to the dashboard"})


@sock.route("/ws/helmet")
def ws_helmet(ws):
    """
    A real (or simulated, via a client script) helmet connects here and sends a
    continuous sequence of binary JPEG frames. Each frame is run through detection
    and scoring with its own dedicated tracker and model instance, so this session's
    track IDs never collide with any other concurrent session. If a frame produces a
    medium or high alert, a short JSON message is sent back down this same connection
    so the physical helmet can fire its own LEDs and motors immediately, independent
    of whether anyone is watching the dashboard.

    Receiving and inference are decoupled on purpose. YOLO inference is slower than
    the Pi's send rate, so if we ran it inline in this loop, incoming frames would
    queue up behind a slow ws.receive()/process cycle and the feed would drift
    further and further behind real time (this is what caused the ~10-15s lag).
    Instead, this loop's only job is to receive and decode frames as fast as they
    arrive and drop them into a single-slot queue, always discarding whatever stale
    frame was waiting there. A separate worker thread continuously pulls the newest
    available frame and runs detection on it, so the dashboard/helmet always sees
    the most current frame the model can keep up with, never a growing backlog.
    """
    tracker = ThreatTracker()
    helmet_model = new_model()
    session_id = str(uuid.uuid4())[:8]
    print(f"[aegis] helmet session {session_id} connected")
    mark_helmet_paired()

    latest_frame_q = queue.Queue(maxsize=1)
    stop_event = threading.Event()
    thread_pool = gevent.get_hub().threadpool

    def infer_worker():
        while not stop_event.is_set():
            try:
                frame = latest_frame_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                # model_instance.track() is CPU-bound native code (torch/opencv),
                # which does not yield back to gevent's event loop while it runs.
                # threading.Thread under gevent's monkey-patching is a greenlet, not
                # a real OS thread, so running inference directly on it would still
                # stall the whole worker process, including this connection's own
                # ws.receive() loop, for the full duration of every frame. Routing
                # it through gevent's real OS threadpool lets the event loop keep
                # servicing ws.receive() and other connections while inference runs.
                thread_pool.spawn(
                    process_and_broadcast,
                    frame, tracker, helmet_model,
                    source="helmet", session_id=session_id, helmet_ws=ws
                ).get()
            except Exception:
                traceback.print_exc()

    worker = threading.Thread(target=infer_worker, daemon=True)
    worker.start()

    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            if isinstance(data, str):
                continue  # ignore any text control messages for now
            arr = np.frombuffer(data, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                continue
            # Always keep only the newest frame: if the worker hasn't finished
            # the previous one yet, drop it and swap in this one.
            try:
                latest_frame_q.put_nowait(frame)
            except queue.Full:
                try:
                    latest_frame_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    latest_frame_q.put_nowait(frame)
                except queue.Full:
                    pass
    except Exception:
        traceback.print_exc()
    finally:
        stop_event.set()
        print(f"[aegis] helmet session {session_id} disconnected")


@sock.route("/ws/dashboard")
def ws_dashboard(ws):
    """
    A browser viewing the dashboard connects here and receives every frame and alert
    broadcast by process_and_broadcast, from whichever helmet or simulated session is
    currently active. No messages are expected from the browser, this is a one way
    push channel.
    """
    with dashboard_lock:
        dashboard_clients.add(ws)
        dashboard_send_locks[ws] = threading.Lock()
    try:
        while True:
            data = ws.receive(timeout=30)
            if data is None:
                continue
    except Exception:
        pass
    finally:
        with dashboard_lock:
            dashboard_clients.discard(ws)
            dashboard_send_locks.pop(ws, None)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
