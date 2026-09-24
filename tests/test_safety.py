import asyncio
import io
import unittest
from unittest.mock import Mock

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
