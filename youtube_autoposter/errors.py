"""Fehler-Hierarchie.

Jede Episode wird einzeln verarbeitet: Ein Fehler fuehrt zu einem
``EpisodeError`` (wird sauber in der DB + im Log gespeichert) und niemals
zu einem Absturz der gesamten Anwendung.
"""

from __future__ import annotations


class AutoPosterError(Exception):
    """Basis aller Anwendungsfehler."""

    #: Kann die Aktion erneut versucht werden?
    retriable: bool = False

    def __init__(self, message: str, *, details: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:  # pragma: no cover - Komfort
        if self.details:
            return f"{self.message} ({self.details})"
        return self.message


class ConfigError(AutoPosterError):
    """Ungueltige oder fehlende Konfiguration."""


class CredentialsError(AutoPosterError):
    """credentials.json fehlt/ist ungueltig."""


class AuthError(AutoPosterError):
    """Keine (gueltige) OAuth-Verbindung zum YouTube-Konto."""

    #: Benoetigt eine erneute Autorisierung durch den Menschen?
    needs_user_action: bool = True

    def __init__(
        self,
        message: str,
        *,
        details: str | None = None,
        status_code: int | None = None,
        reason: str | None = None,
        retriable: bool = False,
    ) -> None:
        # Die Attribute entsprechen denen von PlatformError, damit
        # Fehlerbehandlung/Protokollierung fuer beide Typen gleich funktioniert.
        super().__init__(message, details=details)
        self.status_code = status_code
        self.reason = reason
        self.retriable = retriable


class AuthExpiredError(AuthError):
    """Token abgelaufen und nicht automatisch erneuerbar."""


class InsufficientScopeError(AuthError):
    """Token gueltig, aber der Scope reicht fuer die Aktion nicht aus."""


class MetadataError(AutoPosterError):
    """JSON fehlt, ist ungueltig oder verletzt YouTube-Grenzwerte."""


class VideoFileError(AutoPosterError):
    """Videodatei fehlt, ist leer, beschaedigt oder nicht unterstuetzt."""


class UnsupportedFormatError(VideoFileError):
    """Nicht unterstuetztes Videoformat."""


class ThumbnailError(AutoPosterError):
    """Thumbnail ungueltig. Blockiert den Upload bewusst NICHT."""

    fatal = False


class FileOperationError(AutoPosterError):
    """Verschieben/Archivieren fehlgeschlagen."""


class DuplicateError(AutoPosterError):
    """Episode oder Datei-Inhalt wurde bereits erfolgreich hochgeladen."""


class PlatformError(AutoPosterError):
    """Fehler bei einem Plattform-API-Aufruf (z. B. YouTube)."""

    def __init__(
        self,
        message: str,
        *,
        details: str | None = None,
        status_code: int | None = None,
        reason: str | None = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message, details=details)
        self.status_code = status_code
        self.reason = reason
        self.retriable = retriable


class UploadError(PlatformError):
    """videos.insert fehlgeschlagen."""


class PublishError(PlatformError):
    """videos.update (privacyStatus -> public) fehlgeschlagen."""


class QuotaExceededError(PlatformError):
    """Tageskontingent erschoepft -> erst morgen weiter."""

    retriable = False


class PrivateLockError(PublishError):
    """Projekt ist nicht auditiert: Video bleibt privat gesperrt."""


class StateError(AutoPosterError):
    """Aktion passt nicht zum aktuellen Status (z. B. doppeltes Veroeffentlichen)."""


class ValidationError(AutoPosterError):
    """Validierung fehlgeschlagen (Sammel-Fehler mit allen Meldungen)."""

    def __init__(self, message: str, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])

    def __str__(self) -> str:  # pragma: no cover - Komfort
        if self.issues:
            return f"{self.message}: " + "; ".join(self.issues)
        return self.message
