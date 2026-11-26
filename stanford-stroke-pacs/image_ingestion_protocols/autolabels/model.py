"""Pure inputs and outputs shared by study rules."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Study:
    timepoint: str | None
    acquired: datetime | None
    clinical: dict
    series: tuple[dict, ...] = ()

    def timestamp(self, key):
        value = self.clinical.get(key)
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None


@dataclass(frozen=True)
class Decision:
    value: str | None
    reason: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StudyRule:
    name: str
    version: int
    output: str
    clinical_fields: tuple[str, ...]
    evaluate: Callable[[Study], Decision]
    # One projected header dictionary per child series, ordered by Series UID.
    dicom_fields: tuple[str, ...] = ()
    reference_label: str | None = None
    conflict: Callable | None = None
    summarize: Callable | None = None
    present: Callable | None = None


@dataclass(frozen=True)
class AcquisitionSeries:
    modality: str | None
    series_type: str | None
    acquired: datetime | None
    clock: str | None
    excluded: bool


@dataclass(frozen=True)
class EpisodeStudy(Study):
    series: tuple["AcquisitionSeries", ...] = ()
    uid: str = ""
    owner: str = ""
    episode: int | None = None
    kind: str | None = None
    clock: str | None = None
    excluded: bool = False


@dataclass(frozen=True)
class EnrollmentRule:
    name: str
    version: int
    output: str
    clinical_fields: tuple[str, ...]
    evaluate: Callable[[list[EpisodeStudy]], dict[str, Decision]]
    study_exclusion: tuple[str, str] | None = None
    series_exclusion: tuple[str, str] | None = None
    reference_label: str | None = None
    conflict: Callable | None = None
    summarize: Callable | None = None
    present: Callable | None = None
    on_enable: Callable | None = None
