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

# --- CORS MIDDLEWARE (robust config for local dev) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
        "https://videogetter.netlify.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Explicit OPTIONS handler for /api/download (for CORS preflight) ---
from fastapi import Response
@app.options("/api/download")
def options_download():
    return Response(status_code=204)

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
                downloader = d.get('_downloader', {})
                if isinstance(downloader, dict) and 'postprocess' in downloader:
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
            'quiet': False,  
            'no_warnings': False,  
            # 'cookiefile': 'youtube_cookies.txt',
            'extractor_args': {
                'youtube': {
                    'skip': ['dash', 'hls'],
                }
            },
            'format_sort': ['res:1080', 'ext:mp4:m4a'],
            'merge_output_format': 'mp4',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'socket_timeout': 60,  # Increased timeout
            'retries': 15,  # Increased retries
            'fragment_retries': 15,
            'file_access_retries': 15,
            'extractor_retries': 15,
            'ignoreerrors': False,  # Don't ignore errors for debugging
            'no_color': True,
            'geo_bypass': True,
            'geo_verification_proxy': None,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
            },
            'age_limit': 99,  # Try to bypass age restriction
            'skip_download': True,  # We only need the info
            'nocheckcertificate': True,
            'prefer_insecure': True,
            'allow_unplayable_formats': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                print(f"Attempting to extract info from URL: {request.url}")
                info = ydl.extract_info(request.url, download=False)
                if info is None:
                    raise HTTPException(status_code=400, detail="Could not extract video information. The video might be private, age-restricted, or not available.")
                print(f"Successfully extracted info for video: {info.get('title', 'Unknown')}")
            except Exception as e:
                error_msg = str(e)
                print(f"Error extracting video info: {error_msg}")
                
                if "Sign in to confirm you're not a bot" in error_msg:
                    raise HTTPException(status_code=403, detail="YouTube requires verification. Please try again later or use a different video.")
                elif "Video unavailable" in error_msg:
                    raise HTTPException(status_code=404, detail="Video is unavailable. It might be private, deleted, or region-restricted.")
                elif "This video is not available" in error_msg:
                    raise HTTPException(status_code=404, detail="This video is not available in your region or has been removed.")
                elif "Video is private" in error_msg:
                    raise HTTPException(status_code=403, detail="This video is private and cannot be accessed.")
                elif "Age restricted" in error_msg:
                    raise HTTPException(status_code=403, detail="This video is age-restricted and cannot be accessed.")
                else:
                    raise HTTPException(status_code=500, detail=f"Error extracting video info: {error_msg}")

            formats = []
            playable_url = None

            print("Available formats from yt-dlp:")
            for f in info.get('formats', []):
                print(f"Format: {f.get('format_note')} - Resolution: {f.get('resolution')} - Has Audio: {f.get('acodec') != 'none'}")

            # Get all available formats
            for f in info.get('formats', []):
                # Only include formats that have video (not just audio)
                if f.get('vcodec') != 'none' and f.get('vcodec') is not None:
                    # Skip storyboard formats (thumbnails)
                    if 'storyboard' in f.get('format_note', '').lower():
                        continue
                    
                    # Skip formats with very low resolution (like 144p)
                    resolution = f.get('resolution', 'unknown')
                    if 'x' in resolution:
                        height = int(resolution.split('x')[1])
                        if height < 144:  # Skip very low quality
                            continue
                        
                    format_info = {
                        'quality': f.get('format_note', 'unknown'),
                        'url': f.get('url'),
                        'mimeType': f.get('ext', 'unknown'),
                        'resolution': f.get('resolution', 'unknown'),
                        'filesize': f.get('filesize', 0),
                        'hasAudio': f.get('acodec') != 'none',
                        'format_id': f.get('format_id', ''),
                        'video_id': info.get('id', 'unknown'),  # Add video ID for progress tracking
                        'isComplete': f.get('acodec') != 'none' and f.get('vcodec') != 'none',
                        'height': int(resolution.split('x')[1]) if 'x' in resolution else 0
                    }
                    formats.append(format_info)
                    # Prefer formats that already have audio
                    if not playable_url and f.get('ext') == 'mp4' and f.get('acodec') != 'none':
                        playable_url = f.get('url')

            if not formats:
                raise HTTPException(status_code=400, detail="No downloadable formats found for this video.")

            print("\nProcessed formats being sent to frontend:")
            for f in formats:
                print(f"Format: {f['quality']} - Resolution: {f['resolution']} - Has Audio: {f['hasAudio']} - Complete: {f['isComplete']}")

            # Sort formats by resolution (highest first), then by completeness
            formats.sort(key=lambda x: (
                x['height'],  # Higher resolution first
                not x['isComplete']  # Complete formats first within same resolution
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
                'video_id': info.get('id', 'unknown'),
                'channel': info.get('channel') or info.get('uploader', ''),
            }
    except HTTPException:
        raise
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
        
        # Create a unique filename to avoid conflicts
        import uuid
        unique_id = str(uuid.uuid4())[:8]
        base_filename = f"download_{download_id}_{unique_id}"
        
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': base_filename,
            'merge_output_format': 'mp4',
            'quiet': False,
            'no_warnings': False,
            'progress_hooks': [create_progress_hook(download_id)],
            'writesubtitles': False,
            'writeautomaticsub': False,
            'socket_timeout': 60,
            'retries': 15,
            'fragment_retries': 15,
            'file_access_retries': 15,
            'extractor_retries': 15,
            'ignoreerrors': True,
            'no_color': True,
            'geo_bypass': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
            },
            'age_limit': 99,
            'nocheckcertificate': True,
            'prefer_insecure': True,
            'allow_unplayable_formats': False,
        }

        print(f"Starting download for {download_id}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # Find the downloaded file
        downloaded_file = None
        possible_extensions = ['.mp4', '.mkv', '.webm', '.avi']
        
        for ext in possible_extensions:
            test_file = f"{base_filename}{ext}"
            if os.path.exists(test_file):
                downloaded_file = test_file
                break
        
        if not downloaded_file:
            # Try to find any file that starts with our base filename
            import glob
            matching_files = glob.glob(f"{base_filename}*")
            if matching_files:
                downloaded_file = matching_files[0]
        
        if downloaded_file:
            # Store the file path in progress for the main thread to access
            download_progress[download_id] = {
                'status': 'completed', 
                'percent': 100,
                'file_path': downloaded_file
            }
            print(f"Download completed for {download_id}: {downloaded_file}")
        else:
            raise Exception("Downloaded file not found")
        
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
        
        # yt-dlp will add the extension automatically, so we don't include it in the template
        output_template = f"merged_{download_id}"
        output_filename = f"{output_template}.mp4"

        # Start download in background thread
        download_thread = threading.Thread(
            target=download_with_progress,
            args=(url, format_id, output_filename, download_id)
        )
        download_thread.start()

        # Wait for download to complete with better error handling
        max_wait_time = 300  # 5 minutes
        start_time = time.time()
        
        while download_thread.is_alive():
            if time.time() - start_time > max_wait_time:
                # Clean up on timeout
                if download_id in download_progress:
                    del download_progress[download_id]
                raise HTTPException(status_code=408, detail="Download timeout")
            
            # Check for errors during download
            progress = download_progress.get(download_id, {})
            if progress.get('status') == 'error':
                error_msg = progress.get('error', 'Download failed')
                if download_id in download_progress:
                    del download_progress[download_id]
                raise HTTPException(status_code=500, detail=error_msg)
            
            await asyncio.sleep(0.5)

        # Check if download was successful
        final_progress = download_progress.get(download_id, {})
        if final_progress.get('status') == 'error':
            error_msg = final_progress.get('error', 'Download failed')
            if download_id in download_progress:
                del download_progress[download_id]
            raise HTTPException(status_code=500, detail=error_msg)

        # Get the downloaded file path from progress
        downloaded_file = final_progress.get('file_path')
        if not downloaded_file or not os.path.exists(downloaded_file):
            if download_id in download_progress:
                del download_progress[download_id]
            raise HTTPException(status_code=500, detail="Downloaded file not found")

        file_size = os.path.getsize(downloaded_file)
        headers = {
            "Content-Length": str(file_size),
            "Content-Disposition": f"attachment; filename=video_{download_id}.mp4"
        }

        def iterfile():
            try:
                with open(downloaded_file, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)  # 1MB chunks
                        if not chunk:
                            break
                        yield chunk
            finally:
                # Clean up the file after streaming
                if os.path.exists(downloaded_file):
                    try:
                        os.remove(downloaded_file)
                    except Exception as e:
                        print(f"Error cleaning up file {downloaded_file}: {e}")
                # Clean up progress tracking
                if download_id in download_progress:
                    del download_progress[download_id]

        return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)
        
    except HTTPException:
        raise
    except Exception as e:
        # Clean up on error
        if 'download_id' in locals() and download_id in download_progress:
            del download_progress[download_id]
        if 'downloaded_file' in locals() and os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
            except:
                pass
        print(f"Download error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5001))
    uvicorn.run(app, host="0.0.0.0", port=port)