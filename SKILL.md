# Blindspot Guardian

A hosted vehicle blind-spot threat detection service — send it a photo or video and it returns a structured threat assessment (what's approaching, from which side, how dangerous).

**Base URL:** `https://REPLACE-WITH-YOUR-RAILWAY-URL.up.railway.app`

## Endpoints

### `GET /health`

Liveness check.

```bash
curl https://REPLACE-WITH-YOUR-RAILWAY-URL.up.railway.app/health
```

Response:

```json
{ "status": "ok", "service": "blindspot-guardian" }
```

### `POST /analyze`

Send a single image. Returns every detected vehicle/pedestrian and the single highest-priority threat in that frame. No motion history on a lone image, so this scores proximity only (no "is it approaching" credit) — good for a fast sanity check of the detector.

```bash
curl -X POST https://REPLACE-WITH-YOUR-RAILWAY-URL.up.railway.app/analyze \
  -F "file=@road_photo.jpg"
```

Response:

```json
{
  "threat_level": "medium",
  "worst": {
    "track_id": 0,
    "cls_id": 2,
    "class": "car",
    "score": 0.181,
    "side": "LEFT",
    "x1": 140, "y1": 210, "x2": 420, "y2": 480
  },
  "detections": [
    { "track_id": 0, "class": "car", "score": 0.181, "side": "LEFT", "...": "..." },
    { "track_id": 1, "class": "car", "score": 0.006, "side": "RIGHT", "...": "..." }
  ]
}
```

### `POST /analyze_video`

Send a short video clip. Runs the full pipeline — persistent object tracking, multi-frame smoothing, convergence checking (tells real closing threats apart from traffic just passing in the opposite lane), and hysteresis (stops the reported threat from flickering between objects). Returns every medium/high alert fired during the clip.

```bash
curl -X POST https://REPLACE-WITH-YOUR-RAILWAY-URL.up.railway.app/analyze_video \
  -F "file=@dashcam_clip.mp4"
```

Response:

```json
{
  "frames_analyzed": 210,
  "alert_count": 3,
  "peak_threat": { "frame": 88, "class": "truck", "side": "LEFT", "score": 0.412, "level": "high" },
  "events": [
    { "frame": 61, "class": "car", "side": "LEFT", "score": 0.145, "level": "medium" },
    { "frame": 88, "class": "truck", "side": "LEFT", "score": 0.412, "level": "high" },
    { "frame": 142, "class": "bus", "side": "RIGHT", "score": 0.19, "level": "medium" }
  ]
}
```

## How an agent should use this

1. Call `GET /health` first to confirm the service is live.
2. For a single frame (e.g., a photo an agent has been handed): `POST /analyze` with the image and read `threat_level` and `worst` from the response.
3. For a clip (e.g., a short dashcam or helmet camera recording): `POST /analyze_video` and read `events` for every medium/high alert that fired, or just `peak_threat` for the single worst moment.
4. `threat_level` / `level` values are `"none"`, `"low"`, `"medium"`, or `"high"`. Treat `"medium"` and `"high"` as actionable alerts; `"low"`/`"none"` as no action needed.
5. `side` is `"LEFT"` or `"RIGHT"` — which side of the frame (and therefore the rider/vehicle) the threat is on.

## The problem

Bicyclist deaths in the U.S. rose 13% in a single year (976 → 1,105, NHTSA 2022 data), and blind-spot collisions from large vehicles are disproportionately deadly — buses hit cyclists from the right side 40% of the time, versus just 6% for vehicles overall. In India, two-wheelers make up nearly half of all road deaths — 177,000 people in 2024. Riders have no way to perceive what's directly behind or beside them. This service is that missing perception layer, callable by anything — a helmet's onboard controller, another agent, a dashboard — that needs to know whether a blind-spot threat is present right now.

## Under the hood

YOLOv8-nano with persistent object tracking, a class-weighted threat score combining proximity and closing speed, temporal smoothing to kill single-frame jitter, a convergence check that distinguishes real closing threats from vehicles just passing in the opposite lane, and hysteresis so the reported threat doesn't flicker between objects. Originally built and validated as the perception layer of a physical smart-helmet prototype (Raspberry Pi 4, rear camera, LED + haptic alerts); this service exposes that same val