# AEGIS

**AEGIS: AI Enhanced Guardian Intelligence System.** Our name stems from the Greek myth of Aegis, which was the impenetrable shield or breastplate used by Zeus and Athena. The name Aegis symbolizes invincible protection and divine authority, and is a perfect proxy for our project. In this instance, Aegis has been cleverly made into an acronym, which stands for the AI Enhanced Guardian Intelligence System. This is a hosted computer vision service that performs real time blind spot threat assessment for cyclists and motorcyclists.

AEGIS runs its detection, threat scoring, and physical alerting entirely on the helmet itself (a Raspberry Pi 4 with a rear-facing camera). The YOLOv8 detector and the full tracking and threat scoring pipeline described below execute locally on the Pi, and the LEDs and vibration motor fire the instant a frame is scored, medium or high, before any network request is ever made. This is a deliberate safety decision: a rider's warning must never depend on wifi coverage or a cloud service being reachable.

This hosted service is not in that safety-critical path at all. Its entire job is to be a searchable, reviewable archive of what happened on a ride. The Pi durably queues every medium/high alert (and, for high severity events, the near-miss video clip it already recorded and encoded on its own) and uploads them here in the background whenever a connection is available. A ride with no signal for its entire duration still gets full local warnings, the data just uploads later once the Pi is back near a network.

**Base URL:** `https://web-production-9062c.up.railway.app`

**Web dashboard:** the base URL now serves a public product page (what the helmet is, how the pipeline works, build photos) rather than redirecting straight into the app. `/history` is the alert log, charts, and near-miss clip viewer built from whatever the helmet has uploaded so far - there is no live feed to watch, the helmet already handled the warning locally by the time anything reaches this service. `/demo` is a separate, optional page that replays an uploaded clip through the identical scoring pipeline via `POST /simulate_stream`, useful for showing the algorithm working without needing the physical helmet in the room; it is not connected to a real rider. `/history` and `/demo` sit behind a lightweight sign-in; the home page does not, and none of the API or WebSocket endpoints below are gated by it.

## The problem this addresses

Bicyclist fatalities in the United States increased 13 percent in a single year, from 976 to 1105, according to NHTSA data in 2022. The distribution of this risk is not uniform across vehicle types: buses strike cyclists from the right side in 40 percent of fatal collisions, compared to a 6 percent baseline across all vehicle classes. In India, two wheelers accounted for nearly half of all road deaths in 2024, roughly 177,000 people. The common failure in every one of these cases is perceptual: a rider has no reliable way to observe a vehicle approaching from behind or beside them, since mirrors have limited coverage and turning to check imposes a real cost in balance and reaction time. AEGIS is designed as that missing perception layer. It was originally developed and field tested as the software core of a physical smart helmet prototype with Raspberry Pi 4 technology and rear-facing cameras. This service exposes that same validated detection and scoring pipeline over HTTP so any agent, controller, or dashboard can query it directly.

## Endpoints

### `GET /health`

Liveness check confirming the service process is running and the detection model has been loaded into memory.

```bash
curl https://web-production-9062c.up.railway.app/health
```

Response:

```json
{ "status": "ok", "service": "aegis", "model_loaded": true }
```

### `GET /api/info`

Machine readable index of every endpoint this service exposes. A good first call for an agent orienting itself.

```bash
curl https://web-production-9062c.up.railway.app/api/info
```

Response:

```json
{
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
}
```

### `POST /analyze`

Accepts a single image and returns a threat assessment for every detected object in that frame. Since a single image carries no temporal information, the kinematic term of the scoring function described below is identically zero here, so only the static proximity term contributes to the final score. This endpoint exists as a fast sanity check of the underlying detector, and for the complete methodology you should use `POST /analyze_video` instead.

**Example Request:**
```bash
curl -X POST https://web-production-9062c.up.railway.app/analyze \
  -F "file=@road_photo.jpg"
```

**Example Response:**

```json
{
  "threat_level": "medium",
  "worst": {
    "track_id": 0, "cls_id": 2, "class": "car", "score": 0.181, "side": "LEFT",
    "x1": 140, "y1": 210, "x2": 420, "y2": 480
  },
  "detections": [
    { "track_id": 0, "cls_id": 2, "class": "car", "score": 0.181, "side": "LEFT", "x1": 140, "y1": 210, "x2": 420, "y2": 480 },
    { "track_id": 1, "cls_id": 2, "class": "car", "score": 0.006, "side": "RIGHT", "x1": 520, "y1": 240, "x2": 560, "y2": 270 }
  ]
}
```

`detections` always carries every field shown above for every object in frame, `worst` is just whichever one of those entries scored highest (or `null` if nothing was detected).

### `POST /analyze_video`

Accepts a video clip and runs the complete temporal pipeline described in the methodology section below: persistent multi object tracking, trend window smoothing, convergence classification, and hysteresis based threat selection. Returns every medium or high severity alert event that fired during the clip, along with the single peak threat.

**Example Request:**
```bash
curl -X POST https://web-production-9062c.up.railway.app/analyze_video \
  -F "file=@dashcam_clip.mp4"
```

**Example Response:**

```json
{
  "frames_analyzed": 210,
  "truncated": false,
  "alert_count": 3,
  "peak_threat": { "frame": 88, "class": "truck", "side": "LEFT", "score": 0.412, "level": "high" },
  "events": [
    { "frame": 61, "class": "car", "side": "LEFT", "score": 0.145, "level": "medium" },
    { "frame": 88, "class": "truck", "side": "LEFT", "score": 0.412, "level": "high" },
    { "frame": 142, "class": "bus", "side": "RIGHT", "score": 0.19, "level": "medium" }
  ]
}
```

### `POST /api/ingest` — how a real helmet actually gets data here now

The primary way real data reaches this service. The helmet (see `helmet_local.py`) already ran detection, scored the threat, and fired its own LEDs/motor entirely offline before this request is ever sent, this call exists purely to make that event show up on `/history` later. Uploads are asynchronous and best-effort: the helmet durably queues every medium/high alert locally the moment it fires and retries this call in the background whenever a connection exists, so it can arrive seconds or hours after the real event.

**Example Request:**
```bash
curl -X POST https://web-production-9062c.up.railway.app/api/ingest \
  -F "timestamp=2026-07-12T14:03:11.204000Z" -F "class=truck" -F "side=LEFT" \
  -F "score=0.412" -F "level=high" -F "source=helmet" \
  -F "clip=@nearmiss_2026-07-12T14-03-11.204000Z_truck_LEFT.mp4"
```

**Example Reason:**

```json
{ "status": "ok", "logged": true, "clip_saved": true }
```

`timestamp`, `class`, `side`, `score`, and `level` are required on every call. `source` defaults to `"helmet"` if omitted. The `clip` file field is optional and should only be attached for `level: "high"` events where the helmet already recorded and encoded a near-miss clip locally, medium alerts are metadata-only.

### `WS /ws/helmet`, `WS /ws/dashboard`, `POST /simulate_stream` — legacy live path, demo use only

These three still exist and still work exactly as before, but they are no longer how a real helmet reports anything, the helmet does its own detection locally now and never opens a live connection to this service at all. They're kept around purely to power the optional `/demo` page: `POST /simulate_stream` replays an uploaded clip through the same scoring pipeline at the clip's frame rate and broadcasts annotated frames over `WS /ws/dashboard`, so the detection algorithm can be demoed to someone without the physical helmet in the room. `WS /ws/helmet` (a live binary JPEG stream, alerts sent back as `{ "type": "alert", "level": "high", "side": "LEFT" }`) still works if you want to point a second camera at it for a demo, but no production helmet uses it. Treat these as a demo utility, not part of the real data path, use `POST /api/ingest` and `GET /api/alerts` for anything about actual rides.

### `GET /api/alerts`

Returns the alert history as JSON, used by `/history` to build its charts and table. This now reflects real rides uploaded via `POST /api/ingest`, alongside anything generated through the `/demo` page. Accepts an optional `days` query parameter, defaulting to 30, and an optional `limit`, defaulting to 500.

**Example Request:**
```bash
curl "https://web-production-9062c.up.railway.app/api/alerts?days=7&limit=3"
```

**Example Response:**

```json
[
  { "id": 142, "timestamp": "2026-07-10T22:14:03.512000Z", "class": "truck", "side": "LEFT", "score": 0.412, "level": "high", "source": "helmet" },
  { "id": 141, "timestamp": "2026-07-10T21:58:47.209000Z", "class": "car", "side": "RIGHT", "score": 0.145, "level": "medium", "source": "simulated" }
]
```

Newest first. `source` is `"helmet"` (a real ride, uploaded via `/api/ingest`), `"simulated"` (from the `/demo` page), or `"demo"` (seeded placeholder data).

### `GET /api/clips` and `GET /api/clips/<filename>`

`GET /api/clips` returns up to the 3 most recent near-miss clip filenames (newest first), whether they were recorded locally on the helmet and uploaded via `/api/ingest`, or captured during a `/demo` session. `GET /api/clips/<filename>` serves the actual mp4 file. Both power the "Recent near misses" panel on `/history`.

```bash
curl https://web-production-9062c.up.railway.app/api/clips
```

```json
["nearmiss_2026-07-12T14-03-11.204000Z_truck_LEFT.mp4"]
```

### Demo and maintenance endpoints

Two additional endpoints exist purely to make the dashboard demo-able, and are not part of the core detection capability. `POST /api/seed_demo_data` inserts a batch of realistic looking alert rows tagged `source: "demo"`, spanning the past week, so the `/history` charts have something to show before real usage accumulates. It never touches real alerts, and is safe to call more than once.

**Example Request:**
```bash
curl -X POST https://web-production-9062c.up.railway.app/api/seed_demo_data
```
**Example Response:**
```json
{ "status": "ok", "inserted": 27 }
```

`POST /api/clear_demo_data` removes every row tagged `source: "demo"`, leaving real helmet and simulated alerts untouched, intended to be run once before final judging or before recording with real footage.

**Example Request:**
```bash
curl -X POST https://web-production-9062c.up.railway.app/api/clear_demo_data
```

**Example Response:**
```json
{ "status": "ok", "removed": 27 }
```

## How the agent should use this

1. Call `GET /health` first to confirm the service is live and the model is loaded. If `model_loaded` is not `true`, stop, the service isn't ready yet.
2. If you want a machine readable map of the whole API before doing anything else, call `GET /api/info` and read `endpoints`.
3. To review what a real helmet has actually detected on rides, call `GET /api/alerts`, optionally with `days` and `limit`, and read the returned array, every entry has `timestamp`, `class`, `side`, `score`, `level`, and `source`. This is the primary way to answer questions about real activity, the helmet already made every decision locally, this endpoint is just the log of what happened.
4. To see or attach a near-miss clip for a specific high-severity event, call `GET /api/clips` for the up-to-3 most recent filenames, then `GET /api/clips/<filename>` for the actual mp4.
5. If you are the helmet reporting a real detection (or building something that stands in for one), call `POST /api/ingest` with the alert's `timestamp`, `class`, `side`, `score`, `level`, and for high severity events, the recorded clip file. Nothing about the alerting itself depends on this call succeeding, it exists purely to make the event reviewable later.
6. To assess a single frame handed to you directly (not from the helmet), call `POST /analyze` and read `threat_level` off the response. If it's `medium` or `high`, `worst` tells you exactly which object and which `side`.
7. To assess a pre-recorded clip end to end, call `POST /analyze_video` and read `events` for every medium or high alert that fired during the clip, or just `peak_threat` if you only care about the single worst moment.
8. To demo the live pipeline visually without a real helmet present, call `POST /simulate_stream` and watch `/demo` in a browser, or consume `WS /ws/dashboard` directly. This is a demo utility only, real rides never go through it.
9. Across every endpoint, `threat_level` / `level` takes the values `none`, `low`, `medium`, or `high`. Treat `medium` and `high` as actionable, `low` and `none` as informational only.
10. Across every endpoint, `side` is `LEFT` or `RIGHT`, indicating which side of the frame (and the rider) the threat occupies.

## Threat scoring methodology

Each frame, every tracked object is assigned a proximity score and a kinematic approach rate, which are combined into a single class weighted danger score. The formulation below is the exact logic implemented in `threat_engine.py`.

**Proximity score.** For a bounding box with area A occupying a frame of area F, proximity is defined as:

```
proximity = A / F
```

This is a direct, dimensionless measure of how much of the visual field an object currently occupies, used as a proxy for physical closeness under the assumption of a roughly fixed camera field of view.

**Approach rate with trend window smoothing.** A naive closing speed estimate would compare the bounding box area between two consecutive frames. In practice this is unusable: single frame area deltas are dominated by detector jitter, small fluctuations in the bounding box regression that have nothing to do with actual object motion. AEGIS instead maintains a four frame rolling history of area per tracked object and computes approach rate against the oldest sample in that window rather than the immediately preceding frame:

```
approach_rate = max(0, (A_current − A_reference) / A_reference)
```

where `A_reference` is the box area recorded four frames prior. The result is clamped at zero, since a shrinking bounding box indicates recession, which contributes no danger regardless of magnitude. Widening the comparison baseline to four frames reduces the variance of this estimate substantially without introducing meaningful latency at typical video frame rates.

**Convergence classification.** An object can legitimately grow larger in frame while posing no merge risk: a vehicle traveling toward the camera in its own lane on a two way road will exhibit positive approach rate purely from perspective, without ever converging into the observer's actual path. To separate these two cases, AEGIS tracks the horizontal offset of each object's bounding box center from the frame's central vertical axis over the same four frame window, and classifies an object as converging only if that offset has measurably decreased:

```
offset(t) = |center_x(t) − frame_center_x|
converging = offset(current) ≤ offset(reference) − 4 pixels
```

The four pixel margin was chosen empirically to exceed the typical noise floor of the bounding box regression, so ordinary detector jitter is not mistaken for genuine lateral convergence. If an object is not converging, its approach rate is credited at only 20 percent of its computed value when forming the final score. Critically, this discount is applied only to the kinematic term; the proximity term is left untouched, since an object's present closeness is a fact independent of its trajectory.

**Class weighted composite score.** The final raw score for a tracked object is:

```
score = class_weight × (0.5 × proximity + 0.5 × credited_approach_rate)
```

Class weights are not arbitrary; they are derived directly from the crash severity statistics cited above. Buses and trucks receive a weight of 1.5, cars 1.2, motorcycles 1.1, and pedestrians and bicycles a baseline of 1.0, reflecting the empirically observed disproportionate lethality of large vehicles in real world cyclist collisions.

**Lateral pass by suppression.** A separate filter targets vehicles crossing the frame laterally at approximately constant range, such as opposite lane traffic passing by without ever closing distance. If an object's average lateral velocity over the trend window exceeds 0.015 frame widths per frame, and its raw approach rate is exactly zero (not merely discounted), its score is multiplied by 0.05. This isolates cross traffic motion specifically, and does not suppress a genuinely close pass: a large vehicle that remains close in frame retains a high proximity term regardless of this filter, which is a deliberate and validated distinction (see the fourth item under validation, below).

**Minimum proximity floor and stationary proximity cap.** Two additional guards constrain the classification. First, any object with proximity below 0.004 (occupying less than four tenths of one percent of the frame) is scored zero unconditionally, removing distant background clutter from consideration regardless of class weight. Second, an object with zero credited approach rate, meaning it is not genuinely closing distance, is capped just below the medium threshold unless its proximity exceeds 0.35. The reasoning is that static or non closing proximity alone should not constitute an actionable alert unless the encounter is already at point blank range, comparable to a vehicle already directly alongside the rider.

**Threat level quantization.** The smoothed score (an eight sample moving average per track, described below) is mapped to a discrete level:

```
score < 0.12          -> low / none
0.12 <= score < 0.28  -> medium
score >= 0.28         -> high
```

**Hysteresis based selection.** At every frame the object with the highest instantaneous score is the global maximum candidate. This candidate is not reported directly, because the underlying multi-object tracker is subject to identity discontinuities under occlusion: a tracked object can vanish from the detection set for one or more frames and later reappear under a new identity, or be temporarily lost entirely. Naive per frame selection under these conditions produces rapid, visually unstable switching between the reported worst object, a failure mode observed directly during empirical validation. AEGIS instead applies two stabilizing constraints. A hysteresis margin requires a new candidate to exceed the currently reported object's score by at least 25 percent, or a customized set threshold, before it is permitted to take over. A grace period of six frames allows the currently reported object to remain the reported threat even if it is briefly absent from the current frame's detections, on the assumption that this is a tracking dropout rather than a genuine disappearance, before the system concedes the identity as lost.

## Engineering for real world feasibility

The aforementioned scoring model was the product of iterative validation against real dashcam footage, in which several concrete failure modes were identified and corrected in turn. They are the following:

Incoming traffic in the opposite lane was repeatedly misclassified as a closing threat under a naive single frame area comparison, because instantaneous area deltas are dominated by detector jitter rather than genuine motion. This was corrected by moving to the four frame trend window baseline described above.

The reported worst object flickered rapidly between multiple simultaneously visible vehicles, caused by per frame reselection with no persistence across the identity discontinuities inherent to the underlying tracker. This was corrected by the hysteresis margin and grace period.

Vehicles traveling toward the camera within their own lane exhibited legitimate positive approach rate from perspective growth alone, despite posing no actual merge risk, since their lateral trajectory never converged toward the observer's path. This was corrected by the convergence classification, which withholds full kinematic credit from any object that is not measurably angling toward the frame center.

Stationary or parked vehicles at close range triggered alerts from static proximity alone, despite zero closing velocity. This was corrected by the stationary proximity cap. Notably, this correction was validated to preserve the opposite case: a large vehicle passing genuinely close, such as a truck occupying roughly half the frame during a narrow road overtake, is correctly retained as a high severity alert, because its proximity term alone exceeds the point blank threshold regardless of lane or convergence status. This distinction, a close pass by a large vehicle is dangerous and must be flagged even though it originates from the opposite lane, while a parked car of similar size is not dangerous absent any closing motion, was confirmed against footage containing both scenarios before the corrected logic was accepted.

After the corrections above eliminated the noise driven score spikes that the original detection thresholds had been implicitly calibrated against, genuine threats stopped reaching the medium and high classification boundaries entirely. The thresholds were recalibrated downward and reverified against controlled test cases with known object proximity before being finalized at the values shown above.

## System architecture & operational hardening

**The safety-critical path never touches this service.** Detection, tracking, threat scoring, and physical alerting (LEDs + vibration motor) all run on the Raspberry Pi itself, in `helmet_local.py`, using the identical `threat_engine.py` scoring module documented above (shared between the Pi and this repo, not a separate copy that could drift). A rider gets their warning even with zero network connectivity for the entire ride. This service's only job is archival: it receives whatever the Pi durably queued and eventually uploaded via `POST /api/ingest`, and makes it browsable on `/history`. If this entire Railway deployment were down, the helmet would keep working exactly the same, it just wouldn't have anywhere to upload to until the deployment came back.

On the Pi side, every medium/high alert is written to a local SQLite outbox (`outbox.py`) the instant it fires, before any upload is even attempted, so a crash or a dead wifi link can never silently lose an event, it just waits for the next retry. A background thread flushes that outbox to `POST /api/ingest` on a fixed interval whenever a connection exists; rows are only deleted once the server confirms a 2xx response. For high severity events, `near_miss.py` (also shared, unmodified between the local and cloud contexts) buffers a rolling window of recent frames and, on trigger, records roughly two seconds before and two seconds after the event into an mp4, capped locally at the 3 most recent clips so the Pi's SD card never fills up regardless of how long uploads are delayed.

This service itself is still intended to be called autonomously, so it remains built to fail safely and predictably. The detection model (YOLOv8 nano) is bundled directly in the deployment rather than fetched at first request. Every request-scoped or streaming session (used only by `POST /analyze`, `POST /analyze_video`, and the `/demo` page's legacy live path) gets its own model instance and its own `ThreatTracker`, so concurrent sessions never share tracking state. File uploads are validated against an explicit extension allowlist and capped at 30 megabytes. Video analysis is bounded at 900 processed frames; if that limit is hit, the response says so explicitly via `truncated` rather than silently returning partial results. Every error path returns a structured JSON error object with an appropriate HTTP status code.

Alert history is persisted to a SQLite database on a mounted Railway Volume, so it survives redeploys rather than living on the container's own ephemeral disk, and is served through `GET /api/alerts`. Alerts are deduplicated per tracked object with a three second cooldown before a new history row is written (this dedup runs identically on the Pi now, before an event is even queued for upload), so a single sustained threat does not flood the archive with near identical entries.

## Limitations

Current limitations of the API and of the product include:

- `POST /analyze` operates on a single frame and therefore has no access to the temporal kinematic term; only the proximity component of the score is meaningful there.
- The scoring model was tuned and validated against forward facing dashcam footage. It has not yet been separately validated against a true rear facing helmet camera angle, which is the deployment configuration on the physical prototype.
- Uploads via `POST /api/ingest` are best-effort and asynchronous, there is no guaranteed upper bound on how long a real alert takes to become visible on `/history` if the helmet has no connectivity for an extended period; it will eventually appear once the Pi reconnects, but nothing currently surfaces "N events still pending upload" anywhere.
- There is no live view of an in-progress ride. `/demo` shows the pipeline running on an uploaded clip, not a real-time feed from the helmet, by design, since the helmet no longer maintains a live connection to this service at all.
- Running YOLOv8 nano plus tracking locally on a Raspberry Pi 4's CPU is meaningfully slower than the same workload on a cloud instance; achievable on-device frame rate has not yet been benchmarked against the frame rate the scoring model was validated at.

## Future extension

Every alert AEGIS produces is structured, timestamped, and readable by machines, whether it originated from a real helmet's local detection or the `/demo` page's live pipeline. The natural extension is a coordinator agent that polls `GET /api/alerts` across multiple riders' independent helmets and aggregates blind spot risk data across a street, a delivery fleet, or an entire city, which is exactly the kind of narrow, independently verifiable specialist agent that NANDA's Internet of Agents is designed to coordinate between. Because each helmet is already fully autonomous and offline-capable, this kind of fleet-level aggregation could layer on top of the existing archive without ever touching the safety-critical path on any individual rider's device.
