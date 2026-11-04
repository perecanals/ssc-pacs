"""Archive volumes (web-app/archive_volumes.py): config parsing, the one
"which volume holds this path?" answer every safety check uses, sentinel-based
online status, and the batch volume selection order. DB-free.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import archive_volumes as av
import config
from archive_volumes import ArchiveVolume, parse_archive_volumes

DATA = Path("/data/imaging")


def _parse(storage):
    return parse_archive_volumes(storage, DATA)


class TestParse:
    def test_legacy_key_is_one_required_volume(self):
        vols, default, dmap, legacy = _parse({"cold_archive_root": "/data/compressed"})
        assert legacy is True and default == "disk1" and dmap == {}
        assert vols == {"disk1": ArchiveVolume("disk1", Path("/data/compressed"), required=True)}

    def test_volume_table(self):
        vols, default, dmap, legacy = _parse({
            "default_archive_volume": "disk1",
            "archive_volumes": {
                "disk1": {"root": "/d1/compressed", "required": True},
                "disk2": {"root": "/d2/compressed", "backup_repository": "imaging-d2"},
            },
            "dataset_archive_volume": {"GUIDE": "disk2"},
        })
        assert legacy is False and default == "disk1" and dmap == {"GUIDE": "disk2"}
        assert vols["disk1"].required and not vols["disk2"].required
        assert vols["disk2"].backup_repository == "imaging-d2"
        assert vols["disk1"].backup_repository == "imaging"

    def test_single_volume_defaults_itself(self):
        _, default, _, _ = _parse({"archive_volumes": {"disk2": {"root": "/d2"}}})
        assert default == "disk2"

    @pytest.mark.parametrize("storage, match", [
        ({"cold_archive_root": "/c", "archive_volumes": {"disk1": {"root": "/d"}}}, "both"),
        ({"archive_volumes": {}}, "at least one"),
        ({"archive_volumes": {"Disk 1": {"root": "/d"}}}, "invalid volume name"),
        ({"archive_volumes": {"disk1": {"root": "relative"}}}, "absolute"),
        ({"archive_volumes": {"disk1": {}}}, "missing root"),
        ({"archive_volumes": {"disk1": {"root": "/d", "rquired": True}}}, "unknown key"),
        ({"archive_volumes": {"disk1": {"root": "/d", "required": "yes"}}}, "true or false"),
        ({"archive_volumes": {"disk1": {"root": "/d", "backup_repository": "../x"}}}, "backup_repository"),
        ({"archive_volumes": {"disk1": {"root": "/a"}, "disk2": {"root": "/b"}}}, "default_archive_volume is required"),
        ({"default_archive_volume": "disk9", "archive_volumes": {"disk1": {"root": "/a"}}}, "not a configured volume"),
        ({"archive_volumes": {"disk1": {"root": "/a"}}, "dataset_archive_volume": {"X": "disk2"}}, "not a configured volume"),
        # Overlaps: nested volumes, a volume inside / around the loose tree.
        ({"default_archive_volume": "disk1",
          "archive_volumes": {"disk1": {"root": "/a"}, "disk2": {"root": "/a/b"}}}, "overlap"),
        ({"archive_volumes": {"disk1": {"root": "/data/imaging/cold"}}}, "overlaps dicom_data_root"),
        ({"archive_volumes": {"disk1": {"root": "/data"}}}, "overlaps dicom_data_root"),
    ])
    def test_rejected(self, storage, match):
        with pytest.raises(RuntimeError, match=match):
            _parse(storage)

    def test_prefix_string_is_not_overlap(self):
        vols, *_ = _parse({"default_archive_volume": "disk1", "archive_volumes": {
            "disk1": {"root": "/data/disk1"}, "disk10": {"root": "/data/disk10"}}})
        assert set(vols) == {"disk1", "disk10"}

    def test_config_loader_runs_the_parser(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            '[storage]\nmode = "cold_path_cache"\ndicom_data_root = "/data/imaging"\n'
            'default_archive_volume = "disk2"\n'
            '[storage.archive_volumes.disk1]\nroot = "/d1"\n'
            '[storage.archive_volumes.disk2]\nroot = "/d2"\n'
        )
        storage, _, _ = config._load_and_validate(path)
        assert storage["_default_volume"] == "disk2"
        assert set(storage["_volumes"]) == {"disk1", "disk2"}
        assert storage["_legacy_volumes"] is False


@pytest.fixture
def two(tmp_path):
    vols = {}
    for name in ("disk1", "disk10"):
        root = (tmp_path / name).resolve()
        root.mkdir()
        vols[name] = ArchiveVolume(name, root)
    return vols


class TestVolumeForPath:
    def test_inside(self, two):
        assert av.volume_for_path(two["disk1"].root / "lvo" / "P" / "1.2", two).name == "disk1"
        assert av.volume_for_path(two["disk10"].root / "x", two).name == "disk10"

    def test_root_itself_and_outside_are_none(self, two, tmp_path):
        assert av.volume_for_path(two["disk1"].root, two) is None
        assert av.volume_for_path(tmp_path / "elsewhere" / "x", two) is None
        assert av.volume_for_path("/", two) is None

    def test_dotdot_escape(self, two):
        assert av.volume_for_path(f"{two['disk1'].root}/lvo/../../disk10/x", two).name == "disk10"
        assert av.volume_for_path(f"{two['disk1'].root}/../outside", two) is None

    def test_symlink_escape(self, two, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, two["disk1"].root / "link")
        assert av.volume_for_path(two["disk1"].root / "link" / "x", two) is None

    def test_volume_of_archives(self, two, tmp_path):
        d1 = two["disk1"].root
        assert av.volume_of_archives([], two) is None
        assert av.volume_of_archives([None, ""], two) is None
        assert av.volume_of_archives([str(d1 / "a"), str(d1 / "b")], two).name == "disk1"
        with pytest.raises(av.ArchiveVolumeError, match="span"):
            av.volume_of_archives([str(d1 / "a"), str(two["disk10"].root / "b")], two)
        with pytest.raises(av.ArchiveVolumeError, match="outside"):
            av.volume_of_archives([str(tmp_path / "nowhere" / "a")], two)


class TestStatus:
    def test_online_with_sentinel(self, two):
        av.write_sentinel(two["disk1"].root, "disk1")
        st = av.status(two["disk1"], max_age=0)
        assert st.online and st.free_bytes > 0 and st.total_bytes > 0
        av.require_online(two["disk1"])

    def test_missing_sentinel_is_offline(self, two):
        st = av.status(two["disk1"], max_age=0)
        assert not st.online and "sentinel" in st.reason
        with pytest.raises(av.ArchiveVolumeOffline, match="disk1"):
            av.require_online(two["disk1"])

    def test_wrong_disk_is_offline(self, two):
        av.write_sentinel(two["disk1"].root, "disk10")
        st = av.status(two["disk1"], max_age=0)
        assert not st.online and "wrong disk" in st.reason

    def test_missing_root_and_garbage_sentinel(self, two, tmp_path):
        assert "root missing" in av.status(ArchiveVolume("disk3", tmp_path / "nope"), max_age=0).reason
        (two["disk1"].root / av.SENTINEL_NAME).write_text("{not json")
        assert "unreadable" in av.status(two["disk1"], max_age=0).reason

    def test_cached_until_invalidated(self, two):
        assert not av.status(two["disk1"]).online
        av.write_sentinel(two["disk1"].root, "disk1")
        assert not av.status(two["disk1"]).online  # cached
        av.invalidate_status_cache()
        assert av.status(two["disk1"]).online


class TestResolveVolume:
    @pytest.fixture(autouse=True)
    def _config(self, two):
        with patch.object(config, "ARCHIVE_VOLUMES", two), \
             patch.object(config, "DEFAULT_ARCHIVE_VOLUME", "disk1"), \
             patch.object(config, "DATASET_ARCHIVE_VOLUME", {"GUIDE": "disk10", "precise": "disk10"}):
            yield

    def test_order(self):
        assert av.resolve_volume("GUIDE", "disk1")[0].name == "disk1"        # YAML wins
        vol, why = av.resolve_volume("GUIDE")
        assert vol.name == "disk10" and "dataset_archive_volume" in why       # dataset default
        assert av.resolve_volume("PRECISE", dataset_slug="precise")[0].name == "disk10"  # by slug
        vol, why = av.resolve_volume("CRISP2/LVO")
        assert vol.name == "disk1" and "default_archive_volume" in why        # global default

    def test_unknown_override(self):
        with pytest.raises(av.ArchiveVolumeError, match="unknown archive volume"):
            av.resolve_volume("GUIDE", "disk9")
