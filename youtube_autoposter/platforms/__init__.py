"""Plattform-Adapter. V1: YouTube.

Erweiterungen (geplant, bewusst noch nicht implementiert):

    platforms/meta/     (Instagram, Facebook)
    platforms/tiktok/
    platforms/spotify/

Jede neue Plattform braucht nur eine Unterklasse von
:class:`youtube_autoposter.platforms.base.PlatformAdapter` und einen
Eintrag in :func:`youtube_autoposter.platforms.registry.build_registry`.
"""

from __future__ import annotations

from .base import (
    PlatformAdapter,
    PlatformInfo,
    PublicationJob,
    PublishResult,
    ThumbnailResult,
    UploadResult,
)
from .registry import build_registry, get_adapter

__all__ = [
    "PlatformAdapter",
    "PlatformInfo",
    "PublicationJob",
    "PublishResult",
    "ThumbnailResult",
    "UploadResult",
    "build_registry",
    "get_adapter",
]
