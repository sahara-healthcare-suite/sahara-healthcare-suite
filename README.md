# Sahara Healthcare Suite (AfriHealth AI)

> **Real-Time Clinical Voice Intake & Multilingual Code-Switched Medical Documentation**

[![Live Application](https://img.shields.io/badge/Live-Application-blue?style=for-the-badge)](https://sahara-healthcare-suite.pages.dev/)
[![YouTube Walkthrough](https://img.shields.io/badge/Video-Walkthrough-red?style=for-the-badge)](https://youtu.be/47ldOyJxyPE)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## Project Authors, Leadership & License

* **Ermias Amare** — Project Management & Architecture Lead (`ermiasamare1713@gmail.com`)
* **Fasil Bazazew** — Software Engineer (Edge & UI/UX)
* **Melaku Bayu** — AI & ML Researcher (ASR Telemetry & Benchmarking)
* **Dr. Hiwot Shewangizaw** — Clinical Advisor & Validation Lead
* **Nurse Rahel Tamru** — Clinical Advisor & Validation Lead
* **License:** Distributed under the MIT License. See LICENSE for more information.

---

## 1. Executive Summary & Project Positioning

The **Sahara Healthcare Suite (AfriHealth AI)** addresses critical clinical documentation bottlenecks and clinician burnout across African health systems through real-time, code-switched speech recognition (Amharic, Swahili, and Yoruba mixed with English medical terminology) and automated SOAP note generation. 

Deployed globally on Cloudflare Pages across 300+ edge nodes, the platform achieves sub-second latency (62 ms TTFB in Nairobi) while maintaining strict adherence to data privacy, clinical safety guardrails, and WCAG 2.1 Level AA accessibility.

---

## 2. Empirical Benchmark Summary

Evaluated across **15.5 hours** of consented, de-identified clinical encounter snippets ($N = 480$) gathered from field workers and outpatient consultations in Kenya, Ethiopia, and Nigeria under clinical supervision.

| Speech Model | Swahili-English WER (%) | Amharic-English WER (%) | Yoruba-English WER (%) | ICD-10 F1 Score (↑) | End-to-End Latency |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Whisper Large-v3** | 28.4% | 38.6% | 32.1% | 0.68 | 2,840 ms |
| **SeamlessM4T v2** | 24.1% | 34.2% | 29.5% | 0.74 | 2,150 ms |
| **Sahara Speech API v2** | **11.2%** | **14.8%** | **12.6%** | **0.94** | **420 ms** |

* **Edge Latency Highlight:** Nairobi Node TTFB = **62 ms** (80.0% reduction compared to standard monolith cloud VMs).
* **Lighthouse Scores:** Performance 98 | Accessibility 100 | Best Practices 100 | SEO 100.

---

## 3. Technical Architecture & Tech Stack

* **Frontend:** Static browser dashboard configured for real-time audio capture, patient ID intake, and code-switching model selection.
* **Backend:** FastAPI asynchronous server handling live STT (`/ws/stream`), reviewed audio upload (`/api/intron/stt/upload-sync`), clinical processing, post-care workflows, and server-side FHIR export (`/api/v1/fhir/export`).
* **Speech Engine Core:** Intron Sahara v2.5 API optimized for low-latency African phonemes and medical nomenclature retention.
* **Interoperability:** Generates structured HL7 FHIR patient documents and ICD-10 diagnostic codes; outbound EMR submission is enabled only when a FHIR endpoint is configured.

---

## 4. Key API Endpoints

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/ws/stream` | WebSocket | Real-time bi-directional audio chunk streaming and transcription. |
| `/api/intron/stt/upload-sync` | POST | Reviewed multipart audio upload to Intron Sahara. |
| `/api/v1/post-care/analyze` | POST | Human-confirmed recovery and maternal decision support. |
| `/api/v1/fhir/export` | POST | Server-side HL7 FHIR Bundle generation. |
| `/api/v1/ehr/commit` | POST | Optional configured FHIR/EHR submission; returns 503 when unconfigured. |

---

## 5. Responsible AI & Data Sovereignty

* **Assistive, Never Autonomous:** Every generated SOAP note and e-prescription carries an uneditable warning badge: *"AI-Drafted Document. Requires Physician Verification Before EMR Commit."*
* **Privacy & Encryption:** All audio ingestion adheres to formal institutional review protocols with written patient consent. PII scrubbing is enforced via automated anonymization pipelines combined with dual-reviewer auditing.
* **Secure Transport:** Enforces end-to-end TLS 1.3 encryption with instant memory purging upon completion—no raw patient audio is permanently stored on public cloud servers.

---

## 6. Local Installation & Quickstart

To run the repository locally for development or auditing:

```bash
# 1. Clone the repository
git clone [https://github.com/your-username/sahara-healthcare-suite.git](https://github.com/your-username/sahara-healthcare-suite.git)
cd sahara-healthcare-suite

# 2. Configure environment variables
cp .env.example .env
# Add your INTRON_API_KEY inside the .env file

# 3. Install dependencies and start the API
python -m pip install -r requirements.txt
export INTRON_API_KEY="your-key"
python -m uvicorn main:app --host 127.0.0.1 --port 8000

# 4. In another terminal, serve the static frontend
npm install
npm start
