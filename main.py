from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio
from fastapi.responses import FileResponse, StreamingResponse
import uuid
import json
from fastapi.responses import Response
from starlette.responses import StreamingResponse as StarletteStreamingResponse
import threading
import time

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://videogetter.netlify.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
db = client["video-downloader"]

# Store download progress with more granular tracking
download_progress = {}
download_locks = {}

class DownloadRequest(BaseModel):
    url: str

class MergeDownloadRequest(BaseModel):
    url: str
    format_id: str

def create_progress_hook(download_id):
    """Create a progress hook for a specific download"""
    def progress_hook(d):
        try:
            if d['status'] == 'downloading':
                # Only update progress when merging is happening
                if 'postprocess' in d.get('_downloader', {}).__dict__:
                    download_progress[download_id] = {
                        'status': 'merging',
                        'percent': 50,  # Start at 50% since download is complete
                        'timestamp': time.time()
                    }
                else:
                    # During download, keep progress at 0-50%
                    total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    downloaded_bytes = d.get('downloaded_bytes', 0)
                    
                    if total_bytes > 0:
                        percent = (downloaded_bytes / total_bytes) * 50  # Scale to 0-50%
                    else:
                        percent = 0
                    
                    download_progress[download_id] = {
                        'status': 'downloading',
                        'percent': round(percent, 1),
                        'downloaded_bytes': downloaded_bytes,
                        'total_bytes': total_bytes,
                        'speed': d.get('speed', 0),
                        'eta': d.get('eta', 0),
                        'timestamp': time.time()
                    }
                
            elif d['status'] == 'finished':
                download_progress[download_id] = {
                    'status': 'finished',
                    'percent': 100,
                    'filename': d.get('filename', ''),
                    'timestamp': time.time()
                }
                
            elif d['status'] == 'error':
                download_progress[download_id] = {
                    'status': 'error',
                    'percent': 0,
                    'error': str(d.get('error', 'Unknown error')),
                    'timestamp': time.time()
                }
                
        except Exception as e:
            print(f"Error in progress hook: {e}")
            download_progress[download_id] = {
                'status': 'error',
                'percent': 0,
                'error': str(e),
                'timestamp': time.time()
            }
    
    return progress_hook

@app.get("/api/health")
async def health_check():
    return {"status": "ok"}

@app.post("/api/download")
async def download_video(request: DownloadRequest):
    try:
        if not request.url:
            raise HTTPException(status_code=400, detail="URL is required")

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'cookiefile': 'youtube_cookies.txt'
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(request.url, download=False)

            formats = []
            playable_url = None

            print("Available formats from yt-dlp:")
            for f in info.get('formats', []):
                print(f"Format: {f.get('format_note')} - Resolution: {f.get('resolution')} - Has Audio: {f.get('acodec') != 'none'}")

            # Get all available formats
            for f in info.get('formats', []):
                # Include formats that have video
                if f.get('vcodec') != 'none':
                    format_info = {
                        'quality': f.get('format_note', 'unknown'),
                        'url': f.get('url'),
                        'mimeType': f.get('ext', 'unknown'),
                        'resolution': f.get('resolution', 'unknown'),
                        'filesize': f.get('filesize', 0),
                        'hasAudio': f.get('acodec') != 'none',
                        'format_id': f.get('format_id', ''),
                        'video_id': info.get('id', 'unknown')  # Add video ID for progress tracking
                    }
                    formats.append(format_info)
                    if not playable_url and f.get('ext') == 'mp4' and f.get('acodec') != 'none':
                        playable_url = f.get('url')

            print("\nProcessed formats being sent to frontend:")
            for f in formats:
                print(f"Format: {f['quality']} - Resolution: {f['resolution']} - Has Audio: {f['hasAudio']}")

            # Sort formats by resolution (highest first)
            formats.sort(key=lambda x: (
                int(x['resolution'].split('x')[1]) if 'x' in x['resolution'] else 0
            ), reverse=True)

            print("\nFinal sorted formats:")
            for f in formats:
                print(f"Format: {f['quality']} - Resolution: {f['resolution']} - Has Audio: {f['hasAudio']}")

            # Fallback: any video+audio format
            if not playable_url and formats:
                for f in formats:
                    if f.get('hasAudio'):
                        playable_url = f['url']
                        break

            return {
                'title': info.get('title'),
                'formats': formats,
                'playable_url': playable_url,
                'video_id': info.get('id', 'unknown')
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/download_progress/{download_id}")
async def get_download_progress(download_id: str):
    """Get current download progress"""
    progress = download_progress.get(download_id, {'status': 'waiting', 'percent': 0})
    return progress

@app.get("/api/progress_stream/{download_id}")
async def progress_stream(download_id: str):
    """Server-Sent Events stream for real-time progress"""
    async def event_stream():
        last_percent = -1
        start_time = time.time()
        
        while True:
            current_time = time.time()
            
            # Timeout after 10 minutes
            if current_time - start_time > 600:
                yield f"data: {json.dumps({'status': 'timeout', 'percent': 0})}\n\n"
                break
                
            progress = download_progress.get(download_id, {'status': 'waiting', 'percent': 0})
            current_percent = progress.get('percent', 0)
            
            # Send update if progress changed or every 2 seconds
            if current_percent != last_percent or (current_time - start_time) % 2 < 1:
                yield f"data: {json.dumps(progress)}\n\n"
                last_percent = current_percent
            
            # Break if download is finished or error
            if progress.get('status') in ['finished', 'completed', 'error']:
                break
                
            await asyncio.sleep(0.5)  # Check every 500ms
    
    return StreamingResponse(
        event_stream(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
    )

def download_with_progress(url, format_id, output_filename, download_id):
    """Download video in a separate thread with progress tracking"""
    try:
        # Initialize progress
        download_progress[download_id] = {'status': 'starting', 'percent': 0}
        
        ydl_opts = {
            'format': f'{format_id}+bestaudio/best',
            'outtmpl': output_filename,
            'merge_output_format': 'mp4',
            'quiet': False,  # Enable some output for debugging
            'no_warnings': False,
            'cookiefile': 'youtube_cookies.txt',
            'progress_hooks': [create_progress_hook(download_id)],
            'writesubtitles': False,
            'writeautomaticsub': False,
        }

        print(f"Starting download for {download_id}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Mark as completed
        download_progress[download_id] = {'status': 'completed', 'percent': 100}
        print(f"Download completed for {download_id}")
        
    except Exception as e:
        print(f"Download error for {download_id}: {e}")
        download_progress[download_id] = {
            'status': 'error', 
            'percent': 0, 
            'error': str(e)
        }

@app.get("/api/merge_download")
async def merge_download(url: str = Query(...), format_id: str = Query(...), download_id: str = Query(None)):
    try:
        if not url or not format_id:
            raise HTTPException(status_code=400, detail="URL and format_id are required")

        # Use provided download_id or create a new one
        if not download_id:
            download_id = str(uuid.uuid4())
        output_filename = f"merged_{download_id}.mp4"

        # Start download in background thread
        download_thread = threading.Thread(
            target=download_with_progress,
            args=(url, format_id, output_filename, download_id)
        )
        download_thread.start()

        # Wait for download to complete
        max_wait_time = 3600  # 1 hour
        start_time = time.time()
        
        while download_thread.is_alive():
            if time.time() - start_time > max_wait_time:
                raise HTTPException(status_code=408, detail="Download timeout")
            await asyncio.sleep(1)

        # Check if download was successful
        final_progress = download_progress.get(download_id, {})
        if final_progress.get('status') == 'error':
            raise HTTPException(status_code=500, detail=final_progress.get('error', 'Download failed'))

        if not os.path.exists(output_filename):
            raise HTTPException(status_code=500, detail="Downloaded file not found")

        file_size = os.path.getsize(output_filename)
        headers = {"Content-Length": str(file_size)}

        def iterfile():
            try:
                with open(output_filename, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)  # 1MB chunks
                        if not chunk:
                            break
                        yield chunk
            finally:
                # Clean up the file after streaming
                if os.path.exists(output_filename):
                    try:
                        os.remove(output_filename)
                    except:
                        pass
                # Clean up progress tracking
                if download_id in download_progress:
                    del download_progress[download_id]

        return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)
        
    except Exception as e:
        # Clean up on error
        if 'download_id' in locals() and download_id in download_progress:
            del download_progress[download_id]
        if 'output_filename' in locals() and os.path.exists(output_filename):
            try:
                os.remove(output_filename)
            except:
                pass
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5001))
    uvicorn.run(app, host="0.0.0.0", port=port)