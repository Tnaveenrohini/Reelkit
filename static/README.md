# ReelKit

Instagram downloader + AI tools (vocal/BGM splitter, video-to-script, caption copier).

## Run
1. Install ffmpeg (`sudo apt install ffmpeg` / `brew install ffmpeg`) and Python 3.10+.
2. `pip install -r requirements.txt`
3. `python server.py`  then open http://localhost:8000

## Optional settings
- `IG_COOKIES=/path/cookies.txt`  Instagram often blocks anonymous servers. Export cookies from a logged-in browser (Netscape format) to fix "rate-limit / login required" errors.
- `WHISPER_MODEL=small`  better accuracy than the default `base` (also: medium, large-v3).
- `PORT=8000`

## How the features work
- Downloader and caption: yt-dlp reads the post; files are streamed to the browser and deleted.
- Video-to-Script: ffmpeg extracts audio, faster-whisper transcribes it locally.
- Vocal Splitter: ffmpeg extracts audio, Demucs (htdemucs) separates vocals from music.

Update yt-dlp regularly (`pip install -U yt-dlp`), since Instagram changes often.

## Deploy the page on Vercel, backend elsewhere
1. Host the backend (HTTPS required). Set `CORS_ORIGINS=https://your-site.vercel.app` to lock it down.
2. Put the backend URL in `static/config.js` (`window.API_BASE = "https://api.example.com"`).
3. Deploy the `static/` folder to Vercel (Framework: Other, no build command).
