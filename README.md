# Afrihealth AI

> Sahara Healthcare Suite

A clinician-reviewed Amharic-English code-switched clinical documentation platform designed to reduce clinician workload, accelerate patient intake, and improve documentation quality in African healthcare environments.

[![Live Application](https://img.shields.io/badge/Live-Application-blue?style=for-the-badge)](https://sahara-healthcare-suite.pages.dev/)
[![GitHub Repository](https://img.shields.io/badge/GitHub-Repository-black?style=for-the-badge)](https://github.com/sahara-healthcare-suite/sahara-healthcare-suite)
[![YouTube Walkthrough](https://img.shields.io/badge/Video-Walkthrough-red?style=for-the-badge)](https://youtu.be/47ldOyJxyPE?si=B4X5WVlztZzwGktZ)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Lead Author:** Ermias Amare (`ermiasamare1713@gmail.com`)

**Live Production Application:** https://sahara-healthcare-suite.pages.dev/

**GitHub Repository:** https://github.com/sahara-healthcare-suite/sahara-healthcare-suite

**Video Walkthrough:** https://youtu.be/47ldOyJxyPE?si=B4X5WVlztZzwGktZ

**Deployment Infrastructure:** Cloudflare Pages (Serverless / Global Edge Network)

---

## Overview

Sahara Healthcare Suite is a clinician-facing documentation platform focused on Amharic-English code-switched clinical conversations. It captures patient audio, transcribes clinical speech in context, and supports downstream review workflows for editable SOAP notes, triage, coding, benchmarking, and FHIR-compatible exports. Other language pairs are future expansion targets.

The system is built around a static frontend and a secure FastAPI gateway that keeps sensitive credentials, such as the Intron API key, out of the browser while enabling real-time clinical processing at edge scale.

---

## Why it matters

- Reduces documentation burden for clinicians working in high-volume care settings
- Supports Amharic-English code-switched conversations in the current release
- Improves time-to-note generation for patient intake, follow-up, and triage workflows
- Creates a safer, review-first documentation pipeline for clinical AI assistance
- Enables interoperability with FHIR-ready export structures for downstream health systems

---

## Key capabilities

- Real-time audio capture and playback in the browser
- Amharic-English code-switched speech support for clinical settings
- Secure backend transcription proxy with server-side API key handling
- Human-in-the-loop clinical review workflow for transcripts and structured outputs
- Editable SOAP encounter panel with clinician sign-off and FHIR Composition output
- FHIR-ready export generation for interoperability
- Optional EHR submission when configured by deployment
- Intron-focused live audio benchmark with transcript-only or reference-scored WER/CER
- Local SQLite fallback or Cloudflare D1 persistence for stream metadata
- Safety-oriented design with physician verification requirements

---

## Evidence and benchmark status

The repository separates fixture demonstrations from measured validation:

- The Module 4 fixture matrix uses embedded hypotheses and is not an audio benchmark or production model ranking.
- The live benchmark accepts one reviewed Amharic-English sample and reports Intron Sahara v2.5 latency, transcript, WER, and CER.
- WER/CER require a verified reference transcript. A provisional Intron-reference mode is available for model-to-model comparison, but it is not independent gold-standard accuracy.
- The clinical validation report covers 15 reviewed simulated cases and reports a 56.38% mean WER, 44.33% target-term recall, and six cases with critical-term misses. These results require clinician review and do not support autonomous care.

---

## Architecture

This repository is structured as a hybrid web application:

- Frontend: static browser interface served from the repository root
- Backend: FastAPI service in `main.py`
- Deployment model: frontend on Cloudflare Pages, API on a secure HTTPS host such as Railway or another server runtime

Typical flow:

1. Clinician records or uploads patient audio in the browser.
2. The frontend sends the request to the API gateway.
3. The backend authenticates to the Intron API using `INTRON_API_KEY`.
4. The system returns transcript, clinical analysis, and optional structured outputs.
5. Module 2 exposes an editable SOAP draft and requires clinician sign-off before FHIR export or EHR commit.
6. Module 4 can send a reviewed sample to `/api/v1/benchmark/live` for Intron-focused scoring.

---

## Tech stack

- Python 3.x
- FastAPI
- JavaScript / static frontend assets
- Intron AI transcription service
- FHIR-compatible export workflow
- Cloudflare Pages-friendly static deployment

---

## Repository structure

```text
.
├── main.py                  # FastAPI application and API routes
├── index.html               # Browser frontend entry point
├── server.js                # Optional local static server behavior
├── package.json             # Frontend run script
├── requirements.txt         # Python dependencies
├── .env.example             # Example environment configuration
├── config.py                # Configuration helpers
├── benchmark_suite.py       # Fixture benchmark and model comparison utilities
├── edge_persistence.py      # SQLite/D1 stream metadata persistence adapter
├── clinical_validation_*    # Validation and reporting scripts
├── tests/                   # Safety and validation tests
├── README.md                # Project documentation
├── DEPLOYMENT.md            # Deployment guidance
├── ARCHITECTURE.md          # Technical design notes
├── LICENSE                  # MIT license
└── ...
```

---

## Prerequisites

Before running locally, ensure the following are installed:

- Python 3.10+
- pip
- Node.js and npm
- An Intron API key

---

## Local development setup

### 1. Clone the repository

```bash
git clone <repository-url>
cd sahara-healthcare-suite
```

### 2. Configure environment variables

Copy the sample environment file and add your API credentials:

```bash
cp .env.example .env
```

Example variables in `.env.example` include:

```env
INTRON_API_KEY=
INTRON_TTS_VOICE_LANGUAGE=am
INTRON_TTS_VOICE_ACCENT=amharic
INTRON_TTS_VOICE_GENDER=female
ALLOWED_ORIGINS=https://your-project.pages.dev
REQUIRE_PROXY_AUTH=false
EHR_FHIR_ENDPOINT=
EHR_API_KEY=
EDGE_SQLITE_PATH=edge_sync.sqlite3
CLOUDFLARE_D1_API_URL=
CLOUDFLARE_D1_API_TOKEN=
OPENAI_API_KEY=
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
GEMINI_API_KEY=
GEMINI_TRANSCRIBE_MODEL=gemini-2.0-flash
```

The FastAPI app loads `.env` automatically for local development. OpenAI and
Gemini are optional comparison providers. Keep all provider keys on the
backend; never add them to `index.html`.

### 3. Install Python dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Start the FastAPI backend

```bash
export INTRON_API_KEY="your-key"
export ALLOWED_ORIGINS="http://localhost:3000"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### 5. Serve the frontend locally

In a second terminal:

```bash
npm install
npm start
```

This uses the static server configuration defined in `package.json` and serves the app from the repository root.

---

## Important security and privacy notes

- Never expose `INTRON_API_KEY` in the browser or source code.
- Keep patient recordings and clinical transcripts protected.
- Use HTTPS and WSS in production.
- Configure CORS restrictions carefully for deployment.
- Do not commit `.env` files or sensitive deployment values.
- Treat generated notes as assistive material that requires clinician verification before clinical action.

---

## Clinical safety principles

This project is designed as a decision-support system, not an autonomous clinical authority. Generated notes, diagnoses, and treatment suggestions should be reviewed by a licensed clinician before being used in patient care or transmitted to an EMR.

The application includes handling for clinician review gating and requires explicit authorization for downstream EHR commit workflows.

---

## Deployment guidance

The project is intended for a split deployment model:

- Static frontend hosted on Cloudflare Pages or similar static hosting
- API service hosted on a secure backend runtime with TLS enabled

For full deployment guidance, see [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Validation and benchmarking

Run the focused regression suite with:

```bash
python -m unittest tests.test_safety tests.test_edge_persistence -v
python -m py_compile main.py edge_persistence.py
```

The live benchmark supports two reference modes:

1. `verified`: paste an approved reference transcript to calculate independent WER/CER.
2. `intron`: use the Intron transcript as a provisional reference for comparing another configured provider.

Both modes are evaluation aids, not standalone clinical validation. Use
simulated or de-identified recordings and require clinician review.

---

## License

This project is distributed under the MIT License. See [LICENSE](LICENSE) for details.

---

## Project contributors

- Ermias Amare — Project Management & Architecture Lead
- Fasil Bazazew — Software Engineer
- Melaku Bayu — AI & ML Researcher
- Dr. Hiwot Shewangizaw — Clinical Advisor & Validation Lead
- Nurse Rahel Tamru — Clinical Advisor & Validation Lead

---

## Support and maintenance

For production deployment and operational guidance, refer to the supporting documentation in this repository, especially [DEPLOYMENT.md](DEPLOYMENT.md) and [ARCHITECTURE.md](ARCHITECTURE.md).
