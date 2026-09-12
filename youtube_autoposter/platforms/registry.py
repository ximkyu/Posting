"""Plattform-Registry.

V1 registriert nur YouTube. Neue Plattformen werden hier ergaenzt, ohne dass
Pipeline, Scanner, Datenbank oder Oberflaeche angepasst werden muessen.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import StateError
from .base import PlatformAdapter, PlatformInfo


class PlatformRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, PlatformAdapter] = {}
        self._default: str | None = None

    def register(self, adapter: PlatformAdapter, *, default: bool = False) -> PlatformAdapter:
        self._adapters[adapter.name] = adapter
        if default or self._default is None:
            self._default = adapter.name
        return adapter

    def get(self, name: str | None = None) -> PlatformAdapter:
        key = (name or self._default or "").strip().lower()
        if key not in self._adapters:
            known = ", ".join(sorted(self._adapters)) or "(keine)"
            raise StateError(
                f"Plattform '{name}' ist nicht verfuegbar. Registriert sind: {known}. "
                "Weitere Plattformen (Instagram/Meta, TikTok, Spotify) sind fuer spaetere "
                "Versionen als eigene Module geplant."
            )
        return self._adapters[key]

    def names(self) -> list[str]:
        return sorted(self._adapters)

    @property
    def default_name(self) -> str:
        return self._default or "youtube"

    def infos(self) -> list[PlatformInfo]:
        return [self._adapters[name].info() for name in self.names()]


def build_registry(
    *,
    settings: Any,
    auth: Any,
    repository: Any = None,
    logger: logging.Logger | None = None,
) -> PlatformRegistry:
    """Standard-Registry aufbauen (V1: YouTube only)."""

    from .youtube import YouTubePlatform

    registry = PlatformRegistry()
    registry.register(
        YouTubePlatform(auth, settings=settings, repository=repository, logger=logger),
        default=True,
    )

    # ------------------------------------------------------------------
    # Erweiterungsplatz (absichtlich noch nicht implementiert):
    #
    # from .meta import MetaPlatform        # Instagram / Facebook
    # from .tiktok import TikTokPlatform
    # from .spotify import SpotifyPlatform
    # registry.register(MetaPlatform(...))
    # registry.register(TikTokPlatform(...))
    # registry.register(SpotifyPlatform(...))
    # ------------------------------------------------------------------
    return registry


def get_adapter(name: str, **kwargs: Any) -> PlatformAdapter:
    """Bequemlichkeitsfunktion fuer CLI/Tests."""

    return build_registry(**kwargs).get(name)


__all__ = ["PlatformRegistry", "build_registry", "get_adapter"]
