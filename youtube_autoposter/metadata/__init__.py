"""Metadaten-Parser: JSON lesen, normalisieren, YouTube-Payload bauen."""

from __future__ import annotations

from .parser import (
    EpisodeMetadata,
    MetadataIssue,
    ParseResult,
    build_upload_body,
    build_update_body,
    parse_metadata_dict,
    parse_metadata_file,
)

__all__ = [
    "EpisodeMetadata",
    "MetadataIssue",
    "ParseResult",
    "build_update_body",
    "build_upload_body",
    "parse_metadata_dict",
    "parse_metadata_file",
]
