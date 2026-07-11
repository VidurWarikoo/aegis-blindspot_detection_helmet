# AEGIS

**AEGIS: AI Enhanced Guardian Intelligence System.** Our name stems from the Greek myth of Aegis, which was the impenetrable shield or breastplate used by Zeus and Athena. The name Aegis symbolizes invincible protection and divine authority, and is a perfect proxy for our project. In this instance, Aegis has been cleverly made into an acronym, which stands for the AI Enhanced Guardian Intelligence System. This is a hosted computer vision service that performs real time blind spot threat assessment for cyclists and motorcyclists.

AEGIS is built as a distributed system, not a single embedded gadget. The physical helmet is a thin edge sensor that only captures and streams frames, it runs no detection of its own. Every frame is sent to this hosted service, which is where the actual intelligence lives: the YOLOv8 detector, the full tracking and threat scoring pipeline described below, and the decision of whether something is genuinely dangerous. When the answer is yes, two things happen at once. A message is sent back to the physical helmet so it can fire its own LEDs and vibration motor immediately, and the same alert is pushed live to a connected web dashboard, hosted at this same base URL, which shows the annotated video feed in real time and keeps a running history of every unsafe moment for later review and comparison.

**Base URL:** `https://web-production-9062c.up.railway.app`

**Live dashboard:** open the base URL directly in a browser to see the operator view: a live annotated feed, an alert banner with sound for medium and high threats, a button to replay a pre recorded clip through the full pipeline without needing the physical helmet connected, and charts built from the alert history.

## The problem this addresses

Bicyclist fatalities in the United States increased 13 percent in a single year, from 976 to 1105,, according to NHTSA data in 2022. The distribution of this risk is not uniform across vehicle types: buses strike cyclists from the right side in 40 percent of fatal collisions, compared to a 6 percent baseline across all vehicle classes. In India, two wheelers accounted for nearly half of all road deaths in 2024, roughly 177,000 people. The common failure in every one of these cases is perceptual: a rider has no reliable way to observe a vehicle approaching from behind or beside them, since mirrors have limited coverage and turning to check imposes a real cost in balance and reaction time. AEGIS is designed as that missing perception layer. It was originally developed and field tested as the software core of a physical smart helmet prototype with Raspberry Pi 4 technology and rear-facing cameras. This service exposes that same validated detection and scoring pipeline over HTTP so any agent, controller, or dashboard can query it directly.

## Endpoints

### `GET /health`

This get request returns the liveness and readiness of the service and confirms that the service processes are running and that the detection model has been successfully loaded into memory.


```bash
curl https://web-production-9062c.up.railway.app/health
```

Response:

```json
{ "status": "ok", "service": "aegis", "model_loaded": true }
```

### `POST /analyze`

This POST request accepts a single image and returns a threat assessment for every detected object in that frame. Since a single image carries no information about the time, the kinematic term of the scoring function described below is identically zero here with only the static proximity term contributing to it. This endpoint exists as a fast sanity check of the underlying detector, whereas, for the complete methodology, use thePOST request `/analyze_video`.

**Example Request:**
```bash
curl -X POST https://web-production-9062c.up.railway.app/analyze \
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
    { "track_id": 0, "class": "car", "score": 0.181, "side": "LEFT" },
    { "track_id": 1, "class": "car", "score": 0.006, "side": "RIGHT" }
  ]
}
```

### `POST /analyze_video`

Accepts a video clip and runs the complete temporal pipeline described in the methodology section below: persistent multi object tracking, trend window smoothing, convergence classification, and hysteresis based threat selection. Returns every medium or high severity alert event that fired during the clip, along with the single peak threat.

```bash
curl -X POST https://web-production-9062c.up.railway.app/analyze_video \
  -F "file=@dashcam_clip.mp4"
```

Response:

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

### `WS /ws/helmet`

The physical (or simulated) helmet connects here and sends a continuous sequence of binary WebSocket messages, each one a JPEG encoded frame. There is no request or response in the usual sense, this is a persistent streaming connection. Every frame is run through detection and scoring, using a tracker and model instance dedicated to this connection so concurrent sessions never share tracking state. Whenever a frame produces a medium or high alert, a short JSON message is sent back down this same connection so the helmet can act immediately:

```json
{ "type": "alert", "level": "high", "side": "LEFT" }
```

### `WS /ws/dashboard`

A browser connects here to receive a live push of every processed frame and every alert, from whichever helmet or simulated session is currently active. This is what powers the live feed and alert banner on the dashboard page served at `/`. Two message types arrive on this channel:

```json
{ "type": "frame", "source": "helmet", "session_id": "a1b2c3d4", "image": "<base64 jpeg>", "level": "medium", "worst": { "class": "car", "side": "LEFT", "score": 0.18 } }
{ "type": "alert", "timestamp": "2026-07-10T22:14:03Z", "class": "truck", "side": "LEFT", "score": 0.41, "level": "high", "source": "helmet" }
```

### `POST /simulate_stream`

Accepts a pre recorded video clip and replays it through the exact same live pipeline a real helmet would produce, at the clip's own frame rate, broadcasting each annotated frame to `/ws/dashboard` as it goes. This exists so the full system, live feed, alerting, and history logging, can be demonstrated convincingly without needing the physical hardware connected. The request returns immediately, the streaming happens in the background.

```bash
curl -X POST https://web-production-9062c.up.railway.app/simulate_stream \
  -F "file=@dashcam_clip.mp4"
```

### `GET /api/alerts`

Returns the alert history as JSON, used by the dashboard to build its charts and history table. Accepts an optional `days` query parameter, defaulting to 30.

```bash
curl "https://web-production-9062c.up.railway.app/api/alerts?days=7"
```

## How the AI uses this

1. Call `GET /health` first to confirm the service is live and the model is loaded.
2. For a single frame handed to an agent, call `POST /analyze` and read `threat_level` and `worst` from the response.
3. For a clip, call `POST /analyze_video` and read `events` for every medium or high alert, or `peak_threat` for the single worst moment in the clip.
4. For a live or continuous source, stream frames to `WS /ws/helmet` instead, and read alerts back off the same connection or off `WS /ws/dashboard`.
5. `threat_level` and `level` take the values `none`, `low`, `medium`, or `high`. Treat `medium` and `high` as actionable. Treat `low` and `none` as informational only.
6. `side` is `LEFT` or `RIGHT`, indicating which side of the frame (and therefore the rider) the threat occupies.

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

This service is intended to be called autonomously by an agent, or by a piece of hardware, with no human supervising the interaction, so it is built to fail safely and predictably rather than to crash.

The detection model (YOLOv8 nano) is bundled directly in the deployment rather than fetched at first request, eliminating any runtime dependency on an external download completing successfully during a cold start. Every streaming or request scoped session gets its own model instance and its own `ThreatTracker`, so concurrent helmet connections, simulated replays, and one shot requests never share tracking state or produce cross contaminated track IDs. File uploads are validated against an explicit extension allowlist before being written to disk or passed to the model, and are capped at 30 megabytes. Video analysis is bounded at 900 processed frames (approximately 30 seconds at 30 frames per second) so a very long or malformed input cannot stall a request indefinitely; if this limit is reached, the response indicates so explicitly via a `truncated` field rather than silently returning partial results. Every error path, including corrupt uploads, unreadable video codecs, and unexpected internal exceptions, returns a structured JSON error object with an appropriate HTTP status code, so the calling agent's response parser is never handed an unparseable HTML error page.

Alert history is persisted to a local SQLite database and served through `GET /api/alerts`, which is what the dashboard's charts and history table read from. The dashboard itself is one WebSocket broadcast channel, `/ws/dashboard`, fed by whichever helmet or simulated session is currently processing frames, so every connected browser sees the identical live view. The physical helmet's own alert response does not depend on the dashboard at all, the cloud sends the alert straight back down the helmet's own connection the instant it fires, so the rider's LEDs and motor react immediately regardless of whether a browser is even open.

## Limitations
Current limitations of the API and of the product include:

- `POST /analyze` operates on a single frame and therefore has no access to the temporal kinematic term; only the proximity component of the score is meaningful there.
- The scoring model was tuned and validated against forward facing dashcam footage. It has not yet been separately validated against a true rear facing helmet camera angle, which is the deployment configuration on the physical prototype.
- Alert history is stored in SQLite on the same filesystem as the running service, so it is not preserved across a redeploy. A managed database is the natural next step once the history needs to survive indefinitely.
- The dashboard shows whichever session is currently streaming; it does not yet support viewing multiple riders' feeds side by side.

## Future extension

Every alert AEGIS produces is structured, timestamped, and readable by machines, and the dashboard already proves that a browser client can consume that stream live. The natural extension is a coordinator agent that subscribes to multiple riders' `/ws/dashboard` style feeds simultaneously and aggregates blind spot risk data across a street, a delivery fleet, or an entire city in real time, which is exactly the kind of narrow, independently verifiable specialist agent that NANDA's Internet of Agents is designed to coordinate between.


