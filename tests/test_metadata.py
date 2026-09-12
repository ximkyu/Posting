"""Tests fuer den JSON-Metadaten-Parser (Anforderung 36)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from youtube_autoposter.constants import (
    DESCRIPTION_MAX_LENGTH,
    ENFORCED_UPLOAD_PRIVACY,
    PRIVACY_PRIVATE,
    PRIVACY_PUBLIC,
    PRIVACY_UNLISTED,
    TAGS_TOTAL_MAX_LENGTH,
    TAG_MAX_LENGTH,
    TITLE_MAX_LENGTH,
)
from youtube_autoposter.errors import MetadataError
from youtube_autoposter.metadata.parser import (
    EpisodeMetadata,
    build_update_body,
    build_upload_body,
    parse_metadata_dict,
    parse_metadata_file,
)


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_valide_datei_ohne_fehler(tmp_path: Path) -> None:
    payload = {
        "episode_id": "folge_07",
        "title": "Folge 7 - Das vergessene Archiv",
        "description": "In dieser Folge oeffnen wir das Archiv.\n\n00:00 Intro\n02:15 Hauptteil",
        "tags": ["doku", "geschichte", "archiv"],
        "category_id": "27",
        "privacy_status": "private",
        "language": "de",
        "made_for_kids": False,
        "thumbnail": "folge_07.jpg",
        "publish_at": None,
    }
    path = tmp_path / "folge_07.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = parse_metadata_file(path)

    assert result.ok, result.error_messages()
    assert result.metadata.title == payload["title"]
    assert result.metadata.description == payload["description"]
    assert result.metadata.tags == payload["tags"]
    assert result.metadata.category_id == "27"
    assert result.metadata.language == "de"
    assert result.metadata.thumbnail == "folge_07.jpg"
    assert result.metadata.made_for_kids is False
    assert result.metadata.publish_at is None
    # Fehler und Warnungen duerfen nicht auftreten
    assert not [i for i in result.issues if i.level in ("error", "warning")]


def test_beschreibung_fehlt_ist_fehler() -> None:
    result = parse_metadata_dict({"title": "Nur Titel", "description": ""})
    assert not result.ok
    assert "description_missing" in _codes(result)


def test_titel_fehlt_ist_fehler() -> None:
    result = parse_metadata_dict({"description": "Nur Beschreibung"})
    assert not result.ok
    assert "title_missing" in _codes(result)


def test_titel_zu_lang() -> None:
    result = parse_metadata_dict({"title": "T" * (TITLE_MAX_LENGTH + 1), "description": "ok"})
    assert "title_too_long" in _codes(result)
    assert not result.ok


def test_beschreibung_zu_lang() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "B" * (DESCRIPTION_MAX_LENGTH + 1)})
    assert "description_too_long" in _codes(result)


def test_tag_zu_lang_und_summe_zu_lang() -> None:
    result = parse_metadata_dict(
        {
            "title": "ok",
            "description": "ok",
            "tags": ["x" * (TAG_MAX_LENGTH + 1), "y" * (TAGS_TOTAL_MAX_LENGTH + 50)],
        }
    )
    codes = _codes(result)
    assert "tag_too_long" in codes
    assert "tags_too_long_total" in codes


def test_tags_als_string_werden_getrennt() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "tags": "a, b , c"})
    assert result.metadata.tags == ["a", "b", "c"]


def test_tags_leer_und_duplikate() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "tags": ["a", "a", "", "  b  "]})
    assert result.metadata.tags == ["a", "b"]


def test_kategorie_muss_numerisch_sein() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "category_id": "Musik"})
    assert "category_invalid" in _codes(result)
    assert not result.ok


def test_kategorie_als_zahl_wird_zu_text() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "category_id": 27})
    assert result.metadata.category_id == "27"
    assert "category_invalid" not in _codes(result)


def test_deutsche_aliase_werden_verstanden() -> None:
    result = parse_metadata_dict(
        {
            "titel": "Deutscher Titel",
            "beschreibung": "Deutsche Beschreibung",
            "schlagwoerter": ["eins", "zwei"],
            "kategorie": "22",
            "sprache": "de",
            "vorschaubild": "bild.jpg",
        }
    )
    assert result.ok, result.error_messages()
    assert result.metadata.title == "Deutscher Titel"
    assert result.metadata.description == "Deutsche Beschreibung"
    assert result.metadata.tags == ["eins", "zwei"]
    assert result.metadata.category_id == "22"
    assert result.metadata.thumbnail == "bild.jpg"


def test_unbekannte_felder_werden_gespeichert_und_markiert() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "eigenes_feld": "wert"})
    assert result.metadata.extra == {"eigenes_feld": "wert"}
    assert "unknown_fields" in _codes(result)
    # Info-Feld -> darf nicht als Fehler gewertet werden
    assert result.ok


def test_privacy_public_wird_ignoriert_und_gewarnt() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "privacy_status": PRIVACY_PUBLIC})
    assert result.metadata.privacy_status_requested == PRIVACY_PUBLIC
    # Die wirksame Privacy ist IMMER private
    assert result.metadata.effective_privacy == ENFORCED_UPLOAD_PRIVACY
    assert "privacy_overridden" in _codes(result)
    warning = [i for i in result.issues if i.code == "privacy_overridden"][0]
    assert warning.level == "warning"
    assert PRIVACY_PRIVATE in warning.message


def test_privacy_unlisted_wird_ebenfalls_ignoriert() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "privacy_status": PRIVACY_UNLISTED})
    assert result.metadata.effective_privacy == ENFORCED_UPLOAD_PRIVACY
    assert "privacy_overridden" in _codes(result)


def test_privacy_ungueltig_ist_fehler() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "privacy_status": "geheim"})
    assert "privacy_invalid" in _codes(result)
    assert not result.ok
    # Auch hier gilt die Sicherheitsregel
    assert result.metadata.effective_privacy == ENFORCED_UPLOAD_PRIVACY


def test_publish_at_wird_nicht_gesendet() -> None:
    result = parse_metadata_dict(
        {"title": "ok", "description": "ok", "publish_at": "2026-10-01T18:00:00+02:00"}
    )
    assert result.metadata.publish_at == "2026-10-01T16:00:00+00:00"
    assert "publish_at_ignored" in _codes(result)
    assert result.ok

    body = build_upload_body(result.metadata)
    assert "publishAt" not in body["status"]


def test_publish_at_ungueltig_warnung() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok", "publish_at": "morgen"})
    assert "publish_at_invalid" in _codes(result)
    assert result.metadata.publish_at is None


def test_made_for_kids_default_ist_falsch() -> None:
    result = parse_metadata_dict({"title": "ok", "description": "ok"})
    assert result.metadata.effective_made_for_kids is False

    result2 = parse_metadata_dict({"title": "ok", "description": "ok", "made_for_kids": True})
    assert result2.metadata.effective_made_for_kids is True

    # selfDeclaredMadeForKids gewinnt, wenn gesetzt
    result3 = parse_metadata_dict(
        {
            "title": "ok",
            "description": "ok",
            "made_for_kids": True,
            "self_declared_made_for_kids": False,
        }
    )
    assert result3.metadata.effective_made_for_kids is False


def test_kaputtes_json_wirft_metadata_error(tmp_path: Path) -> None:
    path = tmp_path / "kaputt.json"
    path.write_text('{"title": "ohne Ende', encoding="utf-8")
    with pytest.raises(MetadataError):
        parse_metadata_file(path)


def test_json_als_liste_wird_abgelehnt(tmp_path: Path) -> None:
    path = tmp_path / "liste.json"
    path.write_text('["title", "beschreibung"]', encoding="utf-8")
    with pytest.raises(MetadataError):
        parse_metadata_file(path)


def test_fehlende_datei(tmp_path: Path) -> None:
    with pytest.raises(MetadataError):
        parse_metadata_file(tmp_path / "gibt_es_nicht.json")


def test_bom_wird_akzeptiert(tmp_path: Path) -> None:
    path = tmp_path / "bom.json"
    path.write_text("\ufeff" + json.dumps({"title": "ok", "description": "ok"}), encoding="utf-8")
    result = parse_metadata_file(path)
    assert result.ok


# ---------------------------------------------------------------------------
# Upload-Body: die wichtigste Sicherheitsregel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gewuenscht", ["", PRIVACY_PRIVATE, PRIVACY_UNLISTED, PRIVACY_PUBLIC, "egal-was"])
def test_upload_body_ist_immer_privat(gewuenscht: str) -> None:
    metadata = EpisodeMetadata(title="Titel", description="Beschreibung", privacy_status_requested=gewuenscht)
    body = build_upload_body(metadata)
    assert body["status"]["privacyStatus"] == PRIVACY_PRIVATE
    assert body["snippet"]["title"] == "Titel"
    assert body["snippet"]["description"] == "Beschreibung"
    assert "publishAt" not in body["status"]


def test_upload_body_defaults_aus_settings() -> None:
    class FakeSettings:
        DEFAULT_CATEGORY_ID = "22"
        DEFAULT_LANGUAGE = "de"
        DEFAULT_MADE_FOR_KIDS = False
        SEND_PUBLISH_AT = True
        SEND_SYNTHETIC_MEDIA_FLAG = True

    metadata = EpisodeMetadata(title="T", description="D", publish_at="2026-10-01T10:00:00+00:00")
    body = build_upload_body(metadata, settings=FakeSettings())
    assert body["snippet"]["categoryId"] == "22"
    assert body["snippet"]["defaultLanguage"] == "de"
    assert body["status"]["privacyStatus"] == PRIVACY_PRIVATE
    assert body["status"]["selfDeclaredMadeForKids"] is False
    # Auch wenn SEND_PUBLISH_AT aktiv ist: im Privat-Modus nie mitsenden
    assert "publishAt" not in body["status"]


def test_update_body_hat_id_und_self_declared_made_for_kids() -> None:
    metadata = EpisodeMetadata(title="T", description="D", tags=["a"], category_id="27")
    body = build_update_body(metadata, "YTID123", privacy_status=PRIVACY_PUBLIC)
    assert body["id"] == "YTID123"
    assert body["status"]["privacyStatus"] == PRIVACY_PUBLIC
    assert body["status"]["selfDeclaredMadeForKids"] is False
    # snippet darf beim Veroeffentlichen NICHT mitgesendet werden (API-Regel)
    assert "snippet" not in body


def test_to_dict_roundtrip() -> None:
    metadata = EpisodeMetadata(title="T", description="D", tags=["x"], category_id="27")
    data = metadata.to_dict()
    assert data["title"] == "T"
    assert data["tags"] == ["x"]
    assert data["effective_privacy_status"] == PRIVACY_PRIVATE
