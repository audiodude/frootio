from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from frootio import media


def test_store_bytes_is_content_addressed(env: Path) -> None:
    data = b"kiwi"
    rel = media.store_bytes("characters/kiwi/images", data, ".PNG")
    assert rel == f"characters/kiwi/images/{hashlib.sha256(data).hexdigest()[:12]}.png"
    assert media.abs_path(rel).read_bytes() == data
    assert media.abs_path(rel).parent == env / "media" / "characters" / "kiwi" / "images"


def test_store_bytes_never_overwrites(env: Path) -> None:
    rel = media.store_bytes("x", b"one", "png")
    path = media.abs_path(rel)
    path.write_bytes(b"tampered")  # simulate: an existing file must be left alone
    assert media.store_bytes("x", b"one", "png") == rel
    assert path.read_bytes() == b"tampered"
    assert list(path.parent.iterdir()) == [path]  # no temp files left behind


def test_store_file_matches_store_bytes(env: Path) -> None:
    src = env / "voice.WAV"
    src.write_bytes(b"RIFF....")
    rel = media.store_file("characters/kiwi/voice", src)
    assert rel == media.store_bytes("characters/kiwi/voice", b"RIFF....", "wav")


@pytest.mark.parametrize("bad", ["../etc/passwd", "/abs/path", "a/../../b", "a//b"])
def test_abs_path_rejects_escapes(bad: str) -> None:
    with pytest.raises(ValueError):
        media.abs_path(bad)


def test_rel_path_and_url(env: Path) -> None:
    rel = media.store_bytes("series/b s/music", b"m", "mp3")
    assert media.rel_path(media.abs_path(rel)) == rel
    assert media.url(rel).startswith("/media/series/b%20s/music/")


def test_slugify() -> None:
    assert media.slugify("Kiwi Kowalski") == "kiwi-kowalski"
    assert media.slugify("  Ms. Plum!! ") == "ms-plum"
    assert media.slugify("Crème Brûlée") == "creme-brulee"
    assert media.slugify("!!!") == "untitled"


def test_take_paths_layout() -> None:
    p = media.take_paths("kiwi-and-lemon", 4, 17, 99)
    assert p["video"] == "series/kiwi-and-lemon/episodes/4/snippets/17/takes/99.mp4"
    assert p["vocals"].endswith("takes/99.vocals.wav")
    assert p["fx"].endswith("takes/99.fx.wav")
    assert p["poster"].endswith("takes/99.poster.jpg")
