#!/usr/bin/env python3
import cv2, time, threading, queue, subprocess, os
import numpy as np
from datetime import datetime
import pytz

# ====== CONFIG ======
URL = "http://192.168.1.116:81/stream"   # ESP MJPEG (single source)
IST = pytz.timezone("Asia/Kolkata")

# Output
BASE_DIR = "/srv/cctv/esp_cam1"
SEGMENT_SECONDS = 600                    # 10 min chunks
BITRATE = "4M"                         # predictable storage
RTSP_OUT = "rtsp://127.0.0.1:8554/esp_cam1_overlay"  # needs a local RTSP server (e.g., MediaMTX)

# Motion
MIN_AREA = 800
ROI_PTS = np.array([
    [147,400],[151,427],[146,487],[148,524],[143,557],[191,551],[222,560],[269,561],[302,553],[345,555],
    [376,556],[434,546],[468,550],[504,545],[564,541],[609,543],[651,542],[701,544],[737,538],[779,536],
    [811,535],[832,506],[836,475],[843,471],[858,457],[858,440],[855,413],[846,391],[841,352],[836,329],
    [819,319],[799,271],[808,238],[809,201],[799,192],[786,191],[759,194],[738,194],[692,196],[659,200],
    [612,201],[572,197],[517,194],[463,197],[408,208],[393,236],[363,236],[329,233],[273,230],[264,232],
    [249,259],[230,273],[196,289],[179,292],[150,291],[128,315],[142,339],[146,363],[146,381],
], dtype=np.int32)

SHOW_PREVIEW = True    # press q to quit
TARGET_FPS = 10        # encode at ~10 fps
FRAME_QUEUE_MAX = 2    # keep latency low
# ====================

os.makedirs(BASE_DIR, exist_ok=True)

# ---- Motion/overlay helpers ----
mog2 = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=True)

def draw_overlay(frame, motion_boxes, motion_flag):
    # timestamp
    ts = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    txt = ts + ("  Motion" if motion_flag else "")
    cv2.putText(frame, txt, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2, cv2.LINE_AA)
    # ROI poly
    cv2.polylines(frame, [ROI_PTS], isClosed=True, color=(0,255,255), thickness=1, lineType=cv2.LINE_AA)
    # motion boxes
    for (x,y,w,h,area) in motion_boxes:
        cv2.rectangle(frame, (x,y), (x+w,y+h), (0,255,255), 2)
        cv2.putText(frame, f"{int(area)}", (x, max(0,y-6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 2)

def detect_motion(frame):
    roi_mask = np.zeros(frame.shape[:2], dtype=np.uint8)
    cv2.fillPoly(roi_mask, [ROI_PTS], 255)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    fg = mog2.apply(gray)
    _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, None)   # type: ignore
    fg = cv2.dilate(fg, None, iterations=2)            # type: ignore
    fg = cv2.bitwise_and(fg, fg, mask=roi_mask)
    cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    motion = False
    for c in cnts:
        area = cv2.contourArea(c)
        if area < MIN_AREA: continue
        x,y,w,h = cv2.boundingRect(c)
        boxes.append((x,y,w,h,area))
        motion = True
    return motion, boxes

# ---- FFmpeg launcher (tee: segment to disk + RTSP publish) ----
ffmpeg_proc = None
def start_ffmpeg(width, height, fps):
    out_pattern = os.path.join(BASE_DIR, "recording_%Y%m%d_%H%M%S.mp4")
    
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", 
        "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        
        # Output 1: HIGH QUALITY for local recording
        "-vf", "format=yuv420p",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",              # High quality (18-23 range)
        "-g", str(int(fps)),
        "-bf", "2",
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        "-reset_timestamps", "1",
        "-strftime", "1",
        out_pattern,
        
        # Output 2: LOWER QUALITY/BITRATE for RTSP streaming
        "-vf", "format=yuv420p",
        "-c:v", "libx264",
        "-preset", "veryfast",     # Faster encoding
        "-tune", "zerolatency",    # Low latency for streaming
        "-b:v", "1.5M",            # Lower bitrate
        "-maxrate", "1.5M",
        "-bufsize", "3M",
        "-g", str(int(fps)),
        "-bf", "0",                # No B-frames for low latency
        "-sc_threshold", "0",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        RTSP_OUT
    ]
    
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, 
                           stdout=subprocess.DEVNULL, 
                           stderr=subprocess.DEVNULL, 
                           bufsize=0)

# ---- Threads ----
frame_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=FRAME_QUEUE_MAX)
running = True

def reader():
    global running
    cap = cv2.VideoCapture(URL)
    if not cap.isOpened(): raise RuntimeError("Could not open ESP MJPEG stream")
    # prime first frame to get size
    ok, f = cap.read()
    if not ok: raise RuntimeError("Failed initial read")
    try: cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception: pass

    while running:
        ok, frame = cap.read()
        if not ok:
            # reconnect
            cap.release()
            time.sleep(0.5)
            cap = cv2.VideoCapture(URL)
            continue
        if frame_q.full():
            try: frame_q.get_nowait()
            except queue.Empty: pass
        try: frame_q.put(frame, block=False)
        except queue.Full: pass
    cap.release()

def processor():
    global running, ffmpeg_proc
    width = height = None
    last_sent = 0.0
    period = 1.0 / max(1, TARGET_FPS)

    while running:
        try:
            frame = frame_q.get(timeout=1.0)
        except queue.Empty:
            continue

        if width is None:
            height, width = frame.shape[:2]
            ffmpeg_proc = start_ffmpeg(width, height, TARGET_FPS)

        # detect + draw overlay (IN-PLACE so overlay is saved & restreamed)
        motion, boxes = detect_motion(frame)
        draw_overlay(frame, boxes, motion)

        # pace to TARGET_FPS (best-effort)
        now = time.time()
        if last_sent:
            sleep = period - (now - last_sent)
            if sleep > 0: time.sleep(sleep)
        last_sent = time.time()

        # send to ffmpeg (raw bgr)
        try:
            ffmpeg_proc.stdin.write(frame.tobytes())     # type: ignore
        except (BrokenPipeError, IOError):
            # restart ffmpeg if it died
            try:
                ffmpeg_proc.stdin.close()   # type: ignore
                ffmpeg_proc.wait(timeout=2)   # type: ignore
            except Exception:
                pass
            ffmpeg_proc = start_ffmpeg(width, height, TARGET_FPS)

        if SHOW_PREVIEW:
            cv2.imshow("ESP Overlay + Motion (recording & RTSP)", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                running = False
                break

    if ffmpeg_proc:
        try:
            ffmpeg_proc.stdin.close()    # type: ignore
            ffmpeg_proc.wait(timeout=3)
        except Exception:
            pass
    if SHOW_PREVIEW:
        cv2.destroyAllWindows()

def main():
    t1 = threading.Thread(target=reader, daemon=True)
    t2 = threading.Thread(target=processor, daemon=True)
    t1.start(); t2.start()
    print(f"Recording chunks in {BASE_DIR}")
    print(f"Restreaming RTSP at {RTSP_OUT} (make sure your RTSP server is running)")
    try:
        while running:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        globals()['running'] = False
        t1.join(timeout=2); t2.join(timeout=2)

if __name__ == "__main__":
    main()
