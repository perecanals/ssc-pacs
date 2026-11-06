import pytest

from series_geometry import dominant_plane, image_plane


@pytest.mark.parametrize("orientation", [None, [], [0]*6, [1]*6, [float("nan")]*6])
def test_invalid_orientation_is_unknown(orientation):
    assert image_plane(orientation) is None


def test_tilted_axial_and_coronal_geometry():
    assert image_plane([1, 0, 0, 0, 0.98, -0.1989974874]) == "AXIAL"
    assert image_plane([1, 0, 0, 0, 0, -1]) == "CORONAL"
    assert image_plane([0, 1, 0, 0, 0, -1]) == "SAGITTAL"
    assert dominant_plane({"AXIAL": 50, "CORONAL": 1}, 51) == "AXIAL"
    assert dominant_plane({"AXIAL": 1}, 10) is None
