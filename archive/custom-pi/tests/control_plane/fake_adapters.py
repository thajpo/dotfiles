"""In-memory process, runtime, presentation, and event adapters.

These adapters intentionally model observations and state transitions only.  No
method creates a host process, container, tmux pane, session, or file outside
objects passed in memory by a test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
from types import MappingProxyType
from typing import Any, Mapping

try:
    from .helpers import (
        DEFAULT_FAILPOINT_CONTROLLER,
        FailpointController,
        sanitize_failpoint_context,
    )
except ImportError:  # Supports unittest discovery with this directory as top level.
    from helpers import (
        DEFAULT_FAILPOINT_CONTROLLER,
        FailpointController,
        sanitize_failpoint_context,
    )


@dataclass(frozen=True)
class ProcessObservation:
    process_id: str
    state: str
    command: tuple[str, ...]
    start_identity: str
    exit_code: int | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeObservation:
    runtime_id: str
    state: str
    writable: bool
    process_ids: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PresentationObservation:
    presentation_id: str
    state: str
    title: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlEvent:
    event_id: str
    sequence: int
    kind: str
    payload: Mapping[str, Any]
    emitted_at: str


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(_copy_mapping(value))


def _clone_process(value: ProcessObservation) -> ProcessObservation:
    return ProcessObservation(
        process_id=value.process_id,
        state=value.state,
        command=tuple(value.command),
        start_identity=value.start_identity,
        exit_code=value.exit_code,
        metadata=_frozen_mapping(value.metadata),
    )


def _clone_runtime(value: RuntimeObservation) -> RuntimeObservation:
    return RuntimeObservation(
        runtime_id=value.runtime_id,
        state=value.state,
        writable=value.writable,
        process_ids=tuple(value.process_ids),
        metadata=_frozen_mapping(value.metadata),
    )


def _clone_presentation(value: PresentationObservation) -> PresentationObservation:
    return PresentationObservation(
        presentation_id=value.presentation_id,
        state=value.state,
        title=value.title,
        metadata=_frozen_mapping(value.metadata),
    )


def _clone_event(value: ControlEvent) -> ControlEvent:
    return ControlEvent(
        event_id=value.event_id,
        sequence=value.sequence,
        kind=value.kind,
        payload=_frozen_mapping(value.payload),
        emitted_at=value.emitted_at,
    )


def _controller(value: FailpointController | None) -> FailpointController:
    return DEFAULT_FAILPOINT_CONTROLLER if value is None else value


class FakeProcessAdapter:
    """A process observation adapter with fake, non-host process identifiers."""

    def __init__(self, *, failpoints: FailpointController | None = None) -> None:
        self.failpoints = _controller(failpoints)
        self._next_id = 1000
        self._processes: dict[str, ProcessObservation] = {}

    def start(
        self,
        command: list[str] | tuple[str, ...],
        *,
        process_id: str | None = None,
        start_identity: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProcessObservation:
        identifier = process_id or f"fake-process-{self._next_id}"
        self._next_id += 1
        if identifier in self._processes:
            raise ValueError(f"process already exists: {identifier}")
        observation = ProcessObservation(
            process_id=identifier,
            state="running",
            command=tuple(str(item) for item in command),
            start_identity=start_identity or f"start-{identifier}",
            metadata=_frozen_mapping({key: str(value) for key, value in (metadata or {}).items()}),
        )
        self._processes[identifier] = observation
        return _clone_process(observation)

    def observe(self, process_id: str | None = None) -> ProcessObservation | tuple[ProcessObservation, ...] | None:
        if process_id is not None:
            value = self._processes.get(process_id)
            return None if value is None else _clone_process(value)
        return tuple(_clone_process(self._processes[key]) for key in sorted(self._processes))

    def stop(self, process_id: str, *, exit_code: int = 0) -> ProcessObservation:
        current = self._processes.get(process_id)
        if current is None:
            raise KeyError(process_id)
        if current.state == "stopped":
            return current
        updated = ProcessObservation(
            process_id=current.process_id,
            state="stopped",
            command=current.command,
            start_identity=current.start_identity,
            exit_code=exit_code,
            metadata=_frozen_mapping(current.metadata),
        )
        self._processes[process_id] = updated
        return _clone_process(updated)

    def mark_lost(self, process_id: str) -> ProcessObservation:
        current = self._processes.get(process_id)
        if current is None:
            raise KeyError(process_id)
        updated = ProcessObservation(
            process_id=current.process_id,
            state="lost",
            command=current.command,
            start_identity=current.start_identity,
            exit_code=current.exit_code,
            metadata=_frozen_mapping(current.metadata),
        )
        self._processes[process_id] = updated
        return _clone_process(updated)

    def snapshot(self) -> dict[str, Any]:
        return {
            process_id: {
                "process_id": observation.process_id,
                "state": observation.state,
                "command": observation.command,
                "start_identity": observation.start_identity,
                "exit_code": observation.exit_code,
                "metadata": dict(sorted(observation.metadata.items())),
            }
            for process_id, observation in sorted(self._processes.items())
        }

    @property
    def observations(self) -> dict[str, Any]:
        return self.snapshot()


class FakeRuntimeAdapter:
    """In-memory runtime/container adapter with before/after failpoint seams."""

    def __init__(self, *, failpoints: FailpointController | None = None) -> None:
        self.failpoints = _controller(failpoints)
        self._next_id = 1
        self._runtimes: dict[str, RuntimeObservation] = {}

    def create(
        self,
        *,
        runtime_id: str | None = None,
        writable: bool = False,
        process_ids: tuple[str, ...] | list[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeObservation:
        identifier = runtime_id or f"fake-runtime-{self._next_id}"
        self._next_id += 1
        if identifier in self._runtimes:
            raise ValueError(f"runtime already exists: {identifier}")
        context = sanitize_failpoint_context(
            {"runtime_id": identifier, "writable": writable}
        )
        self.failpoints.hit("runtime.create.before", context)
        observation = RuntimeObservation(
            runtime_id=identifier,
            state="running",
            writable=bool(writable),
            process_ids=tuple(sorted(str(item) for item in process_ids)),
            metadata=_frozen_mapping({key: str(value) for key, value in (metadata or {}).items()}),
        )
        self._runtimes[identifier] = observation
        self.failpoints.hit("runtime.create.after", context)
        return _clone_runtime(observation)

    def observe(self, runtime_id: str | None = None) -> RuntimeObservation | tuple[RuntimeObservation, ...] | None:
        if runtime_id is not None:
            value = self._runtimes.get(runtime_id)
            return None if value is None else _clone_runtime(value)
        return tuple(_clone_runtime(self._runtimes[key]) for key in sorted(self._runtimes))

    def stop(self, runtime_id: str) -> RuntimeObservation:
        current = self._runtimes.get(runtime_id)
        if current is None:
            raise KeyError(runtime_id)
        context = sanitize_failpoint_context({"runtime_id": runtime_id, "state": current.state})
        self.failpoints.hit("runtime.stop.before", context)
        if current.state != "stopped":
            updated = RuntimeObservation(
                runtime_id=current.runtime_id,
                state="stopped",
                writable=False,
                process_ids=current.process_ids,
                metadata=_frozen_mapping(current.metadata),
            )
            self._runtimes[runtime_id] = updated
        else:
            updated = current
        self.failpoints.hit("runtime.stop.after", context)
        return _clone_runtime(updated)

    def mark_unknown(self, runtime_id: str) -> RuntimeObservation:
        current = self._runtimes.get(runtime_id)
        if current is None:
            raise KeyError(runtime_id)
        updated = RuntimeObservation(
            runtime_id=current.runtime_id,
            state="unknown",
            writable=current.writable,
            process_ids=current.process_ids,
            metadata=_frozen_mapping(current.metadata),
        )
        self._runtimes[runtime_id] = updated
        return _clone_runtime(updated)

    def snapshot(self) -> dict[str, Any]:
        return {
            runtime_id: {
                "runtime_id": observation.runtime_id,
                "state": observation.state,
                "writable": observation.writable,
                "process_ids": observation.process_ids,
                "metadata": dict(sorted(observation.metadata.items())),
            }
            for runtime_id, observation in sorted(self._runtimes.items())
        }

    @property
    def observations(self) -> dict[str, Any]:
        return self.snapshot()


class FakePresentationAdapter:
    """Fake tmux/Herdr-like presentation surface with no host integration."""

    def __init__(self) -> None:
        self._presentations: dict[str, PresentationObservation] = {}
        self._next_id = 1

    def create(
        self,
        *,
        presentation_id: str | None = None,
        title: str = "fixture",
        metadata: Mapping[str, Any] | None = None,
    ) -> PresentationObservation:
        identifier = presentation_id or f"fake-presentation-{self._next_id}"
        self._next_id += 1
        if identifier in self._presentations:
            raise ValueError(f"presentation already exists: {identifier}")
        observation = PresentationObservation(
            presentation_id=identifier,
            state="visible",
            title=str(title),
            metadata=_frozen_mapping({key: str(value) for key, value in (metadata or {}).items()}),
        )
        self._presentations[identifier] = observation
        return _clone_presentation(observation)

    def observe(self, presentation_id: str | None = None) -> PresentationObservation | tuple[PresentationObservation, ...] | None:
        if presentation_id is not None:
            value = self._presentations.get(presentation_id)
            return None if value is None else _clone_presentation(value)
        return tuple(_clone_presentation(self._presentations[key]) for key in sorted(self._presentations))

    def close(self, presentation_id: str) -> PresentationObservation:
        current = self._presentations.get(presentation_id)
        if current is None:
            raise KeyError(presentation_id)
        updated = PresentationObservation(
            presentation_id=current.presentation_id,
            state="closed",
            title=current.title,
            metadata=_frozen_mapping(current.metadata),
        )
        self._presentations[presentation_id] = updated
        return _clone_presentation(updated)

    def snapshot(self) -> dict[str, Any]:
        return {
            presentation_id: {
                "presentation_id": observation.presentation_id,
                "state": observation.state,
                "title": observation.title,
                "metadata": dict(sorted(observation.metadata.items())),
            }
            for presentation_id, observation in sorted(self._presentations.items())
        }

    @property
    def observations(self) -> dict[str, Any]:
        return self.snapshot()


class FakeEventEmitter:
    """Transactional-outbox-shaped event collector for deterministic tests."""

    def __init__(self, *, failpoints: FailpointController | None = None) -> None:
        self.failpoints = _controller(failpoints)
        self._events: list[ControlEvent] = []

    def emit(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        event_id: str | None = None,
        emitted_at: str = "2024-01-01T00:00:00.000000Z",
    ) -> ControlEvent:
        sequence = len(self._events) + 1
        identifier = event_id or f"event-{sequence:04d}"
        if any(event.event_id == identifier for event in self._events):
            raise ValueError(f"event already exists: {identifier}")
        context = sanitize_failpoint_context(
            {"event_id": identifier, "sequence": sequence, "kind": kind}
        )
        self.failpoints.hit("event.commit.before", context)
        event = ControlEvent(
            event_id=identifier,
            sequence=sequence,
            kind=str(kind),
            payload=_frozen_mapping(payload),
            emitted_at=str(emitted_at),
        )
        self._events.append(event)
        self.failpoints.hit("event.commit.after", context)
        return _clone_event(event)

    def list(self, *, after_sequence: int = 0) -> tuple[ControlEvent, ...]:
        return tuple(
            _clone_event(event)
            for event in self._events
            if event.sequence > after_sequence
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "event_id": event.event_id,
                "sequence": event.sequence,
                "kind": event.kind,
                "payload": copy.deepcopy(dict(event.payload)),
                "emitted_at": event.emitted_at,
            }
            for event in self._events
        ]

    @property
    def events(self) -> tuple[ControlEvent, ...]:
        return tuple(_clone_event(event) for event in self._events)

    @property
    def observations(self) -> list[dict[str, Any]]:
        return self.snapshot()


FakeProcessRuntimeAdapter = FakeRuntimeAdapter
FakePresentation = FakePresentationAdapter
FakeProcess = FakeProcessAdapter
FakeRuntime = FakeRuntimeAdapter
FakeEvents = FakeEventEmitter


__all__ = [
    "ControlEvent",
    "FakeEventEmitter",
    "FakeEvents",
    "FakePresentation",
    "FakePresentationAdapter",
    "FakeProcess",
    "FakeProcessAdapter",
    "FakeProcessRuntimeAdapter",
    "FakeRuntime",
    "FakeRuntimeAdapter",
    "PresentationObservation",
    "ProcessObservation",
    "RuntimeObservation",
]
