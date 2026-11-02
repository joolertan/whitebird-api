import os
import stat
import sys
import threading
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


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"refresh_token": "r"}',
        '["a"]',
        '{"access_token": 1}',
        '{"access_token": ""}',
        '{"access_token": "a", "refresh_token": 5}',
        '{"access_token": "a", "expires_at": "1700000000"}',
        '{"access_token": "a", "expires_at": true}',
    ],
)
def test_file_store_ignores_invalid_files(tmp_path: Path, content: str) -> None:
    path = tmp_path / "session.json"
    path.write_text(content)
    assert FileTokenStore(path).load() is None


def test_file_store_saves_from_many_threads(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "session.json")
    errors: list[Exception] = []

    def save(index: int) -> None:
        try:
            for attempt in range(50):
                store.save(Tokens(f"a{index}-{attempt}"))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=save, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert store.load() is not None
    assert [path.name for path in tmp_path.iterdir()] == ["session.json"]


def test_file_store_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert FileTokenStore("~/session.json").path == tmp_path / "session.json"


def test_expiry() -> None:
    assert not Tokens("a").expires_within(30)
    assert Tokens("a", expires_at=time.time() + 10).expires_within(30)
    assert not Tokens("a", expires_at=time.time() + 60).expires_within(30)
