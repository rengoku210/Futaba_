"""
Futaba Video Pipeline — Transform instructional videos into executable tasks.

This is a multi-stage pipeline that:
1. Acquires video (local file, URL, screen recording)
2. Extracts audio and key frames
3. Transcribes speech
4. Analyzes visual content (OCR, UI elements, code, commands)
5. Detects actions and instructions
6. Reconstructs a timeline of demonstrated steps
7. Generates a structured workflow
8. Creates an execution plan for Hermes

Architecture:
    Video → VideoAcquirer → frames + audio
                                ↓
    AudioAnalyzer (transcription + timing)
    FrameAnalyzer (OCR + visual + UI detection)
                                ↓
    ActionExtractor (combine speech + visuals → actions)
                                ↓
    WorkflowGenerator (structured plan from actions)
                                ↓
    ExecutionPlan (for the AgentController)

Key insight: The difficult part is NOT extracting frames.
The difficult part is understanding WHAT the tutorial teaches
and translating it into RELIABLE EXECUTABLE ACTIONS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from futaba.core.config import get_config, get_cache_dir
from futaba.routing.model_router import ModelRouter, ChatMessage, ModelRequest, TaskComplexity, TaskCapability
from futaba.tasks.task_manager import ExecutionPlan, ExecutionStep

logger = logging.getLogger("futaba.video")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class VideoFrame:
    """A single extracted frame from the video."""
    timestamp: float  # seconds
    image_path: str
    text_content: str = ""  # OCR text
    ui_elements: list[dict] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    description: str = ""  # AI-generated description


@dataclass
class TranscriptSegment:
    """A segment of the audio transcription."""
    start: float
    end: float
    text: str
    speaker: str = ""
    is_instruction: bool = False
    is_narration: bool = False
    is_warning: bool = False


@dataclass
class DetectedAction:
    """An action detected from the video analysis."""
    timestamp: float
    description: str
    action_type: str = ""  # command, click, type, navigate, install, configure, code
    tool: str = ""         # terminal, browser, application, filesystem
    parameters: dict[str, Any] = field(default_factory=dict)
    source: str = ""       # speech, visual, both
    confidence: float = 0.0
    is_required: bool = True
    is_optional: bool = False
    prerequisites: list[str] = field(default_factory=list)


@dataclass
class VideoAnalysis:
    """Complete analysis of a video."""
    source: str = ""
    duration: float = 0.0
    frames: list[VideoFrame] = field(default_factory=list)
    transcript: list[TranscriptSegment] = field(default_factory=list)
    actions: list[DetectedAction] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    summary: str = ""
    workflow_description: str = ""


# ---------------------------------------------------------------------------
# Video Acquirer
# ---------------------------------------------------------------------------

class VideoAcquirer:
    """
    Acquire video from various sources and extract frames + audio.

    Requires ffmpeg for video processing.
    """

    def __init__(self):
        self._ffmpeg = self._find_ffmpeg()

    def _find_ffmpeg(self) -> str | None:
        """Find ffmpeg executable."""
        # Check PATH
        for name in ("ffmpeg", "ffmpeg.exe"):
            try:
                result = subprocess.run(
                    [name, "-version"],
                    capture_output=True,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode == 0:
                    return name
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

        # Check common locations
        common_paths = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Futaba" / "runtime" / "ffmpeg.exe",
            Path("C:/ffmpeg/bin/ffmpeg.exe"),
        ]
        for p in common_paths:
            if p.exists():
                return str(p)

        return None

    @property
    def is_available(self) -> bool:
        return self._ffmpeg is not None

    async def extract_frames(
        self,
        video_path: str,
        output_dir: str,
        fps: float = 0.5,  # 1 frame per 2 seconds by default
        max_frames: int = 100,
    ) -> list[VideoFrame]:
        """
        Extract key frames from a video.

        Uses scene-change detection to pick meaningful frames
        rather than fixed intervals.
        """
        if not self._ffmpeg:
            raise RuntimeError("ffmpeg not found")

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(parents=True, exist_ok=True)

        # Extract frames at specified FPS
        pattern = str(output_dir_path / "frame_%04d.jpg")
        cmd = [
            self._ffmpeg,
            "-i", video_path,
            "-vf", f"fps={fps},scale=1280:-1",
            "-q:v", "2",
            "-frames:v", str(max_frames),
            pattern,
            "-y",
        ]

        loop = asyncio.get_event_loop()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning("ffmpeg frame extraction warning: %s", stderr.decode()[:200])

        # Collect extracted frames
        frames = []
        for i, path in enumerate(sorted(output_dir_path.glob("frame_*.jpg"))):
            timestamp = i / fps
            frames.append(VideoFrame(
                timestamp=timestamp,
                image_path=str(path),
            ))

        logger.info("Extracted %d frames from %s", len(frames), video_path)
        return frames

    async def extract_audio(
        self,
        video_path: str,
        output_path: str,
    ) -> str:
        """Extract audio track as WAV."""
        if not self._ffmpeg:
            raise RuntimeError("ffmpeg not found")

        cmd = [
            self._ffmpeg,
            "-i", video_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            output_path,
            "-y",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError("Failed to extract audio")

        return output_path

    async def get_duration(self, video_path: str) -> float:
        """Get video duration in seconds."""
        if not self._ffmpeg:
            return 0.0

        cmd = [
            self._ffmpeg.replace("ffmpeg", "ffprobe"),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            stdout, _ = await proc.communicate()
            return float(stdout.decode().strip())
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# Audio Analyzer
# ---------------------------------------------------------------------------

class AudioAnalyzer:
    """Transcribe audio and classify segments."""

    def __init__(self, model_router: ModelRouter | None = None):
        self._router = model_router

    async def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        """Transcribe audio into timed segments."""
        segments = []

        try:
            import whisper
            model = whisper.load_model("base")
            result = model.transcribe(audio_path, word_timestamps=True)

            for seg in result.get("segments", []):
                segments.append(TranscriptSegment(
                    start=seg["start"],
                    end=seg["end"],
                    text=seg["text"].strip(),
                ))

        except ImportError:
            # Fallback: use speech_recognition with the WAV file
            import speech_recognition as sr
            recognizer = sr.Recognizer()

            with sr.AudioFile(audio_path) as source:
                audio = recognizer.record(source)
                try:
                    text = recognizer.recognize_google(audio)
                    segments.append(TranscriptSegment(
                        start=0, end=0, text=text,
                    ))
                except Exception:
                    logger.warning("Audio transcription failed")

        logger.info("Transcribed %d segments", len(segments))
        return segments


# ---------------------------------------------------------------------------
# Frame Analyzer
# ---------------------------------------------------------------------------

class FrameAnalyzer:
    """Analyze video frames using OCR and vision models."""

    def __init__(self, model_router: ModelRouter | None = None):
        self._router = model_router

    async def analyze_frame(self, frame: VideoFrame) -> VideoFrame:
        """Analyze a single frame for text, UI elements, and code."""
        # OCR
        frame.text_content = await self._ocr(frame.image_path)

        # Extract code blocks (heuristic: monospace text, indentation patterns)
        frame.code_blocks = self._extract_code(frame.text_content)

        # Extract terminal commands (lines starting with $ or >)
        frame.commands = self._extract_commands(frame.text_content)

        return frame

    async def _ocr(self, image_path: str) -> str:
        """Extract text from an image using OCR."""
        try:
            from PIL import Image
            import pytesseract
            image = Image.open(image_path)
            text = pytesseract.image_to_string(image)
            return text
        except ImportError:
            # Fallback: OpenCV + basic detection
            try:
                import cv2
                img = cv2.imread(image_path)
                if img is None:
                    return ""
                # Try EasyOCR as alternative
                try:
                    import easyocr
                    reader = easyocr.Reader(["en"])
                    results = reader.readtext(img)
                    return "\n".join([r[1] for r in results])
                except ImportError:
                    return ""
            except Exception:
                return ""

    def _extract_code(self, text: str) -> list[str]:
        """Extract code blocks from OCR text."""
        if not text:
            return []

        code_blocks = []
        lines = text.split("\n")
        current_block = []

        for line in lines:
            # Heuristic: lines that look like code
            stripped = line.strip()
            if (
                stripped.startswith(("import ", "from ", "def ", "class ", "if ",
                                     "for ", "while ", "return ", "print(", "const ",
                                     "let ", "var ", "function ", "npm ", "pip ",
                                     "git ", "cd ", "mkdir ", "echo "))
                or stripped.endswith(("{", "};", ");"))
                or "=" in stripped and not stripped.startswith("#")
            ):
                current_block.append(line)
            elif current_block:
                if len(current_block) >= 2:
                    code_blocks.append("\n".join(current_block))
                current_block = []

        if current_block and len(current_block) >= 2:
            code_blocks.append("\n".join(current_block))

        return code_blocks

    def _extract_commands(self, text: str) -> list[str]:
        """Extract terminal commands from text."""
        if not text:
            return []

        commands = []
        for line in text.split("\n"):
            stripped = line.strip()
            # Lines starting with common terminal prompts
            for prefix in ("$ ", "> ", "PS> ", "C:\\>", ">>> "):
                if stripped.startswith(prefix):
                    cmd = stripped[len(prefix):].strip()
                    if cmd:
                        commands.append(cmd)
                    break
        return commands


# ---------------------------------------------------------------------------
# Action Extractor
# ---------------------------------------------------------------------------

class ActionExtractor:
    """
    Combine transcript + frame analysis to extract demonstrated actions.

    This is the most complex part: understanding WHAT the video is
    teaching by correlating speech content with visual changes.
    """

    def __init__(self, model_router: ModelRouter | None = None):
        self._router = model_router

    async def extract(
        self,
        frames: list[VideoFrame],
        transcript: list[TranscriptSegment],
    ) -> list[DetectedAction]:
        """
        Extract actions from combined frame + transcript analysis.

        Uses the vision/reasoning model to understand what's being
        demonstrated and generate structured actions.
        """
        if not self._router:
            return self._extract_heuristic(frames, transcript)

        # Build a comprehensive context from frames and transcript
        context = self._build_context(frames, transcript)

        # Ask the vision model to analyze and extract actions
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "You are analyzing a video tutorial. Given the transcript and "
                    "frame descriptions, extract a list of actions the tutorial demonstrates.\n\n"
                    "For each action, determine:\n"
                    "- What is being done\n"
                    "- What tool is needed (terminal, browser, file editor, application)\n"
                    "- The exact command or action parameters\n"
                    "- Whether it's required or optional\n"
                    "- Prerequisites\n\n"
                    "Distinguish between:\n"
                    "- Narration (explanation, not an action)\n"
                    "- Instructions (things to do)\n"
                    "- Demonstrations (things being shown)\n"
                    "- Warnings (things to avoid)\n\n"
                    "Respond with a JSON array of actions:\n"
                    '[{"description": "...", "action_type": "command|click|type|navigate|install|configure|code", '
                    '"tool": "terminal|browser|application|filesystem", '
                    '"parameters": {"command": "...", "code": "...", "url": "..."}, '
                    '"is_required": true, "is_optional": false, '
                    '"prerequisites": ["..."]}]'
                ),
            ),
            ChatMessage(role="user", content=context),
        ]

        request = ModelRequest(
            role="planner",
            complexity=TaskComplexity.COMPLEX,
            capabilities=[TaskCapability.VISION, TaskCapability.REASONING],
        )

        try:
            response = await self._router.complete(messages, request, max_tokens=8192)
            actions_data = self._parse_json(response.content)

            actions = []
            for i, ad in enumerate(actions_data if isinstance(actions_data, list) else []):
                actions.append(DetectedAction(
                    timestamp=i * 10.0,  # Approximate
                    description=ad.get("description", ""),
                    action_type=ad.get("action_type", ""),
                    tool=ad.get("tool", ""),
                    parameters=ad.get("parameters", {}),
                    source="both",
                    confidence=0.8,
                    is_required=ad.get("is_required", True),
                    is_optional=ad.get("is_optional", False),
                    prerequisites=ad.get("prerequisites", []),
                ))

            return actions

        except Exception as e:
            logger.error("AI action extraction failed: %s", e)
            return self._extract_heuristic(frames, transcript)

    def _build_context(
        self,
        frames: list[VideoFrame],
        transcript: list[TranscriptSegment],
    ) -> str:
        """Build a text context from frames and transcript."""
        parts = ["# Video Analysis\n"]

        # Transcript
        if transcript:
            parts.append("## Transcript")
            for seg in transcript:
                parts.append(f"[{seg.start:.1f}s] {seg.text}")
            parts.append("")

        # Frame analysis
        if frames:
            parts.append("## Key Frames")
            for frame in frames:
                parts.append(f"\n### Frame at {frame.timestamp:.1f}s")
                if frame.text_content:
                    parts.append(f"Text: {frame.text_content[:500]}")
                if frame.commands:
                    parts.append(f"Commands: {', '.join(frame.commands)}")
                if frame.code_blocks:
                    for code in frame.code_blocks[:3]:
                        parts.append(f"Code:\n```\n{code[:300]}\n```")

        return "\n".join(parts)

    def _extract_heuristic(
        self,
        frames: list[VideoFrame],
        transcript: list[TranscriptSegment],
    ) -> list[DetectedAction]:
        """Fallback: extract actions using simple heuristics."""
        actions = []

        # Extract commands from frames
        for frame in frames:
            for cmd in frame.commands:
                actions.append(DetectedAction(
                    timestamp=frame.timestamp,
                    description=f"Run command: {cmd}",
                    action_type="command",
                    tool="terminal",
                    parameters={"command": cmd},
                    source="visual",
                    confidence=0.6,
                ))

        # Extract instructions from transcript
        instruction_keywords = [
            "install", "run", "create", "open", "click", "type",
            "navigate", "download", "configure", "build", "execute",
            "copy", "paste", "save", "add", "remove", "update",
        ]
        for seg in transcript:
            lower = seg.text.lower()
            for keyword in instruction_keywords:
                if keyword in lower:
                    actions.append(DetectedAction(
                        timestamp=seg.start,
                        description=seg.text,
                        action_type=keyword,
                        tool="terminal" if keyword in ("install", "run", "build", "execute") else "application",
                        source="speech",
                        confidence=0.5,
                    ))
                    break

        return actions

    def _parse_json(self, content: str) -> Any:
        """Parse JSON from model response."""
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines)

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(content[start:end])
                except json.JSONDecodeError:
                    pass
            return []


# ---------------------------------------------------------------------------
# Workflow Generator
# ---------------------------------------------------------------------------

class WorkflowGenerator:
    """
    Generate an ExecutionPlan from detected video actions.

    This translates the list of detected actions into a proper
    execution plan with dependencies, verification, and recovery.
    """

    def __init__(self, model_router: ModelRouter | None = None):
        self._router = model_router

    async def generate(
        self,
        analysis: VideoAnalysis,
    ) -> ExecutionPlan:
        """Generate an execution plan from video analysis."""
        plan = ExecutionPlan(
            objective=f"Reproduce workflow from video: {analysis.source}",
            context=analysis.summary,
            requirements=analysis.requirements,
        )

        # Convert actions to steps
        for i, action in enumerate(analysis.actions):
            if not action.is_required and action.is_optional:
                continue

            step = ExecutionStep(
                index=i,
                description=action.description,
                tool=action.tool or "terminal",
                action=action.action_type,
                parameters=action.parameters,
                verification=f"Verify: {action.description}",
                recovery_strategy="retry" if action.action_type == "command" else "skip",
                idempotent=action.action_type in ("command", "install", "navigate"),
                destructive=action.action_type in ("configure", "remove"),
            )

            # Add prerequisites as dependencies
            if action.prerequisites:
                step.depends_on = action.prerequisites

            plan.steps.append(step)

        plan.validation_criteria = [
            f"All {len(plan.steps)} steps completed successfully",
            "Final state matches demonstrated outcome",
        ]

        return plan


# ---------------------------------------------------------------------------
# Video Pipeline
# ---------------------------------------------------------------------------

class VideoPipeline:
    """
    Complete video-to-task pipeline.

    Usage:
        pipeline = VideoPipeline(model_router=router)
        plan = await pipeline.process("tutorial.mp4")
    """

    def __init__(self, model_router: ModelRouter | None = None):
        self._acquirer = VideoAcquirer()
        self._audio_analyzer = AudioAnalyzer(model_router)
        self._frame_analyzer = FrameAnalyzer(model_router)
        self._action_extractor = ActionExtractor(model_router)
        self._workflow_generator = WorkflowGenerator(model_router)

    @property
    def is_available(self) -> bool:
        return self._acquirer.is_available

    async def process(
        self,
        video_source: str,
        analyze_every_n_seconds: float = 5.0,
        max_frames: int = 50,
    ) -> ExecutionPlan:
        """
        Process a video and generate an execution plan.

        Args:
            video_source: Path to video file or URL
            analyze_every_n_seconds: Frame extraction interval
            max_frames: Maximum frames to analyze
        """
        logger.info("Starting video pipeline for: %s", video_source)

        # Create temp directory for processing
        cache = get_cache_dir() / "video"
        cache.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(dir=cache))

        try:
            analysis = VideoAnalysis(source=video_source)

            # 1. Get duration
            analysis.duration = await self._acquirer.get_duration(video_source)
            logger.info("Video duration: %.1f seconds", analysis.duration)

            # 2. Extract frames
            fps = 1.0 / analyze_every_n_seconds
            analysis.frames = await self._acquirer.extract_frames(
                video_source,
                str(work_dir / "frames"),
                fps=fps,
                max_frames=max_frames,
            )

            # 3. Extract audio
            audio_path = str(work_dir / "audio.wav")
            await self._acquirer.extract_audio(video_source, audio_path)

            # 4. Transcribe audio
            analysis.transcript = await self._audio_analyzer.transcribe(audio_path)

            # 5. Analyze frames (OCR, code detection)
            for frame in analysis.frames:
                await self._frame_analyzer.analyze_frame(frame)

            # 6. Extract actions
            analysis.actions = await self._action_extractor.extract(
                analysis.frames, analysis.transcript
            )
            logger.info("Detected %d actions", len(analysis.actions))

            # 7. Generate execution plan
            plan = await self._workflow_generator.generate(analysis)
            logger.info(
                "Generated plan: %d steps from %d actions",
                len(plan.steps), len(analysis.actions)
            )

            return plan

        except Exception as e:
            logger.error("Video pipeline error: %s", e)
            raise
