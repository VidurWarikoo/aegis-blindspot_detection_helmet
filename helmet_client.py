"""
Aegis helmet client, runs on the Raspberry Pi.

This replaces the old helmet_pi.py's local YOLO pipeline entirely. The Pi no longer
runs any detection or tracking itself, it only captures frames and streams them to
the cloud, which is where threat_engine.py and the actual YOLO model now live (see
app.py's /ws/helmet endpoint). This is a deliberate simplification: the Pi never
needed to be doing the heavy lifting, and dropping torch and ultralytics from this
side removes the install problems that came with getting them onto the Pi in the
first place.

The one thing that stays local is the immediate physical alert. The moment the cloud
decides a frame is a medium or high threat, it sends a short JSON message back down
this same connection, and this script fires the LEDs and motor on the correct side
right away. The rider should never be waiting on a phone or a dashboard to know
something is wrong, that channel is for review and history, not the first warning.

Install on the Pi (much lighter than before, no torch or ultralytics needed here):
  sudo pip install picamera2 opencv-python-headless websocket-client rpi_ws281x RPi.GPIO --break-system-packages

Run with:
  sudo python helmet_client.py
(sudo is still required for GPIO and the LED DMA channels.)
"""

import json
import threading
import time

import cv2
import websocket

try:
    import RPi.GPIO as GPIO
    from rpi_ws281x import PixelStrip, Color
    HARDWARE_AVAILABLE = True
except ImportError:
    HARDWARE_AVAILABLE = False

try:
    from picamera2 import Picamera2
    CAMERA_AVAILABLE = True
except ImportError:
    CAMERA_AVAILABLE = False

# Point this at your Aegis deployment. wss because Railway terminates TLS for you.
SERVER_WS_URL = "wss://web-production-9062c.up.railway.app/ws/helmet"

FRAME_WIDTH = 640
FRAME_HEIGHT = 480
TARGET_FPS = 8
JPEG_QUALITY = 60

# GPIO pin assignments (BCM numbering), same wiring as the original helmet_pi.py
LEFT_MOTOR = 17
RIGHT_MOTOR = 27
LEFT_STRIP_PIN = 12   # PWM0, channel 0
RIGHT_STRIP_PIN = 13  # PWM1, channel 1
LED_COUNT = 15

left_strip = None
right_strip = None
YELLOW = None
RED = None
OFF = None


def setup_hardware():
    global left_strip, right_strip, YELLOW, RED, OFF
    if not HARDWARE_AVAILABLE:
        print("[helmet] GPIO/LED libraries not available, running in capture only mode")
        return

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LEFT_MOTOR, GPIO.OUT)
    GPIO.setup(RIGHT_MOTOR, GPIO.OUT)
    GPIO.output(LEFT_MOTOR, GPIO.LOW)
    GPIO.output(RIGHT_MOTOR, GPIO.LOW)

    YELLOW = Color(255, 150, 0)
    RED = Color(255, 0, 0)
    OFF = Color(0, 0, 0)

    left_strip = PixelStrip(LED_COUNT, LEFT_STRIP_PIN, channel=0, dma=10)
    right_strip = PixelStrip(LED_COUNT, RIGHT_STRIP_PIN, channel=1, dma=5)
    left_strip.begin()
    right_strip.begin()
    print("[helmet] GPIO and LED strips ready")


def set_strip(strip, color):
    if strip is None:
        return
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, color)
    strip.show()


def clear_alert(strip, motor):
    set_strip(strip, OFF)
    if HARDWARE_AVAILABLE:
        GPIO.output(motor, GPIO.LOW)


def fire_alert(level, side):
    """Drives the local LEDs and motor the instant a cloud alert arrives, independent
    of whether anyone is watching the dashboard."""
    if not HARDWARE_AVAILABLE:
        print(f"[helmet] ALERT {level} {side} (no hardware attached to act on this)")
        return

    color = RED if level == "high" else YELLOW
    strip = left_strip if side == "LEFT" else right_strip
    motor = LEFT_MOTOR if side == "LEFT" else RIGHT_MOTOR

    set_strip(strip, color)
    GPIO.output(motor, GPIO.HIGH)
    duration = 0.6 if level == "high" else 0.3
    threading.Timer(duration, lambda: clear_alert(strip, motor)).start()


def on_message(ws, message):
    try:
        data = json.loads(message)
    except ValueError:
        return
    if data.get("type") == "alert":
        fire_alert(data.get("level"), data.get("side"))


def on_error(ws, error):
    print(f"[helmet] websocket error: {error}")


def on_close(ws, close_status_code, close_msg):
    print("[helmet] connection closed")


def on_open(ws):
    print("[helmet] connected to Aegis, starting capture")
    threading.Thread(target=capture_loop, args=(ws,), daemon=True).start()


def capture_loop(ws):
    if not CAMERA_AVAILABLE:
        print("[helmet] picamera2 not available, cannot capture")
        return

    picam2 = Picamera2()
    config = picam2.create_video_configuration(main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "RGB888"})
    picam2.configure(config)
    picam2.start()
    time.sleep(1)  # let auto exposure settle

    frame_interval = 1.0 / TARGET_FPS
    try:
        while True:
            start = time.time()
            frame = picam2.capture_array()
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                try:
                    ws.send(jpeg.tobytes(), opcode=websocket.ABNF.OPCODE_BINARY)
                except Exception as e:
                    print(f"[helmet] send failed: {e}")
                    break
            elapsed = time.time() - start
            time.sleep(max(0, frame_interval - elapsed))
    finally:
        picam2.stop()


def main():
    setup_hardware()
    while True:
        ws = websocket.WebSocketApp(
            SERVER_WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        ws.run_forever(ping_interval=20, ping_timeout=10)
        print("[helmet] disconnected, retrying in 3 seconds")
        time.sleep(3)


if __name__ == "__main__":
    main()
