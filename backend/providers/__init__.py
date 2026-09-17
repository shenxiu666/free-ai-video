"""Providers 包：Agnes 图/视频 + 开放文本适配。

图片/视频只能走 Agnes（见 docs/01 ADR-2、docs/05.1）；
文本无主力、用户自配（见 docs/05.1）。
"""

from .agnes_image import (
    IMAGE_ENDPOINT,
    IMAGE_MODEL,
    ALLOWED_RATIOS,
    ALLOWED_SIZES,
    build_image_payload,
    generate_image,
    validate_image_params,
)
from .agnes_video import (
    VIDEO_MODEL,
    VIDEO_POLL_ENDPOINT,
    VIDEO_SUBMIT_ENDPOINT,
    build_video_payload,
    generate_video,
    poll_video,
    submit_video,
    validate_video_params,
)
from .text import TextNotConfigured, generate_text

__all__ = [
    "IMAGE_ENDPOINT",
    "IMAGE_MODEL",
    "ALLOWED_SIZES",
    "ALLOWED_RATIOS",
    "build_image_payload",
    "generate_image",
    "validate_image_params",
    "VIDEO_SUBMIT_ENDPOINT",
    "VIDEO_POLL_ENDPOINT",
    "VIDEO_MODEL",
    "build_video_payload",
    "submit_video",
    "poll_video",
    "generate_video",
    "validate_video_params",
    "TextNotConfigured",
    "generate_text",
]
