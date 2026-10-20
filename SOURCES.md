# Sources, dependencies, and rights register

This is a prototype inventory, not legal advice. Re-check the current licence and terms of every dependency, model, clip, and generated output before commercial deployment.

| Component | Use | Source / licence notes |
|---|---|---|
| Python 3.11 | Runtime | PSF licence |
| FastAPI | HTTP API | MIT licence |
| Pydantic | Request validation | MIT licence |
| Requests | HTTP client | Apache-2.0 licence |
| FFmpeg | Audio/video processing | LGPL/GPL build variants exist; use and verify a compatible build for your distribution model |
| pyttsx3 / OS TTS voice | Offline narration for script input | pyttsx3 is MPL-2.0; voice engine and voice assets vary by OS; verify separately |
| espeak-ng | Optional offline TTS fallback in container | GPL-3.0; **this default container dependency needs legal review against the assessment's no-AGPL rule and distribution requirements**. Replace with a commercially approved TTS adapter if required. |
| Pexels API (optional) | Stock footage source | https://www.pexels.com/license/ and https://www.pexels.com/api/ ; record each source URL and re-check current terms. API access/limits may vary. |
| Procedural demo visuals | No-key fallback for testing | Created by this project with FFmpeg; clearly labelled in timeline/manifest. Not real B-roll and not a substitute for stock footage relevance review. |
| Optional faster-whisper | Audio transcription adapter | https://github.com/SYSTRAN/faster-whisper ; review the exact code/model licences and commercial terms before enabling. Not installed by default. |

The mock mode uses no third-party footage or paid API. The stock mode is only commercially safe if the specific clip and the provider's current terms permit the intended use. This project records licence metadata but does not provide a legal guarantee.
