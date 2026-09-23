"""
Futaba Gemini Live Voice Provider — Realtime Bidirectional Voice Interface.

Uses Google's official `google-genai` Live API over WebSockets:
- Realtime streaming microphone input (16kHz PCM)
- Realtime streaming audio output (24kHz native PCM)
- Low-latency interruption handling (stops speaking when user speaks)
- High-level tool calls routed into FUTABA & Hermes
- Local low-CPU wake-word detection ("Hey Futaba")
- HUD state synchronization (idle, listening, thinking, executing, speaking, etc.)
- Graceful error recovery and free-tier rate-limit protection
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable, Optional

import numpy as np

from futaba.core.config import get_config
from futaba.security.credentials import CredentialStore
from futaba.voice.command_router import VoiceCommandRouter

logger = logging.getLogger("futaba.voice.gemini_live")

# Audio stream parameters
INPUT_SAMPLE_RATE = 16000     # Gemini Live expects 16kHz PCM
INPUT_CHANNELS = 1            # Mono
INPUT_CHUNK_SIZE = 1024       # Samples per chunk (~64ms)

OUTPUT_SAMPLE_RATE = 24000    # Gemini Live returns 24kHz PCM
OUTPUT_CHANNELS = 1           # Mono
OUTPUT_CHUNK_SIZE = 1024

# Active session invariant tracker across the process
_active_live_sessions: int = 0
_global_audio_player: Optional[AudioPlayer] = None


def get_audio_player(device: Optional[int | str] = None, sample_rate: int = OUTPUT_SAMPLE_RATE) -> AudioPlayer:
    """Return the global singleton AudioPlayer instance."""
    global _global_audio_player
    if _global_audio_player is None:
        _global_audio_player = AudioPlayer(device=device, sample_rate=sample_rate)
    elif device is not None:
        _global_audio_player.device = device
    return _global_audio_player


class AudioPlayer:
    """
    Realtime streaming PCM audio player using sounddevice.
    Supports low-latency playback, jitter smoothing, unbounded queuing,
    response generation tracking, and clean interruption.
    """

    def __init__(self, device: Optional[int | str] = None, sample_rate: int = OUTPUT_SAMPLE_RATE):
        self.device = device
        self.sample_rate = sample_rate
        # Unbounded queue: never drops chunks during fast WebSocket bursts
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=0)
        self._stream = None
        self._running = False
        self._current_chunk = bytearray()
        self._lock = threading.Lock()
        self._prebuffering = True
        self.on_playback_state: Optional[Callable[[bool], None]] = None

        # Turn / Generation metrics
        self._current_response_id: str = ""
        self._bytes_received: int = 0
        self._bytes_played: int = 0
        self._chunks_received: int = 0
        self._interrupted: bool = False
        self._interruption_reason: str = ""
        self._is_playing: bool = False
        self._turn_complete_received: bool = False
        self._last_sample_consumed_time: float = time.monotonic()

    def start_response(self, response_id: Optional[str] = None) -> str:
        """Start tracking a new audio response turn."""
        with self._lock:
            self._current_response_id = response_id or uuid.uuid4().hex[:8]
            self._bytes_received = 0
            self._bytes_played = 0
            self._chunks_received = 0
            self._interrupted = False
            self._interruption_reason = ""
            self._turn_complete_received = False
            self._is_playing = True
            self._prebuffering = True
            self._last_sample_consumed_time = time.monotonic()
        logger.debug("Audio response started: id=%s", self._current_response_id)
        if self.on_playback_state:
            try:
                self.on_playback_state(True)
            except Exception as e:
                logger.debug("Playback state callback error: %s", e)
        return self._current_response_id

    def mark_turn_complete(self) -> None:
        """Mark that Gemini Live has finished transmitting chunks for this turn."""
        with self._lock:
            self._turn_complete_received = True
            if self._queue.empty() and not self._current_chunk:
                self._is_playing = False
                self._notify_completed()

    def _notify_completed(self) -> None:
        """Log turn completion and fire on_playback_state(False)."""
        logger.info(
            "Audio response completed: id=%s chunks=%d received=%d played=%d interrupted=%s reason=%s",
            self._current_response_id or "none",
            self._chunks_received,
            self._bytes_received,
            self._bytes_played,
            self._interrupted,
            self._interruption_reason or "none",
        )
        if self.on_playback_state:
            try:
                self.on_playback_state(False)
            except Exception as e:
                logger.debug("Playback state callback error: %s", e)

    def is_playing(self) -> bool:
        """True if player is currently buffering, queue has chunks, or audio is rendering."""
        with self._lock:
            return bool(self._is_playing or not self._queue.empty() or self._current_chunk)

    def play_chunk(self, audio_bytes: bytes) -> None:
        """Queue an audio chunk for streaming playback without dropping."""
        if not self._running or not audio_bytes:
            return
        with self._lock:
            self._queue.put(audio_bytes)
            self._bytes_received += len(audio_bytes)
            self._chunks_received += 1
            self._is_playing = True

    def check_watchdog(self) -> None:
        """Watchdog: recover cleanly if sounddevice stalled for > 2.5s while queue has chunks."""
        now = time.monotonic()
        with self._lock:
            stalled = self._is_playing and (not self._queue.empty() or self._current_chunk) and (now - self._last_sample_consumed_time > 2.5)
        if stalled:
            logger.warning("Audio playback watchdog: stream stalled >2.5s; resetting jitter buffer")
            with self._lock:
                self._prebuffering = False
                self._last_sample_consumed_time = now

    def interrupt(self, reason: str = "unspecified") -> None:
        """Immediately stop playback and discard all buffered audio."""
        with self._lock:
            was_playing = bool(self._is_playing or not self._queue.empty() or self._current_chunk)
            self._interrupted = True
            self._interruption_reason = reason
            self._current_chunk.clear()
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._prebuffering = True
            self._is_playing = False
            self._turn_complete_received = False

        if was_playing:
            logger.info("Audio player interrupted (reason=%s, id=%s); buffer cleared.", reason, self._current_response_id)
            if self.on_playback_state:
                try:
                    self.on_playback_state(False)
                except Exception as e:
                    logger.debug("Playback state callback error: %s", e)

    def start(self) -> bool:
        """Start the audio output stream."""
        if self._running:
            return True
        try:
            import sounddevice as sd

            def callback(outdata, frames, time_info, status):
                if status:
                    logger.debug("Audio output status: %s", status)

                bytes_needed = frames * 2  # 16-bit mono = 2 bytes per sample
                output_buf = bytearray()
                completed = False

                with self._lock:
                    self._last_sample_consumed_time = time.monotonic()

                    # Jitter buffer smoothing: wait until 2 chunks arrive or turn finished
                    if self._prebuffering:
                        if self._queue.qsize() >= 2 or self._turn_complete_received:
                            self._prebuffering = False
                        else:
                            outdata[:] = np.zeros((frames, 1), dtype=np.int16)
                            return

                    while len(output_buf) < bytes_needed:
                        if not self._current_chunk:
                            try:
                                self._current_chunk = bytearray(self._queue.get_nowait())
                            except queue.Empty:
                                break

                        needed = bytes_needed - len(output_buf)
                        take = min(needed, len(self._current_chunk))
                        output_buf.extend(self._current_chunk[:take])
                        self._current_chunk = self._current_chunk[take:]
                        self._bytes_played += take

                    if not self._current_chunk and self._queue.empty():
                        self._prebuffering = True
                        if self._turn_complete_received and self._is_playing:
                            self._is_playing = False
                            self._turn_complete_received = False
                            completed = True

                if len(output_buf) < bytes_needed:
                    output_buf.extend(b"\x00" * (bytes_needed - len(output_buf)))

                outdata[:] = np.frombuffer(output_buf, dtype=np.int16).reshape(-1, 1)

                if completed:
                    self._notify_completed()

            self._stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=OUTPUT_CHANNELS,
                dtype="int16",
                blocksize=OUTPUT_CHUNK_SIZE,
                device=self.device,
                callback=callback,
            )
            self._stream.start()
            self._running = True
            logger.info("Audio player started (device=%s, rate=%d)", self.device or "default", self.sample_rate)
            return True
        except Exception as e:
            logger.error("Failed to start audio player: %s", e)
            self._running = False
            return False

    def stop(self) -> None:
        """Stop and close the audio output stream."""
        self._running = False
        self.interrupt(reason="player_stop")
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        logger.info("Audio player stopped.")


class GeminiLiveVoiceProvider:
    """
    Full Gemini Live voice implementation for FUTABA.
    """

    def __init__(
        self,
        command_router: VoiceCommandRouter,
        on_state_change: Optional[Callable[[str, str], None]] = None,
        wake_phrase: str = "hey futaba",
    ):
        self.router = command_router
        self.on_state_change = on_state_change
        self.wake_phrase = wake_phrase.lower().strip()

        self._running = False
        self._active_session = None
        self._input_stream = None
        self._player = get_audio_player()
        self._player.on_playback_state = self._on_playback_state_changed
        self._local_barge_in_active = False
        self._last_barge_in_time: float = 0.0
        self._main_task: Optional[asyncio.Task] = None
        self._send_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._state = "idle"
        self._engagement_state = "awake"
        self._last_user_voice = 0.0
        self._last_interaction_time = time.monotonic()
        self._session_active = False
        self._session_count = 0
        self._audio_streaming_allowed = asyncio.Event()
        self._audio_streaming_allowed.set()

        config = get_config()
        self._silence_timeout = getattr(config.voice, "engagement_silence_timeout", 45.0)

        # Wire router's sleep callback to this provider
        self.router.on_sleep_requested = self.go_to_sleep

        # Lazy wake-word detector for idle phase
        self._wake_detector = None

    def _on_playback_state_changed(self, is_playing: bool) -> None:
        """Called when AudioPlayer starts or finishes physical audio rendering."""
        if not is_playing and self._state == "speaking":
            logger.debug("Playback queue drained; transitioning speaking -> listening")
            self._set_state("listening", "Futaba listening")
            now = time.monotonic()
            self._last_user_voice = now
            self._last_interaction_time = now

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_listening(self) -> bool:
        return self._state in ("listening", "speaking", "thinking", "tool_call", "executing")

    @property
    def state(self) -> str:
        return self._state

    @property
    def engagement_state(self) -> str:
        return self._engagement_state

    def wake_up(self) -> None:
        """Wake Futaba into active conversational listening."""
        self._engagement_state = "awake"
        self._last_interaction_time = time.monotonic()
        self._set_state("listening", "Futaba awake and listening")
        logger.info("Engagement state transitioned: DORMANT -> AWAKE")

    def go_to_sleep(self) -> None:
        """Put Futaba into dormant sleep state."""
        self._engagement_state = "dormant"
        self._set_state("idle", "Futaba dormant — say 'Hey Futaba'")
        logger.info("Engagement state transitioned: AWAKE -> DORMANT")

    def _set_state(self, state: str, detail: str = "") -> None:
        """Set voice state and notify HUD/subscribers."""
        self._state = state
        logger.debug("Gemini Live state: %s (%s)", state, detail)
        if self.on_state_change:
            try:
                self.on_state_change(state, detail)
            except Exception as e:
                logger.debug("State callback error: %s", e)

    def _get_api_key(self) -> str | None:
        """Retrieve Gemini API key from Windows Credential Store or environment."""
        cred_store = CredentialStore()
        key = cred_store.get("provider:gemini")
        if not key:
            import os
            key = os.environ.get("GEMINI_API_KEY")
        return key

    async def start(self) -> bool:
        """Start the Gemini Live voice interface service."""
        if self._running:
            return True

        api_key = self._get_api_key()
        if not api_key:
            logger.warning("Gemini API key not configured in Windows Credential Store. Gemini Live unavailable.")
            self._set_state("error", "API key missing")
            return False

        config = get_config()

        # Select devices
        in_device = None
        out_device = None
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            if config.voice.microphone_device:
                for i, d in enumerate(devices):
                    if config.voice.microphone_device.lower() in d["name"].lower() and d["max_input_channels"] > 0:
                        in_device = i
                        break
            if config.voice.speaker_device:
                for i, d in enumerate(devices):
                    if config.voice.speaker_device.lower() in d["name"].lower() and d["max_output_channels"] > 0:
                        out_device = i
                        break
        except Exception as e:
            logger.warning("Device resolution error: %s", e)

        # Initialize audio player
        self._player.device = out_device
        if not self._player.start():
            logger.warning("Could not start audio output player.")

        # Initialize wake detector for low-CPU idle monitoring
        from futaba.voice.pipeline import WakeWordDetector
        self._wake_detector = WakeWordDetector(
            wake_phrase=self.wake_phrase,
            sample_rate=INPUT_SAMPLE_RATE,
            sensitivity=config.voice.activation_sensitivity,
        )

        self._running = True
        self._set_state("idle", "Waiting for 'Hey Futaba'")

        # Start background coordinator loop
        self._main_task = asyncio.create_task(self._coordinator_loop(in_device))
        logger.info("Gemini Live voice provider initialized and running.")
        return True

    async def stop(self) -> None:
        """Stop Gemini Live voice interface."""
        self._running = False
        self._session_active = False

        if self._input_stream:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None

        self._player.stop()

        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

        self._set_state("idle", "Stopped")
        logger.info("Gemini Live voice provider stopped.")

    async def respond(self, text: str) -> None:
        """Speak a text response if in active session."""
        if self._active_session:
            try:
                from google.genai import types
                await self._active_session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=f"[System Notification for user: {text}]")]),
                    turn_complete=True,
                )
            except Exception as e:
                logger.warning("Failed to inject text into Gemini Live session: %s", e)

    # -----------------------------------------------------------------------
    # Main Coordinator Loop
    # -----------------------------------------------------------------------

    async def _coordinator_loop(self, in_device: Optional[int]) -> None:
        """Main coordinator: maintain active real-time Gemini Live session."""
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=100)

        def mic_callback(indata, frames, time_info, status):
            if status:
                logger.debug("Mic status: %s", status)
            try:
                audio_queue.put_nowait(indata.copy().flatten())
            except queue.Full:
                pass

        try:
            self._input_stream = sd.InputStream(
                samplerate=INPUT_SAMPLE_RATE,
                channels=INPUT_CHANNELS,
                dtype="int16",
                blocksize=INPUT_CHUNK_SIZE,
                device=in_device,
                callback=mic_callback,
            )
            self._input_stream.start()
            logger.info("Microphone input stream active on device %s", in_device if in_device is not None else "default")
        except Exception as e:
            logger.error("Failed to start microphone input stream: %s", e)
            self._set_state("error", f"Microphone error: {e}")
            return

        backoff = 1.0
        while self._running:
            try:
                self._session_active = True
                await self._run_live_session(audio_queue)
                # Session ended normally (e.g. server closed stream or timeout)
                # Pause 1s before reconnecting so we maintain availability without tight looping
                backoff = 1.0
                if self._running:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Gemini Live session interrupted: %s. Reconnecting in %.1fs...", e, backoff)
                self._set_state("reconnecting", f"Reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 15.0)

    # -----------------------------------------------------------------------
    # Live Session Management
    # -----------------------------------------------------------------------

    async def _run_live_session(self, audio_queue: queue.Queue[np.ndarray]) -> None:
        """Run a bidirectional realtime Gemini Live session."""
        from google import genai
        from google.genai import types

        api_key = self._get_api_key()
        if not api_key:
            self._session_active = False
            return

        config = get_config()
        model_name = config.voice.gemini_live_model or "gemini-3.8-live"
        voice_name = config.voice.gemini_live_voice or "Aoede"

        self._session_count += 1
        session_id = self._session_count
        session_start = time.monotonic()

        global _active_live_sessions
        if _active_live_sessions > 0:
            logger.warning("[Session #%d] Gemini Live session already active (%d). Rejecting duplicate session.", session_id, _active_live_sessions)
            return
        _active_live_sessions += 1

        client = genai.Client(api_key=api_key)

        # System instructions enforcing Futaba personality, continuous assistant behavior,
        # wake response, multi-turn context, and strict tool execution boundaries
        custom_prompt = getattr(config.voice, "personality_system_prompt", "").strip()
        intensity = getattr(config.voice, "personality_intensity", "medium").lower().strip()
        if custom_prompt:
            system_instruction = custom_prompt
        else:
            if intensity == "low":
                sarcasm_guide = "Your tone is direct, professional, observant, with very subtle, dry wit."
            elif intensity == "high":
                sarcasm_guide = "Your tone is sharp, witty, highly sarcastic, playfully cynical, and curious, but NEVER mean-spirited."
            else:  # medium
                sarcasm_guide = "You are concise, confident, subtly sarcastic, observant, and occasionally playful."

            system_instruction = (
                f"You are FUTABA, an autonomous Windows AI copilot with the personality of a curious, scientifically minded assistant. "
                f"{sarcasm_guide} "
                f"You speak naturally and informally with native spoken voice. "
                f"You genuinely try to help the user accomplish things on their PC. Your sarcasm is light and NEVER interferes with execution. "
                f"When asked to perform an action, perform it first and only claim success after verification. "
                f"Maintain conversational context across turns. Ask concise follow-up questions only when required information is missing. "
                f"Stay engaged during an active conversation instead of requiring the wake word before every sentence.\n\n"
                f"CONVERSATIONAL & EXECUTION RULES:\n"
                f"1. Wake acknowledgement: When the user addresses you ('Hey Futaba', 'Hay Futaba', 'Futaba'), acknowledge immediately with personality "
                f"(e.g. 'What\\'s up?', 'Futaba here. Ready.', 'Yeah? What are we testing today?', 'Listening.'). "
                f"If they say 'Hey Futaba' alone, acknowledge and wait for their command without executing anything. "
                f"If they say 'Hey Futaba, [command]' in one sentence (e.g. 'Hey Futaba, open Brave'), acknowledge briefly and execute immediately.\n"
                f"2. Continuous conversation: After answering or completing a command, DO NOT go to sleep immediately. REMAIN AWAKE AND LISTENING. "
                f"The user will speak subsequent commands directly without saying 'Hey Futaba' (e.g. 'Open Brave', then 'Open YouTube', then 'Search for Techno Gamerz'). "
                f"Execute each subsequent command immediately in the same continuous session.\n"
                f"3. Multi-turn context awareness: Remember previous applications, windows, and websites. If you opened Brave, a follow-up 'Open YouTube' means navigate Brave to YouTube, "
                f"NOT launching a second browser window. Use navigate_browser or open_application intelligently.\n"
                f"4. Sleep / Dormant mode: When the user says 'Go to sleep', 'Stop listening', 'Good night', 'Dismiss', or 'That\\'s all for now', "
                f"CALL the tool 'go_to_sleep', and say a short dry sign-off (e.g. 'Entering sleep mode. Wake me when you need me.', 'Dormant. Have fun being productive.').\n"
                f"5. STRICT TOOL EXECUTION RULE: Whenever the user asks to open an app, navigate, search, type, click, or perform any action, "
                f"YOU MUST CALL THE APPROPRIATE TOOL (open_application, navigate_browser, submit_task, search_web, get_active_window, get_screen_context, go_to_sleep). "
                f"DO NOT CONFUSE SPEAKING WITH EXECUTION: If the user says 'Type Hello World into Notepad', DO NOT just verbally say 'Hello World'. "
                f"You must invoke submit_task to actually type it into Notepad! Sarcasm or conversation must NEVER replace tool execution.\n"
                f"6. Concise post-action confirmations: After receiving the tool result, give a short, punchy confirmation "
                f"(e.g. 'Done. Opened Brave.', 'YouTube is ready.', 'Typed and verified in Notepad.'). Keep verbal responses concise (1-2 sentences).\n"
                f"7. Screen Questions: When the user asks 'What am I looking at?' or 'What is on my screen?', "
                f"CALL the tool 'get_screen_context'. Do NOT attempt to launch an application named after their question! Summarize what is focused and visible concisely."
            )

        speech_config = types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        )

        connect_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=speech_config,
            system_instruction=types.Content(parts=[types.Part(text=system_instruction)]),
            tools=self.router.get_tool_declarations(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(target_tokens=4000)
            ),
        )

        logger.info("=== [Session #%d] Opening Gemini Live WebSocket session (model=%s, voice=%s) ===", session_id, model_name, voice_name)

        try:
            async with client.aio.live.connect(model=model_name, config=connect_config) as session:
                self._active_session = session
                self._audio_streaming_allowed.set()
                self._set_state("listening", "Futaba listening — say 'Hey Futaba'")
                self._last_user_voice = time.monotonic()
                logger.info("=== [Session #%d] Gemini Live Session Connected ===", session_id)

                # Launch audio sender task
                send_task = asyncio.create_task(self._audio_stream_sender(session, audio_queue, session_id))

                try:
                    turn_active = False
                    # Persistent multi-turn receive loop across the entire session lifecycle
                    while self._running and self._session_active:
                        turn_chunks_received = 0
                        async for chunk in session.receive():
                            if not self._running or not self._session_active:
                                break
                            turn_chunks_received += 1

                            # 1. Check for user interruption of assistant speech (server-side VAD)
                            if chunk.server_content:
                                if chunk.server_content.interrupted:
                                    barge_in_confirmed = self._local_barge_in_active or (time.monotonic() - self._last_barge_in_time < 3.0)
                                    if barge_in_confirmed:
                                        logger.info("[Session #%d] Gemini Live: User interrupted assistant speech (confirmed barge-in).", session_id)
                                        self._player.interrupt(reason="user_barge_in")
                                        self._set_state("interrupted", "User interrupted")
                                    else:
                                        logger.info("[Session #%d] Suppressed spurious server VAD interruption (speaker bleed echo; no user barge-in detected).", session_id)
                                    continue

                                # 2. Assistant audio output chunks
                                turn = chunk.server_content.model_turn
                                if turn:
                                    if not turn_active:
                                        self._player.start_response()
                                        turn_active = True
                                        self._set_state("speaking", "Futaba speaking")

                                    for part in turn.parts:
                                        if part.inline_data and part.inline_data.data:
                                            self._player.play_chunk(part.inline_data.data)

                                if chunk.server_content.turn_complete:
                                    logger.debug("[Session #%d] Gemini Live: Server turn completed (waiting for audio drain).", session_id)
                                    self._player.mark_turn_complete()
                                    turn_active = False
                                    # If audio output has already drained, transition immediately to listening
                                    if not self._player.is_playing():
                                        self._set_state("listening", "Futaba listening")
                                        now = time.monotonic()
                                        self._last_user_voice = now
                                        self._last_interaction_time = now
                                        self._engagement_state = "awake"

                            # 3. Tool call execution
                            if chunk.tool_call:
                                self._set_state("tool_call", "Executing requested action")
                                # CRITICAL: Pause microphone streaming during tool execution!
                                # The Gemini Live API drops connections with 1008 if realtime_input
                                # is received while a tool response is pending.
                                self._audio_streaming_allowed.clear()
                                tool_responses = []

                                try:
                                    for call in chunk.tool_call.function_calls:
                                        logger.info("[Session #%d] Dispatching tool: %s (%s)", session_id, call.name, call.args)
                                        self._set_state("executing", f"Running {call.name}")

                                        # Execute via command router (Hermes/Windows)
                                        result = await self.router.execute_tool_call(call.name, call.args)

                                        tool_responses.append(
                                            types.FunctionResponse(
                                                name=call.name,
                                                response=result,
                                                id=call.id,
                                            )
                                        )

                                    # Send results back to Gemini Live
                                    self._set_state("thinking", "Reporting tool results back to Gemini")
                                    await session.send_tool_response(function_responses=tool_responses)
                                finally:
                                    # Resume audio streaming once tool response has been sent
                                    self._audio_streaming_allowed.set()

                        # If receive generator exited without any chunks, check if socket closed
                        if turn_chunks_received == 0:
                            if hasattr(session, "_ws") and getattr(session._ws, "closed", False):
                                logger.info("[Session #%d] WebSocket closed by server.", session_id)
                                break
                            await asyncio.sleep(0.05)

                finally:
                    self._audio_streaming_allowed.clear()
                    send_task.cancel()
                    try:
                        await send_task
                    except asyncio.CancelledError:
                        pass
                    uptime = time.monotonic() - session_start
                    logger.info("=== [Session #%d] Gemini Live Session Closed (uptime=%.1fs) ===", session_id, uptime)

        except Exception as e:
            logger.warning("[Session #%d] Gemini Live session closed with error: %s", session_id, e)
            raise
        finally:
            _active_live_sessions = max(0, _active_live_sessions - 1)
            self._active_session = None
            self._session_active = False

    async def _audio_stream_sender(
        self,
        session: Any,
        audio_queue: queue.Queue[np.ndarray],
        session_id: int = 0,
    ) -> None:
        """Stream microphone PCM chunks to Gemini Live WebSocket with speaker bleed suppression."""
        from google.genai import types

        BARGE_IN_THRESHOLD = 140.0
        BARGE_IN_CONSECUTIVE_FRAMES = 3
        consecutive_barge_in_frames = 0
        last_watchdog_check = time.monotonic()

        while self._running and self._session_active:
            try:
                now = time.monotonic()
                if now - last_watchdog_check > 1.0:
                    self._player.check_watchdog()
                    last_watchdog_check = now

                # Gather available chunks from mic
                chunks = []
                while not audio_queue.empty():
                    chunks.append(audio_queue.get_nowait())

                if chunks:
                    combined = np.concatenate(chunks)
                    pcm_bytes = combined.astype(np.int16).tobytes()

                    energy = np.sqrt(np.mean(combined.astype(np.float64) ** 2))
                    is_speaking = self._player.is_playing()

                    if is_speaking:
                        # Assistant is speaking through the speakers.
                        # Microphone picks up speaker bleed (typically 20-80 RMS).
                        # Only send audio to Gemini if user is deliberately shouting / barging in.
                        if energy > BARGE_IN_THRESHOLD:
                            consecutive_barge_in_frames += 1
                            if consecutive_barge_in_frames >= BARGE_IN_CONSECUTIVE_FRAMES:
                                self._local_barge_in_active = True
                                self._last_barge_in_time = time.monotonic()
                                logger.info(
                                    "[Session #%d] Confirmed user barge-in (energy=%.1f > %.1f across %d frames)",
                                    session_id, energy, BARGE_IN_THRESHOLD, consecutive_barge_in_frames
                                )
                                self._player.interrupt(reason="user_barge_in")
                                self._set_state("interrupted", "User interrupted")
                                if self._audio_streaming_allowed.is_set() and self._session_active:
                                    await session.send_realtime_input(
                                        audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
                                    )
                        else:
                            consecutive_barge_in_frames = 0
                            if time.monotonic() - self._last_barge_in_time > 1.5:
                                self._local_barge_in_active = False
                            # Suppress mic audio: do NOT stream speaker bleed into Gemini Live!
                    else:
                        consecutive_barge_in_frames = 0
                        if time.monotonic() - self._last_barge_in_time > 1.5:
                            self._local_barge_in_active = False

                        if energy > 25.0:
                            self._last_user_voice = now
                            if self._engagement_state == "awake":
                                self._last_interaction_time = now

                        if self._audio_streaming_allowed.is_set() and self._session_active:
                            await session.send_realtime_input(
                                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
                            )

                # Check conversational silence timeout in awake state (45 seconds)
                if self._engagement_state == "awake":
                    if time.monotonic() - self._last_interaction_time > self._silence_timeout:
                        logger.info("Silence timeout (%.1fs) reached without speech. Returning to DORMANT.", self._silence_timeout)
                        self.go_to_sleep()

                await asyncio.sleep(0.04)  # ~25 fps streaming chunks
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[Session #%d] Audio sender error: %s", session_id, e)
                await asyncio.sleep(0.05)
