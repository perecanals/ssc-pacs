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
