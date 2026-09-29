import json
import math
import os
import random
import re
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from gtts import gTTS
from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    TextClip,
    VideoFileClip,
    afx,
    vfx,
)
from starlette.background import BackgroundTask

# ---------- Config ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BG_DIR = os.path.join(BASE_DIR, "backgrounds")   # put all your background video clips in this folder
BG_EXTS = (".mp4", ".mov", ".webm", ".m4v")
MUSIC_DIR = os.path.join(BASE_DIR, "music")       # put background-music tracks in this folder (optional)
MUSIC_EXTS = (".mp3", ".m4a", ".wav", ".ogg")
MUSIC_VOLUME = 0.12                               # fraction of full volume, kept low under narration
W, H = 720, 1280            # vertical 9:16; kept modest so free CPUs render quickly
FPS = 24
MAX_SCRIPT_CHARS = 450      # ~30 seconds of speech
WORDS_PER_CAPTION = 3

FONT_CANDIDATES = [
    os.path.join(BASE_DIR, "font.ttf"),                          # drop your own font here
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",      # installed by the Dockerfile
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

app = FastAPI(title="Topic to Video")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],                      # tighten to your Netlify URL once it works
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],   # lets the front end read the filename
)

render_lock = threading.Semaphore(1)  # free Spaces have ~2 CPUs: one render at a time


# ---------- Script text ----------
def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "topic-video-space/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _wiki_summary(title: str) -> str:
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))
    data = _get_json(url)
    if data.get("type") == "disambiguation":
        return ""
    return data.get("extract", "")


def build_script(topic: str) -> str:
    """Short narration for the topic, taken from Wikipedia. Falls back to a simple line."""
    text = ""
    try:
        try:
            text = _wiki_summary(topic)
        except Exception:
            q = urllib.parse.urlencode({"action": "opensearch", "search": topic, "limit": 1, "format": "json"})
            hits = _get_json("https://en.wikipedia.org/w/api.php?" + q)[1]
            if hits:
                text = _wiki_summary(hits[0])
    except Exception:
        text = ""

    text = re.sub(r"\s*\([^)]*\)", "", text)          # drop parentheticals like pronunciations
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return f"Let's take a quick look at {topic}."

    # Trim to whole sentences under the length cap
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sentences:
        if out and len(out) + len(s) + 1 > MAX_SCRIPT_CHARS:
            break
        out = f"{out} {s}".strip()
    return out[:MAX_SCRIPT_CHARS]


# ---------- Video ----------
def pick_background() -> str:
    """Return a random clip from the backgrounds/ folder."""
    if not os.path.isdir(BG_DIR):
        raise RuntimeError(f"Missing folder: backgrounds/ (expected at {BG_DIR})")
    clips = [
        f for f in os.listdir(BG_DIR)
        if f.lower().endswith(BG_EXTS) and os.path.isfile(os.path.join(BG_DIR, f))
    ]
    if not clips:
        raise RuntimeError("No background videos found in backgrounds/. Add at least one .mp4 file.")
    return os.path.join(BG_DIR, random.choice(clips))


def pick_music() -> str | None:
    """Return a random track from the music/ folder, or None if there isn't one."""
    if not os.path.isdir(MUSIC_DIR):
        return None
    tracks = [
        f for f in os.listdir(MUSIC_DIR)
        if f.lower().endswith(MUSIC_EXTS) and os.path.isfile(os.path.join(MUSIC_DIR, f))
    ]
    return os.path.join(MUSIC_DIR, random.choice(tracks)) if tracks else None


def find_font() -> str:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError("No font found. Add a font.ttf next to main.py.")


def fit_vertical(clip):
    """Scale to cover W x H, then center-crop."""
    scale = max(W / clip.w, H / clip.h)
    nw, nh = math.ceil(clip.w * scale), math.ceil(clip.h * scale)
    clip = clip.resized(new_size=(nw, nh))
    return clip.cropped(x1=(nw - W) // 2, y1=(nh - H) // 2, width=W, height=H)


def make_captions(script: str, total: float, font: str):
    """Show a few words at a time, timed by character count across the narration."""
    words = script.split()
    groups = [" ".join(words[i:i + WORDS_PER_CAPTION]) for i in range(0, len(words), WORDS_PER_CAPTION)]
    weights = [len(g) + 1 for g in groups]
    per_unit = total / sum(weights)

    clips, t = [], 0.0
    for text, weight in zip(groups, weights):
        dur = weight * per_unit
        clip = (
            TextClip(
                font=font,
                text=text,
                font_size=78,
                color="white",
                stroke_color="black",
                stroke_width=5,
                method="caption",
                size=(W - 100, None),
                text_align="center",
                margin=(0, 24),
            )
            .with_start(t)
            .with_duration(dur)
            .with_position(("center", "center"))
        )
        clips.append(clip)
        t += dur
    return clips


def render_video(script: str, workdir: str) -> str:
    audio_path = os.path.join(workdir, "voice.mp3")
    out_path = os.path.join(workdir, "video.mp4")

    gTTS(text=script, lang="en").save(audio_path)

    voice = bg = music = mixed_audio = final = None
    captions = []
    try:
        voice = AudioFileClip(audio_path)
        duration = voice.duration

        bg_path = pick_background()
        bg = VideoFileClip(bg_path, audio=False)
        if bg.duration <= duration:
            bg = bg.with_effects([vfx.Loop(duration=duration)])      # too short: loop it
        else:
            start = random.uniform(0, bg.duration - duration)         # too long: cut a random section
            bg = bg.subclipped(start, start + duration)
        bg = fit_vertical(bg)

        music_path = pick_music()
        if music_path:
            music = AudioFileClip(music_path)
            if music.duration <= duration:
                music = music.with_effects([afx.AudioLoop(duration=duration)])   # too short: loop it
            else:
                m_start = random.uniform(0, music.duration - duration)            # too long: random section
                music = music.subclipped(m_start, m_start + duration)
            music = music.with_effects([afx.MultiplyVolume(MUSIC_VOLUME)])        # keep it quiet under the voice
            mixed_audio = CompositeAudioClip([music, voice]).with_duration(duration)
        else:
            mixed_audio = voice

        captions = make_captions(script, duration, find_font())
        final = (
            CompositeVideoClip([bg, *captions], size=(W, H))
            .with_duration(duration)
            .with_audio(mixed_audio)
        )
        final.write_videofile(
            out_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            threads=2,
            logger=None,
            temp_audiofile=os.path.join(workdir, "tmp_audio.m4a"),
            ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],  # plays on every phone
        )
    finally:
        for c in (final, *captions, bg, music, voice, mixed_audio):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
    return out_path


# ---------- Routes ----------
@app.get("/")
def health():
    return {"status": "ok", "usage": "/generate?topic=How volcanoes form"}


@app.get("/generate")
def generate(
    topic: str = Query(..., min_length=1, max_length=100),
    script: str | None = Query(None, max_length=MAX_SCRIPT_CHARS, description="Optional: exact text to narrate"),
):
    topic = topic.strip()
    if not topic:
        raise HTTPException(400, "Please provide a topic.")
    if not os.path.isdir(BG_DIR) or not any(
        f.lower().endswith(BG_EXTS) for f in os.listdir(BG_DIR)
    ):
        raise HTTPException(500, "No background videos found in the backgrounds/ folder.")

    if not render_lock.acquire(blocking=False):
        raise HTTPException(429, "Another video is rendering. Try again in a minute.")

    workdir = tempfile.mkdtemp(prefix="vid_")
    try:
        text = (script or "").strip() or build_script(topic)
        out_path = render_video(text, workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(500, f"Render failed: {e}")
    finally:
        render_lock.release()

    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:50] or "video"
    return FileResponse(
        out_path,
        media_type="video/mp4",
        filename=f"{slug}.mp4",  # sets Content-Disposition: attachment
        background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True),
    )
