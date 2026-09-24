# AfriHealth AI Architecture

## 1. System purpose

AfriHealth AI is a browser-based clinical voice documentation and
decision-support prototype for community health workers and clinicians. The
current release focuses on Amharic-English code-switching. Afaan Oromoo,
Tigrinya, and other language pairs are future expansion targets.
The system produces draft transcripts and clinician-review artifacts; it is
not an autonomous diagnostic or prescribing service.

## 2. Runtime components

```text
Browser (index.html)
  |-- microphone PCM capture and local WAV review
  |-- WebSocket client for live STT
  |-- REST client for saved-recording STT upload
  |-- triage, EHR intake, follow-up, benchmark, and audit views
  |
  +--> FastAPI service (main.py :8000)
          |-- server-side Intron API-key boundary
          |-- streaming STT WebSocket proxy
          |-- follow-up TTS generation bridge
          |-- synchronous STT upload bridge
          |-- server-side FHIR export and optional EHR commit
          |-- synchronous TTS generation and status bridges
          |-- clinical artifact and medication safety gate
          |-- edge persistence adapter (Cloudflare D1 or local SQLite fallback)
          |
          +--> Intron Sahara STT/TTS APIs
          +--> Configured FHIR/EHR endpoint (optional)

Optional local static server (server.js or npm start :3000)
  |-- serves the browser files
  +-- optional legacy Intron proxy route
```

The browser must not receive or store the Intron API key. Production
deployments should expose only the FastAPI service through an allowlisted
origin and configure `ALLOWED_ORIGINS`.

## 3. Main request flows

### Live transcription

1. The browser requests microphone access and captures audio.
2. Audio bytes and control messages are sent to `/ws/stream`.
3. FastAPI adds the server-side Authorization header and connects to Intron.
4. Partial and final transcript messages are returned to the browser.
5. Final text populates the transcript, entity, triage, and safety-review
   panels.

### Saved recording review and upload

1. The browser captures PCM and encodes a local WAV blob.
2. The user reviews playback and may download the local copy.
3. Upload occurs only after the explicit upload action.
4. FastAPI forwards the multipart file to Intron's synchronous STT endpoint.
5. The browser displays the returned transcript and clinician-review triage
   output.

Synchronous uploads are limited by the provider's documented 120-second
maximum. The controlled clinical uploader uses one reviewed recording per
case and keeps raw audio and provider response data outside Git.

### Clinical artifact generation

`/api/v1/clinical/process-text` creates draft SOAP, ICD-10, symptom, and
medication artifacts. Medication candidates remain blocked unless the request
contains explicit clinician confirmation and patient context. Possible viral
features and penicillin-family allergy conflicts produce alerts and block
amoxicillin suggestions.

## 4. Evidence and evaluation layers

The repository contains separate evidence types:

| Evidence | Purpose | Interpretation |
| --- | --- | --- |
| `benchmark_suite.py` fixture | Reproducible UI/scoring demonstration | Not a population performance claim |
| AfriSwitch pilot scripts | General code-switched ASR import/inference | Not clinical validation |
| `clinical_validation_report.json` | Aggregate result from 15 reviewed simulated clinical recordings | Benchmark and error-analysis baseline; not real-patient evidence |
| Review protocols and safety documentation | Human review, safety scenarios, SOAP scoring | Maintained outside the public demo flow |

Do not combine fixture metrics, AfriSwitch results, and the clinical
validation baseline into one model ranking.

`clinical_validation_evaluator.py` scores provider results using the same
simulated reference cases and emits only aggregate metrics plus critical-term
miss counts. The recordings may be reused for benchmarking, error analysis,
and terminology improvement, but the evaluator does not train a model or
support autonomous clinical decisions.

### Durable stream sync

Each live stream receives a session ID and clinic ID. Partial and committed
transcripts are stored as metadata in Cloudflare D1 when
`CLOUDFLARE_D1_API_URL` and `CLOUDFLARE_D1_API_TOKEN` are configured; local
development falls back to `EDGE_SQLITE_PATH`. Raw audio is never stored by
this layer. The schema is created automatically at service startup.

### Live benchmark comparison

Module 4 keeps the fixture matrix separate from live measurements. A reviewed
audio sample and verified Amharic-English reference transcript can be sent to
`/api/v1/benchmark/live`, which compares configured Intron, OpenAI, and Gemini
transcription providers and returns provider status, latency, WER, and CER.
Missing provider keys are reported as unavailable; no scores are fabricated.

The separate clinical validation audit form is intentionally not exposed in
the public application navigation. The product demo focuses on the three care
modules and benchmark matrix; review protocols remain available for controlled
evaluation and future clinical governance.

## 5. Security and privacy boundaries

- Keep `INTRON_API_KEY` in a server environment variable.
- Never place credentials in `index.html`, commits, screenshots, or demo
  recordings.
- Keep raw recordings, consent forms, identity mappings, full transcripts, and
  provider file IDs outside the public repository.
- Use de-identified or simulated cases for public demonstrations.
- Configure exact production CORS origins rather than broad wildcards.
- Treat generated transcripts, entities, codes, triage labels, and medication
  candidates as clinician-review drafts.

## 6. Known limitations

- The current frontend uses a lightweight browser capture path and requires a
  compatible microphone/browser for live recording.
- Oromo handling depends on provider support; the bundled Whisper checkpoints
  do not accept forced `om` language decoding.
- Clinical validation currently reports a baseline, not a safety or efficacy
  claim.
- No demo video is included in this repository. The written
  [DEMO_SCRIPT.md](./DEMO_SCRIPT.md) is provided for a future recording or
  live judging session.
