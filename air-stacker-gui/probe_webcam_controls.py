"""Probe what controls the Anker PowerConf C200 webcam exposes.

Two sections:
  1. cv2.VideoCapture CAP_PROP_* values via both DirectShow and Media
     Foundation backends — anything that returns -1 isn't supported on
     this camera; anything else is readable (and usually writable).
  2. PySide6.QtMultimedia: import sanity check, video-device enumeration,
     and the supported-formats table per device.

Read-only — does not change camera settings or touch the FLIR camera.
"""
import cv2

print("=== cv2 build info ===")
print(f"cv2 version: {cv2.__version__}")

# All CAP_PROP_* constants in cv2
props = [
    ("BRIGHTNESS",            cv2.CAP_PROP_BRIGHTNESS),
    ("CONTRAST",              cv2.CAP_PROP_CONTRAST),
    ("SATURATION",            cv2.CAP_PROP_SATURATION),
    ("HUE",                   cv2.CAP_PROP_HUE),
    ("GAIN",                  cv2.CAP_PROP_GAIN),
    ("EXPOSURE",              cv2.CAP_PROP_EXPOSURE),
    ("AUTO_EXPOSURE",         cv2.CAP_PROP_AUTO_EXPOSURE),
    ("FOCUS",                 cv2.CAP_PROP_FOCUS),
    ("AUTOFOCUS",             cv2.CAP_PROP_AUTOFOCUS),
    ("AUTO_WB",               cv2.CAP_PROP_AUTO_WB),
    ("WB_TEMPERATURE",        cv2.CAP_PROP_WB_TEMPERATURE),
    ("TEMPERATURE",           cv2.CAP_PROP_TEMPERATURE),
    ("ZOOM",                  cv2.CAP_PROP_ZOOM),
    ("PAN",                   cv2.CAP_PROP_PAN),
    ("TILT",                  cv2.CAP_PROP_TILT),
    ("ROLL",                  cv2.CAP_PROP_ROLL),
    ("SHARPNESS",             cv2.CAP_PROP_SHARPNESS),
    ("GAMMA",                 cv2.CAP_PROP_GAMMA),
    ("BACKLIGHT",             cv2.CAP_PROP_BACKLIGHT),
    ("IRIS",                  cv2.CAP_PROP_IRIS),
    ("BUFFERSIZE",            cv2.CAP_PROP_BUFFERSIZE),
    ("FPS",                   cv2.CAP_PROP_FPS),
    ("FRAME_WIDTH",           cv2.CAP_PROP_FRAME_WIDTH),
    ("FRAME_HEIGHT",          cv2.CAP_PROP_FRAME_HEIGHT),
    ("FOURCC",                cv2.CAP_PROP_FOURCC),
    ("MODE",                  cv2.CAP_PROP_MODE),
    ("FORMAT",                cv2.CAP_PROP_FORMAT),
    ("CONVERT_RGB",           cv2.CAP_PROP_CONVERT_RGB),
    ("SETTINGS",              cv2.CAP_PROP_SETTINGS),
]

for backend_id, backend_name in [(cv2.CAP_DSHOW, "DSHOW"), (cv2.CAP_MSMF, "MSMF")]:
    print(f"\n=== backend = {backend_name} ===")
    cap = cv2.VideoCapture(0, backend_id)
    if not cap.isOpened():
        print("  cannot open")
        cap.release()
        continue
    print("  Readable / current values:")
    for name, prop_id in props:
        v = cap.get(prop_id)
        print(f"    {name:20s} = {v}")
    cap.release()

print("\n=== QtMultimedia availability ===")
# These imports look unused to a static linter — that's the point. Their
# importability IS what the probe is checking. Hence `noqa: F401`.
try:
    from PySide6 import QtMultimedia  # noqa: F401
    print("  PySide6.QtMultimedia importable: yes")
    from PySide6.QtMultimedia import (  # noqa: F401
        QCamera,
        QCameraDevice,
        QImageCapture,
        QMediaCaptureSession,
        QMediaDevices,
        QMediaRecorder,
        QVideoSink,
    )
    print(
        "  Core classes importable: QMediaDevices, QCamera, QCameraDevice, "
        "QMediaCaptureSession, QVideoSink, QImageCapture, QMediaRecorder"
    )
    # Need a QCoreApplication for QMediaDevices to enumerate
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    devices = QMediaDevices.videoInputs()
    print(f"  Video inputs ({len(devices)}):")
    for d in devices:
        print(f"    id={d.id()!r}  description={d.description()!r}  isDefault={d.isDefault()}")
        formats = d.videoFormats()
        # Group formats by (resolution, pixelFormat)
        seen = set()
        for f in formats:
            res = f.resolution()
            pf = str(f.pixelFormat()).split(".")[-1]
            fps_min = f.minFrameRate()
            fps_max = f.maxFrameRate()
            key = (res.width(), res.height(), pf, round(fps_max, 1))
            if key in seen:
                continue
            seen.add(key)
            print(f"      {res.width()}x{res.height():4d}  {pf:30s}  {fps_min:.1f}..{fps_max:.1f}fps")
except ImportError as e:
    print(f"  PySide6.QtMultimedia NOT importable: {e}")
try:
    from PySide6 import QtMultimediaWidgets  # noqa: F401
    print("  PySide6.QtMultimediaWidgets importable: yes (QVideoWidget available)")
except ImportError as e:
    print(f"  PySide6.QtMultimediaWidgets NOT importable: {e}")
