"""Probe Anker PowerConf C200 USB webcam capabilities via OpenCV.

Walks a list of common resolutions through both DirectShow and Media
Foundation backends, measures achievable fps over a 30-frame window,
and reports the negotiated FourCC. The Anker C200 enumerates as
DirectShow index 0 on the Air Stacker PC; index 1 is the Spinnaker
DirectShow filter for the FLIR Flea3 and should not be opened from
here.

Read-only — does not change camera settings, write to disk, or touch
the FLIR camera.
"""
import cv2
import time


def probe(backend, backend_name, index_range=(0, 4)):
    print(f"\n=== backend = {backend_name} ===", flush=True)
    for idx in range(*index_range):
        cap = cv2.VideoCapture(idx, backend)
        if not cap.isOpened():
            cap.release()
            continue
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        fps = cap.get(cv2.CAP_PROP_FPS)
        fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
        fourcc = "".join([chr((fourcc_int >> (8*i)) & 0xff) for i in range(4)])
        t0 = time.perf_counter()
        ok, frame = cap.read()
        t1 = time.perf_counter()
        shape = frame.shape if ok else None
        print(f"  index={idx}: default={w:.0f}x{h:.0f} @ {fps:.1f}fps fourcc={fourcc!r} frame={shape} read_ms={(t1-t0)*1000:.1f}", flush=True)
        for tw, th in [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160), (640, 480), (320, 240)]:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, tw)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, th)
            aw = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            ah = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            afps = cap.get(cv2.CAP_PROP_FPS)
            # measure achievable fps by reading 30 frames
            n = 0
            t0 = time.perf_counter()
            while n < 30 and (time.perf_counter() - t0) < 3.0:
                ok, frame = cap.read()
                if ok:
                    n += 1
            elapsed = time.perf_counter() - t0
            achieved_fps = n / elapsed if elapsed > 0 else 0
            shape = frame.shape if ok else None
            print(f"    req {tw}x{th} -> actual {aw:.0f}x{ah:.0f} @ reported {afps:.1f}fps  achieved {achieved_fps:.1f}fps over {n} frames  shape={shape}", flush=True)
        cap.release()

probe(cv2.CAP_DSHOW, "CAP_DSHOW (DirectShow)")
probe(cv2.CAP_MSMF, "CAP_MSMF (Media Foundation)")
