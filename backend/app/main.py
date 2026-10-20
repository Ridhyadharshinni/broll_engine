from __future__ import annotations
import hashlib, json, logging, os, re, shutil, sqlite3, subprocess, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

ROOT = Path(os.getenv("BROLL_DATA_DIR", "./data")).resolve()
OUT = ROOT / "outputs"
UPLOADS = ROOT / "uploads"
DB_PATH = ROOT / "jobs.sqlite3"
for p in (ROOT, OUT, UPLOADS): p.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format='{"timestamp":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}')
log = logging.getLogger("broll")

app = FastAPI(title="Script-to-B-roll Video Engine", version="0.1.0", description="Turns narration or scripts into scene-based B-roll videos with captions and rights metadata.")

class Captions(BaseModel):
    burn_in: bool = True
    srt: bool = True

class SceneInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    visual_hint: Optional[str] = None

class JobRequest(BaseModel):
    audio_url: Optional[str] = None
    script: Optional[str] = None
    scenes: Optional[list[SceneInput]] = None
    voice: str = "en"
    aspect_ratios: list[str] = Field(default_factory=lambda: ["16:9"])
    style: str = "bright, modern, real-world footage"
    avoid: list[str] = Field(default_factory=list)
    captions: Captions = Field(default_factory=Captions)
    callback_url: Optional[str] = None
    audio_local_path: Optional[str] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_input(self):
        if not self.audio_url and not self.audio_local_path and not self.script and not self.scenes:
            raise ValueError("Provide one of audio_url, script, or scenes")
        if (self.audio_url or self.audio_local_path) and (self.script or self.scenes):
            raise ValueError("Use audio_url OR script/scenes, not both")
        if not self.aspect_ratios or any(x not in {"16:9", "9:16", "1:1"} for x in self.aspect_ratios):
            raise ValueError("aspect_ratios must contain only 16:9, 9:16, or 1:1")
        return self


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, status TEXT, progress INTEGER, request_json TEXT, result_json TEXT, error_json TEXT, idempotency_key TEXT UNIQUE, created_at TEXT, updated_at TEXT)")
    conn.commit()
    return conn

DB_LOCK = threading.Lock()

def update_job(job_id: str, **values):
    allowed = {"status", "progress", "result_json", "error_json", "updated_at"}
    values = {k: v for k, v in values.items() if k in allowed}
    values["updated_at"] = datetime.now(timezone.utc).isoformat()
    with DB_LOCK:
        conn = db()
        conn.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE job_id=?", [*values.values(), job_id])
        conn.commit(); conn.close()

def get_job(job_id: str):
    with DB_LOCK:
        conn = db(); row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(); conn.close()
    return row

def json_error(code: str, message: str, retryable: bool = False, details: Any = None):
    return {"error": {"code": code, "message": message, "retryable": retryable, "details": details}}

def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text: return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]

# Context-aware concept mapping. Technical terms must be searched with their domain,
# not as isolated everyday words (for example, "flow table" must not become a river).
CONCEPTS = {
    "saving money": ["budget planning", "coins and savings", "personal finance"],
    "budget": ["budget planning", "calculator finances", "household expenses"],
    "cloud computing": ["cloud computing data center servers", "enterprise server racks", "cloud infrastructure network"],
    "trust": ["people collaborating", "handshake partnership", "teamwork"],
    "health": ["healthy lifestyle", "medical care", "wellness"],
    "climate": ["renewable energy", "solar panels", "wind turbines"],
    "education": ["students learning", "classroom study", "online learning"],
    "sales": ["business analytics dashboard", "retail shopping", "business meeting"],
    "technology": ["computer coding", "technology workplace", "digital devices"],
    "food": ["fresh vegetables", "cooking ingredients", "healthy meal"],
    "travel": ["travel landscape", "airport luggage", "city street"],
    "sad": ["quiet landscape", "rain on window", "supporting community"],
}
# Longer/specific rules are checked before general concepts. These queries deliberately
# include networking context to avoid literal matches such as water flow or matchboxes.
TECHNICAL_CONCEPTS = [
    (r"\b(openflow)\b", [
        "OpenFlow protocol software defined networking SDN switch controller",
        "SDN controller communicating with network switches computer network architecture",
        "network switch packet forwarding control plane data plane",
    ]),
    (r"\b(flow tables?|flow entries)\b", [
        "SDN network switch flow table packet forwarding rules",
        "computer networking packet flow entries match action rules",
        "software defined networking switch packet processing architecture",
    ]),
    (r"\b(match[- ]action|match fields?|packet matching|matching rules)\b", [
        "computer network packet header matching rules SDN switch",
        "network packet classification match action forwarding rules",
        "OpenFlow flow table packet header fields network switch",
    ]),
    (r"\b(software[- ]defined networking|sdn)\b", [
        "software defined networking SDN controller switches architecture",
        "SDN network topology connected switches controller diagram",
        "computer networking data center network infrastructure",
    ]),
    (r"\b(network controller|sdn controller|control plane)\b", [
        "software defined networking controller managing network switches",
        "computer network control plane network management server",
        "network operations center switches routers server racks",
    ]),
    (r"\b(network switch|switches|packet forwarding|data plane)\b", [
        "computer networking ethernet switches routers data center",
        "network switch packet forwarding infrastructure close up",
        "computer network topology connected switches routers",
    ]),
]
SENSITIVE = ["suicide", "self-harm", "mass shooting", "terror", "tragedy", "death", "war crime"]
BRANDS_OR_NAMES = re.compile(r"\b(?:Elon Musk|Taylor Swift|Apple Inc\.?|Microsoft|Google|Amazon)\b", re.I)

def intent_queries(text: str, visual_hint: Optional[str] = None, context: Optional[str] = None) -> list[str]:
    local = f"{text} {visual_hint or ''}".lower()
    combined = local
    # Specific technical concepts in this scene take precedence over dictionary meanings.
    for pattern, queries in TECHNICAL_CONCEPTS:
        if re.search(pattern, local, re.I):
            return queries[:5]
    # Short ambiguous words sometimes depend on the previous/next narration sentence.
    nearby = f"{context or ''}".lower()
    networking_context = bool(re.search(r"\b(sdn|openflow|flow table|network switch|packet|controller|control plane|data plane)\b", nearby))
    if networking_context and re.search(r"\b(match|matches|matched|matching)\b", local):
        return [
            "computer network packet matching rules SDN switch",
            "network packet classification match action forwarding rules",
            "OpenFlow flow table packet header fields network switch",
        ]
    # If the current sentence is not enough, use nearby context to resolve the domain.
    combined = f"{local} {nearby}"
    for pattern, queries in TECHNICAL_CONCEPTS:
        if re.search(pattern, combined, re.I):
            return queries[:5]
    found = []
    # Prefer phrase-level concept matches; the original narration is retained for audit.
    for phrase, queries in CONCEPTS.items():
        if phrase in combined:
            found.extend(queries)
    if not found:
        # Preserve useful surrounding context; avoid standalone ambiguous words like "match".
        stop = set("the a an and or but to of in on for with is are was were be this that it we you they our your from by as at using uses used into can will its their these those".split())
        terms = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", combined) if w not in stop]
        found = [" ".join(terms[:7]) or "abstract calm nature background", "wide cinematic real-world scene"]
    return list(dict.fromkeys(found))[:5]

class FootageProvider:
    name = "base"
    def search(self, query: str, avoid: list[str]) -> Optional[dict]: raise NotImplementedError

class PexelsProvider(FootageProvider):
    name = "pexels"
    def __init__(self): self.key = os.getenv("PEXELS_API_KEY", "")
    def search(self, query: str, avoid: list[str]) -> Optional[dict]:
        if not self.key: return None
        try:
            r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": self.key}, params={"query": query, "per_page": 8, "orientation": "landscape"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            for video in data.get("videos", []):
                # Prefer a single stable file variant and exclude unsuitable low-quality candidates.
                files = sorted(video.get("video_files", []), key=lambda f: (abs((f.get("width") or 0)-1280), -(f.get("height") or 0)))
                for f in files:
                    link = f.get("link")
                    if link and (f.get("width") or 0) >= 640:
                        return {"url": link, "provider": "Pexels", "source_url": video.get("url", "https://www.pexels.com/license/"), "license": "Pexels License (review current terms before commercial deployment)", "license_url": "https://www.pexels.com/license/", "attribution": video.get("user", {}).get("name", "Pexels contributor"), "provider_id": str(video.get("id")), "query": query, "width": f.get("width"), "height": f.get("height")}
        except Exception as e:
            log.warning("Pexels provider failed; fallback will be used: %s", type(e).__name__)
        return None

class PixabayProvider(FootageProvider):
    name = "pixabay"
    def __init__(self):
        # PEXELS_API_KEY is accepted as a compatibility alias for users who put their
        # Pixabay key there before this provider was added. Prefer the correctly named variable.
        self.key = os.getenv("PIXABAY_API_KEY", "").strip() or os.getenv("PEXELS_API_KEY", "").strip()

    def search(self, query: str, avoid: list[str]) -> Optional[dict]:
        if not self.key: return None
        try:
            r = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": self.key, "q": query, "per_page": 10, "safesearch": "true", "video_type": "all"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            candidates = []
            query_terms = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 3}
            for video in data.get("hits", []):
                # Medium is generally a practical B-roll size; small is the lightweight fallback.
                variants = video.get("videos", {})
                variant = variants.get("medium") or variants.get("small") or variants.get("large") or variants.get("tiny")
                if not variant or not variant.get("url"):
                    continue
                tags = str(video.get("tags", ""))
                tags_lower = tags.lower()
                if any(term.strip().lower() in tags_lower for term in avoid if term.strip()):
                    continue
                tag_terms = set(re.findall(r"[a-z0-9]+", tags_lower))
                score = len(query_terms & tag_terms)
                candidates.append((score, video, variant))
            # Pixabay does not offer semantic relevance scoring. Rank its returned clips by
            # tag overlap so a broad/ambiguous result is less likely to win by list order.
            candidates.sort(key=lambda item: (item[0], (item[2].get("width") or 0)), reverse=True)
            if candidates:
                best_score, video, variant = candidates[0]
                best_tag_terms = set(re.findall(r"[a-z0-9]+", str(video.get("tags", "")).lower()))
                return {
                    "url": variant["url"], "provider": "Pixabay",
                    "source_url": video.get("pageURL", "https://pixabay.com/videos/"),
                    "license": "Pixabay Content License (review current terms before commercial deployment)",
                    "license_url": "https://pixabay.com/service/license-summary/",
                    "attribution": video.get("user", "Pixabay contributor"),
                    "provider_id": str(video.get("id")), "query": query,
                    "tags": str(video.get("tags", "")), "relevance_tag_overlap": best_score,
                    "width": variant.get("width"), "height": variant.get("height"),
                }
        except Exception as e:
            log.warning("Pixabay provider failed; fallback will be used: %s", type(e).__name__)
        return None

class MockProvider(FootageProvider):
    name = "procedural-demo"
    def search(self, query: str, avoid: list[str]) -> Optional[dict]:
        return {"url": None, "provider": self.name, "source_url": None, "license": "Project-generated procedural visual; no third-party footage", "license_url": None, "attribution": "Generated by this project", "provider_id": "generated-" + hashlib.sha1(query.encode()).hexdigest()[:10], "query": query, "generated": True}

def get_provider() -> FootageProvider:
    # Provider order is measurable and recorded. If stock fails, procedural fallback keeps the job alive.
    mode = os.getenv("FOOTAGE_PROVIDER", "auto").strip().lower()
    if mode == "mock": return MockProvider()
    if mode == "pixabay": return PixabayProvider()
    if mode == "pexels": return PexelsProvider()
    if os.getenv("PIXABAY_API_KEY", "").strip(): return PixabayProvider()
    return PexelsProvider()

def safe_download(url: str, dest: Path, max_bytes: int = 150_000_000):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("audio_url must be an http(s) URL")
    # Avoid common SSRF targets. Production deployments should also use network egress controls.
    if parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        raise ValueError("Local URLs are not allowed")
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = 0
        with dest.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                total += len(chunk)
                if total > max_bytes: raise ValueError("Downloaded file exceeds size limit")
                f.write(chunk)

def run_cmd(args: list[str], timeout: int = 600):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "Command failed")[-2500:])
    return p

def ffmpeg_available(): return shutil.which("ffmpeg") is not None

def duration_seconds(path: Path) -> float:
    p = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=30)
    return max(0.1, float(p.stdout.strip()))

def make_tts(text: str, path: Path, voice: str):
    # pyttsx3 is an offline, replaceable TTS adapter; voice availability depends on OS.
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", int(os.getenv("TTS_RATE", "165")))
        engine.save_to_file(text, str(path)); engine.runAndWait()
        if path.exists() and path.stat().st_size > 1000: return "pyttsx3"
    except Exception as e: log.warning("Offline TTS unavailable: %s", type(e).__name__)
    # Optional system fallback on Linux. The executable is provided by espeak-ng/espeak.
    for executable in ("espeak-ng", "espeak"):
        if shutil.which(executable):
            run_cmd([executable, "-v", voice if voice in {"en", "en-us", "en-gb", "ta", "hi"} else "en", "-w", str(path), text])
            return executable
    raise RuntimeError("No offline TTS engine found. Install pyttsx3 with an OS voice or espeak-ng.")

def transcribe_audio(path: Path) -> tuple[str, str]:
    # Optional adapter; install faster-whisper to enable real audio transcription.
    try:
        from faster_whisper import WhisperModel
        model_name = os.getenv("WHISPER_MODEL", "base")
        model = WhisperModel(model_name, device=os.getenv("WHISPER_DEVICE", "cpu"), compute_type="int8")
        segments, _ = model.transcribe(str(path), vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text: raise ValueError("No speech detected in the audio")
        return text, f"faster-whisper:{model_name}"
    except ImportError:
        raise RuntimeError("Audio input requires faster-whisper. Install the optional audio dependencies in README.md.")

def create_fallback_clip(path: Path, seconds: float, query: str, ratio: str):
    w, h = {"16:9": (1280,720), "9:16": (720,1280), "1:1": (1080,1080)}[ratio]
    # Procedural visual is a transparent demo fallback, not stock footage.
    label = re.sub(r"[^a-zA-Z0-9 ,.-]", "", query)[:70].replace(":", " ")
    color = "0x17324D" if "server" in query.lower() or "technology" in query.lower() else "0x285943"
    draw = f"drawtext=text='{label.replace(chr(39), '')}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.25:boxborderw=18"
    run_cmd(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r=30:d={seconds:.3f}", "-vf", draw, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-t", f"{seconds:.3f}", str(path)])

def download_video(clip: dict, dest: Path):
    safe_download(clip["url"], dest)

def srt_time(seconds: float) -> str:
    ms = int(round(max(0, seconds) * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def escape_drawtext(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")

def render_job(job_id: str, req: JobRequest, correlation_id: str):
    started = time.time(); work = OUT / job_id; work.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []; provider = get_provider(); audio_path = work / "narration.wav"; tts_engine = None
    try:
        update_job(job_id, status="running", progress=5)
        if not ffmpeg_available() or not shutil.which("ffprobe"):
            raise RuntimeError("FFmpeg and ffprobe are required. Install them and restart the service.")
        if req.audio_url or req.audio_local_path:
            ext = Path(urlparse(req.audio_url).path).suffix.lower() if req.audio_url else Path(req.audio_local_path).suffix.lower()
            if ext not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
                raise ValueError("Unsupported audio file extension; use mp3, wav, m4a, aac, ogg, or flac")
            source_audio = work / ("input" + ext)
            if req.audio_url: safe_download(req.audio_url, source_audio)
            else: shutil.copyfile(req.audio_local_path, source_audio)
            # Normalize audio into WAV for processing.
            run_cmd(["ffmpeg", "-y", "-i", str(source_audio), "-vn", "-ac", "2", "-ar", "44100", str(audio_path)])
            script, transcription_model = transcribe_audio(audio_path)
        else:
            scene_inputs = req.scenes or [SceneInput(text=s) for s in sentences(req.script or "")]
            if not scene_inputs: raise ValueError("Script is empty")
            script = " ".join(s.text for s in scene_inputs)
            transcription_model = "user-provided-script"
            tts_engine = make_tts(script, audio_path, req.voice)
        duration = duration_seconds(audio_path)
        if duration > 600: raise ValueError("Audio duration exceeds 10-minute limit")
        if duration < 0.5: raise ValueError("Audio is too short or silent")
        raw_scenes = req.scenes or [SceneInput(text=s) for s in sentences(script)]
        if not raw_scenes: raw_scenes = [SceneInput(text=script)]
        # Keep scene lengths practical and preserve phrase boundaries; narration determines all timestamps.
        words = [max(1, len(re.findall(r"\w+", s.text))) for s in raw_scenes]
        total_words = sum(words)
        lengths = [duration * n / total_words for n in words]
        # Merge tiny segments with neighbors by extending scene minimum where possible.
        if len(raw_scenes) > 1:
            for i, val in enumerate(lengths):
                if val < 2.0:
                    lengths[i] = 2.0
            scale = duration / sum(lengths); lengths = [x * scale for x in lengths]
        scenes = []; cursor = 0.0; used_ids = set(); media_cache = {}
        for idx, scene in enumerate(raw_scenes):
            end = duration if idx == len(raw_scenes)-1 else cursor + lengths[idx]
            scene_duration = max(0.1, end-cursor)
            if idx == len(raw_scenes)-1: end = duration
            if any(s.lower() in scene.text.lower() for s in SENSITIVE): warnings.append(f"Scene {idx+1}: sensitive topic detected; using neutral fallback imagery if stock is not clearly appropriate.")
            if BRANDS_OR_NAMES.search(scene.text): warnings.append(f"Scene {idx+1}: named person or brand mentioned; no face/logo-specific footage is generated.")
            if req.avoid: warnings.append(f"Scene {idx+1}: avoid rules applied to search queries: {', '.join(req.avoid)}")
            nearby_context = " ".join(s.text for s in raw_scenes[max(0, idx-1):min(len(raw_scenes), idx+2)] if s is not scene)
            queries = intent_queries(scene.text, scene.visual_hint, context=nearby_context)
            chosen = None; chosen_query = None
            for q in queries:
                candidate = provider.search(q, req.avoid)
                if candidate and candidate.get("provider_id") not in used_ids:
                    chosen, chosen_query = candidate, q; break
            if not chosen:
                chosen_query = f"{queries[0]} scene {idx+1}"
                chosen = MockProvider().search(chosen_query, req.avoid)
                warnings.append(f"Scene {idx+1}: stock source unavailable or no unique clip found; procedural demo fallback used.")
            used_ids.add(chosen["provider_id"])
            clip_path = work / f"scene_{idx+1}_source.mp4"
            if chosen.get("url"):
                try: download_video(chosen, clip_path)
                except Exception as e:
                    warnings.append(f"Scene {idx+1}: footage download failed ({type(e).__name__}); procedural fallback used.")
                    chosen = MockProvider().search(chosen_query, req.avoid); clip_path = work / f"scene_{idx+1}_source.mp4"
                    create_fallback_clip(clip_path, scene_duration, chosen_query, req.aspect_ratios[0])
            else:
                create_fallback_clip(clip_path, scene_duration, chosen_query, req.aspect_ratios[0])
            used_ids.add(chosen["provider_id"])
            scenes.append({"scene_id": f"scene-{idx+1:03}", "text": scene.text, "visual_hint": scene.visual_hint, "start": round(cursor, 3), "end": round(end, 3), "duration": round(scene_duration, 3), "clip_path": str(clip_path), "clip": {k:v for k,v in chosen.items() if k != "url"}, "match_query": chosen_query, "selection_reason": "Phrase-level concept routing, then first available unique clip; procedural fallback if provider fails.", "relevance_score": None, "relevance_score_note": "Not independently measured; human rating required for assessment evidence.", "ai_generated": False, "procedural_fallback": bool(chosen.get("generated")), "license_commercial_use": "provider licence recorded; verify current terms before publication" if chosen.get("provider") in {"Pexels", "Pixabay"} else "project-generated procedural graphic"})
            cursor = end
            update_job(job_id, progress=min(65, 15 + int(50*(idx+1)/len(raw_scenes))))
        # Create caption file and render each requested aspect ratio. Source clips are normalized to a fixed output canvas.
        srt_path = work / "captions.srt"
        if req.captions.srt:
            srt = []
            for i, s in enumerate(scenes, 1): srt.append(f"{i}\n{srt_time(s['start'])} --> {srt_time(s['end'])}\n{s['text']}\n")
            srt_path.write_text("\n".join(srt), encoding="utf-8")
        output_links = {}; render_started = time.time()
        for ri, ratio in enumerate(req.aspect_ratios):
            w,h = {"16:9":(1920,1080), "9:16":(1080,1920), "1:1":(1080,1080)}[ratio]
            concat_file = work / f"concat_{ratio.replace(':','x')}.txt"
            normalized = []
            for i, s in enumerate(scenes, 1):
                np = work / f"scene_{i}_{ratio.replace(':','x')}.mp4"
                vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,fps=30,format=yuv420p,setsar=1"
                run_cmd(["ffmpeg", "-y", "-stream_loop", "-1", "-i", s["clip_path"], "-t", f"{s['duration']:.3f}", "-vf", vf, "-an", "-c:v", "libx264", "-preset", os.getenv("FFMPEG_PRESET", "veryfast"), "-pix_fmt", "yuv420p", str(np)])
                normalized.append(np)
            concat_file.write_text("\n".join("file '" + str(p).replace("'", "'\\''") + "'" for p in normalized), encoding="utf-8")
            silent_video = work / f"silent_{ratio.replace(':','x')}.mp4"
            run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(silent_video)])
            out_file = work / f"video_{ratio.replace(':','x')}.mp4"
            # Burn captions only when requested; subtitles filter expects an escaped path.
            video_input = silent_video
            if req.captions.burn_in and req.captions.srt and srt_path.exists():
                captioned = work / f"captioned_{ratio.replace(':','x')}.mp4"
                subpath = str(srt_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
                run_cmd(["ffmpeg", "-y", "-i", str(silent_video), "-vf", f"subtitles='{subpath}':force_style='FontSize=20,Outline=2,Shadow=1,Alignment=2,MarginV=60'", "-c:v", "libx264", "-preset", os.getenv("FFMPEG_PRESET", "veryfast"), "-pix_fmt", "yuv420p", "-an", str(captioned)])
                video_input = captioned
            run_cmd(["ffmpeg", "-y", "-i", str(video_input), "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out_file)])
            output_links[ratio] = f"/outputs/{job_id}/{out_file.name}"
            update_job(job_id, progress=min(95, 70 + int(25*(ri+1)/len(req.aspect_ratios))))
        # Store relative clip paths, never local absolute paths in the public timeline.
        timeline = {"job_id": job_id, "narration": {"text": script, "duration_seconds": round(duration, 3), "input_type": "audio_url" if req.audio_url else ("audio_upload" if req.audio_local_path else "script_or_scenes"), "transcription_provider": transcription_model, "tts_provider": tts_engine}, "settings": {"aspect_ratios": req.aspect_ratios, "style": req.style, "avoid": req.avoid}, "scenes": []}
        for s in scenes:
            timeline["scenes"].append({k:v for k,v in s.items() if k != "clip_path"} | {"clip_file": Path(s["clip_path"]).name})
        (work / "timeline.json").write_text(json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8")
        elapsed = round(time.time()-started, 3)
        manifest = {"job_id": job_id, "created_at": datetime.now(timezone.utc).isoformat(), "models_and_providers": {"footage": provider.name, "tts": tts_engine, "transcription": transcription_model, "scene_router": "concept-map-v1"}, "versions": {"application": "0.1.0", "ffmpeg": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0] if shutil.which("ffmpeg") else "unavailable"}, "settings": {"aspect_ratios": req.aspect_ratios, "style": req.style, "avoid": req.avoid}, "seed": None, "timings_seconds": {"total": elapsed, "render": round(time.time()-render_started,3)}, "cost_estimate_usd": 0.0 if provider.name not in {"pexels", "pixabay"} else None, "cost_estimate_note": "Does not include compute, bandwidth, or any provider plan fees; measure on deployment.", "warnings": list(dict.fromkeys(warnings)), "rights": [{"scene_id":s["scene_id"],"source":s["clip"].get("source_url"),"license":s["clip"].get("license"),"license_url":s["clip"].get("license_url"),"attribution":s["clip"].get("attribution"),"ai_generated":s["ai_generated"],"procedural_fallback":s["procedural_fallback"]} for s in scenes], "correlation_id": correlation_id}
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        result = {"job_id": job_id, "status": "succeeded", "progress": 100, "videos": output_links, "captions": f"/outputs/{job_id}/captions.srt" if srt_path.exists() else None, "timeline": f"/outputs/{job_id}/timeline.json", "manifest": f"/outputs/{job_id}/manifest.json", "warnings": manifest["warnings"], "duration_seconds": round(duration,3), "elapsed_seconds": elapsed}
        update_job(job_id, status="succeeded", progress=100, result_json=json.dumps(result), error_json=None)
        log.info("job completed job_id=%s correlation_id=%s elapsed=%.3f", job_id, correlation_id, elapsed)
        if req.callback_url: send_callback(req.callback_url, result, job_id)
    except Exception as e:
        code = "PROCESSING_FAILED"; message = str(e)
        if isinstance(e, ValueError): code = "INVALID_INPUT"
        err = json_error(code, message, retryable=code != "INVALID_INPUT", details={"job_id":job_id})
        update_job(job_id, status="failed", progress=100, error_json=json.dumps(err))
        log.exception("job failed job_id=%s correlation_id=%s", job_id, correlation_id)
        (work / "failure.json").write_text(json.dumps(err, indent=2), encoding="utf-8")
        if req.callback_url: send_callback(req.callback_url, err, job_id)

def send_callback(url: str, payload: dict, job_id: str):
    secret = os.getenv("CALLBACK_HMAC_SECRET", "")
    if not secret: log.warning("Callback skipped: CALLBACK_HMAC_SECRET is not configured"); return
    import hmac
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try: requests.post(url, data=body, headers={"Content-Type":"application/json", "X-IncuBrix-Signature":signature}, timeout=10)
    except Exception as e: log.warning("Callback failed job_id=%s error=%s", job_id, type(e).__name__)

@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    response = await call_next(request); response.headers["X-Correlation-Id"] = cid
    return response

@app.get("/health")
def health(): return {"status":"ok"}

@app.get("/ready")
def ready():
    return {"status":"ready", "ffmpeg":ffmpeg_available(), "storage_writable":os.access(ROOT, os.W_OK), "footage_provider":os.getenv("FOOTAGE_PROVIDER","auto")}

@app.get("/", response_class=HTMLResponse)
def home():
    p = Path(__file__).resolve().parents[2] / "web" / "index.html"
    return p.read_text(encoding="utf-8") if p.exists() else "Script-to-B-roll API is running. Visit /docs."

@app.post("/api/v1/broll/jobs", status_code=202)
def create_job(req: JobRequest, background_tasks: BackgroundTasks, idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"), request: Request = None):
    if not idempotency_key: raise HTTPException(status_code=400, detail=json_error("IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header on POST requests"))
    if len(idempotency_key) > 200: raise HTTPException(status_code=400, detail=json_error("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key is too long"))
    with DB_LOCK:
        conn = db(); row = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row:
            conn.close()
            return {"job_id":row["job_id"], "status":row["status"], "progress":row["progress"], "idempotent_replay":True, "status_url":f"/api/v1/jobs/{row['job_id']}"}
        job_id = str(uuid.uuid4()); now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO jobs(job_id,status,progress,request_json,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id,"queued",0,req.model_dump_json(),idempotency_key,now,now)); conn.commit(); conn.close()
    cid = request.headers.get("X-Correlation-Id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    background_tasks.add_task(render_job, job_id, req, cid)
    return {"job_id":job_id, "status":"queued", "progress":0, "status_url":f"/api/v1/jobs/{job_id}"}

@app.post("/api/v1/broll/jobs/upload", status_code=202)
async def create_job_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    voice: str = Form("en"),
    aspect_ratios: str = Form("16:9"),
    style: str = Form("bright, modern, real-world footage"),
    avoid: str = Form(""),
    burn_in: bool = Form(True),
    srt: bool = Form(True),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    request: Request = None,
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail=json_error("IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header on POST requests"))
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        raise HTTPException(status_code=400, detail=json_error("UNSUPPORTED_AUDIO_TYPE", "Upload mp3, wav, m4a, aac, ogg, or flac"))
    # Upload to the app's controlled storage; enforce a hard limit while streaming.
    upload_path = UPLOADS / f"{uuid.uuid4()}{ext}"
    size = 0
    try:
        with upload_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > 150_000_000: raise HTTPException(status_code=413, detail=json_error("FILE_TOO_LARGE", "Audio upload exceeds 150 MB"))
                out.write(chunk)
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    ratios = [x.strip() for x in aspect_ratios.split(",") if x.strip()]
    try:
        req = JobRequest(audio_local_path=str(upload_path), voice=voice, aspect_ratios=ratios, style=style, avoid=[x.strip() for x in avoid.split(",") if x.strip()], captions=Captions(burn_in=burn_in, srt=srt))
    except Exception as e:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=json_error("INVALID_INPUT", str(e)))
    with DB_LOCK:
        conn = db(); row = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row:
            conn.close(); upload_path.unlink(missing_ok=True)
            return {"job_id":row["job_id"], "status":row["status"], "progress":row["progress"], "idempotent_replay":True, "status_url":f"/api/v1/jobs/{row['job_id']}"}
        job_id = str(uuid.uuid4()); now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO jobs(job_id,status,progress,request_json,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id,"queued",0,json.dumps({"audio_upload":file.filename,"aspect_ratios":ratios}),idempotency_key,now,now)); conn.commit(); conn.close()
    cid = request.headers.get("X-Correlation-Id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    background_tasks.add_task(render_job, job_id, req, cid)
    return {"job_id":job_id, "status":"queued", "progress":0, "status_url":f"/api/v1/jobs/{job_id}"}


@app.get("/api/v1/jobs/{job_id}")
def job_status(job_id: str):
    row = get_job(job_id)
    if not row: raise HTTPException(status_code=404, detail=json_error("JOB_NOT_FOUND", "No job exists with this id"))
    data = {"job_id":row["job_id"], "status":row["status"], "progress":row["progress"], "created_at":row["created_at"], "updated_at":row["updated_at"]}
    if row["result_json"]: data["result"] = json.loads(row["result_json"])
    if row["error_json"]: data["error"] = json.loads(row["error_json"])["error"]
    return data

@app.get("/api/v1/jobs")
def list_jobs(limit: int = 20):
    limit = min(max(limit,1),100)
    with DB_LOCK:
        conn = db(); rows = conn.execute("SELECT job_id,status,progress,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall(); conn.close()
    return {"jobs":[dict(r) for r in rows]}

@app.get("/outputs/{job_id}/{filename}")
def output_file(job_id: str, filename: str):
    if Path(filename).name != filename: raise HTTPException(status_code=400, detail=json_error("INVALID_FILENAME", "Invalid output filename"))
    path = OUT / job_id / filename
    if not path.is_file(): raise HTTPException(status_code=404, detail=json_error("FILE_NOT_FOUND", "Output file not found"))
    return FileResponse(path)

@app.get("/api/v1/jobs/{job_id}/download/{filename}")
def download_output(job_id: str, filename: str): return output_file(job_id, filename)

# Serve generated files at /outputs/<job_id>/<filename>.
