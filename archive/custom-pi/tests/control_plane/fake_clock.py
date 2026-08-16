"""Deterministic wall and monotonic clocks for control-plane tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import re
import secrets
import threading
from typing import Any


UTC = timezone.utc
_DEFAULT_START = datetime(2024, 1, 1, tzinfo=UTC)
_ORIGIN_LOCK = threading.Lock()
_ORIGIN_REGISTRY: set[str] = set()
_RFC3339_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


class ClockError(ValueError):
    """Base error for invalid deterministic clock input."""


class MonotonicOriginMismatch(ClockError):
    """Raised instead of comparing timestamps from different process origins."""


class InvalidUTCDateTime(ClockError):
    """Raised when a wall-clock value is naive or not UTC-normalizable."""


def _ensure_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock value must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidUTCDateTime("wall-clock timestamps must be timezone-aware")
    return value.astimezone(UTC)


def format_rfc3339_utc(value: datetime, *, timespec: str = "microseconds") -> str:
    """Format an aware datetime as deterministic RFC3339 UTC with ``Z``."""

    normalized = _ensure_utc(value)
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_rfc3339_utc(value: str) -> datetime:
    """Parse RFC3339 values and return an aware UTC datetime."""

    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        raise InvalidUTCDateTime(f"invalid RFC3339 timestamp: {value!r}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise InvalidUTCDateTime(str(error)) from error
    return _ensure_utc(parsed)


@dataclass(frozen=True)
class MonotonicTimestamp:
    """A monotonic tick paired with the process origin that created it."""

    origin: str
    nanoseconds: int

    def _other(self, other: Any) -> "MonotonicTimestamp":
        if not isinstance(other, MonotonicTimestamp):
            raise TypeError("monotonic timestamps can only be compared to monotonic timestamps")
        if self.origin != other.origin:
            raise MonotonicOriginMismatch(
                f"cannot compare monotonic origins {self.origin!r} and {other.origin!r}"
            )
        return other

    def __lt__(self, other: Any) -> bool:
        return self.nanoseconds < self._other(other).nanoseconds

    def __le__(self, other: Any) -> bool:
        return self.nanoseconds <= self._other(other).nanoseconds

    def __gt__(self, other: Any) -> bool:
        return self.nanoseconds > self._other(other).nanoseconds

    def __ge__(self, other: Any) -> bool:
        return self.nanoseconds >= self._other(other).nanoseconds

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, MonotonicTimestamp):
            return NotImplemented
        if self.origin != other.origin:
            raise MonotonicOriginMismatch(
                f"cannot compare monotonic origins {self.origin!r} and {other.origin!r}"
            )
        return self.nanoseconds == other.nanoseconds

    def __sub__(self, other: Any) -> int:
        other_timestamp = self._other(other)
        return self.nanoseconds - other_timestamp.nanoseconds

    def __add__(self, seconds: int | float) -> "MonotonicTimestamp":
        if not isinstance(seconds, (int, float)):
            return NotImplemented
        return MonotonicTimestamp(
            self.origin,
            self.nanoseconds + _seconds_to_nanoseconds(seconds),
        )

    def __float__(self) -> float:
        return self.nanoseconds / 1_000_000_000

    @property
    def seconds(self) -> float:
        return float(self)


# Alternative descriptive spelling for callers that want to make units clear.
FakeMonotonicTimestamp = MonotonicTimestamp


def _seconds_to_nanoseconds(seconds: int | float) -> int:
    if not isinstance(seconds, (int, float)):
        raise TypeError("clock advance must be numeric seconds")
    result = round(float(seconds) * 1_000_000_000)
    if result < 0:
        raise ValueError("fake monotonic time cannot move backwards")
    return result


def _validate_origin(origin: Any) -> str:
    if not isinstance(origin, str) or not origin:
        raise ValueError("monotonic origin must be a non-empty string")
    return origin


def _issue_origin(origin: str | None) -> str:
    if origin is None:
        while True:
            candidate = f"fixture-process-{os.getpid()}-{secrets.token_hex(12)}"
            with _ORIGIN_LOCK:
                if candidate not in _ORIGIN_REGISTRY:
                    _ORIGIN_REGISTRY.add(candidate)
                    return candidate
    validated = _validate_origin(origin)
    with _ORIGIN_LOCK:
        if validated in _ORIGIN_REGISTRY:
            raise ClockError(f"monotonic origin has already been issued: {validated!r}")
        _ORIGIN_REGISTRY.add(validated)
    return validated


class FakeClock:
    """A wall/monotonic clock whose values advance only by explicit calls."""

    def __init__(
        self,
        *,
        start: datetime | str = _DEFAULT_START,
        monotonic_start: int | float = 0,
        origin: str | None = None,
    ) -> None:
        if isinstance(start, str):
            start_datetime = parse_rfc3339_utc(start)
        else:
            start_datetime = _ensure_utc(start)
        monotonic_value = _seconds_to_nanoseconds(monotonic_start)
        issued_origin = _issue_origin(origin)
        self._wall = start_datetime
        self._monotonic = monotonic_value
        self._origin = issued_origin
        # Every synthetic process receives a distinct origin, even when the
        # caller creates several fresh clocks without resetting this clock.
        self._generation = 0
        self._used_origins = {issued_origin}

    def now(self) -> datetime:
        return self._wall

    def utc_now(self) -> datetime:
        return self._wall

    def rfc3339(self, *, timespec: str = "microseconds") -> str:
        return format_rfc3339_utc(self._wall, timespec=timespec)

    def monotonic(self) -> MonotonicTimestamp:
        return MonotonicTimestamp(self._origin, self._monotonic)

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def monotonic_origin(self) -> str:
        return self._origin

    def advance(self, seconds: int | float) -> MonotonicTimestamp:
        nanoseconds = _seconds_to_nanoseconds(seconds)
        self._wall += timedelta(microseconds=nanoseconds / 1_000)
        self._monotonic += nanoseconds
        return self.monotonic()

    def advance_wall(self, seconds: int | float) -> datetime:
        nanoseconds = _seconds_to_nanoseconds(seconds)
        self._wall += timedelta(microseconds=nanoseconds / 1_000)
        return self._wall

    def advance_monotonic(self, seconds: int | float) -> MonotonicTimestamp:
        self._monotonic += _seconds_to_nanoseconds(seconds)
        return self.monotonic()

    def sleep(self, seconds: int | float) -> None:
        self.advance(seconds)

    def reset_monotonic(self, *, origin: str | None = None, seconds: int | float = 0) -> None:
        """Model a process restart/reboot by changing monotonic origin."""

        monotonic_value = _seconds_to_nanoseconds(seconds)
        next_generation = self._generation + 1
        next_origin = (
            _validate_origin(origin)
            if origin is not None
            else f"{self._origin.split('#', 1)[0]}#{next_generation}"
        )
        if next_origin in self._used_origins:
            raise ClockError(f"monotonic origin has already been used: {next_origin!r}")
        with _ORIGIN_LOCK:
            if next_origin in _ORIGIN_REGISTRY:
                raise ClockError(f"monotonic origin has already been issued: {next_origin!r}")
            _ORIGIN_REGISTRY.add(next_origin)
        self._generation = next_generation
        self._used_origins.add(next_origin)
        self._origin = next_origin
        self._monotonic = monotonic_value

    def new_process(self, *, origin: str | None = None) -> "FakeClock":
        """Create a fresh-origin clock at the current wall time.

        The allocation counter advances even when the caller supplies an
        explicit origin, so repeated calls cannot accidentally share a
        monotonic comparison domain.
        """

        next_generation = self._generation + 1
        next_origin = (
            _validate_origin(origin)
            if origin is not None
            else f"{self._origin.split('#', 1)[0]}#{next_generation}"
        )
        if next_origin in self._used_origins:
            raise ClockError(f"monotonic origin has already been used: {next_origin!r}")
        child = FakeClock(start=self._wall, monotonic_start=0, origin=next_origin)
        self._generation = next_generation
        self._used_origins.add(next_origin)
        return child

    def snapshot(self) -> dict[str, Any]:
        return {
            "wall": self.rfc3339(),
            "monotonic_origin": self._origin,
            "monotonic_nanoseconds": self._monotonic,
        }


DeterministicClock = FakeClock
FakeWallClock = FakeClock


__all__ = [
    "UTC",
    "ClockError",
    "DeterministicClock",
    "FakeClock",
    "FakeMonotonicTimestamp",
    "FakeWallClock",
    "InvalidUTCDateTime",
    "MonotonicOriginMismatch",
    "MonotonicTimestamp",
    "format_rfc3339_utc",
    "parse_rfc3339_utc",
]
