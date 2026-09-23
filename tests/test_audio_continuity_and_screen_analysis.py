"""
Automated regression tests for Audio Continuity & Screen Analysis Crash Fix.

Verifies:
1. AudioPlayer unbounded queue does not drop chunks on large bursts.
2. AudioPlayer is_playing accurately tracks queue draining.
3. Speaker bleed suppression vs genuine user barge-in detection.
4. ScreenContextProvider safe execution and error boundaries (never crashes).
5. Screen query semantic intent classification (SCREEN_UNDERSTANDING).
6. Task state machine transition idempotence (FAILED -> FAILED is a safe no-op).
"""

import asyncio
import numpy as np
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from futaba.voice.gemini_live import AudioPlayer, get_audio_player, GeminiLiveVoiceProvider
from futaba.system.screen_provider import ScreenContextProvider, get_screen_provider, ScreenAnalysisResult
from futaba.system.context_tracker import classify_intent, UserIntent
from futaba.tasks.task_manager import Task, TaskState, TaskManager, InvalidTransitionError


class TestAudioContinuity:
    """Test AudioPlayer unbounded jitter buffer and playback tracking."""

    def test_audio_player_unbounded_queue_no_drop(self):
        player = AudioPlayer()
        player._running = True

        # Send a massive burst of 1,000 chunks (each 1024 bytes)
        chunk_data = b"\x01\x00" * 512
        for _ in range(1000):
            player.play_chunk(chunk_data)

        # In the old code with maxsize=100, 900 chunks were silently dropped!
        # With unbounded queue, all 1000 must be preserved.
        assert player._queue.qsize() == 1000
        assert player._bytes_received == 1000 * len(chunk_data)
        assert player._chunks_received == 1000

    def test_audio_player_playback_state_and_draining(self):
        player = AudioPlayer()
        player._running = True
        state_changes = []
        player.on_playback_state = lambda playing: state_changes.append(playing)

        # 1. Start turn
        resp_id = player.start_response("test_turn_1")
        assert resp_id == "test_turn_1"
        assert player.is_playing() is True
        assert state_changes[-1] is True

        # 2. Queue 2 chunks
        chunk = b"\x00\x00" * 512
        player.play_chunk(chunk)
        player.play_chunk(chunk)
        assert player.is_playing() is True

        # 3. Mark turn complete (WebSocket finished transmission)
        player.mark_turn_complete()
        # Audio chunks are still in the queue, so is_playing() must STILL be True!
        assert player.is_playing() is True

        # 4. Drain queue simulating sounddevice callback consuming frames
        while not player._queue.empty():
            player._queue.get_nowait()
        with player._lock:
            player._current_chunk.clear()
            player._is_playing = False
            player._notify_completed()

        assert player.is_playing() is False
        assert state_changes[-1] is False
        assert player._interrupted is False

    def test_audio_player_interrupt(self):
        player = AudioPlayer()
        player._running = True
        player.start_response("test_turn_2")
        player.play_chunk(b"\x05" * 1000)

        player.interrupt(reason="user_barge_in")
        assert player.is_playing() is False
        assert player._queue.empty() is True
        assert player._interrupted is True
        assert player._interruption_reason == "user_barge_in"


class TestSpeakerBleedAndBargeIn:
    """Test speaker bleed suppression and user barge-in detection."""

    @pytest.mark.asyncio
    async def test_speaker_bleed_suppressed_during_playback(self):
        router_mock = MagicMock()
        provider = GeminiLiveVoiceProvider(command_router=router_mock)
        provider._running = True
        provider._session_active = True
        provider._audio_streaming_allowed.set()

        session_mock = AsyncMock()
        audio_queue = asyncio.Queue()

        # Simulate assistant speaking
        provider._player.start_response("speak_turn")
        assert provider._player.is_playing() is True

        # Create low-energy speaker bleed audio chunk (RMS ~ 40.0)
        bleed_chunk = np.full(1024, 40, dtype=np.int16)

        # Run sender loop for one iteration
        audio_queue_sync = MagicMock()
        audio_queue_sync.empty.side_effect = [False, True]
        audio_queue_sync.get_nowait.return_value = bleed_chunk

        sender_task = asyncio.create_task(
            provider._audio_stream_sender(session_mock, audio_queue_sync, session_id=1)
        )
        await asyncio.sleep(0.08)
        provider._running = False
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass

        # Because Futaba was speaking and energy (40) was below barge-in threshold (140),
        # mic audio must NOT have been sent to Gemini Live!
        assert session_mock.send_realtime_input.call_count == 0
        assert provider._local_barge_in_active is False

    @pytest.mark.asyncio
    async def test_genuine_barge_in_triggers_interruption(self):
        import queue
        router_mock = MagicMock()
        provider = GeminiLiveVoiceProvider(command_router=router_mock)
        provider._running = True
        provider._session_active = True
        provider._audio_streaming_allowed.set()

        session_mock = AsyncMock()

        # Simulate assistant speaking
        provider._player.start_response("speak_turn")
        assert provider._player.is_playing() is True

        # High-energy barge-in audio (RMS ~ 500)
        loud_chunk = np.full(1024, 500, dtype=np.int16)

        audio_queue_sync = queue.Queue()
        sender_task = asyncio.create_task(
            provider._audio_stream_sender(session_mock, audio_queue_sync, session_id=1)
        )

        # Feed 4 consecutive loud chunks across sender ticks (> 3 consecutive high energy frames)
        for _ in range(4):
            audio_queue_sync.put(loud_chunk)
            await asyncio.sleep(0.06)

        provider._running = False
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass

        # High sustained energy confirmed barge-in:
        # Player was interrupted, and realtime input was transmitted to server
        assert provider._local_barge_in_active is True
        assert provider._player.is_playing() is False
        assert session_mock.send_realtime_input.call_count >= 1


class TestScreenPerceptionAndCrashResilience:
    """Test ScreenContextProvider safety and crash prevention."""

    @pytest.mark.asyncio
    async def test_screen_provider_analyze_never_crashes(self):
        provider = get_screen_provider()
        res = await provider.analyze(query="What is on my screen?")

        assert isinstance(res, ScreenAnalysisResult)
        assert res.success is True
        assert len(res.summary) > 0
        assert "active_window" in res.to_dict()
        assert "summary" in res.to_dict()

    @pytest.mark.asyncio
    async def test_screen_provider_with_simulated_grab_failure(self):
        provider = ScreenContextProvider()

        # Simulate PIL ImageGrab completely failing with OSError
        with patch("PIL.ImageGrab.grab", side_effect=OSError("Display context locked")):
            res = await provider.analyze(query="Analyze screen")
            assert res.success is True
            assert res.visual_captured is False
            assert "Visual screenshot capture is currently in fallback mode" in res.summary

    def test_screen_intent_classification(self):
        # All screen analysis queries must classify to SCREEN_UNDERSTANDING
        queries = [
            "analyze my current screen",
            "analyze the screen",
            "what am i watching",
            "what is playing",
            "what's on my screen",
            "what am i looking at",
            "describe the screen",
            "what video is this",
        ]
        for q in queries:
            assert classify_intent(q) == UserIntent.SCREEN_UNDERSTANDING, f"Failed for '{q}'"

        # Application launches must still classify to APPLICATION_CONTROL
        assert classify_intent("open notepad") == UserIntent.APPLICATION_CONTROL
        assert classify_intent("launch calculator") == UserIntent.APPLICATION_CONTROL

        # Browser navigation must classify to BROWSER_NAVIGATION
        assert classify_intent("open youtube") == UserIntent.BROWSER_NAVIGATION


class TestTaskStateMachineIdempotence:
    """Test state machine idempotence and recovery."""

    def test_task_transition_idempotence(self):
        task = Task(user_request="Test task")
        task.transition_to(TaskState.PLANNING)
        task.transition_to(TaskState.RUNNING)
        task.transition_to(TaskState.FAILED, "Original failure")

        # Second transition to FAILED must be a safe no-op, NOT raise InvalidTransitionError
        task.transition_to(TaskState.FAILED, "Duplicate failure notice")
        assert task.state == TaskState.FAILED

    @pytest.mark.asyncio
    async def test_task_manager_fail_task_idempotent(self, tmp_path):
        from futaba.tasks.task_manager import TaskJournal
        journal = TaskJournal(base_dir=tmp_path)
        tm = TaskManager(journal=journal)
        task = await tm.create_task("Test task")
        await tm.transition(task.task_id, TaskState.RUNNING)
        await tm.fail_task(task.task_id, "Failure 1")

        # Calling fail_task again must succeed cleanly
        failed_again = await tm.fail_task(task.task_id, "Failure 2")
        assert failed_again.state == TaskState.FAILED
