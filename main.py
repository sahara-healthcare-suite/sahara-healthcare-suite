# """
# Sahara Healthcare Suite (AfriHealth AI) - FastAPI Backend Gateway
# Includes:
# - Intron v2.5 speech-to-text gateway
# - Provider response parsing based on the documented File Upload Sync API
# - Payload size validation and rate limiting
# - CORS configuration for static frontends & Cloudflare Pages
# """

# import os
# import json
# import time
# import logging
# import tempfile
# from collections import deque
# from typing import List, Optional
# from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse, StreamingResponse
# from starlette.middleware.base import BaseHTTPMiddleware
# import httpx
# from pydantic import BaseModel
# from fastapi import WebSocket, WebSocketDisconnect
# import asyncio
# import base64
# import websockets

# # Benchmark-only dependencies (Sahara vs. Whisper WER/CER/triage comparison).
# # These are heavier ML deps than the rest of the gateway needs, so they're
# # imported lazily inside the benchmark helpers below rather than at module
# # load time -- keeps `/health` and normal transcription fast to boot even on
# # an instance that doesn't have torch/transformers installed.

# # Initialize logger
# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger("afrihealth_gateway")

# app = FastAPI(
#     title="AfriHealth AI Gateway",
#     description="Secure proxy for Intron v2.5 ASR",
#     version="2.5.0"
# )

# # ---------------------------------------------------------------------------
# # Rate limiting
# # The module docstring has always claimed "rate limiting" as a feature, but
# # no limiter was actually wired up. This adds a lightweight in-memory
# # sliding-window limiter (per client IP) with no extra dependency, applied
# # to the expensive ASR-proxy routes. It's process-local, which is fine for a
# # single Railway/Cloudflare-Pages-fronted instance; swap for a Redis-backed
# # limiter if this ever runs behind multiple worker processes.
# # ---------------------------------------------------------------------------
# RATE_LIMIT_WINDOW_SECONDS = 60
# RATE_LIMIT_MAX_REQUESTS = 20
# RATE_LIMITED_PATH_PREFIXES = ("/api/v1/transcribe", "/api/intron/stt/upload-sync")

# _request_log: dict[str, deque] = {}


# class RateLimitMiddleware(BaseHTTPMiddleware):
#     async def dispatch(self, request: Request, call_next):
#         if request.url.path.startswith(RATE_LIMITED_PATH_PREFIXES):
#             client_ip = request.client.host if request.client else "unknown"
#             now = time.monotonic()
#             window = _request_log.setdefault(client_ip, deque())

#             while window and now - window[0] > RATE_LIMIT_WINDOW_SECONDS:
#                 window.popleft()

#             if len(window) >= RATE_LIMIT_MAX_REQUESTS:
#                 retry_after = int(RATE_LIMIT_WINDOW_SECONDS - (now - window[0]))
#                 return JSONResponse(
#                     status_code=429,
#                     content={
#                         "detail": "Rate limit exceeded. Please slow down and try again shortly.",
#                         "retry_after_seconds": max(retry_after, 1),
#                     },
#                     headers={"Retry-After": str(max(retry_after, 1))},
#                 )

#             window.append(now)

#         return await call_next(request)


# app.add_middleware(RateLimitMiddleware)

# # CORS configuration - Allow Cloudflare Pages and local dev
# ORIGINS = [
#     "https://sahara-healthcare-suite.pages.dev",
#     "https://sahara-healthcare-suite-1.pages.dev",
#     "http://localhost:3000",
#     "http://127.0.0.1:3000",
#     "http://localhost:8000"
# ]

# # NOTE: we wrap the entire FastAPI application with CORSMiddleware below,
# # rather than relying only on add_middleware(). This ensures CORS headers are
# # present even when an unhandled exception produces a 500/503 response.

# # Configuration & Keys
# INTRON_API_KEY = os.getenv("INTRON_API_KEY", "")

# # Ethiopian medical vocabulary retained for application-layer use.\n# Intron File Upload Sync does not expose a documented phrase-boost field.
# ETHIOPIAN_MEDICAL_VOCABULARY: List[str] = [
#     # Local pharmacological terms
#     "Paracetamol", "Amoxicillin", "Ciprofloxacin", "Metronidazole",
#     "Artemether", "Lumefantrine", "Coartem", "ORSL", "Zinc Sulfate",
#     # Symptoms in Amharic & Afaan Oromoo (transliterated & localized)
#     "Tefeteno", "Kusli", "Tebat", "Chink", "Mewt", "Derek Kosa",
#     "Tussis", "Fever", "Tiyaa", "Dhukuba", "Garaachaa", "Miti",
#     # Clinical jargon & dosage forms
#     "Sublingual", "Intramuscular", "IV Drip", "BP 120/80", "SpO2",
#     "Triage Level 1", "Triage Level 2", "Triage Level 3", "Referral",
#     "Maternal Health", "Antenatal Care", "ANC", "PNC", "Malaria RDT"
# ]

# class TranscriptionRequest(BaseModel):
#     language_code: str = "am-ET"  # Amharic / Code-switched default
#     # Retained for frontend compatibility. The documented Intron sync API
#     # does not accept a vocabulary-boost field, so this is not sent upstream.
#     boost_vocabulary: Optional[List[str]] = None

# @app.get("/health")
# async def health_check():
#     return {
#         "status": "online",
#         "service": "AfriHealth AI Gateway",
#         "intron_configured": bool(INTRON_API_KEY)
#     }


# def _normalize_language_code(language_code: str) -> str:
#     """
#     The sync-upload endpoint (proven working via the mic-recording feature)
#     expects short codes like "am", "en", "yo", "ha" via use_language_asr_input
#     -- not BCP-47 style tags like "am-ET". Everywhere else in this gateway
#     (the REST /api/v1/transcribe route, the benchmark route) still takes the
#     BCP-47-ish "am-ET" form for backward compatibility with the frontend, so
#     we normalize here rather than pushing this concern out to every caller.
#     """
#     if not language_code:
#         return "am"
#     return language_code.split("-")[0].lower()


# INTRON_SYNC_UPLOAD_ENDPOINT = "https://infer.voice.intron.io/file/v1/upload/sync"
# INTRON_FILE_STATUS_ENDPOINT = "https://infer.voice.intron.io/file/v1/status/{file_id}"
# INTRON_TTS_GENERATE_ENDPOINT = "https://infer.voice.intron.io/tts/v1/generate"
# INTRON_TTS_STATUS_ENDPOINT = "https://infer.voice.intron.io/tts/v1/status/{text_id}"
# INTRON_TTS_VOICE_LANGUAGE = os.getenv("INTRON_TTS_VOICE_LANGUAGE", "am")
# INTRON_TTS_VOICE_ACCENT = os.getenv("INTRON_TTS_VOICE_ACCENT", "amharic")
# INTRON_TTS_VOICE_GENDER = os.getenv("INTRON_TTS_VOICE_GENDER", "female")


# async def _call_intron_transcribe(
#     contents: bytes,
#     filename: str,
#     content_type: Optional[str],
#     language_code: str = "am-ET",
# ) -> dict:
#     """
#     Call Intron's documented synchronous File Upload Sync API.

#     The documented successful response is:
#         {
#             "data": {
#                 "file_id": "...",
#                 "processing_status": "FILE_TRANSCRIBED",
#                 "audio_file_name": "...",
#                 "audio_transcript": "...",
#                 "processed_audio_duration_in_seconds": 20,
#                 "use_language_asr_input": "en"
#             },
#             "message": "file status found",
#             "status": "Ok"
#         }

#     There are no documented per-word confidence scores in this response,
#     and the sync-upload API does not document a `phrase_boost` field.

#     The sync API supports audio durations up to 120 seconds. If Intron
#     returns HTTP 503 after timing out, the response may contain a file_id.
#     In that case we preserve the provider's 503 and expose the file_id so
#     the caller can retrieve the result from the status endpoint.
#     """
#     if not INTRON_API_KEY:
#         logger.warning("INTRON_API_KEY not set. Returning judge-mode mock response.")
#         return {
#             "mode": "fallback_judge_mode",
#             "transcript": (
#                 "ከፍተኛ ትኩሳት እና ሳል አለው:: "
#                 "Paracetamol 500mg t.i.d. given."
#             ),
#         }

#     if len(contents) > 25 * 1024 * 1024:
#         raise HTTPException(
#             status_code=413,
#             detail="Audio file size exceeds maximum limit of 25MB",
#         )

#     # These are the fields documented for the sync-upload endpoint.
#     data_payload = {
#         "audio_file_name": filename or "recording.wav",
#         "use_language_asr_input": _normalize_language_code(language_code),
#         "use_category": "file_category_telehealth",
#         "use_disable_llm_corrections": "FALSE",
#     }

#     files_payload = {
#         "audio_file_blob": (
#             filename or "recording.wav",
#             contents,
#             content_type or "audio/wav",
#         )
#     }

#     headers = {"Authorization": f"Bearer {INTRON_API_KEY}"}

#     try:
#         async with httpx.AsyncClient(timeout=130.0) as client:
#             response = await client.post(
#                 INTRON_SYNC_UPLOAD_ENDPOINT,
#                 headers=headers,
#                 data=data_payload,
#                 files=files_payload,
#             )

#             # Intron documents 503 as a possible synchronous timeout and may
#             # include a file_id that can be checked through /file/v1/status/{id}.
#             if response.status_code == 503:
#                 try:
#                     timeout_json = response.json()
#                 except ValueError:
#                     timeout_json = {}

#                 timeout_data = (
#                     timeout_json.get("data")
#                     if isinstance(timeout_json, dict)
#                     else None
#                 )
#                 timeout_data = timeout_data if isinstance(timeout_data, dict) else {}

#                 file_id = timeout_data.get("file_id") or timeout_json.get("file_id")
#                 logger.warning(
#                     "Intron sync upload timed out (503); file_id=%s",
#                     file_id,
#                 )

#                 detail = {
#                     "message": "Intron processing timed out. Check the file status endpoint.",
#                     "file_id": file_id,
#                     "status": timeout_json.get("status") if isinstance(timeout_json, dict) else None,
#                 }

#                 raise HTTPException(status_code=503, detail=detail)

#             response.raise_for_status()
#             res_json = response.json()

#             data = res_json.get("data")
#             if not isinstance(data, dict):
#                 logger.error(
#                     "Unexpected Intron sync-upload response: %s",
#                     json.dumps(res_json)[:3000],
#                 )
#                 raise HTTPException(
#                     status_code=502,
#                     detail="Intron returned an unexpected response format.",
#                 )

#             transcript_text = data.get("audio_transcript", "")
#             processing_status = data.get("processing_status")
#             file_id = data.get("file_id")

#             if not isinstance(transcript_text, str):
#                 transcript_text = str(transcript_text or "")

#             if processing_status != "FILE_TRANSCRIBED" and not transcript_text:
#                 logger.warning(
#                     "Intron file is not transcribed yet: file_id=%s status=%s",
#                     file_id,
#                     processing_status,
#                 )

#             return {
#                 "mode": "live",
#                 "transcript": transcript_text,
#                 "file_id": file_id,
#                 "processing_status": processing_status,
#                 "audio_file_name": data.get("audio_file_name"),
#                 "processed_audio_duration_in_seconds": data.get(
#                     "processed_audio_duration_in_seconds"
#                 ),
#                 "use_language_asr_input": data.get("use_language_asr_input"),
#                 "raw_response": res_json,
#             }

#     except httpx.HTTPStatusError as e:
#         logger.error(
#             "Intron API Error: %s - %s",
#             e.response.status_code,
#             e.response.text,
#         )
#         raise HTTPException(
#             status_code=e.response.status_code,
#             detail=f"ASR Provider Error: {e.response.text}",
#         )
#     except HTTPException:
#         raise
#     except (ValueError, json.JSONDecodeError) as e:
#         logger.error("Invalid JSON returned by Intron: %s", e)
#         raise HTTPException(
#             status_code=502,
#             detail="Intron returned invalid JSON.",
#         )
#     except Exception as e:
#         logger.error(f"Internal gateway error: {str(e)}")
#         raise HTTPException(
#             status_code=500,
#             detail="Audio transcription gateway processing failed.",
#         )


# @app.post("/api/v1/transcribe")
# async def transcribe_audio(
#     request: Request,
#     file: UploadFile = File(...),
#     language_code: str = "am-ET"
# ):
#     """
#     Proxy an audio file to Intron's documented synchronous upload endpoint.
#     """

#     contents = await file.read()
#     result = await _call_intron_transcribe(contents, file.filename, file.content_type, language_code)

#     if result["mode"] == "fallback_judge_mode":
#         return JSONResponse(status_code=200, content=result)

#     return {
#         "status": "success",
#         "transcript": result["transcript"],
#         "file_id": result.get("file_id"),
#         "processing_status": result.get("processing_status"),
#         "audio_file_name": result.get("audio_file_name"),
#         "processed_audio_duration_in_seconds": result.get(
#             "processed_audio_duration_in_seconds"
#         ),
#         "use_language_asr_input": result.get("use_language_asr_input"),
#     }
# INTRON_STREAM_ENDPOINT = "wss://infer.voice.intron.io/stt/v1/stream"

# @app.websocket("/ws/stream")
# async def websocket_stream(websocket: WebSocket):
#     await websocket.accept()

#     language = websocket.query_params.get("use_language_asr_input", "am")

#     if not INTRON_API_KEY:
#         await websocket.send_json({"error": "Intron API key not configured on server"})
#         await websocket.close()
#         return

#     intron_url = f"{INTRON_STREAM_ENDPOINT}?sample_rate=16000&bit_rate=16&num_channels=1&use_language_asr_input={language}"

#     try:
#         async with websockets.connect(
#             intron_url,
#             extra_headers={"Authorization": f"Bearer {INTRON_API_KEY}"}
#         ) as intron_ws:

#             async def forward_browser_to_intron():
#                 try:
#                     while True:
#                         data = await websocket.receive()
#                         if data.get("bytes") is not None:
#                             audio_b64 = base64.b64encode(data["bytes"]).decode("utf-8")
#                             await intron_ws.send(json.dumps({
#                                 "message_type": "INPUT_AUDIO_CHUNK",
#                                 "audio_base_64": audio_b64
#                             }))
#                         elif data.get("text") is not None:
#                             try:
#                                 msg = json.loads(data["text"])
#                                 if msg.get("event") == "stop":
#                                     await intron_ws.send(json.dumps({"message_type": "COMMIT"}))
#                             except json.JSONDecodeError:
#                                 pass
#                 except WebSocketDisconnect:
#                     pass

#             async def forward_intron_to_browser():
#                 async for message in intron_ws:
#                     payload = json.loads(message)
#                     msg_type = payload.get("message_type")
#                     if msg_type == "PARTIAL_TRANSCRIPT":
#                         await websocket.send_json({"transcript": payload.get("transcript", "")})
#                     elif msg_type == "COMMITTED_TRANSCRIPT":
#                         await websocket.send_json({"transcript": payload.get("transcript_text", "")})
#                     elif msg_type in ("ERROR", "INPUT_ERROR", "AUTHENTICATION_ERROR", "QUOTA_EXCEEDED"):
#                         await websocket.send_json({"error": payload.get("message", msg_type)})

#             forward_task = asyncio.create_task(forward_browser_to_intron())
#             backward_task = asyncio.create_task(forward_intron_to_browser())
#             done, pending = await asyncio.wait(
#                 [forward_task, backward_task], return_when=asyncio.FIRST_COMPLETED
#             )
#             for task in pending:
#                 task.cancel()

#     except Exception as e:
#         logger.error(f"Intron streaming bridge error: {e}")
#         try:
#             await websocket.send_json({"error": str(e)})
#         except Exception:
#             pass
#     finally:
#         try:
#             await websocket.close()
#         except Exception:
#             pass


# @app.post("/api/intron/stt/upload-sync")
# async def intron_stt_upload_sync(request: Request):
#     """
#     Direct proxy for Intron's documented File Upload Sync API.

#     Expected successful response shape includes:
#         data.audio_transcript
#         data.processing_status
#         data.file_id

#     No confidence or phrase-boost fields are fabricated here.
#     """

#     if not INTRON_API_KEY:
#         raise HTTPException(status_code=503, detail="Intron API key not configured on server")

#     form = await request.form()
#     audio_file = form.get("audio_file_blob")
#     if audio_file is None:
#         raise HTTPException(status_code=400, detail="Missing audio_file_blob in form data")

#     file_bytes = await audio_file.read()
#     filename = form.get("audio_file_name") or getattr(audio_file, "filename", "recording.wav")

#     files_payload = {
#         "audio_file_blob": (filename, file_bytes, audio_file.content_type or "audio/wav")
#     }
#     data_payload = {
#         "audio_file_name": filename,
#         "use_language_asr_input": form.get("use_language_asr_input", "am"),
#         "use_category": form.get("use_category", "file_category_telehealth"),
#         "use_disable_llm_corrections": form.get("use_disable_llm_corrections", "FALSE"),
#     }
#     headers = {"Authorization": f"Bearer {INTRON_API_KEY}"}

#     try:
#         async with httpx.AsyncClient(timeout=130.0) as client:
#             response = await client.post(
#                 INTRON_SYNC_UPLOAD_ENDPOINT,
#                 headers=headers,
#                 data=data_payload,
#                 files=files_payload
#             )
#             if response.status_code == 503:
#                 try:
#                     timeout_json = response.json()
#                 except ValueError:
#                     timeout_json = {}
#                 timeout_data = timeout_json.get("data") if isinstance(timeout_json, dict) else None
#                 timeout_data = timeout_data if isinstance(timeout_data, dict) else {}
#                 file_id = timeout_data.get("file_id") or timeout_json.get("file_id")
#                 logger.warning("Intron sync upload timed out (503); file_id=%s", file_id)
#                 raise HTTPException(
#                     status_code=503,
#                     detail={
#                         "message": "Intron processing timed out. Check the file status endpoint.",
#                         "file_id": file_id,
#                     },
#                 )
#             response.raise_for_status()
#             return response.json()
#     except httpx.HTTPStatusError as e:
#         logger.error(f"Intron sync upload error: {e.response.status_code} - {e.response.text}")
#         raise HTTPException(status_code=e.response.status_code, detail=f"Intron upload error: {e.response.text}")
#     except Exception as e:
#         logger.error(f"Intron sync upload gateway error: {e}")
#         raise HTTPException(status_code=500, detail="Audio upload processing failed.")

# @app.get("/api/intron/stt/status/{file_id}")
# async def intron_stt_file_status(file_id: str):
#     """
#     Proxy Intron's documented Get File Status endpoint.

#     Use this after a synchronous upload returns HTTP 503 with a file_id.
#     """
#     if not INTRON_API_KEY:
#         raise HTTPException(
#             status_code=503,
#             detail="Intron API key not configured on server",
#         )

#     headers = {"Authorization": f"Bearer {INTRON_API_KEY}"}
#     status_url = INTRON_FILE_STATUS_ENDPOINT.format(file_id=file_id)

#     try:
#         async with httpx.AsyncClient(timeout=30.0) as client:
#             response = await client.get(
#                 status_url,
#                 headers=headers,
#                 params={"get_structured_post_processing": "f"},
#             )
#             response.raise_for_status()
#             return response.json()

#     except httpx.HTTPStatusError as e:
#         logger.error(
#             "Intron file status error: %s - %s",
#             e.response.status_code,
#             e.response.text,
#         )
#         raise HTTPException(
#             status_code=e.response.status_code,
#             detail=f"Intron file status error: {e.response.text}",
#         )
#     except Exception as e:
#         logger.error(f"Intron file status gateway error: {e}")
#         raise HTTPException(
#             status_code=500,
#             detail="File status lookup failed.",
#         )


# @app.post("/api/intron/tts")
# async def intron_tts(payload: dict):
#     """Generate Amharic speech through Intron TTS without exposing the API key."""
#     if not INTRON_API_KEY:
#         raise HTTPException(status_code=503, detail="Intron API key not configured on server")

#     text = str(payload.get("text", "")).strip()
#     if not text:
#         raise HTTPException(status_code=400, detail="Text is required")
#     if len(text) > 4096:
#         raise HTTPException(status_code=400, detail="TTS text exceeds Intron's 4096 character limit")

#     voice_language = str(payload.get("voice_language") or INTRON_TTS_VOICE_LANGUAGE).strip().lower()
#     voice_accent = str(payload.get("voice_accent") or INTRON_TTS_VOICE_ACCENT).strip().lower()
#     voice_gender = str(payload.get("voice_gender") or INTRON_TTS_VOICE_GENDER).strip().lower()

#     request_body = {
#         "text": text,
#         "voice_language": voice_language,
#         "voice_accent": voice_accent,
#         "voice_gender": voice_gender,
#         "output_audio_format": "wav",
#     }
#     headers = {
#         "Authorization": f"Bearer {INTRON_API_KEY}",
#         "Content-Type": "application/json",
#     }

#     try:
#         async with httpx.AsyncClient(timeout=130.0) as client:
#             response = await client.post(
#                 INTRON_TTS_GENERATE_ENDPOINT,
#                 headers=headers,
#                 json=request_body,
#             )
#             if response.status_code == 503:
#                 try:
#                     timeout_json = response.json()
#                 except ValueError:
#                     timeout_json = {}
#                 data = timeout_json.get("data") if isinstance(timeout_json, dict) else {}
#                 data = data if isinstance(data, dict) else {}
#                 text_id = data.get("text_id") or timeout_json.get("text_id")
#                 raise HTTPException(
#                     status_code=503,
#                     detail={
#                         "message": "Intron TTS processing timed out. Check the TTS status endpoint.",
#                         "text_id": text_id,
#                     },
#                 )
#             response.raise_for_status()
#             result = response.json()
#             result_data = result.get("data") if isinstance(result, dict) else {}
#             result_data = result_data if isinstance(result_data, dict) else {}
#             return {
#                 "status": result.get("status", "Ok"),
#                 "message": result.get("message", "text status found"),
#                 "data": {
#                     "audio_duration_in_seconds": result_data.get("audio_duration_in_seconds"),
#                     "audio_path": result_data.get("audio_path"),
#                     "processing_status": result_data.get("processing_status"),
#                 },
#             }
#     except HTTPException:
#         raise
#     except httpx.HTTPStatusError as e:
#         logger.error("Intron TTS error: %s - %s", e.response.status_code, e.response.text)
#         raise HTTPException(
#             status_code=e.response.status_code,
#             detail=f"Intron TTS error: {e.response.text}",
#         )
#     except Exception as e:
#         logger.error("Intron TTS gateway error: %s", e)
#         raise HTTPException(status_code=500, detail="TTS gateway processing failed.")


# @app.get("/api/intron/tts/status/{text_id}")
# async def intron_tts_status(text_id: str):
#     """Proxy Intron's TTS status endpoint after an async/timeout response."""
#     if not INTRON_API_KEY:
#         raise HTTPException(status_code=503, detail="Intron API key not configured on server")

#     headers = {"Authorization": f"Bearer {INTRON_API_KEY}"}
#     status_url = INTRON_TTS_STATUS_ENDPOINT.format(text_id=text_id)
#     try:
#         async with httpx.AsyncClient(timeout=30.0) as client:
#             response = await client.get(status_url, headers=headers)
#             response.raise_for_status()
#             return response.json()
#     except httpx.HTTPStatusError as e:
#         raise HTTPException(status_code=e.response.status_code, detail=f"Intron TTS status error: {e.response.text}")
#     except Exception as e:
#         logger.error("Intron TTS status gateway error: %s", e)
#         raise HTTPException(status_code=500, detail="TTS status lookup failed.")


# @app.post("/api/intron/tts/audio")
# async def intron_tts_audio(payload: dict):
#     """Generate Intron TTS and proxy the resulting WAV bytes to the browser."""
#     result = await intron_tts(payload)
#     audio_path = result.get("data", {}).get("audio_path") if isinstance(result, dict) else None
#     processing_status = result.get("data", {}).get("processing_status") if isinstance(result, dict) else None
#     if not audio_path or processing_status != "TTS_TEXT_AUDIO_GENERATED":
#         raise HTTPException(status_code=502, detail="Intron TTS did not return generated audio.")

#     try:
#         async with httpx.AsyncClient(timeout=60.0) as client:
#             audio_response = await client.get(audio_path)
#             audio_response.raise_for_status()
#             return StreamingResponse(
#                 iter([audio_response.content]),
#                 media_type=audio_response.headers.get("content-type", "audio/wav"),
#                 headers={"Cache-Control": "no-store"},
#             )
#     except httpx.HTTPStatusError as e:
#         logger.error("Intron TTS audio fetch error: %s - %s", e.response.status_code, e.response.text)
#         raise HTTPException(status_code=502, detail="Could not retrieve generated Intron audio.")
#     except Exception as e:
#         logger.error("Intron TTS audio proxy error: %s", e)
#         raise HTTPException(status_code=502, detail="Could not retrieve generated Intron audio.")


# # ---------------------------------------------------------------------------
# # Sahara v2.5 vs. Whisper Medium benchmark
# #
# # Ports the Colab comparison notebook (WER/CER via jiwer + a keyword-based
# # triage classifier) into the gateway as a real endpoint, so the frontend's
# # "Benchmark Matrix" tab can show actual measured numbers on operatorsupplied
# # audio + reference transcripts, instead of only the embedded fixture rows.
# #
# # Methodology follows the Intron AfriHealth MultiBench approach (normalized
# # WER/CER via jiwer, per-language breakdown, keyword-based triage as a
# # clinician-attention flag rather than an autonomous decision):
# # https://github.com/intron-innovation/Intron-Multimodal-Benchmarking
# #
# # Heavy ML deps (torch, torchaudio, transformers, jiwer) are only imported
# # when this endpoint is first hit, and the Whisper pipeline is cached after
# # first load so repeat calls don't reload the model.
# # ---------------------------------------------------------------------------

# WHISPER_MODEL_ID = os.getenv("WHISPER_BENCHMARK_MODEL", "openai/whisper-medium")
# _whisper_pipeline = None  # lazy-loaded singleton


# def _get_whisper_pipeline():
#     global _whisper_pipeline
#     if _whisper_pipeline is None:
#         from transformers import pipeline  # imported lazily, see note above
#         logger.info(f"Loading Whisper benchmark model '{WHISPER_MODEL_ID}' (first call only)...")
#         _whisper_pipeline = pipeline("automatic-speech-recognition", model=WHISPER_MODEL_ID)
#     return _whisper_pipeline


# def _transcribe_with_whisper(audio_path: str) -> str:
#     """Resamples audio to 16kHz (Whisper's expected rate) and transcribes it."""
#     import numpy as np
#     import soundfile as sf
#     import torch
#     import torchaudio

#     # Read directly with soundfile (libsndfile) -- avoids torchaudio.load()
#     # entirely, which on newer torchaudio always routes through the
#     # torchcodec/FFmpeg backend regardless of the `backend=` kwarg.
#     data, sample_rate = sf.read(audio_path, dtype="float32", always_2d=True)
#     # soundfile gives (frames, channels); torchaudio expects (channels, frames)
#     waveform = torch.from_numpy(data.T)

#     if sample_rate != 16000:
#         resampler = torchaudio.transforms.Resample(orig_freq=sample_rate, new_freq=16000)
#         waveform = resampler(waveform)
#         sample_rate = 16000

#     with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
#         resampled_path = tmp.name
#     try:
#         torchaudio.save(resampled_path, waveform, sample_rate)
#         whisper = _get_whisper_pipeline()
#         result = whisper(resampled_path)
#         return result["text"]
#     finally:
#         if os.path.exists(resampled_path):
#             os.remove(resampled_path)


# def _compute_wer_cer(reference: str, hypothesis: str) -> dict:
#     from jiwer import wer, cer
#     return {
#         "wer": wer(reference, hypothesis),
#         "cer": cer(reference, hypothesis),
#     }


# # Keyword-based triage heuristic, ported directly from the benchmark
# # notebook. This is a coarse text-matching signal only -- it exists to flag
# # transcripts for clinician attention, not to make an autonomous triage
# # decision. It must not be used to gate or replace clinical review.
# EMERGENCY_TERMS = [
#     "severe difficulty breathing",
#     "unconscious",
#     "severe bleeding",
#     "seizure",
#     "cannot breathe",
# ]

# URGENT_TERMS = [
#     "high fever",
#     "chest pain",
#     "dehydration",
#     "persistent vomiting",
#     "difficulty breathing",
# ]


# def triage_classifier(text: str) -> str:
#     text = (text or "").lower()

#     for term in EMERGENCY_TERMS:
#         if term in text:
#             return "EMERGENCY"

#     for term in URGENT_TERMS:
#         if term in text:
#             return "URGENT"

#     return "ROUTINE"


# @app.post("/api/v1/benchmark")
# async def benchmark_asr(
#     file: UploadFile = File(...),
#     reference_transcript: str = Form(...),
#     language_code: str = Form("am-ET"),
# ):
#     """
#     Runs a single audio sample through both Intron Sahara v2.5 and Whisper
#     Medium, scores each against a supplied reference transcript with
#     WER/CER, and runs the keyword triage heuristic on both outputs.

#     This is a real, on-demand comparison (not the embedded frontend
#     fixture rows) -- intended for building up the gold-standard evidence
#     set, not as a substitute for the clinician-reviewed validation summary.
#     """
#     contents = await file.read()
#     if len(contents) > 25 * 1024 * 1024:
#         raise HTTPException(status_code=413, detail="Audio file size exceeds maximum limit of 25MB")

#     # 1. Sahara (Intron) transcript -- reuses the same call path as /api/v1/transcribe
#     sahara_started = time.perf_counter()
#     try:
#         sahara_result = await _call_intron_transcribe(
#             contents, file.filename, file.content_type, language_code
#         )
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.exception("Intron benchmark transcription failed")
#         raise HTTPException(
#             status_code=502,
#             detail=f"Intron benchmark transcription failed: {type(e).__name__}: {e}",
#         )
#     sahara_latency_ms = round((time.perf_counter() - sahara_started) * 1000, 2)
#     sahara_transcript = sahara_result.get("transcript", "")
#     if not isinstance(sahara_transcript, str):
#         sahara_transcript = str(sahara_transcript)

#     # 2. Whisper Medium transcript -- needs a real file on disk for torchaudio
#     suffix = os.path.splitext(file.filename or "")[1] or ".wav"
#     with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
#         tmp.write(contents)
#         raw_audio_path = tmp.name

#     try:
#         try:
#             whisper_started = time.perf_counter()
#             whisper_transcript = await asyncio.to_thread(_transcribe_with_whisper, raw_audio_path)
#             whisper_latency_ms = round((time.perf_counter() - whisper_started) * 1000, 2)
#         except ImportError as e:
#             raise HTTPException(
#                 status_code=503,
#                 detail=(
#                     "Whisper benchmark dependencies not installed on this instance "
#                     f"(torch/torchaudio/transformers). Missing: {e}"
#                 ),
#             )
#         except Exception as e:
#             logger.exception("Whisper benchmark failed")
#             raise HTTPException(
#                 status_code=503,
#                 detail=f"Whisper benchmark processing failed: {type(e).__name__}: {e}",
#             )
#     finally:
#         if os.path.exists(raw_audio_path):
#             os.remove(raw_audio_path)

#     # 3. Score both against the reference transcript
#     sahara_scores = _compute_wer_cer(reference_transcript, sahara_transcript)
#     whisper_scores = _compute_wer_cer(reference_transcript, whisper_transcript)

#     results = [
#         {
#             "model": "Intron Sahara v2.5",
#             "transcript": sahara_transcript,
#             "WER": sahara_scores["wer"],
#             "CER": sahara_scores["cer"],
#             "WER_percent": round(sahara_scores["wer"] * 100, 2),
#             "CER_percent": round(sahara_scores["cer"] * 100, 2),
#             "triage": triage_classifier(sahara_transcript),
#             "latency_ms": sahara_latency_ms,
#             "mode": sahara_result["mode"],
#         },
#         {
#             "model": "OpenAI Whisper Medium",
#             "transcript": whisper_transcript,
#             "WER": whisper_scores["wer"],
#             "CER": whisper_scores["cer"],
#             "WER_percent": round(whisper_scores["wer"] * 100, 2),
#             "CER_percent": round(whisper_scores["cer"] * 100, 2),
#             "triage": triage_classifier(whisper_transcript),
#             "latency_ms": whisper_latency_ms,
#             "mode": "live",
#         },
#     ]

#     return {
#         "status": "success",
#         "benchmark_type": "live_local_validation",
#         "reference_transcript": reference_transcript,
#         "results": results,
#         "triage_agreement": results[0]["triage"] == results[1]["triage"],
#     }


# # Wrap the fully configured ASGI app so CORS also covers framework-level
# # exception responses generated outside the route handlers.
# app = CORSMiddleware(
#     app=app,
#     allow_origins=ORIGINS,
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
import os
import json
import time
import logging
import re
from collections import deque
from typing import List, Optional, Tuple
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
import base64
import websockets
from edge_persistence import persistence
# Initialize logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("afrihealth_gateway")

app = FastAPI(
    title="AfriHealth AI Gateway",
    description="Secure proxy for Intron v2.5 ASR with Ethiopian medical term boosting",
    version="2.5.0"
)

# ---------------------------------------------------------------------------
# Rate limiting
# The module docstring has always claimed "rate limiting" as a feature, but
# no limiter was actually wired up. This adds a lightweight in-memory
# sliding-window limiter (per client IP) with no extra dependency, applied
# to the expensive ASR-proxy routes. It's process-local, which is fine for a
# single Railway/Cloudflare-Pages-fronted instance; swap for a Redis-backed
# limiter if this ever runs behind multiple worker processes.
# ---------------------------------------------------------------------------
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMITED_PATH_PREFIXES = ("/api/v1/transcribe", "/api/intron/stt/upload-sync")
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_ACTIVE_WEBSOCKETS = 100
_active_websockets = 0
REQUIRE_PROXY_AUTH = os.getenv("REQUIRE_PROXY_AUTH", "false").lower() == "true"
PROXY_IDENTITY_HEADERS = (
    "cf-access-authenticated-user-email",
    "x-authenticated-user",
)

_request_log: dict[str, deque] = {}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(RATE_LIMITED_PATH_PREFIXES):
            client_ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            window = _request_log.setdefault(client_ip, deque())

            while window and now - window[0] > RATE_LIMIT_WINDOW_SECONDS:
                window.popleft()

            if len(window) >= RATE_LIMIT_MAX_REQUESTS:
                retry_after = int(RATE_LIMIT_WINDOW_SECONDS - (now - window[0]))
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Rate limit exceeded. Please slow down and try again shortly.",
                        "retry_after_seconds": max(retry_after, 1),
                    },
                    headers={"Retry-After": str(max(retry_after, 1))},
                )

            window.append(now)

        return await call_next(request)


class ProxyIdentityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        protected_path = request.url.path.startswith("/api/")
        if REQUIRE_PROXY_AUTH and protected_path:
            if not any(request.headers.get(header) for header in PROXY_IDENTITY_HEADERS):
                return JSONResponse(status_code=401, content={"detail": "Authenticated clinical access is required"})
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)
app.add_middleware(ProxyIdentityMiddleware)


@app.on_event("startup")
async def initialize_edge_persistence():
    await persistence.initialize()

# CORS configuration - strict allowlist only
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "https://sahara-healthcare-suite.pages.dev,https://sahara-healthcare-suite-1.pages.dev,http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# Configuration & Keys
INTRON_API_KEY = os.getenv("INTRON_API_KEY") or ""
INTRON_ENDPOINT = os.getenv("INTRON_ENDPOINT", "https://api.intron.io/v1/transcribe")
EHR_FHIR_ENDPOINT = os.getenv("EHR_FHIR_ENDPOINT") or ""
EHR_API_KEY = os.getenv("EHR_API_KEY") or ""
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or ""
GEMINI_TRANSCRIBE_MODEL = os.getenv("GEMINI_TRANSCRIBE_MODEL", "gemini-2.0-flash")


async def _read_limited_upload(upload: UploadFile) -> bytes:
    contents = await upload.read(MAX_AUDIO_BYTES + 1)
    if len(contents) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio file size exceeds maximum limit of 25MB")
    return contents


def _keyword_is_negated(text: str, keyword: str) -> bool:
    normalized_keyword = keyword.lower().strip()
    if normalized_keyword.startswith(("no ", "absent ", "cannot ", "can't ")):
        return False
    keyword_match = re.search(re.escape(normalized_keyword), text)
    if not keyword_match:
        return False
    preceding_text = text[max(0, keyword_match.start() - 36):keyword_match.start()]
    return bool(re.search(r"\b(?:no|not|never|denies?|without)\b[^.!?]{0,32}$", preceding_text))


def _has_proxy_identity(headers) -> bool:
    return any(headers.get(header) for header in PROXY_IDENTITY_HEADERS)

# Ethiopian Medical & Local Symptom Phrase Boosting Dictionary
ETHIOPIAN_MEDICAL_VOCABULARY: List[str] = [
    # Local pharmacological terms
    "Paracetamol", "Amoxicillin", "Ciprofloxacin", "Metronidazole",
    "Artemether", "Lumefantrine", "Coartem", "ORSL", "Zinc Sulfate",
    # Symptoms in Amharic & Afaan Oromoo (transliterated & localized)
    "Tefeteno", "Kusli", "Tebat", "Chink", "Mewt", "Derek Kosa",
    "Tussis", "Fever", "Tiyaa", "Dhukuba", "Garaachaa", "Miti",
    # Clinical jargon & dosage forms
    "Sublingual", "Intramuscular", "IV Drip", "BP 120/80", "SpO2",
    "Triage Level 1", "Triage Level 2", "Triage Level 3", "Referral",
    "Maternal Health", "Antenatal Care", "ANC", "PNC", "Malaria RDT"
]

class TranscriptionRequest(BaseModel):
    language_code: str = "am-ET"  # Amharic / Code-switched default
    boost_vocabulary: Optional[List[str]] = None


class ICD10Diagnosis(BaseModel):
    code: str = Field(..., description="Standard ICD-10 diagnostic code (e.g., R51, R50.9)")
    description: str = Field(..., description="Official ICD-10 diagnostic description")


class ClinicalSOAPSchema(BaseModel):
    subjective: str = Field(
        ...,
        description="Patient chief complaint, history of present illness, and reported symptoms",
    )
    objective: str = Field(
        ...,
        description="Vital signs, physical exam findings, and clinical measurements",
    )
    assessment: str = Field(
        ...,
        description="Clinical reasoning, differential diagnosis, and primary assessment",
    )
    plan: str = Field(
        ...,
        description="Treatment strategy, medications prescribed, and follow-up instructions",
    )
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Joint ASR and LLM confidence score (0.0 to 1.0)",
    )
    icd10_codes: List[ICD10Diagnosis] = Field(
        default_factory=list,
        description="List of mapped ICD-10 diagnosis codes",
    )
    flagged_code_switches: List[str] = Field(
        default_factory=list,
        description="Detected code-switched phrases (e.g., Amharic/Oromo terms)",
    )
    requires_manual_review: bool = Field(
        default=False,
        description="Flag set when low confidence (<0.82) or parsing fallback is triggered",
    )
    requires_manual_entry: bool = Field(
        default=False,
        description="Graceful fallback flag for downstream manual completion. Must be true when structured output cannot be validated.",
    )

    @model_validator(mode="after")
    def sync_manual_flags(self):
        if self.requires_manual_entry is False and self.requires_manual_review:
            self.requires_manual_entry = True
        if self.requires_manual_review is False and self.requires_manual_entry:
            self.requires_manual_review = True
        return self


class ClinicalProcessRequest(BaseModel):
    transcript: str = Field(..., min_length=1, description="Raw code-switched clinical transcript text")
    language_hint: Optional[str] = Field("auto", description="Primary language pair hint")


class ClinicalProcessResponse(BaseModel):
    success: bool = True
    soap: ClinicalSOAPSchema
    scrubbed_transcript: str
    redactions_count: int = Field(..., ge=0)
    discrepancies: List[str] = Field(default_factory=list)
    sign_off_required: bool = True


class SOAPDraftPayload(BaseModel):
    transcript: str = ""
    subjective: str = ""
    objective: str = ""
    assessment: str = ""
    plan: str = ""


class FHIRExportRequest(BaseModel):
    patient_id: str = Field(..., min_length=1, max_length=128)
    encounter_id: str = Field(..., min_length=1, max_length=128)
    gender: Optional[str] = Field(default=None, max_length=32)
    chief_complaint: str = Field(..., min_length=1, max_length=1000)
    duration: Optional[str] = Field(default=None, max_length=128)
    blood_pressure: Optional[str] = Field(default=None, max_length=32)
    pulse: Optional[str] = Field(default=None, max_length=32)
    temperature: Optional[str] = Field(default=None, max_length=32)
    diagnosis_code: Optional[str] = Field(default=None, max_length=32)
    diagnosis_display: Optional[str] = Field(default=None, max_length=256)
    medications: List[str] = Field(default_factory=list, max_length=20)
    soap: Optional[SOAPDraftPayload] = None


class EHRCommitRequest(FHIRExportRequest):
    clinician_id: Optional[str] = Field(default=None, max_length=256)


CLINICAL_SYMPTOM_MAP = {
    "fever": ["fever", "pyrexia", "high temperature", "temperature 38", "temp 38"],
    "cough": ["cough", "productive cough", "dry cough"],
    "shortness_of_breath": ["shortness of breath", "dyspnea", "difficulty breathing"],
    "pain": ["pain", "headache", "abdominal pain", "chest pain"],
    "nausea": ["nausea", "vomiting", "diarrhea", "dehydration"],
    "wound_infection": ["wound", "pus", "discharge", "surgical site"],
}


def _normalize_text(value: str) -> str:
    return (value or "").lower().replace("\n", " ")


def validate_transcript_soap_discrepancy(transcript: str, soap: ClinicalSOAPSchema) -> List[str]:
    """Raises a warning when obvious contradictions or omissions exist between transcript and SOAP extraction."""
    transcript_text = _normalize_text(transcript)
    soap_text = _normalize_text(
        " ".join(
            [
                soap.subjective,
                soap.objective,
                soap.assessment,
                soap.plan,
                *[item.description for item in soap.icd10_codes],
            ]
        )
    )
    discrepancies: List[str] = []

    negated_phrases = [
        "no fever",
        "no cough",
        "no pain",
        "denies fever",
        "without fever",
        "without cough",
        "no shortness of breath",
    ]
    for symptom_name, terms in CLINICAL_SYMPTOM_MAP.items():
        transcript_has_positive = any(term in transcript_text for term in terms)
        transcript_has_negative = any(phrase in transcript_text for phrase in negated_phrases if symptom_name in phrase or any(term in phrase for term in terms[:2]))
        soap_has_positive = any(term in soap_text for term in terms)
        if transcript_has_negative and soap_has_positive:
            discrepancies.append(f"Transcript explicitly denies {symptom_name}, but the SOAP note includes {symptom_name}.")
        if transcript_has_positive and not soap_has_positive:
            discrepancies.append(f"Transcript mentions {symptom_name}, but SOAP fields do not capture it.")

    medication_terms = ["amoxicillin", "paracetamol", "ciprofloxacin", "metronidazole", "ibuprofen", "aspirin"]
    transcript_has_med = any(term in transcript_text for term in medication_terms)
    soap_has_med = any(term in soap_text for term in medication_terms)
    if transcript_has_med and not soap_has_med:
        discrepancies.append("Transcript names a medication, but the SOAP plan does not include the medication summary.")

    return discrepancies


def _fallback_soap(raw_transcript: str, *, reason: str = "structured output validation failed") -> ClinicalSOAPSchema:
    safe_text = (raw_transcript or '').strip() or 'No transcript supplied.'
    summary = safe_text[:220]
    return ClinicalSOAPSchema(
        subjective=f"Clinical transcript summary: {summary}",
        objective="Pending clinician-entered vitals, physical exam, and objective findings.",
        assessment=f"Automatic structured SOAP parsing was not valid. Reason: {reason}. Manual review required before documentation is finalized.",
        plan="1. Verify transcript against patient encounter.\n2. Document vital signs and physical exam.\n3. Confirm assessment, medications, and follow-up plan before EMR commit.",
        confidence_score=0.0,
        icd10_codes=[],
        flagged_code_switches=[],
        requires_manual_review=True,
        requires_manual_entry=True,
    )


def _coerce_soap_payload(payload: object) -> ClinicalSOAPSchema:
    if isinstance(payload, ClinicalSOAPSchema):
        return payload

    if not isinstance(payload, dict):
        raise TypeError("SOAP payload must be a dict")

    normalized = dict(payload)
    for key in ("requires_manual_entry", "requires_manual_review"):
        if key in normalized and isinstance(normalized[key], bool):
            normalized["requires_manual_entry"] = normalized.get("requires_manual_entry", False) or normalized.get("requires_manual_review", False)
            normalized["requires_manual_review"] = normalized.get("requires_manual_review", False) or normalized.get("requires_manual_entry", False)
            break

    # Tolerate common structured-output formatting imperfections.
    fallback_keys = {
        "patient_summary": "subjective",
        "history_of_present_illness": "subjective",
        "physical_exam": "objective",
        "plan_of_care": "plan",
        "clinical_assessment": "assessment",
    }
    for old_key, new_key in fallback_keys.items():
        if old_key in normalized and new_key not in normalized:
            normalized[new_key] = normalized[old_key]

    if "flagged_code_switches" in normalized and not isinstance(normalized["flagged_code_switches"], list):
        normalized["flagged_code_switches"] = [str(normalized["flagged_code_switches"])]

    if "icd10_codes" in normalized and isinstance(normalized["icd10_codes"], list):
        normalized["icd10_codes"] = [
            {"code": item.get("code", "R69"), "description": item.get("description", "Illness, unspecified")}
            if isinstance(item, dict) else {"code": "R69", "description": "Illness, unspecified"}
            for item in normalized["icd10_codes"]
        ]

    return ClinicalSOAPSchema.model_validate(normalized)


def _generate_structured_soap(raw_transcript: str, *, attempts: int = 3) -> ClinicalSOAPSchema:
    if not raw_transcript or not raw_transcript.strip():
        return _fallback_soap(raw_transcript, reason="empty transcript")

    clean = raw_transcript.strip()
    for attempt in range(1, attempts + 1):
        candidate = clean
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)

        try:
            parsed = json.loads(candidate)
            return _coerce_soap_payload(parsed)
        except (TypeError, ValueError, ValidationError):
            compact = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
            if compact:
                try:
                    return _coerce_soap_payload(json.loads(compact.group(0)))
                except (TypeError, ValueError, ValidationError):
                    pass
            if attempt < attempts:
                candidate = candidate.replace("'", '"').replace("\n", " ")
                clean = candidate
                continue

    return _fallback_soap(raw_transcript, reason="retries exhausted")


def _scrub_transcript(transcript: str) -> Tuple[str, int]:
    """Remove common direct identifiers before a transcript is echoed or stored."""
    patterns = (
        (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED EMAIL]"),
        (r"\b(?:\+?251|0)?9\d{8}\b", "[REDACTED PHONE]"),
    )
    scrubbed = transcript
    redactions = 0
    for pattern, replacement in patterns:
        scrubbed, count = re.subn(pattern, replacement, scrubbed, flags=re.IGNORECASE)
        redactions += count
    return scrubbed, redactions


def get_fallback_soap(raw_transcript: str) -> ClinicalSOAPSchema:
    """
    Guarantees deterministic system recovery when LLM parsing or schema validation fails.
    Prevents application crashes during non-standard transcript inputs.
    """
    return _generate_structured_soap(raw_transcript, attempts=3)


@app.post("/api/v1/clinical/process-text", response_model=ClinicalProcessResponse)
async def process_clinical_text(payload: ClinicalProcessRequest) -> ClinicalProcessResponse:
    """Validate all SOAP output against the strict clinical schema and degrade gracefully without 500s."""
    scrubbed_transcript, redactions_count = _scrub_transcript(payload.transcript)

    try:
        soap = _generate_structured_soap(scrubbed_transcript, attempts=3)
    except Exception:
        soap = _fallback_soap(scrubbed_transcript, reason="exception while building SOAP")

    discrepancies = validate_transcript_soap_discrepancy(scrubbed_transcript, soap)
    return ClinicalProcessResponse(
        soap=soap,
        scrubbed_transcript=scrubbed_transcript,
        redactions_count=redactions_count,
        discrepancies=discrepancies,
        sign_off_required=bool(discrepancies) or soap.requires_manual_review or soap.requires_manual_entry,
    )


def _build_fhir_bundle(payload: FHIRExportRequest) -> dict:
    entries = [
        {
            "resource": {
                "resourceType": "Patient",
                "id": payload.patient_id,
                "active": True,
                **({"gender": payload.gender} if payload.gender else {}),
            }
        },
        {
            "resource": {
                "resourceType": "Encounter",
                "id": payload.encounter_id,
                "status": "finished",
                "class": {"code": "AMB", "display": "ambulatory"},
                "subject": {"reference": f"Patient/{payload.patient_id}"},
                "reasonCode": [{"text": payload.chief_complaint}],
                **(
                    {"extension": [{"url": "https://sahara-healthcare-suite.example/fhir/duration", "valueString": payload.duration}]}
                    if payload.duration
                    else {}
                ),
            }
        },
    ]

    if payload.soap:
        entries.append(
            {
                "resource": {
                    "resourceType": "Composition",
                    "status": "final",
                    "type": {"text": "Structured SOAP clinical note"},
                    "subject": {"reference": f"Patient/{payload.patient_id}"},
                    "encounter": {"reference": f"Encounter/{payload.encounter_id}"},
                    "section": [
                        {"title": "Subjective", "text": {"status": "generated", "div": payload.soap.subjective}},
                        {"title": "Objective", "text": {"status": "generated", "div": payload.soap.objective}},
                        {"title": "Assessment", "text": {"status": "generated", "div": payload.soap.assessment}},
                        {"title": "Plan", "text": {"status": "generated", "div": payload.soap.plan}},
                    ],
                    "extension": [
                        {
                            "url": "https://sahara-healthcare-suite.example/fhir/transcript",
                            "valueString": payload.soap.transcript,
                        }
                    ],
                }
            }
        )

    vital_components = []
    if payload.blood_pressure:
        vital_components.append({"code": {"text": "Blood pressure"}, "valueString": payload.blood_pressure})
    if payload.pulse:
        vital_components.append({"code": {"text": "Heart rate"}, "valueString": payload.pulse})
    if payload.temperature:
        vital_components.append({"code": {"text": "Body temperature"}, "valueString": payload.temperature})
    if vital_components:
        entries.append(
            {
                "resource": {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {"text": "Vital signs"},
                    "subject": {"reference": f"Patient/{payload.patient_id}"},
                    "encounter": {"reference": f"Encounter/{payload.encounter_id}"},
                    "component": vital_components,
                }
            }
        )

    if payload.diagnosis_code:
        entries.append(
            {
                "resource": {
                    "resourceType": "Condition",
                    "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
                    "code": {
                        "coding": [
                            {
                                "system": "http://hl7.org/fhir/sid/icd-10",
                                "code": payload.diagnosis_code,
                                "display": payload.diagnosis_display or payload.diagnosis_code,
                            }
                        ]
                    },
                    "subject": {"reference": f"Patient/{payload.patient_id}"},
                    "encounter": {"reference": f"Encounter/{payload.encounter_id}"},
                }
            }
        )

    for medication in payload.medications:
        entries.append(
            {
                "resource": {
                    "resourceType": "MedicationStatement",
                    "status": "active",
                    "medicationCodeableConcept": {"text": medication},
                    "subject": {"reference": f"Patient/{payload.patient_id}"},
                    "context": {"reference": f"Encounter/{payload.encounter_id}"},
                }
            }
        )

    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


@app.post("/api/v1/fhir/export")
async def export_fhir(payload: FHIRExportRequest) -> dict:
    """Build a FHIR bundle server-side after the clinician review gate."""
    return _build_fhir_bundle(payload)


@app.post("/api/v1/ehr/commit")
async def commit_to_ehr(payload: EHRCommitRequest) -> dict:
    """Send a signed-off FHIR bundle to an explicitly configured EHR endpoint."""
    if not EHR_FHIR_ENDPOINT:
        raise HTTPException(status_code=503, detail="EHR_FHIR_ENDPOINT is not configured")

    bundle = _build_fhir_bundle(payload)
    headers = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
    if EHR_API_KEY:
        headers["Authorization"] = f"Bearer {EHR_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(EHR_FHIR_ENDPOINT, headers=headers, json=bundle)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("EHR commit failed: %s - %s", exc.response.status_code, exc.response.text[:1000])
        raise HTTPException(status_code=502, detail="Configured EHR rejected the FHIR bundle")
    except httpx.RequestError as exc:
        logger.error("EHR commit request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Configured EHR endpoint is unavailable")

    return {"status": "committed", "provider_status": response.status_code}


# ---------------------------------------------------------------------------
# Post-Care Protocol Engine
# Implements two clinical decision-support layers on top of a transcript:
#
#   1. Recovery risk scoring (post-operative follow-up): detects persistent
#      fever, swelling, unmanaged pain, and missed medication doses, and
#      produces a 0-100 risk score.
#
#   2. Maternal triage acuity classification: implements the 5-level
#      obstetric acuity scale provided by the clinical team (Annex 1-3 --
#      Triage Assessment Sheet, Acuity Scale Classification Tool, and
#      Acuity Level Assessment & Intervention Guide). Level 1/2 keyword
#      matches ALWAYS force an immediate emergency escalation regardless
#      of the recovery risk score, because under-triaging an obstetric
#      emergency is the failure mode that actually hurts people.
#
# SAFETY NOTE: the real acuity scale was designed around a nurse/doctor
# taking vitals and doing a physical exam (BP, FHB, pelvic exam, Doppler
# study, etc.) -- most Annex 2 criteria cannot be assessed from voice
# alone. This endpoint scans REPORTED SYMPTOMS only and returns decision
# support, never a finished triage decision. Every response carries
# requires_human_confirmation=True.
# ---------------------------------------------------------------------------

RECOVERY_INDICATORS = {
    "persistent_fever": [
        "fever", "high temperature", "burning up", "chills",
        "ትኩሳት", "ሙቀት",
    ],
    "swelling": [
        "swelling", "swollen", "puffy", "inflamed",
        "እብጠት", "አበጠ",
    ],
    "unmanaged_pain": [
        "pain won't stop", "still hurts", "severe pain", "can't sleep from pain",
        "unbearable pain", "pain is worse",
        "ህመም አለብኝ", "አይሻለኝም",
    ],
    "missed_doses": [
        "forgot to take", "missed a dose", "haven't taken", "ran out of medicine",
        "couldn't afford", "stopped taking",
        "መድሃኒት አልወሰድኩም", "ረሳሁ",
    ],
}
RECOVERY_INDICATOR_WEIGHTS = {
    "persistent_fever": 35, "swelling": 20, "unmanaged_pain": 25, "missed_doses": 30,
}
RECOVERY_COMPOUND_BONUS = 15
RECOVERY_HIGH_RISK_THRESHOLD = 60
RECOVERY_AT_RISK_THRESHOLD = 30

# Transcribed from the clinical team's Annex 2 (Triage Acuity Scale
# Classification Tool). English + a few Amharic phrasings -- a native
# Amharic-speaking clinician should review/expand these before real use.
MATERNAL_ACUITY_LEVELS = {
    1: {"label": "Level 1 (Resuscitative)", "color": "red",
        "intervention_time": "Immediate", "reassessment": "Continuous care"},
    2: {"label": "Level 2 (Emergent)", "color": "orange",
        "intervention_time": "\u226415 minutes", "reassessment": "Every 15 minutes"},
    3: {"label": "Level 3 (Urgent)", "color": "yellow",
        "intervention_time": "\u226430 minutes", "reassessment": "Every 15 minutes"},
    4: {"label": "Level 4 (Less Urgent)", "color": "green",
        "intervention_time": "\u226460 minutes", "reassessment": "Every 30 minutes"},
    5: {"label": "Level 5 (Non-Urgent)", "color": "blue",
        "intervention_time": "Within 120 minutes or less", "reassessment": "Every 60 minutes"},
}
MATERNAL_LEVEL_1_KEYWORDS = [
    "imminent birth", "crowning", "baby is coming", "active vaginal bleeding",
    "heavy bleeding", "bleeding heavily", "hemodynamically unstable",
    "seizure", "convulsion", "convulsing", "coma", "unconscious", "unresponsive",
    "altered level of consciousness", "abnormal fetal heart", "no fetal movement",
    "absent fetal movement", "cord prolapse", "acute severe abdominal pain",
    "cannot breathe", "can't breathe", "severe respiratory distress", "suspected sepsis",
    "ምጥ መጣ", "ብዙ ደም እየፈሰሰ", "መንቀጥቀጥ", "ራሴን ስቻለሁ", "ራሴን ስቷል",
]
MATERNAL_LEVEL_2_KEYWORDS = [
    "preterm labor", "preterm labour", "water broke early", "moderate bleeding",
    "high blood pressure", "hypertension", "severe headache", "blurred vision",
    "vision changes", "upper abdominal pain", "epigastric pain",
    "reduced fetal movement", "decreased fetal movement", "abnormal doppler",
    "major trauma", "shortness of breath", "unattended delivery",
    "ራስ ምታት ኃይለኛ", "እብጠት",
]
MATERNAL_LEVEL_3_KEYWORDS = [
    "labor pains", "labour pains", "contractions", "mild bleeding",
    "spotting with cramping", "moderate hypertension", "severe back pain",
    "flank pain", "blood in urine", "hematuria", "vomiting and diarrhea", "dehydration",
]
MATERNAL_LEVEL_4_KEYWORDS = [
    "early labor", "early labour", "water broke", "spotting", "minor accident",
    "fell down", "minor fall", "nausea", "vomiting", "fever", "chills", "signs of infection",
]
MATERNAL_LEVEL_KEYWORDS = {
    1: MATERNAL_LEVEL_1_KEYWORDS, 2: MATERNAL_LEVEL_2_KEYWORDS,
    3: MATERNAL_LEVEL_3_KEYWORDS, 4: MATERNAL_LEVEL_4_KEYWORDS,
}


class PostCareAnalysisRequest(BaseModel):
    transcript: str
    care_track: str = "general"  # "general" or "maternal"


NEGATION_WORDS = {"no", "not", "denies", "denying", "without", "never", "none", "negative"}


def _keyword_present_unnegated(text_lower: str, keyword: str, window: int = 3) -> bool:
    """True if keyword appears and isn't immediately preceded by a negation
    word (e.g. "no fever" should NOT count as the fever indicator). This is
    a simple word-window check, not real NLP negation scope detection --
    good enough to kill the most common false-positive pattern, not a
    substitute for a clinician reviewing ambiguous transcripts."""
    idx = text_lower.find(keyword.lower())
    if idx == -1:
        return False
    preceding = text_lower[:idx].split()[-window:]
    if any(w.strip(".,!?;:") in NEGATION_WORDS for w in preceding):
        return False
    return True


def _score_recovery_risk(transcript: str) -> dict:
    text_lower = (transcript or "").lower()
    detail = {
        name: any(_keyword_present_unnegated(text_lower, kw) for kw in kws)
        for name, kws in RECOVERY_INDICATORS.items()
    }
    score = sum(RECOVERY_INDICATOR_WEIGHTS[name] for name, hit in detail.items() if hit)
    if sum(1 for hit in detail.values() if hit) >= 2:
        score += RECOVERY_COMPOUND_BONUS
    score = min(score, 100)
    level = "high_risk" if score >= RECOVERY_HIGH_RISK_THRESHOLD else \
        "at_risk" if score >= RECOVERY_AT_RISK_THRESHOLD else "on_track"
    return {
        "risk_score": score, "risk_level": level,
        "indicators_detected": [name for name, hit in detail.items() if hit],
        "escalate_to_nurse": level == "high_risk",
    }


def _classify_maternal_acuity(transcript: str) -> Optional[dict]:
    text_lower = (transcript or "").lower()
    for level in (1, 2, 3, 4):
        matched = [
            kw for kw in MATERNAL_LEVEL_KEYWORDS[level]
            if kw.lower() in text_lower and not _keyword_is_negated(text_lower, kw)
        ]
        if matched:
            info = MATERNAL_ACUITY_LEVELS[level]
            return {
                "level": level, "label": info["label"], "color": info["color"],
                "intervention_time": info["intervention_time"], "reassessment": info["reassessment"],
                "matched_keywords": matched, "is_emergency_trigger": level <= 2,
            }
    info = MATERNAL_ACUITY_LEVELS[5]
    return {
        "level": 5, "label": info["label"], "color": info["color"],
        "intervention_time": info["intervention_time"], "reassessment": info["reassessment"],
        "matched_keywords": [], "is_emergency_trigger": False,
    }


@app.post("/api/v1/post-care/analyze")
async def analyze_post_care_checkin(payload: PostCareAnalysisRequest):
    """
    Runs the post-care protocol engine on a check-in transcript. For
    care_track="maternal", a Level 1/2 Annex-2 keyword match overrides the
    recovery risk score and forces nurse escalation -- see the module
    docstring above for why this override direction is deliberate.
    """
    recovery = _score_recovery_risk(payload.transcript)
    maternal_acuity = None
    nurse_escalated = recovery["escalate_to_nurse"]

    if payload.care_track == "maternal":
        maternal_acuity = _classify_maternal_acuity(payload.transcript)
        if maternal_acuity["is_emergency_trigger"]:
            nurse_escalated = True

    return {
        "transcript": payload.transcript,
        "care_track": payload.care_track,
        "recovery_risk_score": recovery["risk_score"],
        "recovery_risk_level": recovery["risk_level"],
        "recovery_indicators": recovery["indicators_detected"],
        "nurse_escalated": nurse_escalated,
        "maternal_acuity": maternal_acuity,
        "requires_human_confirmation": True,
        "safety_note": (
            "This is decision support generated from reported symptoms only. "
            "A nurse or doctor must confirm with actual vitals and physical "
            "exam before any clinical action is taken."
        ),
    }

@app.get("/health")
async def health_check():
    return {
        "status": "online",
        "service": "AfriHealth AI Gateway",
        "intron_configured": bool(INTRON_API_KEY),
        "ehr_configured": bool(EHR_FHIR_ENDPOINT),
        "boost_phrases_loaded": len(ETHIOPIAN_MEDICAL_VOCABULARY)
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready"}


def _benchmark_tokens(value: str) -> list[str]:
    return re.findall(r"[\w\u1200-\u137f]+", (value or "").lower(), re.UNICODE)


def _benchmark_edit_distance(expected: list[str], actual: list[str]) -> int:
    previous = list(range(len(actual) + 1))
    for row, expected_token in enumerate(expected, start=1):
        current = [row]
        for column, actual_token in enumerate(actual, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected_token != actual_token),
                )
            )
        previous = current
    return previous[-1]


def _benchmark_scores(reference: str, hypothesis: str) -> dict:
    reference_tokens = _benchmark_tokens(reference)
    hypothesis_tokens = _benchmark_tokens(hypothesis)
    reference_chars = list("".join(reference_tokens))
    hypothesis_chars = list("".join(hypothesis_tokens))
    return {
        "WER": round(_benchmark_edit_distance(reference_tokens, hypothesis_tokens) / max(1, len(reference_tokens)), 4),
        "CER": round(_benchmark_edit_distance(reference_chars, hypothesis_chars) / max(1, len(reference_chars)), 4),
    }


async def _post_intron_sync_upload(contents: bytes, filename: str, content_type: str, language_code: str) -> dict:
    response_data = {
        "audio_file_name": filename or "recording.wav",
        "use_language_asr_input": (language_code or "am").split("-")[0].lower(),
        "use_category": "file_category_telehealth",
        "use_disable_llm_corrections": "FALSE",
    }
    files = {"audio_file_blob": (filename or "recording.wav", contents, content_type or "audio/wav")}
    async with httpx.AsyncClient(timeout=130.0) as client:
        response = await client.post(
            INTRON_SYNC_UPLOAD_ENDPOINT,
            headers={"Authorization": f"Bearer {INTRON_API_KEY}"},
            data=response_data,
            files=files,
        )
        response.raise_for_status()
        return response.json()


async def _benchmark_intron(contents: bytes, filename: str, content_type: str, language_code: str) -> str:
    if not INTRON_API_KEY:
        raise RuntimeError("INTRON_API_KEY is not configured")
    response = await _post_intron_sync_upload(contents, filename, content_type, language_code)
    data = response.get("data", {}) if isinstance(response, dict) else {}
    return str(data.get("audio_transcript") or data.get("transcript") or "")


async def _benchmark_openai(contents: bytes, filename: str, content_type: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=130.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            data={"model": OPENAI_TRANSCRIBE_MODEL, "language": "am"},
            files={"file": (filename, contents, content_type or "audio/wav")},
        )
        response.raise_for_status()
        return str(response.json().get("text", ""))


async def _benchmark_gemini(contents: bytes, content_type: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    import base64 as _base64

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TRANSCRIBE_MODEL}:generateContent"
        f"?key={GEMINI_API_KEY}"
    )
    body = {
        "contents": [{"parts": [
            {"text": "Transcribe this clinical Amharic-English code-switched audio verbatim. Return only the transcript."},
            {"inline_data": {"mime_type": content_type or "audio/wav", "data": _base64.b64encode(contents).decode("ascii")}},
        ]}]
    }
    async with httpx.AsyncClient(timeout=130.0) as client:
        response = await client.post(endpoint, json=body)
        response.raise_for_status()
        candidates = response.json().get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        return " ".join(str(part.get("text", "")) for part in parts).strip()


async def _run_live_benchmark_provider(name: str, provider, *args) -> dict:
    started = time.perf_counter()
    try:
        transcript = await provider(*args)
        return {
            "model": name,
            "status": "complete",
            "transcript": transcript,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:
        return {
            "model": name,
            "status": "unavailable",
            "transcript": "",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": str(exc),
        }


@app.post("/api/v1/benchmark/live")
async def live_benchmark(
    file: UploadFile = File(...),
    reference_transcript: str = Form(...),
    language_code: str = Form("am-ET"),
):
    """Compare configured ASR providers on one reviewed Amharic-English sample."""
    contents = await _read_limited_upload(file)
    if not reference_transcript.strip():
        raise HTTPException(status_code=400, detail="Reference transcript is required")
    providers = await asyncio.gather(
        _run_live_benchmark_provider(
            "Intron Sahara v2.5", _benchmark_intron, contents, file.filename or "sample.wav", file.content_type or "audio/wav", language_code
        ),
        _run_live_benchmark_provider(
            f"OpenAI {OPENAI_TRANSCRIBE_MODEL}", _benchmark_openai, contents, file.filename or "sample.wav", file.content_type or "audio/wav"
        ),
        _run_live_benchmark_provider(
            f"Google Gemini {GEMINI_TRANSCRIBE_MODEL}", _benchmark_gemini, contents, file.content_type or "audio/wav"
        ),
    )
    for result in providers:
        result.update(_benchmark_scores(reference_transcript, result["transcript"]) if result["transcript"] else {})
    return {
        "status": "success",
        "benchmark_type": "live_provider_comparison",
        "language_code": language_code,
        "reference_transcript": reference_transcript,
        "results": providers,
        "interpretation": "Measured sample comparison only; not a clinical performance claim.",
    }


@app.post("/api/v1/transcribe")
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    language_code: str = "am-ET"
):
    """
    Proxies audio payload to Intron v2.5 API with medical phrase boosting.
    """
    if not INTRON_API_KEY:
        raise HTTPException(status_code=503, detail="Intron API key not configured on server")

    # Validate file size (Limit to 25MB)
    contents = await _read_limited_upload(file)

    # Merge default medical vocabulary with any runtime custom phrases
    payload_keywords = ETHIOPIAN_MEDICAL_VOCABULARY.copy()

    headers = {
        "Authorization": f"Bearer {INTRON_API_KEY}",
        "Accept": "application/json"
    }

    data_payload = {
        "language": language_code,
        "phrase_boost": payload_keywords,  # Intron Custom Vocabulary Boosting
        "enable_word_confidence": True
    }

    files_payload = {
        "file": (file.filename, contents, file.content_type or "audio/wav")
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                INTRON_ENDPOINT,
                headers=headers,
                data=data_payload,
                files=files_payload
            )
            response.raise_for_status()
            res_json = response.json()

            # Process word confidence scoring
            words = res_json.get("words", [])
            low_confidence_terms = [
                w["word"] for w in words if w.get("confidence", 1.0) < 0.70
            ]

            return {
                "status": "success",
                "transcript": res_json.get("transcript", ""),
                "confidence_score": res_json.get("confidence", 0.0),
                "low_confidence_flagged": low_confidence_terms,
                "boosted_vocabulary_count": len(payload_keywords)
            }

    except httpx.HTTPStatusError as e:
        logger.error(f"Intron API Error: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=f"ASR Provider Error: {e.response.text}")
    except Exception as e:
        logger.error(f"Internal gateway error: {str(e)}")
        raise HTTPException(status_code=500, detail="Audio transcription gateway processing failed.")
INTRON_STREAM_ENDPOINT = "wss://infer.voice.intron.io/stt/v1/stream"

@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    global _active_websockets
    origin = websocket.headers.get("origin")
    if origin not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return
    if REQUIRE_PROXY_AUTH and not _has_proxy_identity(websocket.headers):
        await websocket.close(code=1008)
        return
    if not INTRON_API_KEY:
        await websocket.close(code=1011)
        return
    if _active_websockets >= MAX_ACTIVE_WEBSOCKETS:
        await websocket.close(code=1013)
        return

    _active_websockets += 1
    await websocket.accept()

    language = websocket.query_params.get("use_language_asr_input", "am")
    clinic_id = websocket.query_params.get("clinic_id", "default-clinic")
    if not re.fullmatch(r"[a-z]{2,8}(?:-[A-Z]{2})?", language):
        await websocket.close(code=1008)
        _active_websockets -= 1
        return

    intron_url = f"{INTRON_STREAM_ENDPOINT}?sample_rate=16000&bit_rate=16&num_channels=1&use_language_asr_input={language}"

    try:
        session_id = await persistence.start_session(
            clinic_id=clinic_id,
            language_code=language,
        )
        await websocket.send_json({"session_id": session_id, "persistence": persistence.backend})
        persistence_tasks: set[asyncio.Task] = set()

        def queue_persistence(transcript: str, event_type: str) -> None:
            task = asyncio.create_task(
                persistence.record_transcript(
                    session_id=session_id,
                    clinic_id=clinic_id,
                    transcript=transcript,
                    event_type=event_type,
                )
            )
            persistence_tasks.add(task)
            task.add_done_callback(persistence_tasks.discard)

        async with websockets.connect(
            intron_url,
            extra_headers={"Authorization": f"Bearer {INTRON_API_KEY}"}
        ) as intron_ws:

            async def forward_browser_to_intron():
                try:
                    while True:
                        data = await websocket.receive()
                        if data.get("bytes") is not None:
                            audio_b64 = base64.b64encode(data["bytes"]).decode("utf-8")
                            await intron_ws.send(json.dumps({
                                "message_type": "INPUT_AUDIO_CHUNK",
                                "audio_base_64": audio_b64
                            }))
                        elif data.get("text") is not None:
                            try:
                                msg = json.loads(data["text"])
                                if msg.get("type") == "audio_meta":
                                    timestamp_ms = msg.get("timestamp_ms")
                                    if isinstance(timestamp_ms, (int, float)) and not isinstance(timestamp_ms, bool):
                                        await websocket.send_json({"ack_ts": timestamp_ms})
                                elif msg.get("event") == "stop":
                                    await intron_ws.send(json.dumps({"message_type": "COMMIT"}))
                            except json.JSONDecodeError:
                                pass
                except WebSocketDisconnect:
                    pass

            async def forward_intron_to_browser():
                async for message in intron_ws:
                    payload = json.loads(message)
                    msg_type = payload.get("message_type")
                    if msg_type == "PARTIAL_TRANSCRIPT":
                        transcript = payload.get("transcript", "")
                        queue_persistence(transcript, "partial")
                        await websocket.send_json({"transcript": transcript, "session_id": session_id})
                    elif msg_type == "COMMITTED_TRANSCRIPT":
                        transcript = payload.get("transcript_text", "")
                        queue_persistence(transcript, "final")
                        await websocket.send_json({"transcript": transcript, "session_id": session_id})
                    elif msg_type in ("ERROR", "INPUT_ERROR", "AUTHENTICATION_ERROR", "QUOTA_EXCEEDED"):
                        await websocket.send_json({"error": payload.get("message", msg_type)})

            forward_task = asyncio.create_task(forward_browser_to_intron())
            backward_task = asyncio.create_task(forward_intron_to_browser())
            done, pending = await asyncio.wait(
                [forward_task, backward_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        logger.error(f"Intron streaming bridge error: {e}")
        try:
            await websocket.send_json({"error": str(e)})
        except Exception:
            pass
    finally:
        if "persistence_tasks" in locals() and persistence_tasks:
            await asyncio.gather(*persistence_tasks, return_exceptions=True)
        _active_websockets = max(0, _active_websockets - 1)
        try:
            await websocket.close()
        except Exception:
            pass

INTRON_SYNC_UPLOAD_ENDPOINT = "https://infer.voice.intron.io/file/v1/upload/sync"

@app.post("/api/intron/stt/upload-sync")
async def intron_stt_upload_sync(request: Request):
    if not INTRON_API_KEY:
        raise HTTPException(status_code=503, detail="Intron API key not configured on server")

    form = await request.form()
    audio_file = form.get("audio_file_blob")
    if audio_file is None:
        raise HTTPException(status_code=400, detail="Missing audio_file_blob in form data")

    file_bytes = await _read_limited_upload(audio_file)
    filename = form.get("audio_file_name") or getattr(audio_file, "filename", "recording.wav")

    files_payload = {
        "audio_file_blob": (filename, file_bytes, audio_file.content_type or "audio/wav")
    }
    data_payload = {
        "audio_file_name": filename,
        "use_language_asr_input": form.get("use_language_asr_input", "am"),
        "use_category": form.get("use_category", "file_category_telehealth"),
        "use_disable_llm_corrections": form.get("use_disable_llm_corrections", "FALSE"),
    }
    headers = {"Authorization": f"Bearer {INTRON_API_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=130.0) as client:
            response = await client.post(
                INTRON_SYNC_UPLOAD_ENDPOINT,
                headers=headers,
                data=data_payload,
                files=files_payload
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Intron sync upload error: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=f"Intron upload error: {e.response.text}")
    except Exception as e:
        logger.error(f"Intron sync upload gateway error: {e}")
        raise HTTPException(status_code=500, detail="Audio upload processing failed.")

# ---------------------------------------------------------------------------
# Real multi-turn post-care follow-up call
#
# NOT a simulation: asks protocol-driven questions (fixed, safety-reviewed
# question sets -- not LLM-generated), transcribes each spoken answer via
# Intron's real STT endpoint, runs the post-care protocol engine on every
# answer, and narrates the next question (or final result) via Intron's
# real TTS endpoint. No LLM/Anthropic key needed anywhere in this loop --
# the questions and narration phrases are pre-written, which is also the
# safer choice for a safety-critical follow-up call (no risk of an LLM
# improvising something clinically wrong mid-call).
#
# Session state is in-memory (dict keyed by session_id), same pattern as
# the rate limiter above -- fine for a single instance, swap for Redis if
# this ever runs behind multiple workers.
# ---------------------------------------------------------------------------
import uuid

INTRON_TTS_ENDPOINT = "https://infer.voice.intron.io/tts/v1/generate"

# Bilingual (Amharic + English) protocol-driven follow-up questions.
# care_track="maternal" uses the pregnancy/postpartum set instead. A native
# Amharic-speaking clinician should review this wording before real use --
# this is a first pass, not clinically reviewed copy.
FOLLOWUP_QUESTIONS = {
    "general": [
        "ከሆስፒታል ከወጡ በኋላ ስሜትዎ እንዴት ነው? How are you feeling since you left the hospital?",
        "የታዘዘውን መድሃኒት በትክክል እየወሰዱ ነው? Are you taking your prescribed medication correctly?",
        "ትኩሳት፣ እብጠት ወይም የማይቀንስ ህመም አለብዎት? Do you have fever, swelling, or pain that isn't going away?",
        "ስለ ማገገምዎ የሚያሳስብዎት ነገር አለ? Is there anything about your recovery that's worrying you?",
    ],
    "maternal": [
        "የምጥ ስሜት፣ የፈሳሽ መፍሰስ ወይም የደም መፍሰስ አለብዎት? Are you having labor sensations, fluid leakage, or bleeding?",
        "ጽንሱ በደንብ ይንቀሳቀሳል? Is the baby moving well?",
        "ራስ ምታት፣ የዓይን መደብዘዝ ወይም ያልተለመደ ከፍተኛ የደም ግፊት ምልክቶች አለብዎት? Do you have severe headache, blurred vision, or high blood pressure symptoms?",
        "ስለ እርግዝናዎ የሚያሳስብዎት ሌላ ነገር አለ? Is there anything else about your pregnancy that's worrying you?",
    ],
}

RISK_NARRATION_AM = {
    "on_track": "አመሰግናለሁ። ምላሾችዎ ጥሩ ናቸው። ማገገምዎ በትክክለኛ ጎዳና ላይ ይመስላል።",
    "at_risk": "እናመሰግናለን። አንዳንድ ምልክቶች ተገኝተዋል፤ እባክዎ በቅርብ ጊዜ ውስጥ ክሊኒክ ያነጋግሩ።",
    "high_risk": "እባክዎ ወዲያውኑ የሕክምና እርዳታ ይፈልጉ። ይህ ጉዳይ አስቸኳይ ሊሆን ይችላል።",
}
MATERNAL_EMERGENCY_NARRATION_AM = (
    "እባክዎ ወዲያውኑ ወደ ጤና ተቋም ይሂዱ ወይም ድንገተኛ እርዳታ ይደውሉ። ይህ በጣም አስቸኳይ ሁኔታ ሊሆን ይችላል።"
)

FOLLOWUP_SESSION_TTL_SECONDS = 60 * 60
MAX_FOLLOWUP_SESSIONS = 1000
_followup_sessions: dict = {}


def _prune_followup_sessions() -> None:
    now = time.time()
    expired = [
        session_id for session_id, session in _followup_sessions.items()
        if now - session["last_access"] > FOLLOWUP_SESSION_TTL_SECONDS
    ]
    for session_id in expired:
        _followup_sessions.pop(session_id, None)

    if len(_followup_sessions) > MAX_FOLLOWUP_SESSIONS:
        oldest = sorted(_followup_sessions, key=lambda key: _followup_sessions[key]["last_access"])
        for session_id in oldest[:len(_followup_sessions) - MAX_FOLLOWUP_SESSIONS]:
            _followup_sessions.pop(session_id, None)


class SessionStartRequest(BaseModel):
    care_track: str = "general"


async def _intron_tts_generate(text: str, voice_gender: str = "female") -> Optional[str]:
    """Returns a real Intron-hosted audio URL, or None if no key is
    configured or the call fails -- callers must handle None (show text,
    or fall back to browser speechSynthesis) rather than assume audio."""
    if not INTRON_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {INTRON_API_KEY}", "Content-Type": "application/json"}
    body = {
        "text": text, "voice_language": "am", "voice_accent": "amharic",
        "voice_gender": voice_gender, "output_audio_format": "wav",
    }
    try:
        async with httpx.AsyncClient(timeout=130.0) as client:
            resp = await client.post(INTRON_TTS_ENDPOINT, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json().get("data", {}).get("audio_path")
    except Exception as e:
        logger.error(f"Intron TTS error: {e}")
        return None


async def _intron_stt_transcribe(file_bytes: bytes, filename: str, content_type: str) -> str:
    files_payload = {"audio_file_blob": (filename, file_bytes, content_type or "audio/wav")}
    data_payload = {
        "audio_file_name": filename, "use_language_asr_input": "am",
        "use_category": "file_category_telehealth", "use_disable_llm_corrections": "FALSE",
    }
    headers = {"Authorization": f"Bearer {INTRON_API_KEY}"}
    async with httpx.AsyncClient(timeout=130.0) as client:
        resp = await client.post(INTRON_SYNC_UPLOAD_ENDPOINT, headers=headers, data=data_payload, files=files_payload)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("audio_transcript", "")


@app.post("/api/v1/post-care/session/start")
async def start_followup_session(payload: SessionStartRequest):
    _prune_followup_sessions()
    care_track = payload.care_track if payload.care_track in FOLLOWUP_QUESTIONS else "general"
    session_id = str(uuid.uuid4())
    _followup_sessions[session_id] = {
        "care_track": care_track, "question_index": 0, "transcripts": [],
        "created_at": time.time(), "last_access": time.time(), "complete": False,
    }

    question_text = FOLLOWUP_QUESTIONS[care_track][0]
    audio_url = await _intron_tts_generate(question_text)

    return {
        "session_id": session_id,
        "question_index": 0,
        "total_questions": len(FOLLOWUP_QUESTIONS[care_track]),
        "question_text": question_text,
        "question_audio_url": audio_url,
        "narration_engine": "intron_tts" if audio_url else None,
    }


@app.post("/api/v1/post-care/session/answer")
async def answer_followup_question(request: Request, session_id: str):
    _prune_followup_sessions()
    if session_id not in _followup_sessions:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not INTRON_API_KEY:
        raise HTTPException(status_code=503, detail="Intron API key not configured on server")

    session = _followup_sessions[session_id]
    if session["complete"]:
        raise HTTPException(status_code=409, detail="Session is already complete")
    session["last_access"] = time.time()
    form = await request.form()
    audio_file = form.get("audio_file_blob")
    if audio_file is None:
        raise HTTPException(status_code=400, detail="Missing audio_file_blob in form data")

    file_bytes = await _read_limited_upload(audio_file)
    filename = getattr(audio_file, "filename", "answer.wav")
    transcript = await _intron_stt_transcribe(file_bytes, filename, audio_file.content_type)
    session["transcripts"].append(transcript)

    recovery = _score_recovery_risk(transcript)
    maternal_acuity = _classify_maternal_acuity(transcript) if session["care_track"] == "maternal" else None
    emergency = recovery["escalate_to_nurse"] or bool(maternal_acuity and maternal_acuity["is_emergency_trigger"])

    if emergency:
        is_maternal_emergency = bool(maternal_acuity and maternal_acuity["is_emergency_trigger"])
        narration_text = MATERNAL_EMERGENCY_NARRATION_AM if is_maternal_emergency else RISK_NARRATION_AM["high_risk"]
        audio_url = await _intron_tts_generate(narration_text)
        session["complete"] = True
        return {
            "session_id": session_id, "transcript": transcript, "done": True, "emergency": True,
            "recovery_risk_score": recovery["risk_score"], "recovery_risk_level": recovery["risk_level"],
            "maternal_acuity": maternal_acuity, "nurse_escalated": True,
            "narration_text": narration_text, "narration_audio_url": audio_url,
            "narration_engine": "intron_tts" if audio_url else None,
            "safety_note": "This is decision support from reported symptoms only. Seek immediate clinical evaluation.",
        }

    questions = FOLLOWUP_QUESTIONS[session["care_track"]]
    session["question_index"] += 1
    idx = session["question_index"]

    if idx < len(questions):
        next_question = questions[idx]
        audio_url = await _intron_tts_generate(next_question)
        return {
            "session_id": session_id, "transcript": transcript, "done": False, "emergency": False,
            "question_index": idx, "total_questions": len(questions),
            "question_text": next_question, "question_audio_url": audio_url,
            "narration_engine": "intron_tts" if audio_url else None,
        }

    full_text = " ".join(session["transcripts"])
    final_recovery = _score_recovery_risk(full_text)
    narration_text = RISK_NARRATION_AM.get(final_recovery["risk_level"], RISK_NARRATION_AM["on_track"])
    audio_url = await _intron_tts_generate(narration_text)
    session["complete"] = True
    return {
        "session_id": session_id, "transcript": transcript, "done": True, "emergency": False,
        "recovery_risk_score": final_recovery["risk_score"], "recovery_risk_level": final_recovery["risk_level"],
        "recovery_indicators": final_recovery["indicators_detected"],
        "narration_text": narration_text, "narration_audio_url": audio_url,
        "narration_engine": "intron_tts" if audio_url else None,
        "safety_note": "This is decision support from reported symptoms only.",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
