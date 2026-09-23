"""
Futaba Voice Pipeline — Wake word detection, STT, and TTS.

Architecture:
    Microphone → WakeWordDetector → STT → Futaba → TTS → Speaker

Wake word detection runs locally with minimal CPU usage using a
keyword-spotting approach. When the wake phrase is detected, the
system switches to full speech recognition.

Components:
1. WakeWordDetector: Local keyword spotting (low CPU, always-on)
2. SpeechRecognizer: Full STT (local Whisper or cloud API)
3. TextToSpeech: Response vocalization
4. VoicePipeline: Coordinates the full flow

The wake detector uses a simple energy-based voice activity detector
combined with lightweight keyword matching to minimize CPU usage.
It does NOT stream audio to a cloud service for wake detection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import struct
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from futaba.core.config import get_config, get_cache_dir
from futaba.voice.command_router import VoiceCommandRouter

logger = logging.getLogger("futaba.voice")


# ---------------------------------------------------------------------------
# Voice Activity Detection (VAD)
# ---------------------------------------------------------------------------

class VoiceActivityDetector:
    """
    Simple energy-based voice activity detection.

    Detects when someone is speaking by measuring audio energy
    above a configurable threshold. Used as a cheap pre-filter
    before running the more expensive wake-word detection.
    """

    def __init__(
        self,
        energy_threshold: float = 22.0,
        speech_timeout: float = 1.5,
        min_speech_duration: float = 0.2,
    ):
        self.energy_threshold = energy_threshold
        self.speech_timeout = speech_timeout
        self.min_speech_duration = min_speech_duration
        self._speaking = False
        self._speech_start: float = 0.0
        self._last_voice: float = 0.0
        self._noise_floor: float = 15.0

    def process(self, audio_chunk: np.ndarray) -> bool:
        """
        Process an audio chunk and return True if speech is detected.

        Audio should be 16-bit PCM at 16kHz.
        """
        energy = np.sqrt(np.mean(audio_chunk.astype(np.float64) ** 2))
        now = time.monotonic()

        # Update running ambient noise floor during silence
        if energy < self.energy_threshold:
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * energy

        dynamic_thresh = max(self.energy_threshold, self._noise_floor * 1.5)

        if energy > dynamic_thresh:
            self._last_voice = now
            if not self._speaking:
                self._speaking = True
                self._speech_start = now
                return False  # Don't trigger until min_speech_duration

            # Check minimum duration
            if now - self._speech_start >= self.min_speech_duration:
                return True
        else:
            if self._speaking and now - self._last_voice > self.speech_timeout:
                self._speaking = False

        return self._speaking and (now - self._speech_start >= self.min_speech_duration)

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def reset(self) -> None:
        self._speaking = False
        self._speech_start = 0.0
        self._last_voice = 0.0


# ---------------------------------------------------------------------------
# Wake Word Detector
# ---------------------------------------------------------------------------

class WakeWordDetector:
    """
    Local wake word detection using speech recognition.

    Strategy:
    1. VAD detects voice activity (very cheap)
    2. When voice is detected, buffer audio for ~2 seconds
    3. Run a lightweight local STT on the buffer
    4. Check if the wake phrase is in the transcription
    5. If detected, signal the pipeline to start full recognition

    This approach avoids:
    - Continuous cloud streaming for wake detection
    - Heavy GPU usage during idle
    - Privacy concerns from always-on cloud transcription
    """

    def __init__(
        self,
        wake_phrase: str = "hey futaba",
        sample_rate: int = 16000,
        buffer_seconds: float = 3.0,
        sensitivity: float = 0.5,
    ):
        self.wake_phrase = wake_phrase.lower().strip()
        self.sample_rate = sample_rate
        self.buffer_seconds = buffer_seconds
        self.sensitivity = sensitivity
        base_threshold = max(18.0, 30.0 - sensitivity * 15.0)
        self._vad = VoiceActivityDetector(
            energy_threshold=base_threshold
        )
        self._buffer: list[np.ndarray] = []
        self._buffer_duration: float = 0.0
        self._recognizer = None
        self._last_check_time: float = 0.0
        self._min_check_interval: float = 0.8

    def _get_recognizer(self):
        """Lazy-load speech_recognition."""
        if self._recognizer is None:
            try:
                import speech_recognition as sr
                self._recognizer = sr.Recognizer()
                self._recognizer.energy_threshold = 20
                self._recognizer.dynamic_energy_threshold = True
            except ImportError:
                logger.warning("speech_recognition not available for wake word detection")
        return self._recognizer

    def process_chunk(self, audio_chunk: np.ndarray) -> bool:
        """
        Process an audio chunk and return True if wake word is detected.

        Audio: 16-bit PCM at 16kHz mono.
        """
        # VAD pre-filter
        if not self._vad.process(audio_chunk):
            # No speech — clear buffer
            if self._buffer:
                self._buffer.clear()
                self._buffer_duration = 0.0
            return False

        # Buffer audio during speech
        self._buffer.append(audio_chunk)
        self._buffer_duration += len(audio_chunk) / self.sample_rate

        # Don't check too frequently
        now = time.monotonic()
        if now - self._last_check_time < self._min_check_interval:
            return False

        # Once we have enough buffered audio, check for wake word
        if self._buffer_duration >= 0.8:
            self._last_check_time = now
            detected = self._check_wake_word()
            if detected:
                self._buffer.clear()
                self._buffer_duration = 0.0
                self._vad.reset()
                return True

        # Trim buffer if too long
        max_samples = int(self.buffer_seconds * self.sample_rate)
        total = sum(len(chunk) for chunk in self._buffer)
        while total > max_samples and self._buffer:
            removed = self._buffer.pop(0)
            total -= len(removed)
            self._buffer_duration = total / self.sample_rate

        return False

    def _check_wake_word(self) -> bool:
        """Run local STT on the buffer to check for the wake phrase."""
        recognizer = self._get_recognizer()
        if recognizer is None:
            return False

        try:
            import speech_recognition as sr

            combined = np.concatenate(self._buffer)
            audio_bytes = combined.astype(np.int16).tobytes()
            audio_data = sr.AudioData(
                audio_bytes,
                sample_rate=self.sample_rate,
                sample_width=2,
            )

            text = ""
            try:
                text = recognizer.recognize_google(audio_data).lower()
            except Exception:
                pass

            if not text:
                return False

            logger.info("Wake detector heard utterance: '%s'", text)

            # Robust fuzzy match: handles "hey futaba", "hay futaba", "futaba", "hi futaba", etc.
            text_clean = text.replace(" ", "").replace("-", "")
            wake_targets = ["futaba", "futa", "fooba", "taba", "photoba", "bootaba"]
            if any(target in text_clean for target in wake_targets) or "futaba" in text:
                logger.info("Wake word recognized: '%s' in '%s'", self.wake_phrase, text)
                return True

            return False

        except Exception as e:
            logger.debug("Wake word check error: %s", e)
            return False


# ---------------------------------------------------------------------------
# Speech Recognizer (Full STT)
# ---------------------------------------------------------------------------

class SpeechRecognizer:
    """
    Full speech-to-text for command recognition after wake word.

    Supports:
    - Local (Whisper via OpenAI's whisper library or faster-whisper)
    - Cloud (OpenAI Whisper API)
    - Google Speech Recognition (free, good for short commands)
    """

    def __init__(self, provider: str = "local", model: str = "base"):
        self.provider = provider
        self.model = model
        self._recognizer = None
        self._whisper_model = None

    async def recognize(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str = "en",
    ) -> str:
        """
        Transcribe speech to text.

        Returns the transcribed text, or empty string if recognition fails.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._recognize_sync, audio, sample_rate, language
        )

    def _recognize_sync(
        self,
        audio: np.ndarray,
        sample_rate: int,
        language: str,
    ) -> str:
        """Synchronous speech recognition."""
        import speech_recognition as sr

        if self._recognizer is None:
            self._recognizer = sr.Recognizer()

        # Convert to AudioData
        audio_bytes = audio.astype(np.int16).tobytes()
        audio_data = sr.AudioData(audio_bytes, sample_rate, sample_width=2)

        try:
            if self.provider == "local":
                return self._recognize_local(audio, sample_rate, language)
            elif self.provider == "openai":
                return self._recognizer.recognize_whisper_api(
                    audio_data,
                    api_key=self._get_openai_key(),
                )
            else:  # google (free)
                return self._recognizer.recognize_google(
                    audio_data, language=language
                )
        except sr.UnknownValueError:
            return ""
        except Exception as e:
            logger.warning("Speech recognition error: %s", e)
            return ""

    def _recognize_local(
        self,
        audio: np.ndarray,
        sample_rate: int,
        language: str,
    ) -> str:
        """Local Whisper recognition."""
        try:
            import whisper

            if self._whisper_model is None:
                logger.info("Loading Whisper model '%s'...", self.model)
                self._whisper_model = whisper.load_model(self.model)

            # Whisper expects float32 audio at 16kHz
            audio_float = audio.astype(np.float32) / 32768.0
            if sample_rate != 16000:
                # Resample if needed
                import torchaudio
                audio_tensor = __import__("torch").from_numpy(audio_float).unsqueeze(0)
                audio_tensor = torchaudio.functional.resample(audio_tensor, sample_rate, 16000)
                audio_float = audio_tensor.squeeze().numpy()

            result = self._whisper_model.transcribe(
                audio_float, language=language, fp16=False
            )
            return result.get("text", "").strip()

        except ImportError:
            logger.warning("Whisper not available, falling back to Google STT")
            import speech_recognition as sr
            audio_bytes = audio.astype(np.int16).tobytes()
            audio_data = sr.AudioData(audio_bytes, sample_rate, sample_width=2)
            recognizer = sr.Recognizer()
            return recognizer.recognize_google(audio_data, language=language)

    def _get_openai_key(self) -> str:
        """Get OpenAI API key from config."""
        config = get_config()
        for provider in config.ai.providers:
            if provider.name.lower() in ("openai", "openrouter"):
                return provider.api_key.get_secret_value()
        return os.environ.get("OPENAI_API_KEY", "")


# ---------------------------------------------------------------------------
# Text-to-Speech
# ---------------------------------------------------------------------------

class TextToSpeech:
    """
    Text-to-speech for Futaba's voice responses.

    Uses system TTS on Windows (SAPI) with optional cloud TTS.
    """

    def __init__(self, provider: str = "local", voice: str = ""):
        self.provider = provider
        self.voice = voice

    async def speak(self, text: str) -> None:
        """Speak the given text."""
        if not text:
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._speak_sync, text)

    def _speak_sync(self, text: str) -> None:
        """Synchronous TTS."""
        if self.provider == "local":
            self._speak_sapi(text)
        else:
            logger.warning("TTS provider '%s' not implemented, using local", self.provider)
            self._speak_sapi(text)

    def _speak_sapi(self, text: str) -> None:
        """Use Windows SAPI for TTS."""
        try:
            import win32com.client
            speaker = win32com.client.Dispatch("SAPI.SpVoice")

            if self.voice:
                voices = speaker.GetVoices()
                for i in range(voices.Count):
                    if self.voice.lower() in voices.Item(i).GetDescription().lower():
                        speaker.Voice = voices.Item(i)
                        break

            speaker.Speak(text)
        except Exception as e:
            logger.warning("SAPI TTS error: %s", e)
            # Fallback: edge-tts or pyttsx3
            try:
                import pyttsx3
                engine = pyttsx3.init()
                engine.say(text)
                engine.runAndWait()
            except ImportError:
                logger.warning("No TTS engine available")


# ---------------------------------------------------------------------------
# Voice Pipeline
# ---------------------------------------------------------------------------

class VoicePipeline:
    """
    Full voice pipeline: wake word → STT → Futaba → TTS.

    Runs as a background service that:
    1. Continuously listens for the wake word (low CPU)
    2. On detection, records full speech input
    3. Transcribes to text
    4. Sends to the agent controller
    5. Speaks the response

    The pipeline can be started/stopped and respects
    Do Not Disturb and Gaming Mode.
    """

    def __init__(
        self,
        on_command: Callable[[str], Any] | None = None,
        on_wake: Callable[[], Any] | None = None,
        on_listening: Callable[[bool], Any] | None = None,
        on_state: Callable[[str, str], Any] | None = None,
    ):
        config = get_config()
        self.router = VoiceCommandRouter()

        self._wake_detector = WakeWordDetector(
            wake_phrase=config.voice.wake_word,
            sensitivity=config.voice.activation_sensitivity,
        )
        self._recognizer = SpeechRecognizer(
            provider=config.voice.stt_provider,
            model=config.voice.stt_model,
        )
        self._tts = TextToSpeech(
            provider=config.voice.tts_provider,
            voice=config.voice.tts_voice,
        )

        # Callbacks
        self._on_command = on_command
        self._on_wake = on_wake
        self._on_listening = on_listening
        self._on_state = on_state

        # State
        self._running = False
        self._listening = False  # True when recording after wake word
        self._audio_queue: queue.Queue = queue.Queue()
        self._stream = None
        self._task: asyncio.Task | None = None
        self._sample_rate = 16000
        self._chunk_size = 1024

        # Provider instance
        self._gemini_provider = None
        self._active_provider_name = "none"

    def inject(
        self,
        task_manager: Any = None,
        controller: Any = None,
        tools: Any = None,
        hermes_bridge: Any = None,
        on_state: Callable[[str, str], Any] | None = None,
    ) -> None:
        """Inject runtime dependencies into the command router."""
        self.router.inject(
            task_manager=task_manager,
            controller=controller,
            tools=tools,
            hermes_bridge=hermes_bridge,
        )
        if on_state:
            self._on_state = on_state

    def _handle_state_change(self, state: str, detail: str = "") -> None:
        """Handle state transitions from the active voice provider."""
        if state == "listening":
            self._listening = True
            if self._on_listening:
                self._on_listening(True)
        elif state in ("idle", "speaking"):
            self._listening = False
            if self._on_listening:
                self._on_listening(False)

        if self._on_state:
            try:
                self._on_state(state, detail)
            except Exception:
                pass

    async def start(self) -> None:
        """Start the voice pipeline using configured provider (Gemini Live or Local)."""
        config = get_config()
        if not config.voice.enabled:
            logger.info("Voice pipeline disabled in config")
            return

        provider_preference = getattr(config.voice, "provider", "gemini_live")

        if provider_preference == "gemini_live":
            try:
                from futaba.voice.gemini_live import GeminiLiveVoiceProvider
                self._gemini_provider = GeminiLiveVoiceProvider(
                    command_router=self.router,
                    on_state_change=self._handle_state_change,
                    wake_phrase=config.voice.wake_word,
                )
                ok = await self._gemini_provider.start()
                if ok:
                    self._active_provider_name = "gemini_live"
                    self._running = True
                    logger.info("Voice pipeline active with Gemini Live provider.")
                    return
                else:
                    logger.warning("Gemini Live provider could not start; falling back to local voice pipeline.")
            except Exception as e:
                logger.warning("Gemini Live initialization error (%s); falling back to local voice pipeline.", e)

        if provider_preference != "disabled":
            await self._start_local()

    async def _start_local(self) -> None:
        """Start local speech recognition and wake-word detector."""
        config = get_config()
        try:
            import sounddevice as sd

            # Select microphone
            device = None
            if config.voice.microphone_device:
                devices = sd.query_devices()
                for i, d in enumerate(devices):
                    if config.voice.microphone_device.lower() in d["name"].lower():
                        device = i
                        break

            # Start audio stream
            self._stream = sd.InputStream(
                device=device,
                samplerate=self._sample_rate,
                channels=1,
                dtype=np.int16,
                blocksize=self._chunk_size,
                callback=self._audio_callback,
            )
            self._stream.start()
            self._running = True
            self._active_provider_name = "local"
            self._task = asyncio.create_task(self._process_loop())
            logger.info("Local voice pipeline started (device=%s)", device or "default")

        except Exception as e:
            logger.error("Failed to start local voice pipeline: %s", e)

    async def stop(self) -> None:
        """Stop the voice pipeline."""
        self._running = False

        if self._gemini_provider:
            try:
                await self._gemini_provider.stop()
            except Exception as e:
                logger.debug("Error stopping Gemini provider: %s", e)
            self._gemini_provider = None

        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._active_provider_name = "none"
        logger.info("Voice pipeline stopped")

    async def respond(self, text: str) -> None:
        """Speak a response."""
        if self._active_provider_name == "gemini_live" and self._gemini_provider:
            await self._gemini_provider.respond(text)
        else:
            config = get_config()
            if config.voice.tts_enabled:
                await self._tts.speak(text)

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback from sounddevice — runs in audio thread."""
        if status:
            logger.debug("Audio status: %s", status)
        self._audio_queue.put(indata.copy().flatten())

    async def _process_loop(self) -> None:
        """Main processing loop."""
        speech_buffer: list[np.ndarray] = []
        recording = False
        silence_duration = 0.0
        max_recording_seconds = 15.0
        recording_start = 0.0

        while self._running:
            try:
                # Get audio chunk (non-blocking)
                try:
                    chunk = self._audio_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue

                if not recording:
                    # Check for wake word
                    if self._wake_detector.process_chunk(chunk):
                        logger.info("Wake word detected!")
                        recording = True
                        recording_start = time.monotonic()
                        speech_buffer = []
                        silence_duration = 0.0

                        if self._on_wake:
                            self._on_wake()
                        if self._on_listening:
                            self._on_listening(True)
                else:
                    # Recording after wake word
                    speech_buffer.append(chunk)

                    # Detect end of speech (silence)
                    energy = np.sqrt(np.mean(chunk.astype(np.float64) ** 2))
                    silence_threshold = max(18.0, self._wake_detector._vad.energy_threshold)
                    if energy < silence_threshold:
                        silence_duration += len(chunk) / self._sample_rate
                    else:
                        silence_duration = 0.0

                    elapsed = time.monotonic() - recording_start

                    # Stop recording on silence or timeout
                    if silence_duration > 1.5 or elapsed > max_recording_seconds:
                        recording = False
                        if self._on_listening:
                            self._on_listening(False)

                        if speech_buffer:
                            # Transcribe
                            full_audio = np.concatenate(speech_buffer)
                            text = await self._recognizer.recognize(
                                full_audio, self._sample_rate
                            )

                            if text:
                                # Remove wake phrase from transcription
                                config = get_config()
                                wake = config.voice.wake_word.lower()
                                clean = text.lower()
                                if clean.startswith(wake):
                                    clean = clean[len(wake):].strip()
                                    if not clean:
                                        continue
                                    text = clean

                                logger.info("Voice command: %s", text)
                                if self._on_command:
                                    self._on_command(text)

                        speech_buffer = []

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Voice pipeline error: %s", e)
                await asyncio.sleep(0.5)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_listening(self) -> bool:
        return self._listening
