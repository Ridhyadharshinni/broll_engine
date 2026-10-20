# Script-to-B-roll Video Engine

A practical starter implementation for Assessment 3. It converts a script into narration and a scene-based video, writes captions, and records source/licence metadata in a timeline and manifest.

> **Honesty note:** This is a runnable prototype/starter, not a verified full-pass submission. In mock mode it creates labelled procedural visuals rather than real B-roll. Review `REPORT.md` for gaps that must be closed before claiming assessment compliance.

## Features

- `POST /api/v1/broll/jobs` accepts a script or scene list; also accepts an audio URL when optional Faster-Whisper is installed.
- Background job lifecycle (`queued`, `running`, `succeeded`, `failed`) and progress endpoint.
- `Idempotency-Key` required on POST; same key returns the original job.
- Phrase-level concept routing for ambiguous concepts such as cloud computing, budget, trust, health, education, climate, and travel.
- Optional Pixabay and Pexels stock-video providers with transparent procedural fallback when unavailable.
- FFmpeg normalization, 30 fps H.264 video, AAC narration, aspect ratios 16:9 / 9:16 / 1:1, SRT and optional burned-in captions.
- `timeline.json`, `manifest.json`, warning records, health/readiness, batch runner, tests, and OpenAPI 3.1 document.
- Mock/default path runs without stock API keys.

## Requirements

- Docker Desktop (recommended) **or** Python 3.11+, FFmpeg and ffprobe.
- For local script-to-speech: pyttsx3 with a working OS voice, or espeak-ng installed.
- For audio input: optional `faster-whisper` and its compatible model assets.
- For real stock footage: a Pixabay or Pexels API key and acceptance of the provider's current API/licence terms.

## Run with Docker

1. Extract the ZIP.
2. Copy `.env.example` to `.env` (optional; defaults to mock mode).
3. From the project folder, run:

```bash
docker compose up --build
```

4. Open `http://localhost:8000` for the web UI or `http://localhost:8000/docs` for Swagger UI.
5. Readiness: `http://localhost:8000/ready`.

To enable Pixabay stock footage, set `FOOTAGE_PROVIDER=pixabay` and `PIXABAY_API_KEY=your_key` in `.env`, then restart. For convenience, `FOOTAGE_PROVIDER=pixabay` also reads the key from `PEXELS_API_KEY` if that is where you already placed your Pixabay key. Prefer renaming it to `PIXABAY_API_KEY` when convenient. To use Pexels explicitly, set `FOOTAGE_PROVIDER=pexels` and `PEXELS_API_KEY=your_key`. Never commit `.env` or API keys.

## Run locally on Windows

Install FFmpeg and ensure both `ffmpeg` and `ffprobe` are on PATH. In PowerShell or CMD:

```powershell
cd backend
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
$env:FOOTAGE_PROVIDER="mock"
uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000`. If pyttsx3 cannot access a voice, install/configure an OS TTS voice or use the Docker image's espeak-ng fallback.

## API quick start

```bash
curl -X POST http://localhost:8000/api/v1/broll/jobs ^
  -H "Content-Type: application/json" ^
  -H "Idempotency-Key: demo-001" ^
  -d @examples/sample_request.json
```

The response returns a `job_id`. Poll it:

```bash
curl http://localhost:8000/api/v1/jobs/YOUR_JOB_ID
```

The JSON request accepts `script`, `scenes` (`[{"text":"...", "visual_hint":"..."}]`) or `audio_url`. For an audio upload, use `POST /api/v1/broll/jobs/upload` with multipart field `file`; aspect ratios are `16:9`, `9:16`, and `1:1`. Only one of audio_url and script/scenes should be provided. Use a unique Idempotency-Key per intended job.

## Test and batch runner

Local test:

```bash
cd backend
pip install -r requirements.txt
pytest -q
```

Batch runner (from project root; server must be running):

```bash
python scripts/batch_runner.py examples/sample_request.json --output test-results
```

It saves request, submission response, job status, timeline, manifest, captions, video files and a summary under `test-results/`.

## Output files

- `video_16x9.mp4`, `video_9x16.mp4`, and/or `video_1x1.mp4`
- `captions.srt`
- `timeline.json` — scene text/timing, clip source, query, licence and selection reason
- `manifest.json` — providers, settings, runtime, estimated cost field, rights records and warnings

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `FOOTAGE_PROVIDER` | `mock`, `pixabay`, `pexels`, or `auto` | `mock` in Compose |
| `PIXABAY_API_KEY` | Optional Pixabay video API key | empty |
| `PEXELS_API_KEY` | Optional Pexels key; also accepted as a compatibility alias in `pixabay` mode | empty |
| `CALLBACK_HMAC_SECRET` | HMAC-SHA256 secret for callbacks | empty (callbacks skipped) |
| `BROLL_DATA_DIR` | Persistent job/output directory | `./data` |
| `FFMPEG_PRESET` | FFmpeg encoding speed/quality preset | `veryfast` |
| `TTS_RATE` | pyttsx3 speech rate | `165` |
| `WHISPER_MODEL` | Optional Faster-Whisper model name | `base` |
| `WHISPER_DEVICE` | Optional transcription device | `cpu` |

## Architecture

```text
Browser / API client
       |
       v
FastAPI -> SQLite jobs + idempotency
       |
       v
Background processor
  |-- TTS or audio download/transcription adapter
  |-- sentence/scene segmentation
  |-- concept-based footage routing
  |-- Pixabay / Pexels adapter -> procedural fallback
  |-- FFmpeg normalize / concat / caption / mux
       |
       +--> videos + captions.srt + timeline.json + manifest.json
```

## Rights and limitations

Read `SOURCES.md`, `AI_USE.md`, and `REPORT.md`. The software records licence details but does not guarantee that every clip is legally cleared. Check current provider terms, contributor rights, dependency licences, and media licences before commercial publication. Procedural fallback clips are demo graphics, not real footage. Do not submit false claims about measured relevance, commercial rights, or full assessment completion.
