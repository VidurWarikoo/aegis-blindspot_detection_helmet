"""
Near-miss clip recorder.

Proof-of-concept feature: whenever a session's threat score reaches HIGH, capture a
short clip spanning a couple of seconds before and after that moment, so the dashboard
can show what an actual near miss looked like instead of just a text row. This is
deliberately simple for a POC: no database table, no cloud storage, just mp4 files on
disk, capped at the 3 most recent so the demo never accumulates unbounded clips.

How it works:
  - Every processed frame is appended to a small rolling per-session buffer
    (record_frame), which keeps only the last PRE_EVENT_SECONDS worth of frames.
  - The moment a HIGH alert fires, maybe_start_clip() snapshots that buffer as the
    "before" half of the clip and starts collecting the next POST_EVENT_SECONDS of
    incoming frames as the "after" half.
  - Once enough post-event frames have arrived, the two halves are stitched together
    and written out as a single mp4 via cv2.VideoWriter, and anything past the 3
    newest clips on disk is deleted.

CLIPS_DIR defaults to living next to this file, which is wiped on every Railway
redeploy since that's the container's own disk. Set CLIPS_DIR to a path on the same
mounted volume used for DB_PATH (for example /data/clips) to make clips survive
across deploys.
"""

import glob
import os
import threading
import time
from collections import deque

import cv2

PRE_EVENT_SECONDS = 2.0
POST_EVENT_SECONDS = 2.0
MAX_CLIPS = 3
CLIP_FPS = 8.0  # matches the helmet's TARGET_FPS; simulated sessions will just play
                # back slightly off from their source rate, which is fine for a POC

CLIPS_DIR = os.environ.get("CLIPS_DIR", os.path.join(os.path.dirname(__file__), "clips"))
os.makedirs(CLIPS_DIR, exist_ok=True)

_lock = threading.Lock()
_buffers = {}            # session_id -> deque[(timestamp, frame)]
_active_recordings = {}  # session_id -> {pre_frames, post_frames, trigger_time, meta}


def _prune(buf, now):
    while buf and now - buf[0][0] > PRE_EVENT_SECONDS:
        buf.popleft()


def record_frame(session_id, frame):
    """Call on every processed frame, live helmet or simulated, so a rolling
    pre-event buffer is always warm and any clip currently mid-capture keeps
    getting fed."""
    now = time.time()
    finished = None
    with _lock:
        buf = _buffers.setdefault(session_id, deque())
        buf.append((now, frame.copy()))
        _prune(buf, now)

        rec = _active_recordings.get(session_id)
        if rec is not None:
            rec["post_frames"].append(frame.copy())
            if now - rec["trigger_time"] >= POST_EVENT_SECONDS:
                finished = rec
                del _active_recordings[session_id]

    if finished is not None:
        _finalize(finished)


def maybe_start_clip(session_id, alert):
    """Call right after a HIGH alert is detected. No-op if a clip is already being
    captured for this session, so a sustained threat doesn't spawn overlapping
    recordings."""
    with _lock:
        if session_id in _active_recordings:
            return
        buf = _buffers.get(session_id, deque())
        _active_recordings[session_id] = {
            "pre_frames": [f for _, f in buf],
            "post_frames": [],
            "trigger_time": time.time(),
            "meta": alert,
        }


def _finalize(rec):
    frames = rec["pre_frames"] + rec["post_frames"]
    if not frames:
        return
    h, w = frames[0].shape[:2]
    meta = rec["meta"]
    safe_ts = meta["timestamp"].replace(":", "-").replace("+", "")
    filename = f"nearmiss_{safe_ts}_{meta['class']}_{meta['side']}.mp4"
    path = os.path.join(CLIPS_DIR, filename)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(path, fourcc, CLIP_FPS, (w, h))
    try:
        for f in frames:
            writer.write(f)
    finally:
        writer.release()

    _enforce_retention()


def _enforce_retention():
    files = sorted(
        glob.glob(os.path.join(CLIPS_DIR, "nearmiss_*.mp4")),
        key=os.path.getmtime, reverse=True
    )
    for stale in files[MAX_CLIPS:]:
        try:
            os.remove(stale)
        except OSError:
            pass


def list_clips():
    """Newest first, capped at MAX_CLIPS. Filenames encode timestamp/class/side so
    the dashboard doesn't need a separate metadata store for a POC."""
    files = sorted(
        glob.glob(os.path.join(CLIPS_DIR, "nearmiss_*.mp4")),
        key=os.path.getmtime, reverse=True
    )
    return [os.path.basename(f) for f in files[:MAX_CLIPS]]
