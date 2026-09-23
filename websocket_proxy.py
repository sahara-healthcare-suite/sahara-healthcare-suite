import asyncio
import base64
import contextlib
import json
import random
import time
from typing import Any, Awaitable, Callable, Optional

try:
    import websockets
except ImportError as exc:  # pragma: no cover - only used for runtime integration.
    raise RuntimeError("websocket_proxy.py requires the 'websockets' package") from exc


class WebSocketProxy:
    """Resilient socket proxy with ack-based replay and exponential backoff."""

    def __init__(
        self,
        upstream_url: str,
        *,
        base_delay: float = 0.1,
        multiplier: float = 2.0,
        max_delay: float = 3.0,
        jitter_ratio: float = 0.2,
        audio_queue_size: int = 100,
    ) -> None:
        self.upstream_url = upstream_url
        self.base_delay = base_delay
        self.multiplier = multiplier
        self.max_delay = max_delay
        self.jitter_ratio = jitter_ratio
        self.retry_attempts = 0
        self.last_ack_ts = 0
        self._stop_event = asyncio.Event()
        self._connected = False
        self.websocket = None
        self._send_lock = asyncio.Lock()
        self.audio_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, audio_queue_size))
        self.asr_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, audio_queue_size))
        self.llm_queue: asyncio.Queue = asyncio.Queue(maxsize=25)
        self._tasks: list[asyncio.Task] = []

    def reset_backoff(self) -> None:
        self.retry_attempts = 0

    def backoff_delay(self) -> float:
        raw = self.base_delay * (self.multiplier ** self.retry_attempts)
        bounded = min(raw, self.max_delay)
        jitter = bounded * self.jitter_ratio * random.uniform(-1, 1)
        return max(0.05, bounded + jitter)

    async def _connect(self):
        return await websockets.connect(self.upstream_url)

    async def _send_resume(self, websocket) -> None:
        if self.last_ack_ts > 0:
            await websocket.send(
                json.dumps({"type": "resume", "ack_ts": self.last_ack_ts})
            )

    async def _handle_incoming(self, websocket) -> None:
        async for message in websocket:
            if isinstance(message, str):
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue

                if isinstance(payload, dict) and "ack_ts" in payload:
                    self.last_ack_ts = max(self.last_ack_ts, float(payload["ack_ts"]))
                continue

            if isinstance(message, (bytes, bytearray)):
                continue

    async def _audio_ingestion_worker(self) -> None:
        while not self._stop_event.is_set():
            frame = await self.audio_queue.get()
            if frame is None:
                self.audio_queue.task_done()
                break
            try:
                if isinstance(frame, dict):
                    await self.asr_queue.put(frame)
            finally:
                self.audio_queue.task_done()

    async def _forward_audio_frame(self, frame: Any) -> None:
        if not isinstance(frame, dict):
            return
        pcm_bytes = frame.get("pcm_bytes")
        if pcm_bytes is None:
            return
        if not self._connected or self.websocket is None:
            return
        # Keep the high-priority audio path independent from LLM work.
        await self.send_audio_chunk(
            self.websocket,
            sequence=frame.get("sequence", 0),
            timestamp_ms=frame.get("timestamp_ms", time.time() * 1000),
            pcm_bytes=pcm_bytes,
        )

    async def _asr_stream_worker(self) -> None:
        while not self._stop_event.is_set():
            frame = await self.asr_queue.get()
            if frame is None:
                self.asr_queue.task_done()
                break
            try:
                await self._forward_audio_frame(frame)
            finally:
                self.asr_queue.task_done()

    async def _llm_worker(self) -> None:
        while not self._stop_event.is_set():
            event = await self.llm_queue.get()
            if event is None:
                self.llm_queue.task_done()
                break
            try:
                await self._handle_llm_event(event)
            finally:
                self.llm_queue.task_done()

    async def _handle_llm_event(self, event: dict) -> None:
        transcript = event.get("transcript")
        if transcript is None:
            return
        await asyncio.sleep(0)

    async def enqueue_audio(self, *, sequence: int, timestamp_ms: float, pcm_bytes: bytes) -> None:
        await self.audio_queue.put({
            "sequence": sequence,
            "timestamp_ms": timestamp_ms,
            "pcm_bytes": pcm_bytes,
        })

    async def enqueue_transcript_event(self, *, transcript: str, **metadata: Any) -> None:
        await self.llm_queue.put({"transcript": transcript, **metadata})

    async def start_workers(self) -> None:
        self._tasks.extend([
            asyncio.create_task(self._audio_ingestion_worker()),
            asyncio.create_task(self._asr_stream_worker()),
            asyncio.create_task(self._llm_worker()),
        ])

    async def stop_workers(self) -> None:
        self._stop_event.set()
        for _ in range(max(1, len(self._tasks))):
            await self.audio_queue.put(None)
            await self.asr_queue.put(None)
            await self.llm_queue.put(None)
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def run(self) -> None:
        await self.start_workers()
        while not self._stop_event.is_set():
            try:
                async with await self._connect() as websocket:
                    self.websocket = websocket
                    self._connected = True
                    self.reset_backoff()
                    await self._send_resume(websocket)
                    await self._handle_incoming(websocket)
            except (OSError, websockets.ConnectionClosed, websockets.WebSocketException):
                self._connected = False
                self.websocket = None
                delay = self.backoff_delay()
                self.retry_attempts += 1
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
        await self.stop_workers()

    async def send_audio_chunk(self, websocket, *, sequence: int, timestamp_ms: float, pcm_bytes: bytes) -> None:
        if websocket is None:
            websocket = self.websocket
        if websocket is None:
            return
        frame = {
            "type": "audio_meta",
            "sequence": sequence,
            "timestamp_ms": timestamp_ms,
            "ack_ts": self.last_ack_ts,
        }
        async with self._send_lock:
            await websocket.send(json.dumps(frame))
            await websocket.send(pcm_bytes)

    async def send_resume_notice(self, websocket) -> None:
        await self._send_resume(websocket)

    def stop(self) -> None:
        self._stop_event.set()


class StreamingAudioPipeline:
    """Separates audio ingestion, ASR forwarding, and LLM inference onto independent async tasks."""

    def __init__(self, *, audio_queue_size: int = 100, llm_queue_size: int = 25):
        self.audio_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, audio_queue_size))
        self.llm_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, llm_queue_size))
        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

    async def enqueue_audio_frame(self, *, sequence: int, timestamp_ms: float, pcm_bytes: bytes) -> None:
        await self.audio_queue.put({
            "sequence": sequence,
            "timestamp_ms": timestamp_ms,
            "pcm_bytes": pcm_bytes,
        })

    async def enqueue_transcript(self, transcript: str, **metadata: Any) -> None:
        await self.llm_queue.put({"transcript": transcript, **metadata})

    async def _audio_worker(self) -> None:
        while not self._stop_event.is_set():
            item = await self.audio_queue.get()
            if item is None:
                self.audio_queue.task_done()
                break
            try:
                await asyncio.sleep(0)
            finally:
                self.audio_queue.task_done()

    async def _asr_worker(self) -> None:
        while not self._stop_event.is_set():
            item = await self.audio_queue.get()
            if item is None:
                self.audio_queue.task_done()
                break
            try:
                await asyncio.sleep(0)
            finally:
                self.audio_queue.task_done()

    async def _llm_worker(self) -> None:
        while not self._stop_event.is_set():
            item = await self.llm_queue.get()
            if item is None:
                self.llm_queue.task_done()
                break
            try:
                asyncio.create_task(self._run_llm_task(item))
            finally:
                self.llm_queue.task_done()

    async def _run_llm_task(self, item: dict) -> None:
        await asyncio.sleep(0)

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._audio_worker()),
            asyncio.create_task(self._asr_worker()),
            asyncio.create_task(self._llm_worker()),
        ]

    async def stop(self) -> None:
        self._stop_event.set()
        for _ in range(3):
            await self.audio_queue.put(None)
            await self.llm_queue.put(None)
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
