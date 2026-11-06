"""Glyph 内置工具：业务能力（批改等）以 Tool 协议实现并注册到 ToolRegistry。

运行时内核业务无关，本包承载面向英语学习者的具体工具。装配层通常只需：

    from glyph.tools import build_grade_text_tool, build_ocr_image_tool
    registry.register(build_grade_text_tool(settings))
    registry.register(build_ocr_image_tool(provider, settings))
    registry.register(build_recheck_detection_tool(settings))
"""

from __future__ import annotations

from .analyze_writing import AnalyzeWritingTool
from .artifact import ArtifactReader, ReadArtifactTool
from .attempt_tools import DeleteAttemptTool, ReadAttemptsTool, RecordAttemptTool
from .describe import DescribeImageTool
from .factory import (
    build_analyze_writing_tool,
    build_attempts,
    build_describe_image_tool,
    build_grade_text_tool,
    build_memory,
    build_ocr_image_tool,
    build_polish_text_tool,
    build_read_artifact_tool,
    build_recheck_detection_tool,
)
from .grade import GradeTextTool
from .grading_models import RecheckInfo
from .image import ImageProvider
from .memory_tools import ForgetTool, ReadMemoryTool, RememberTool
from .ocr import OcrImageTool
from .ocr_models import Transcription
from .plan_tools import ProposePlanTool
from .polish import PolishTextTool
from .recheck import RecheckDetectionTool, Rechecker

__all__ = [
    "build_analyze_writing_tool",
    "build_grade_text_tool",
    "build_ocr_image_tool",
    "build_describe_image_tool",
    "build_recheck_detection_tool",
    "build_memory",
    "build_attempts",
    "build_polish_text_tool",
    "build_read_artifact_tool",
    "AnalyzeWritingTool",
    "GradeTextTool",
    "OcrImageTool",
    "DescribeImageTool",
    "PolishTextTool",
    "ProposePlanTool",
    "RecheckDetectionTool",
    "ReadArtifactTool",
    "ArtifactReader",
    "RecordAttemptTool",
    "ReadAttemptsTool",
    "DeleteAttemptTool",
    "RememberTool",
    "ForgetTool",
    "ReadMemoryTool",
    "Rechecker",
    "RecheckInfo",
    "ImageProvider",
    "Transcription",
]
