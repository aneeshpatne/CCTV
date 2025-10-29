from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from pathlib import Path
import os
import socket

app = FastAPI(title="CCTV Video Server", version="1.0")

# Enable CORS for network access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure your CCTV footage directory
CCTV_FOLDER = "/media/aneesh/SSD/recordings/esp_cam1"

@app.get("/")
async def root():
    """Root endpoint with server info"""
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    return {
        "message": "CCTV Video Server",
        "hostname": hostname,
        "local_ip": local_ip,
        "endpoints": {
            "list_videos": "/video/list",
            "by_timestamp": "/video/by-timestamp?timestamp=YYYY-MM-DDTHH:MM:SS",
            "stream_file": "/video/stream/{filename}",
            "docs": "/docs"
        }
    }


def find_video_by_timestamp(timestamp: datetime) -> Path:
    """
    Find video file matching the given timestamp.
    Expected filename format: recording_YYYYMMDD_HHMMSS.mp4
    """
    # Format the timestamp to match your filename pattern
    filename = f"recording_{timestamp.strftime('%Y%m%d_%H%M%S')}.mp4"
    filepath = Path(CCTV_FOLDER) / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"Video not found for timestamp: {timestamp}")
    
    return filepath


@app.get("/video/by-timestamp")
async def get_video_by_timestamp(timestamp: str):
    """
    Get video by timestamp.
    
    Args:
        timestamp: ISO format datetime string (e.g., "2024-10-29T10:58:04")
                  or custom format "YYYY-MM-DD HH:MM:SS"
    
    Example:
        /video/by-timestamp?timestamp=2024-10-29T10:58:04
        /video/by-timestamp?timestamp=2024-10-29 10:58:04
    """
    try:
        # Try parsing ISO format first
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except:
            # Try parsing custom format
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        
        video_path = find_video_by_timestamp(dt)
        
        # Return video file with proper headers for streaming
        return FileResponse(
            path=str(video_path),
            media_type="video/mp4",
            filename=video_path.name
        )
        
    except ValueError as e:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid timestamp format. Use ISO format (2024-10-29T10:58:04) or YYYY-MM-DD HH:MM:SS"
        )


@app.get("/video/stream/{filename}")
async def stream_video(filename: str):
    """
    Stream video by exact filename.
    
    Example:
        /video/stream/recording_20251029_105804.mp4
    """
    filepath = Path(CCTV_FOLDER) / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    
    if not filename.endswith('.mp4'):
        raise HTTPException(status_code=400, detail="Only MP4 files are supported")
    
    return FileResponse(
        path=str(filepath),
        media_type="video/mp4",
        filename=filename
    )


@app.get("/video/list")
async def list_videos():
    """
    List all available video recordings with their timestamps.
    """
    try:
        folder = Path(CCTV_FOLDER)
        videos = []
        
        for file in folder.glob("recording_*.mp4"):
            # Extract timestamp from filename
            try:
                timestamp_str = file.stem.replace("recording_", "")
                dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                videos.append({
                    "filename": file.name,
                    "timestamp": dt.isoformat(),
                    "size_mb": round(file.stat().st_size / (1024 * 1024), 2)
                })
            except ValueError:
                continue
        
        videos.sort(key=lambda x: x['timestamp'], reverse=True)
        return {"videos": videos, "count": len(videos)}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    
    # Get local IP address
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    
    print("\n" + "="*60)
    print("🎥 CCTV Video Server Starting...")
    print("="*60)
    print(f"📍 Local access:   http://127.0.0.1:8000")
    print(f"🌐 Network access: http://{local_ip}:8000")
    print(f"📚 API docs:       http://{local_ip}:8000/docs")
    print("="*60 + "\n")
    
    # Run server accessible on all network interfaces
    uvicorn.run(
        app, 
        host="0.0.0.0",  # Listen on all network interfaces
        port=8005,
        log_level="info"
    )