from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio
from fastapi.responses import FileResponse
import uuid


load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncIOMotorClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
db = client["video-downloader"]

class DownloadRequest(BaseModel):
    url: str

class MergeDownloadRequest(BaseModel):
    url: str
    format_id: str

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
                        'format_id': f.get('format_id', '')
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
                'playable_url': playable_url
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/merge_download")
async def merge_download(request: MergeDownloadRequest):
    try:
        if not request.url or not request.format_id:
            raise HTTPException(status_code=400, detail="URL and format_id are required")

        output_filename = f"merged_{uuid.uuid4()}.mp4"
        ydl_opts = {
            'format': f'{request.format_id}+bestaudio/best',
            'outtmpl': output_filename,
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
            'cookiefile': 'youtube_cookies.txt',
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([request.url])

        return FileResponse(output_filename, filename=output_filename, media_type='video/mp4')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5001))
    uvicorn.run(app, host="0.0.0.0", port=port) 