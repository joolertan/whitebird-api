import os
import stat
import sys
import time
from pathlib import Path

import pytest

from whitebird import FileTokenStore, MemoryTokenStore, Tokens


def test_memory_store() -> None:
    store = MemoryTokenStore()
    assert store.load() is None

    store.save(Tokens("a", "r", 1.0))
    assert store.load() == Tokens("a", "r", 1.0)

    store.clear()
    assert store.load() is None


def test_file_store_round_trip(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "nested" / "session.json")
    assert store.load() is None

    store.save(Tokens("a", "r", 1700000000.5))
    assert FileTokenStore(store.path).load() == Tokens("a", "r", 1700000000.5)

    store.save(Tokens("b"))
    assert store.load() == Tokens("b")

    store.clear()
    assert store.load() is None
    assert not store.path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_file_store_is_private(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "session.json")
    store.save(Tokens("a"))

    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert [path.name for path in tmp_path.iterdir()] == ["session.json"]


def test_file_store_ignores_unreadable_files(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("not json")
    assert FileTokenStore(path).load() is None

    path.write_text('{"refresh_token": "r"}')
    assert FileTokenStore(path).load() is None


def test_file_store_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert FileTokenStore("~/session.json").path == tmp_path / "session.json"


def test_expiry() -> None:
    assert not Tokens("a").expires_within(30)
    assert Tokens("a", expires_at=time.time() + 10).expires_within(30)
    assert not Tokens("a", expires_at=time.time() + 60).expires_within(30)
