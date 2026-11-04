"""scripts/admin/manage_archive_volumes.py: adding a disk is root-only."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "manage_archive_volumes",
    Path(__file__).resolve().parents[2] / "scripts/admin/manage_archive_volumes.py",
)
mav = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mav)


@pytest.mark.parametrize("adopt", [False, True])
def test_execute_requires_root(tmp_path, monkeypatch, capsys, adopt):
    monkeypatch.setattr(mav.os, "geteuid", lambda: 1000)
    root = tmp_path / "disk9"
    root.mkdir()
    assert mav._init("disk9", root, execute=True, adopt=adopt) == 1
    assert "requires root" in capsys.readouterr().err
    assert not (root / ".ssc-volume").exists()


def test_dry_run_needs_no_root_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mav.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(mav, "_mount_point", lambda p: Path("/media/other-disk"))
    root = tmp_path / "disk9"
    assert mav._init("disk9", root, execute=False, adopt=False) == 0
    assert "Dry-run" in capsys.readouterr().out
    assert not root.exists()


def test_root_without_sudo_user_needs_owner(monkeypatch):
    monkeypatch.delenv("SUDO_USER", raising=False)
    with pytest.raises(SystemExit, match="--owner"):
        mav._volume_owner(None)
