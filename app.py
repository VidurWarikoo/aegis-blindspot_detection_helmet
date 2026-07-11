"""
Aegis, hosted computer vision service for real time blind spot threat assessment.

This is the cloud brain in a distributed architecture: the physical helmet is now a
thin edge sensor (see helmet_client.py) that only captures and streams frames, all
detection, tracking, and threat scoring described in threat_engine.py runs here.

Two ways data reaches this service:
  1. Live stream from a real helmet, over the WebSocket at /ws/helmet.
  2. A pre recorded clip replayed in real time via POST /simulate_stream, for demoing
     the full pipeline without needing the physical hardware connected.

Both paths run frames through the same tracker, annotate them, and broadcast the
result to every browser connected at /ws/dashboard, which is what the dashboard page
served at / displays live. Any alert at medium or high severity is logged to a local
SQLite database and is queryable at GET /api/alerts for the dashboard's history and
charts view.

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
import sqlite3
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta

import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template
from flask_sock import Sock
from ultralytics import YOLO

from threat_engine import ThreatTracker, VEHICLE_CLASSES, draw_annotations, encode_jpeg

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 30 MB upload cap
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(app)

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv"}
MAX_VIDEO_FRAMES = 900  # about 30s at 30fps, enough for a demo clip or a simulate run

MODEL_PATH = os.environ.get("MODEL_PATH", os.path.join(os.path.dirname(__file__), "yolov8n.pt"))
DB_PATH = os.path.join(os.path.dirname(__file__), "aegis.db")

model = YOLO(MODEL_PATH)  # shared instance, only used by /analyze which never tracks

dashboard_clients = set()
dashboard_lock = threading.Lock()


def new_model():
    """A fresh YOLO instance per stream, so persist=True tracking state never bleeds
    across concurrent helmet or simulated sessions."""
    return YOLO(MODEL_PATH)


def init_db():
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


def get_alerts(days=30):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM alerts WHERE timestamp >= ? ORDER BY timestamp DESC", (cutoff,)
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
        dead = []
        for client in dashboard_clients:
            try:
                client.send(message)
            except Exception:
                dead.append(client)
        for d in dead:
            dashboard_clients.discard(d)


def process_and_broadcast(frame, tracker, model_instance, source, session_id, helmet_ws=None):
    """Runs one frame through detection and scoring, annotates it, broadcasts the
    result to every connected dashboard, and logs plus pushes an alert if the threat
    level is medium or high. This is the single processing path shared by a live
    helmet stream and a simulated upload, so both produce an identical dashboard
    experience."""
    h, w = frame.shape[:2]
    results = model_instance.track(frame, persist=True, classes=VEHICLE_CLASSES, conf=0.4, verbose=False)

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
        log_alert(alert)
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


@app.route("/", methods=["GET"])
def dashboard():
    return render_template("dashboard.html")


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
    return jsonify(get_alerts(days))


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
        try:
            while frame_num < MAX_VIDEO_FRAMES:
                ret, frame = cap.read()
                if not ret:
                    break
                frame_num += 1
                process_and_broadcast(frame, tracker, sim_model, source="simulated", session_id=session_id)
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
    """
    tracker = ThreatTracker()
    helmet_model = new_model()
    session_id = str(uuid.uuid4())[:8]
    print(f"[aegis] helmet session {session_id} connected")
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
            process_and_broadcast(frame, tracker, helmet_model, source="helmet", session_id=session_id, helmet_ws=ws)
    except Exception:
        traceback.print_exc()
    finally:
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
