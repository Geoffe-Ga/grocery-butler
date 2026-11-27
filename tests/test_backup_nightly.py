"""Tests for the nightly backup and its systemd timer (ADR 0021).

The script is run for real, with the real ``backup.sh`` and gpg, against a
temporary SQLite database. What matters: a good run adds exactly one
encrypted file and keeps the newest fourteen; a failed run removes
nothing, so a broken night can never cost an old backup.
"""

from __future__ import annotations

import configparser
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from grocery_butler.db import init_db
from grocery_butler.members import MemberStore

ROOT = Path(__file__).parent.parent
SCRIPT = str(ROOT / "scripts" / "backup-nightly.sh")
UNITS = ROOT / "deploy" / "vps" / "systemd"
SERVICE = UNITS / "grocery-butler-backup.service"
TIMER = UNITS / "grocery-butler-backup.timer"

BACKUP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json\.gpg$")


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated database with one member."""
    path = str(tmp_path / "household.db")
    init_db(path)
    MemberStore(path).add("geoff", "Geoff", created_by="backup-test")
    return path


@pytest.fixture()
def backups(tmp_path: Path) -> Path:
    """Return a directory for backups that does not exist yet."""
    return tmp_path / "backups"


def _run(
    db_path: str, backups: Path, *, passphrase: str | None
) -> subprocess.CompletedProcess[str]:
    """Run the nightly script with a minimal environment."""
    env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", ""),
        "DATABASE_PATH": db_path,
        "BACKUP_DIR": str(backups),
    }
    if passphrase is not None:
        env["BACKUP_PASSPHRASE"] = passphrase
    return subprocess.run(
        [SCRIPT],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _old_backups(backups: Path, count: int) -> list[Path]:
    """Create dated files from past nights, oldest first."""
    backups.mkdir(parents=True, exist_ok=True)
    made = []
    for day in range(1, count + 1):
        path = backups / f"2020-01-{day:02d}.json.gpg"
        path.write_bytes(b"an older backup")
        made.append(path)
    return made


class TestANightThatWorks:
    """One new encrypted file, and only the newest fourteen kept."""

    def test_it_writes_todays_encrypted_backup(
        self, db_path: str, backups: Path
    ) -> None:
        """The directory is created and holds one file, named by date."""
        result = _run(db_path, backups, passphrase=secrets.token_hex(16))

        (written,) = list(backups.iterdir())
        assert result.returncode == 0, result.stderr
        assert BACKUP_NAME.match(written.name)
        assert b"geoff" not in written.read_bytes()
        assert written.name in result.stdout

    def test_it_keeps_the_newest_fourteen(self, db_path: str, backups: Path) -> None:
        """Sixteen old ones plus tonight's leaves fourteen; the oldest go."""
        old = _old_backups(backups, 16)

        result = _run(db_path, backups, passphrase=secrets.token_hex(16))

        kept = sorted(path.name for path in backups.iterdir())
        assert result.returncode == 0, result.stderr
        assert len(kept) == 14
        assert [path.name for path in old[:3]] == [
            "2020-01-01.json.gpg",
            "2020-01-02.json.gpg",
            "2020-01-03.json.gpg",
        ]
        assert not any(path.exists() for path in old[:3])
        assert all(path.exists() for path in old[3:])
        assert BACKUP_NAME.match(kept[-1])
        assert not kept[-1].startswith("2020-")

    def test_it_only_ever_removes_its_own_files(
        self, db_path: str, backups: Path
    ) -> None:
        """Anything in the directory that isn't a dated backup is left alone."""
        _old_backups(backups, 16)
        notes = backups / "README.txt"
        manual = backups / "before-the-big-change.json.gpg"
        notes.write_text("kept by hand", encoding="utf-8")
        manual.write_bytes(b"a backup someone named themselves")

        _run(db_path, backups, passphrase=secrets.token_hex(16))

        assert notes.exists()
        assert manual.exists()

    def test_a_second_run_the_same_day_replaces_that_days_file(
        self, db_path: str, backups: Path
    ) -> None:
        """Running twice leaves one file for the day, not two."""
        passphrase = secrets.token_hex(16)

        _run(db_path, backups, passphrase=passphrase)
        again = _run(db_path, backups, passphrase=passphrase)

        assert again.returncode == 0, again.stderr
        assert len(list(backups.iterdir())) == 1


class TestANightThatFails:
    """A failed backup is loud and removes nothing."""

    def test_without_a_passphrase_nothing_is_written_or_removed(
        self, db_path: str, backups: Path
    ) -> None:
        """Old backups survive a night that could not make a new one."""
        old = _old_backups(backups, 16)

        result = _run(db_path, backups, passphrase=None)

        assert result.returncode != 0
        assert "BACKUP_PASSPHRASE" in result.stderr
        assert sorted(backups.iterdir()) == sorted(old)

    def test_without_a_directory_it_says_what_to_set(self, db_path: str) -> None:
        """The destination is required; there is no default to surprise anyone."""
        result = subprocess.run(
            [SCRIPT],
            env={
                "PATH": os.environ["PATH"],
                "HOME": os.environ.get("HOME", ""),
                "DATABASE_PATH": db_path,
                "BACKUP_PASSPHRASE": secrets.token_hex(16),
            },
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

        assert result.returncode == 2
        assert "BACKUP_DIR" in result.stderr


def _unit(path: Path) -> configparser.ConfigParser:
    """Parse a systemd unit file. Option names come back in lower case."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path, encoding="utf-8")
    return parser


class TestTheTimer:
    """The units run the script nightly, as the member who owns the checkout."""

    def test_the_timer_fires_nightly_and_catches_up(self) -> None:
        """Once a day, and a night the server was off is run at next boot."""
        timer = _unit(TIMER)["Timer"]

        assert timer["oncalendar"].startswith("*-*-* ")
        assert timer["persistent"] == "true"
        assert _unit(TIMER)["Install"]["wantedby"] == "timers.target"

    def test_the_service_runs_the_script_once(self) -> None:
        """A one-shot run of the repository's own script."""
        service = _unit(SERVICE)["Service"]

        assert service["type"] == "oneshot"
        assert service["execstart"].endswith("/scripts/backup-nightly.sh")
        assert service["workingdirectory"] in service["execstart"]

    def test_secrets_come_from_a_file_outside_the_repository(self) -> None:
        """The units name where the secrets live and hold none themselves."""
        service = _unit(SERVICE)["Service"]
        text = SERVICE.read_text(encoding="utf-8") + TIMER.read_text(encoding="utf-8")

        assert service["environmentfile"] == "%h/.config/grocery-butler/backup.env"
        assert "BACKUP_PASSPHRASE=" not in text
        assert "postgresql://" not in text

    def test_the_units_assume_no_user_name(self) -> None:
        """Paths hang off the home directory, so any account can install them."""
        text = SERVICE.read_text(encoding="utf-8")

        assert "/home/" not in text
        assert "User=" not in text
