import asyncio
import io
import unittest
import wave
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, UploadFile
from starlette.responses import PlainTextResponse
from starlette.requests import Request

import main


class SafetyTests(unittest.TestCase):
    def test_negated_maternal_symptom_does_not_escalate(self):
        result = main._classify_maternal_acuity("The patient reports no heavy bleeding.")
        self.assertEqual(result["level"], 5)
        self.assertFalse(result["is_emergency_trigger"])

    def test_positive_maternal_symptom_still_escalates(self):
        result = main._classify_maternal_acuity("The patient has heavy bleeding.")
        self.assertEqual(result["level"], 1)
        self.assertTrue(result["is_emergency_trigger"])

    def test_missing_provider_key_fails_closed(self):
        original_key = main.INTRON_API_KEY
        try:
            main.INTRON_API_KEY = ""
            request = Mock()
            upload = UploadFile(file=io.BytesIO(b"audio"), filename="test.wav")
            with self.assertRaises(HTTPException) as context:
                asyncio.run(main.transcribe_audio(request, upload))
            self.assertEqual(context.exception.status_code, 503)
        finally:
            main.INTRON_API_KEY = original_key

    def test_upload_limit_is_enforced(self):
        upload = UploadFile(
            file=io.BytesIO(b"x" * (main.MAX_AUDIO_BYTES + 1)),
            filename="too-large.wav",
        )
        with self.assertRaises(HTTPException) as context:
            asyncio.run(main._read_limited_upload(upload))
        self.assertEqual(context.exception.status_code, 413)

    def test_wav_duration_limit_is_enforced(self):
        wav_data = io.BytesIO()
        with wave.open(wav_data, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(1)
            audio.writeframes(b"\0\0" * 121)

        with self.assertRaises(HTTPException) as context:
            main._validate_wav_duration(wav_data.getvalue())
        self.assertEqual(context.exception.status_code, 413)

    def test_tts_text_limit_matches_documented_boundary(self):
        request = main.IntronTTSRequest(text="a" * 4096)
        self.assertEqual(len(request.text), 4096)
        with self.assertRaises(ValueError):
            main.IntronTTSRequest(text="a" * 4097)

    def test_tts_routes_use_documented_provider_urls(self):
        async def exercise():
            payload = main.IntronTTSRequest(text="Clinical reminder")
            for route, endpoint in (
                (main.intron_tts_generate, main.INTRON_TTS_ENDPOINT),
                (main.intron_tts_enqueue, main.INTRON_TTS_ENQUEUE_ENDPOINT),
            ):
                with patch.object(main, "_post_intron_json", new_callable=AsyncMock) as post:
                    post.return_value = {"status": "ok"}
                    result = await route(payload)
                    self.assertEqual(result, {"status": "ok"})
                    post.assert_awaited_once_with(endpoint, payload.model_dump())

        asyncio.run(exercise())

    def test_voicebot_workflow_uses_documented_provider_url(self):
        async def exercise():
            payload = {"name": "Reminder", "workflow_type": "ROBOCALL", "message": "Hello"}
            with patch.object(main, "_post_intron_json", new_callable=AsyncMock) as post:
                post.return_value = {"workflow_id": "workflow-1"}
                result = await main.create_intron_voicebot_workflow(payload)
                self.assertEqual(result, {"workflow_id": "workflow-1"})
                post.assert_awaited_once_with(main.INTRON_VOICEBOT_WORKFLOWS_ENDPOINT, payload)

        asyncio.run(exercise())

    def test_followup_session_pruning_removes_expired_sessions(self):
        original_sessions = main._followup_sessions.copy()
        try:
            main._followup_sessions.clear()
            main._followup_sessions["expired"] = {"last_access": 0}
            main._prune_followup_sessions()
            self.assertNotIn("expired", main._followup_sessions)
        finally:
            main._followup_sessions.clear()
            main._followup_sessions.update(original_sessions)

    def test_proxy_identity_header_is_recognized(self):
        self.assertTrue(main._has_proxy_identity({"x-authenticated-user": "clinician@example.org"}))
        self.assertTrue(main._has_proxy_identity({"cf-access-authenticated-user-email": "clinician@example.org"}))
        self.assertFalse(main._has_proxy_identity({}))

    def test_proxy_auth_middleware_rejects_missing_identity(self):
        original_setting = main.REQUIRE_PROXY_AUTH
        try:
            main.REQUIRE_PROXY_AUTH = True
            middleware = main.ProxyIdentityMiddleware(main.app)
            request = Request({"type": "http", "method": "POST", "path": "/api/v1/post-care/analyze", "headers": []})
            response = asyncio.run(middleware.dispatch(request, lambda _: PlainTextResponse("ok")))
            self.assertEqual(response.status_code, 401)
        finally:
            main.REQUIRE_PROXY_AUTH = original_setting

    def test_fhir_export_contains_patient_and_encounter_references(self):
        payload = main.FHIRExportRequest(
            patient_id="patient-1",
            encounter_id="encounter-1",
            chief_complaint="Chest pain",
            diagnosis_code="R07.9",
            diagnosis_display="Chest pain, unspecified",
        )
        bundle = asyncio.run(main.export_fhir(payload))
        resources = [entry["resource"] for entry in bundle["entry"]]
        self.assertEqual(bundle["resourceType"], "Bundle")
        self.assertEqual(resources[0]["id"], "patient-1")
        self.assertEqual(resources[1]["subject"]["reference"], "Patient/patient-1")
        self.assertEqual(resources[-1]["code"]["coding"][0]["code"], "R07.9")

    def test_fhir_export_contains_signed_soap_composition(self):
        payload = main.FHIRExportRequest(
            patient_id="patient-1",
            encounter_id="encounter-1",
            chief_complaint="Chest pain",
            soap=main.SOAPDraftPayload(
                transcript="Chest pain for two days.",
                subjective="Chest pain for two days.",
                objective="BP 140/90.",
                assessment="R07.9 - Chest pain, unspecified.",
                plan="Clinician review and follow-up.",
            ),
        )
        bundle = asyncio.run(main.export_fhir(payload))
        compositions = [
            entry["resource"]
            for entry in bundle["entry"]
            if entry["resource"]["resourceType"] == "Composition"
        ]
        self.assertEqual(len(compositions), 1)
        self.assertEqual(compositions[0]["section"][0]["text"]["div"], "Chest pain for two days.")

    def test_live_benchmark_reports_unconfigured_providers(self):
        upload = UploadFile(file=io.BytesIO(b"audio"), filename="sample.wav")
        with patch.object(main, "INTRON_API_KEY", ""):
            result = asyncio.run(
                main.live_benchmark(
                    upload,
                    "Patient has fever and cough.",
                    "am-ET",
                    "intron",
                    "verified",
                )
            )
        self.assertEqual(result["benchmark_type"], "live_provider_comparison")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["model"], "Intron Sahara v2.5")
        self.assertEqual(result["results"][0]["status"], "unavailable")

    def test_live_benchmark_allows_transcript_only_mode(self):
        upload = UploadFile(file=io.BytesIO(b"audio"), filename="sample.wav")
        with patch.object(main, "INTRON_API_KEY", ""):
            result = asyncio.run(main.live_benchmark(upload, "", "am-ET", "intron", "verified"))
        self.assertEqual(result["scoring_status"], "transcript_only")

    def test_ehr_commit_fails_closed_without_endpoint(self):
        payload = main.EHRCommitRequest(
            patient_id="patient-1",
            encounter_id="encounter-1",
            chief_complaint="Chest pain",
        )
        original_endpoint = main.EHR_FHIR_ENDPOINT
        try:
            main.EHR_FHIR_ENDPOINT = ""
            with self.assertRaises(HTTPException) as context:
                asyncio.run(main.commit_to_ehr(payload))
            self.assertEqual(context.exception.status_code, 503)
        finally:
            main.EHR_FHIR_ENDPOINT = original_endpoint


if __name__ == "__main__":
    unittest.main()
