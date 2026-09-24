import asyncio
import tempfile
import unittest
from pathlib import Path

from edge_persistence import EdgePersistence


class EdgePersistenceTests(unittest.TestCase):
    def test_sqlite_session_and_transcript_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EdgePersistence(str(Path(directory) / "sync.sqlite3"))

            async def exercise():
                await store.initialize()
                session_id = await store.start_session(
                    clinic_id="clinic-oromia-01", language_code="om-ET"
                )
                await store.record_transcript(
                    session_id=session_id,
                    clinic_id="clinic-oromia-01",
                    transcript="Dhukkuba garaacha qaba.",
                    event_type="final",
                )

            asyncio.run(exercise())
            self.assertEqual(store.backend, "sqlite")
            self.assertTrue(Path(directory, "sync.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
