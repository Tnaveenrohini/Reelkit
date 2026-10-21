"""ReelKit backend: Instagram downloader, caption extractor,
speech-to-script (faster-whisper) and vocal/BGM splitter (Demucs)."""
import os, re, sys, uuid, time, shutil, subprocess, tempfile, threading, urllib.request
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

BASE = Path(__file__).parent
WORK = Path(tempfile.gettempdir()) / "reelkit"
WORK.mkdir(exist_ok=True)
COOKIES = os.getenv("IG_COOKIES")  # optional Netscape cookies.txt exported from your browser
IG_URL = re.compile(r"^https?://(www\.)?instagram\.com/(p|reel|reels|tv|stories)/", re.I)

app = FastAPI(title="ReelKit")
jobs, _model = {}, None
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","), allow_methods=["*"], allow_headers=["*"])


class Req(BaseModel):
    url: str


def clean(url: str) -> str:
    url = url.strip().split("?")[0]
    if not IG_URL.match(url):
        raise HTTPException(400, "Paste a valid Instagram post, Reel, IGTV or Story link.")
    return url


def ydl_opts(**kw):
    o = {"quiet": True, "no_warnings": True}
    if COOKIES:
        o["cookiefile"] = COOKIES
    return {**o, **kw}


def fetch_info(url):
    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as y:
            return y.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(422, "Couldn't read this link. The post may be private, deleted, or Instagram "
                                 "is rate-limiting this server (set IG_COOKIES to fix). " + str(e)[-160:])


def items_of(d):
    return list(d.get("entries") or [d])


def is_video(e):
    return bool(e.get("formats"))


def grab(url, dest: Path, index=0) -> Path:
    o = ydl_opts(outtmpl=str(dest / "%(id)s.%(ext)s"), format="best[ext=mp4]/best",
                 playlist_items=str(index + 1))
    with yt_dlp.YoutubeDL(o) as y:
        y.extract_info(url, download=True)
    return next(p for p in dest.iterdir() if p.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"})


def sh(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {r.stderr[-300:]}")


# ---------- downloader + caption ----------
@app.post("/api/info")
def info(b: Req):
    url = clean(b.url)
    d = fetch_info(url)
    its = items_of(d)
    caption = d.get("description") or (its[0].get("description") if its else "") or ""
    return {
        "uploader": d.get("uploader") or d.get("channel"),
        "thumb": d.get("thumbnail") or (its[0].get("thumbnail") if its else None),
        "caption": caption,
        "hashtags": list(dict.fromkeys(re.findall(r"#\w+", caption))),
        "mentions": list(dict.fromkeys(re.findall(r"@[\w.]+", caption))),
        "items": [{"type": "video" if is_video(e) else "photo"} for e in its],
    }


@app.get("/api/download")
def download(url: str, index: int = 0):
    url = clean(url)
    its = items_of(fetch_info(url))
    item = its[min(max(index, 0), len(its) - 1)]
    d = Path(tempfile.mkdtemp(dir=WORK))
    if is_video(item):
        path = grab(url, d, index)
    else:
        src = item.get("url") or item.get("thumbnail") or ""
        if not (urlparse(src).hostname or "").endswith(("cdninstagram.com", "fbcdn.net")):
            shutil.rmtree(d, True)
            raise HTTPException(404, "No downloadable file found for this item.")
        path = d / f"{item.get('id', 'photo')}.jpg"
        urllib.request.urlretrieve(src, path)
    return FileResponse(path, filename=f"reelkit-{path.name}", background=BackgroundTask(shutil.rmtree, d, True))


# ---------- AI jobs: script + splitter ----------
def transcribe(wav: Path):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(os.getenv("WHISPER_MODEL", "base"), compute_type="int8")
    segs, meta = _model.transcribe(str(wav), vad_filter=True)
    out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs]
    return {"language": meta.language, "segments": out, "text": " ".join(s["text"] for s in out)}


def work(jid, kind, url):
    j, d = jobs[jid], WORK / jid
    try:
        d.mkdir()
        j["step"] = "Downloading the video"
        video = grab(url, d)
        j["step"] = "Extracting audio"
        wav = d / "audio.wav"
        sh(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "2", "-ar", "44100", str(wav)])
        if kind == "script":
            j["step"] = "Transcribing speech"
            j["result"] = transcribe(wav)
        else:
            j["step"] = "Separating vocals from music (a few minutes on CPU)"
            sh([sys.executable, "-m", "demucs", "--two-stems=vocals", "--mp3", "-o", str(d / "out"), str(wav)])
            j["result"] = {"vocals": f"/api/files/{jid}/vocals.mp3", "bgm": f"/api/files/{jid}/no_vocals.mp3"}
        j["status"] = "done"
    except Exception as e:
        j.update(status="error", error=str(e)[-300:])


@app.post("/api/start/{kind}")
def start(kind: str, b: Req):
    if kind not in ("script", "split"):
        raise HTTPException(404)
    url = clean(b.url)
    for p in WORK.iterdir():  # drop anything older than an hour
        if time.time() - p.stat().st_mtime > 3600:
            shutil.rmtree(p, True)
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"status": "running", "step": "Starting"}
    threading.Thread(target=work, args=(jid, kind, url), daemon=True).start()
    return {"id": jid}


@app.get("/api/job/{jid}")
def job(jid: str):
    if jid not in jobs:
        raise HTTPException(404, "Job not found.")
    return jobs[jid]


@app.get("/api/files/{jid}/{name}")
def files(jid: str, name: str, dl: int = 0):
    p = WORK / jid / "out" / "htdemucs" / "audio" / name
    if not re.fullmatch(r"[0-9a-f]{12}", jid) or name not in ("vocals.mp3", "no_vocals.mp3") or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/mpeg", filename=f"reelkit-{name}" if dl else None)


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
