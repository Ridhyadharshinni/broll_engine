# Script-to-B-roll Video Engine — Prototype Report

## 1. Problem and solution

The service accepts a script, an IncuBrix-style list of scenes, or an audio URL and returns an asynchronously generated video for each requested aspect ratio. Scene boundaries follow sentence/scene boundaries, and timing is derived from narration duration and relative word counts. The output includes SRT captions, a timeline JSON, a rights manifest, and warnings.

## 2. Architecture

- **FastAPI** provides job submission, job status, health/readiness, and output-download endpoints.
- **SQLite** stores job state and idempotency keys.
- **Background task** processes jobs and updates progress.
- **TTS/transcription adapters** isolate local TTS and optional Faster-Whisper transcription.
- **Footage provider adapter** tries Pexels when configured, then falls back to project-generated procedural demo visuals.
- **FFmpeg** normalizes resolution, frame rate, pixel format, combines scenes, burns captions, and muxes narration.
- **Timeline/manifest** records scene timing, source, licence, attribution, selection rationale, warnings, and timing/cost fields.

## 3. Why this approach

A concept map handles common ambiguous phrases such as “cloud computing” (servers/data centres rather than sky footage) and “trust” (collaboration/partnership rather than a literal word search). It is intentionally transparent and replaceable. It is not an embedding model, and its matching quality must be measured with a human-rated test pack. The procedural fallback makes no-key testing reliable, but is visibly labelled and is not real B-roll.

## 4. Run and test

See README.md. Run the test suite with `docker compose run --rm broll-api pytest -q /app/backend/tests` after the image is built, or install the Python requirements and run `pytest -q backend/tests` locally. Run the batch runner against sample JSON inputs to collect job logs and outputs.

## 5. Current limitations (do not claim these as complete)

- Audio URL transcription requires the optional `faster-whisper` package and a compatible model; this is not bundled by default.
- Script-to-audio depends on an installed local TTS voice; voice/language availability differs by host.
- Phrase matching is a transparent concept map, not a learned semantic model. Independent relevance scores are not implemented; scene scores remain null until reviewed.
- The stock provider uses Pexels search; current commercial rights must be verified per clip. The prototype records source and licence links but cannot certify legal clearance automatically.
- The first render implementation uses one selected output ratio when creating procedural fallback clips; each final aspect ratio is normalized correctly, but fallback visuals should be regenerated per ratio for best composition.
- Background tasks run inside the API process. For production, use a durable worker queue (e.g., Celery/RQ) and object storage (e.g., MinIO/S3).
- URL fetching has basic checks only; production requires robust SSRF/IP-range blocking, content sniffing, rate limiting, authentication, and quotas.
- Partial scene re-render, two-source measured selection, visual-language relevance scoring, concurrency metrics, and 10-input human evaluation are not complete.
- Metrics in manifest are measured runtime estimates; the cost field does not include all compute/bandwidth/provider plan costs.

## 6. Validation plan

Create at least 10 varied test inputs (tutorial, statistics, abstract statements, product promo, sensitive news, names/brands, ambiguous words, silence/music-only, 5-minute audio, and provider failure). Record every output including failures. Human-rate every scene 1–5, calculate average relevance, coverage (share of narration time with rating >=3), repeat-clip rate, and render success rate. Do not claim Core compliance until all must-haves and the >=3.5 average relevance criterion have evidence.

## 7. Cost and turnaround

The manifest records wall-clock processing time. No measured performance or production cost is asserted here because it must be collected on the target machine and configured provider. Run multiple cold and warm 60-second jobs and record cost per finished minute before submitting.
