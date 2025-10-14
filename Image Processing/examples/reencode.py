import cv2
from datetime import datetime
import pytz
import subprocess
import threading
import queue
import time

URL = "http://192.168.1.116:81/stream"  
cap = cv2.VideoCapture(URL)

if not cap.isOpened():
    raise RuntimeError("Could Not Open Stream")

cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
fps = 10  # Force 10 FPS

ist = pytz.timezone('Asia/Kolkata')
clicked_points = []
running = True

# Thread-safe queue with max size to prevent buffer buildup
frame_queue = queue.Queue(maxsize=2)

def show_coordinates(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:  
        print(f"Clicked at: ({x}, {y})")
        clicked_points.append((x, y))

rtsp_url = "rtsp://localhost:8554/mystream"

# More aggressive settings for low latency
ffmpeg_cmd = [
    'ffmpeg','-y',
    '-f','rawvideo','-vcodec','rawvideo','-pix_fmt','bgr24',
    '-s', f'{width}x{height}', '-r', str(fps), '-i','-',
    '-vf','format=yuv420p',
    '-c:v','libx264',
    '-preset','veryfast',          
    '-tune','zerolatency',
    '-crf','26',                   
    '-profile:v','main',          
    '-g', str(fps),                
    '-bf','0','-refs','1','-sc_threshold','0',
    '-maxrate','2500k',           
    '-bufsize','1200k',            
    '-pix_fmt','yuv420p',
    '-f','rtsp','-rtsp_transport','tcp', rtsp_url
]

ffmpeg_process = subprocess.Popen(
    ffmpeg_cmd,
    stdin=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    bufsize=0  # No buffering
)

SHOW_PREVIEW = False

if SHOW_PREVIEW:
    cv2.namedWindow("frame")
    cv2.setMouseCallback("frame", show_coordinates, {"clicked_points": clicked_points})

print(f"Publishing to RTSP: {rtsp_url}")

# Thread 1: Read frames from camera (drop old frames)
def frame_reader():
    global running
    while running:
        ret, frame = cap.read()
        if not ret:
            print("Failed to read frame")
            running = False
            break
        
        # Drop old frames if queue is full
        if frame_queue.full():
            try:
                frame_queue.get_nowait()  # Remove old frame
            except queue.Empty:
                pass
        
        try:
            frame_queue.put(frame, block=False)
        except queue.Full:
            pass  # Skip if still full

# Thread 2: Process and send frames to FFmpeg
def frame_processor():
    global running
    frame_count = 0
    start_time = time.time()
    
    while running:
        try:
            frame = frame_queue.get(timeout=1)
        except queue.Empty:
            continue
        
        # Draw overlays
        for point in clicked_points:
            cv2.circle(frame, point, 5, (0, 255, 0), -1)
        
        current_time = datetime.now(ist)
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S %p")
        cv2.putText(frame, formatted_time, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                    0.6, (0, 255, 0), 2, cv2.LINE_AA)
        
        # Write to FFmpeg
        try:
            ffmpeg_process.stdin.write(frame.tobytes()) # type: ignore
            ffmpeg_process.stdin.flush() # type: ignore
        except (BrokenPipeError, IOError):
            print("FFmpeg pipe broken")
            running = False
            break
        
        if SHOW_PREVIEW:
            cv2.imshow("frame", frame)
            if cv2.waitKey(1) == ord('q'):
                running = False
                break
        
        # FPS counter
        frame_count += 1
        if frame_count % 50 == 0:
            elapsed = time.time() - start_time
            current_fps = frame_count / elapsed
            print(f"Processing FPS: {current_fps:.2f}")

# Start threads
reader_thread = threading.Thread(target=frame_reader, daemon=True)
processor_thread = threading.Thread(target=frame_processor, daemon=True)

reader_thread.start()
processor_thread.start()

print("Streaming started. Press Ctrl+C to stop.")

try:
    # Keep main thread alive
    while running:
        time.sleep(0.1)
except KeyboardInterrupt:
    print("\nStopping...")
    running = False

# Cleanup
reader_thread.join(timeout=2)
processor_thread.join(timeout=2)

ffmpeg_process.stdin.close() # type: ignore
ffmpeg_process.wait()
cap.release()
if SHOW_PREVIEW:
    cv2.destroyAllWindows()

print("Stream stopped")