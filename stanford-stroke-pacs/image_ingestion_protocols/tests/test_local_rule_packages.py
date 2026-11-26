import pytest

from autolabels import load_packages


def test_installation_owned_packages(tmp_path):
    assert load_packages(tmp_path) == ({}, {})
    package = tmp_path / "example"
    package.mkdir()
    (package / "evaluate.py").write_text(
        "from autolabels.model import Decision\n"
        "def classify(study):\n    return Decision(study.timepoint, 'general')\n"
    )
    declaration = (
        "from autolabels.model import StudyRule\nfrom .evaluate import classify\n"
        "RULES = [StudyRule('visit', 1, 'visit', (), classify)]\n"
        "ANCHORS = {'visit': (('visit_start', 0),)}\n"
    )
    (package / "__init__.py").write_text(declaration)
    rules, anchors = load_packages(tmp_path)
    assert list(rules) == [("visit", 1)]
    assert anchors == {"visit": (("visit_start", 0),)}
    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    (duplicate / "__init__.py").write_text("from autolabels.model import StudyRule\n"
                                           "RULES = [StudyRule('visit', 1, 'visit', (), None)]\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_packages(tmp_path)
