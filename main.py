from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio


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

            # Only consider formats with both video and audio
            for f in info.get('formats', []):
                if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                    formats.append({
                        'quality': f.get('format_note', 'unknown'),
                        'url': f.get('url'),
                        'mimeType': f.get('ext', 'unknown')
                    })
                    if not playable_url and f.get('ext') == 'mp4':
                        playable_url = f.get('url')

            # Fallback: any video+audio format
            if not playable_url and formats:
                playable_url = formats[0]['url']

            return {
                'title': info.get('title'),
                'formats': formats,
                'playable_url': playable_url
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 5001))
    uvicorn.run(app, host="0.0.0.0", port=port) 