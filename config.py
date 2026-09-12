#!/usr/bin/env python3
"""Zentrale Konfiguration (Kompatibilitaets-Einstiegspunkt).

Die eigentliche Implementierung liegt in
:mod:`youtube_autoposter.config`. Diese Datei sorgt dafuer, dass - wie in der
Projektstruktur vorgesehen - ein einfacher ``import config`` auf
Projektebene funktioniert::

    from config import load_settings
    settings = load_settings()
    print(settings.READY_FOLDER, settings.PORT, settings.DEFAULT_PRIVACY_STATUS)
"""

from __future__ import annotations

from youtube_autoposter.config import (
    CONFIG_FILE_NAME,
    ENV_PREFIX,
    Settings,
    get_settings,
    load_settings,
    project_root,
    reset_settings_cache,
    resolve_scopes,
)

__all__ = [
    "CONFIG_FILE_NAME",
    "ENV_PREFIX",
    "Settings",
    "get_settings",
    "load_settings",
    "project_root",
    "reset_settings_cache",
    "resolve_scopes",
]
