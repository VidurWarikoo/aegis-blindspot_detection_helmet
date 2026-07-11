"""
Core threat-scoring engine for Blindspot Guardian.

This is the exact detection + scoring logic validated in detect.py (laptop, dashcam
testing) and helmet_pi.py (Raspberry Pi, live camera) — refactored into a reusable,
stateless-per-instance module so the hosted API (app.py) has one source of truth
instead of duplicating the loop inline.

Fixes baked in here (all validated against real dashcam footage before porting):
  - Trend-window smoothing for area/lateral movement (kills single-frame YOLO jitter)
  - Convergence check: only genuinely converging objects get full "approaching" credit —
    oncoming traffic staying in its own lane does not
  - Stationary proximity cap: a parked/non-approaching object can't hit medium/high
    just from being large in frame, unless it's genuinely point-blank close
  - Hysteresis with a grace period so a track briefly lost to occlusion/re-ID doesn't
    cause the reported "worst" object to flicker
  - Minimum proximity floor so distant background objects can never be flagged
"""

from collections import defaultdict, deque

import cv2

VEHICLE_CLASSES = [0, 1, 2, 3, 5, 7]  # person, bicycle, car, motorcycle, bus, truck
CLASS_NAMES = {0: 'person', 1: 'bicycle', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck'}
CLASS_WEIGHTS = {0: 1.0, 1: 1.0, 2: 1.2, 3: 1.1, 5: 1.5, 7: 1.5}

BOX_COLOR_LOW = (120, 200, 120)     # green, BGR
BOX_COLOR_MEDIUM = (0, 210, 255)    # amber, BGR
BOX_COLOR_HIGH = (0, 0, 255)        # red, BGR
WORST_TRACK_ID_KEY = 'track_id'

ALPHA = 0.5  # weight for proximity
BETA = 0.5   # weight for approach rate

LOW_THRESHOLD = 0.12
HIGH_THRESHOLD = 0.28

TREND_WINDOW = 4               # frames of history used to smooth area/lateral trend
GRACE_FRAMES = 6                # frames a track can vanish before hysteresis gives up on it
MIN_PROXIMITY = 0.004           # objects smaller than this share of frame are ignored entirely
STATIONARY_PROXIMITY_CAP = 0.35 # non-approaching objects below this size are capped at LOW
LATERAL_SPEED_THRESHOLD = 0.015 # avg per-frame sideways movement that flags a "passing" object
HYSTERESIS_MARGIN = 0.25


def calculate_threat_score(proximity_score, approach_rate, cls_id):
    weight = CLASS_WEIGHTS.get(cls_id, 1.0)
    return round(weight * (ALPHA * proximity_score + BETA * approach_rate), 4)


def get_threat_level(score):
    if score >= HIGH_THRESHOLD:
        return 'high'
    elif score >= LOW_THRESHOLD:
        return 'medium'
    else:
        return 'low'


class ThreatTracker:
    """
    Stateful per-video (or per live-stream) tracker. Create ONE instance per video /
    per camera session and feed it frames in order via update() — do not share an
    instance across unrelated videos, since track IDs and history would collide.
    """

    def __init__(self):
        self.area_history = defaultdict(lambda: deque(maxlen=TREND_WINDOW))
        self.cx_history = defaultdict(lambda: deque(maxlen=TREND_WINDOW))
        self.score_history = defaultdict(lambda: deque(maxlen=8))
        self.last_seen = {}
        self.current_worst_id = None
        self.frame_num = 0
        # used by the hosted service to decide when a medium/high result is a new
        # event worth writing to the alert history, versus the same ongoing threat
        # still being scored on the next frame
        self.last_logged_track_id = None
        self.last_logged_at = 0.0

    def update(self, detections, frame_w, frame_h):
        """
        detections: list of dicts with keys track_id, cls_id, x1, y1, x2, y2
        (run detections through model.track()/model.predict() first — this class
        only scores boxes it's handed, it doesn't run YOLO itself).

        Returns (worst_object_or_None, level, all_scored_objects)
        """
        self.frame_num += 1
        frame_area = frame_w * frame_h
        frame_cx = frame_w / 2
        scored_objects = []

        for det in detections:
            track_id = det['track_id']
            cls_id = det['cls_id']
            x1, y1, x2, y2 = det['x1'], det['y1'], det['x2'], det['y2']

            box_area = (x2 - x1) * (y2 - y1)
            cx = (x1 + x2) / 2
            proximity_score = box_area / frame_area if frame_area else 0

            hist_areas = self.area_history[track_id]
            hist_cx = self.cx_history[track_id]

            # compare against the OLDEST sample in the trend window, not just last frame
            if hist_areas and hist_areas[0] > 0:
                approach_rate = max(0, (box_area - hist_areas[0]) / hist_areas[0])
            else:
                approach_rate = 0

            if hist_cx:
                lateral_speed = abs(cx - hist_cx[0]) / frame_w / max(1, len(hist_cx))
                offset_before = abs(hist_cx[0] - frame_cx)
                offset_now = abs(cx - frame_cx)
                converging = offset_now <= offset_before - 4
            else:
                lateral_speed = 0
                converging = False

            # only real convergence gets full "approaching" credit; proximity itself
            # (how close/big it already is) is untouched
            credited_approach_rate = approach_rate if converging else approach_rate * 0.2

            raw_score = calculate_threat_score(proximity_score, credited_approach_rate, cls_id)

            # opposite-lane / passing-by filter
            if lateral_speed > LATERAL_SPEED_THRESHOLD and approach_rate == 0:
                raw_score *= 0.05

            # too far away to matter, regardless of class weight
            if proximity_score < MIN_PROXIMITY:
                raw_score = 0

            # size alone isn't a threat unless it's genuinely point-blank close
            if credited_approach_rate == 0 and proximity_score < STATIONARY_PROXIMITY_CAP:
                raw_score = min(raw_score, LOW_THRESHOLD - 0.001)

            self.area_history[track_id].append(box_area)
            self.cx_history[track_id].append(cx)
            self.last_seen[track_id] = self.frame_num

            self.score_history[track_id].append(raw_score)
            score = sum(self.score_history[track_id]) / len(self.score_history[track_id])

            side = 'LEFT' if cx < frame_cx else 'RIGHT'

            scored_objects.append({
                'track_id': track_id,
                'cls_id': cls_id,
                'class': CLASS_NAMES.get(cls_id, str(cls_id)),
                'score': round(score, 4),
                'side': side,
                'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2
            })

        if not scored_objects:
            return None, 'low', scored_objects

        true_worst = max(scored_objects, key=lambda o: o['score'])
        current_candidates = [o for o in scored_objects if o['track_id'] == self.current_worst_id]

        if current_candidates:
            current_obj = current_candidates[0]
            if true_worst['track_id'] != self.current_worst_id and \
               true_worst['score'] > current_obj['score'] * (1 + HYSTERESIS_MARGIN):
                worst = true_worst
                self.current_worst_id = worst['track_id']
            else:
                worst = current_obj
        elif self.current_worst_id is not None and \
             self.frame_num - self.last_seen.get(self.current_worst_id, -999) <= GRACE_FRAMES:
            worst = true_worst
        else:
            worst = true_worst
            self.current_worst_id = worst['track_id']

        level = get_threat_level(worst['score'])
        return worst, level, scored_objects


def draw_annotations(frame, scored_objects, worst, level):
    """
    Draws a bounding box and label for every scored object directly onto the frame,
    highlighting whichever object is currently the reported worst threat. Used by the
    hosted service to produce annotated JPEGs for the live dashboard, so the browser
    only ever has to display an image, no client side box drawing required.

    frame: a BGR image (numpy array) as returned by cv2.VideoCapture / cv2.imdecode
    scored_objects: the third return value of ThreatTracker.update()
    worst: the first return value of ThreatTracker.update() (or None)
    level: the second return value of ThreatTracker.update()

    Returns the same frame, annotated in place.
    """
    worst_id = worst[WORST_TRACK_ID_KEY] if worst else None

    for obj in scored_objects:
        is_worst = obj[WORST_TRACK_ID_KEY] == worst_id
        obj_level = level if is_worst else get_threat_level(obj['score'])

        if obj_level == 'high':
            color = BOX_COLOR_HIGH
        elif obj_level == 'medium':
            color = BOX_COLOR_MEDIUM
        else:
            color = BOX_COLOR_LOW

        thickness = 3 if is_worst else 1
        cv2.rectangle(frame, (obj['x1'], obj['y1']), (obj['x2'], obj['y2']), color, thickness)

        label = f"{obj['class']} {obj['score']:.2f}"
        cv2.putText(frame, label, (obj['x1'], max(20, obj['y1'] - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    if worst and level in ('medium', 'high'):
        banner_color = BOX_COLOR_HIGH if level == 'high' else BOX_COLOR_MEDIUM
        banner_text = f"{level.upper()} THREAT - {worst['class'].upper()} {worst['side']}"
        cv2.rectangle(frame, (0, 0), (frame.shape[1], 40), banner_color, -1)
        cv2.putText(frame, banner_text, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    return frame


def encode_jpeg(frame, quality=70):
    """Encodes a BGR frame to JPEG bytes for sending over the WebSocket."""
    ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    return buf.tobytes()
