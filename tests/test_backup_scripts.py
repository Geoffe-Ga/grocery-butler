"""Tests for scripts/backup.sh and scripts/restore.sh (ADR 0013).

These run the real scripts with real gpg against a temporary SQLite
database. The passphrase is random per test, so no secret is written down.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from grocery_butler.data_lifecycle import (
    DELETE_PHRASE,
    RESTORE_PHRASE,
    HouseholdData,
)
from grocery_butler.db import init_db
from grocery_butler.members import MemberStore

ROOT = Path(__file__).parent.parent
BACKUP = str(ROOT / "scripts" / "backup.sh")
RESTORE = str(ROOT / "scripts" / "restore.sh")


def _env(db_path: str, passphrase: str | None) -> dict[str, str]:
    """Build a minimal environment for a script run."""
    python_dir = str(Path(sys.executable).parent)
    env = {
        "PATH": f"{python_dir}{os.pathsep}{os.environ['PATH']}",
        "DATABASE_PATH": db_path,
        "HOME": os.environ.get("HOME", ""),
    }
    if passphrase is not None:
        env["BACKUP_PASSPHRASE"] = passphrase
    return env


def _run(
    script: str, *args: str, db_path: str, passphrase: str | None
) -> subprocess.CompletedProcess[str]:
    """Run a script and capture what it printed."""
    return subprocess.run(
        [script, *args],
        env=_env(db_path, passphrase),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _state(db_path: str) -> dict[str, object]:
    """Return the household's tables."""
    tables: dict[str, object] = HouseholdData(db_path).export()["tables"]
    return tables


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated database with one member."""
    path = str(tmp_path / "household.db")
    init_db(path)
    MemberStore(path).add("geoff", "Geoff", created_by="backup-test")
    return path


@pytest.fixture()
def passphrase() -> str:
    """Return a passphrase nobody has seen before."""
    return secrets.token_hex(16)


class TestBackup:
    """backup.sh writes an encrypted file or nothing."""

    def test_backup_is_encrypted(
        self, db_path: str, passphrase: str, tmp_path: Path
    ) -> None:
        """The file holds no readable household data and is private."""
        target = tmp_path / "backup.gpg"

        result = _run(BACKUP, str(target), db_path=db_path, passphrase=passphrase)

        assert result.returncode == 0, result.stderr
        content = target.read_bytes()
        assert b"household_members" not in content
        assert b"geoff" not in content
        assert target.stat().st_mode & 0o777 == 0o600
        assert f"Encrypted backup written to {target}" in result.stdout

    def test_backup_leaves_no_plaintext_behind(
        self, db_path: str, passphrase: str, tmp_path: Path
    ) -> None:
        """Only the encrypted file appears next to the database."""
        target = tmp_path / "out" / "backup.gpg"
        target.parent.mkdir()

        _run(BACKUP, str(target), db_path=db_path, passphrase=passphrase)

        assert [p.name for p in target.parent.iterdir()] == ["backup.gpg"]

    def test_backup_without_a_passphrase_writes_nothing(
        self, db_path: str, tmp_path: Path
    ) -> None:
        """No passphrase, no file: never an unencrypted backup."""
        target = tmp_path / "backup.gpg"

        result = _run(BACKUP, str(target), db_path=db_path, passphrase=None)

        assert result.returncode == 2
        assert "BACKUP_PASSPHRASE is not set" in result.stderr
        assert not target.exists()

    def test_backup_without_a_target_prints_usage(
        self, db_path: str, passphrase: str
    ) -> None:
        """A missing output path is a usage error."""
        result = _run(BACKUP, db_path=db_path, passphrase=passphrase)

        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_a_failed_export_writes_nothing(
        self, passphrase: str, tmp_path: Path
    ) -> None:
        """If the database can't be read there is no file to mistake for a backup."""
        target = tmp_path / "backup.gpg"
        unreadable = str(tmp_path / "missing-dir" / "household.db")

        result = _run(BACKUP, str(target), db_path=unreadable, passphrase=passphrase)

        assert result.returncode == 1
        assert "no backup was written" in result.stderr
        assert not target.exists()


class TestRestore:
    """restore.sh brings the household back, or changes nothing."""

    def _backup(self, db_path: str, passphrase: str, tmp_path: Path) -> str:
        """Take a backup and return its path."""
        target = str(tmp_path / "backup.gpg")
        result = _run(BACKUP, target, db_path=db_path, passphrase=passphrase)
        assert result.returncode == 0, result.stderr
        return target

    def test_backup_delete_restore_round_trip(
        self, db_path: str, passphrase: str, tmp_path: Path
    ) -> None:
        """Back up, delete everything, restore: the household is back."""
        before = _state(db_path)
        backup = self._backup(db_path, passphrase, tmp_path)
        HouseholdData(db_path).delete(DELETE_PHRASE)

        result = _run(
            RESTORE, backup, RESTORE_PHRASE, db_path=db_path, passphrase=passphrase
        )

        assert result.returncode == 0, result.stderr
        assert _state(db_path) == before
        assert f"Restored from {backup}" in result.stdout

    def test_wrong_passphrase_changes_nothing(
        self, db_path: str, passphrase: str, tmp_path: Path
    ) -> None:
        """A backup that can't be decrypted is never half-restored."""
        backup = self._backup(db_path, passphrase, tmp_path)
        MemberStore(db_path).add("jess", "Jess", created_by="backup-test")
        before = _state(db_path)

        result = _run(
            RESTORE,
            backup,
            RESTORE_PHRASE,
            db_path=db_path,
            passphrase=secrets.token_hex(16),
        )

        assert result.returncode == 1
        assert "Could not decrypt" in result.stderr
        assert "Nothing was changed" in result.stderr
        assert _state(db_path) == before

    def test_restore_without_the_phrase_changes_nothing(
        self, db_path: str, passphrase: str, tmp_path: Path
    ) -> None:
        """Without the confirmation phrase the script says it and stops."""
        backup = self._backup(db_path, passphrase, tmp_path)
        MemberStore(db_path).add("jess", "Jess", created_by="backup-test")
        before = _state(db_path)

        result = _run(RESTORE, backup, db_path=db_path, passphrase=passphrase)

        assert result.returncode == 1
        assert RESTORE_PHRASE in result.stderr
        assert _state(db_path) == before

    def test_restore_of_a_missing_file(self, db_path: str, passphrase: str) -> None:
        """A path that isn't there is reported."""
        result = _run(
            RESTORE,
            "/nonexistent/backup.gpg",
            RESTORE_PHRASE,
            db_path=db_path,
            passphrase=passphrase,
        )

        assert result.returncode == 1
        assert "is not a file" in result.stderr

    def test_restore_without_a_passphrase(
        self, db_path: str, passphrase: str, tmp_path: Path
    ) -> None:
        """No passphrase is a usage error before anything is read."""
        backup = self._backup(db_path, passphrase, tmp_path)

        result = _run(RESTORE, backup, RESTORE_PHRASE, db_path=db_path, passphrase=None)

        assert result.returncode == 2
        assert "BACKUP_PASSPHRASE is not set" in result.stderr

    def test_restore_without_arguments_prints_usage(
        self, db_path: str, passphrase: str
    ) -> None:
        """A missing backup path is a usage error."""
        result = _run(RESTORE, db_path=db_path, passphrase=passphrase)

        assert result.returncode == 2
        assert "Usage:" in result.stderr
