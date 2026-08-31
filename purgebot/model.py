# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def require(condition: object, detail: str = "invariant violated") -> None:
    if not condition:
        raise InvariantViolation(detail)


class PurgeBotError(Exception):
    pass


class ConfigError(PurgeBotError):
    pass


class InvariantViolation(PurgeBotError):
    pass


class LostMutex(PurgeBotError):
    pass


class LedgerUnavailable(PurgeBotError):
    pass


class StopReached(PurgeBotError):
    pass


class OperatorRefused(PurgeBotError):
    pass


class InvalidRevision(PurgeBotError):
    def __init__(self, line: int | None, detail: str):
        super().__init__(f"line {line}: {detail}" if line else detail)
        self.line = line
        self.detail = detail


class SurfaceUnavailable(PurgeBotError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class InvalidEnumeration(PurgeBotError):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SelectorOverCap(PurgeBotError):
    def __init__(self, cap: int):
        super().__init__(f"more than {cap} distinct targets")
        self.cap = cap


class TargetMissing(PurgeBotError):
    def __init__(self, title: str):
        super().__init__(f"page {title!r} does not exist")
        self.title = title


class SelectorDrift(PurgeBotError):
    def __init__(self) -> None:
        super().__init__("selector passes disagree")


class IdentityUnknown(PurgeBotError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Action(StrEnum):
    PURGE="purge-page-cache"; PAGE_LINKS="refresh-page-links"
    CATEGORY="refresh-category-members"; TEMPLATE="refresh-template-transclusions"
class JobState(StrEnum):
    QUEUED="QUEUED"; RUNNING="RUNNING"; WAITING="WAITING"
    API_ACCEPTED="API_ACCEPTED"; NEEDS_OPERATOR="NEEDS_OPERATOR"
    PARTIAL="PARTIAL"; REJECTED="REJECTED"; CANCELLED="CANCELLED"
    COMPLETED_NOOP="COMPLETED_NOOP"
class TargetState(StrEnum):
    READY="READY"; WAITING="WAITING"; DISPATCHING="DISPATCHING"
    API_ACCEPTED="API_ACCEPTED"; FAILED="FAILED"; UNKNOWN="UNKNOWN"; CANCELLED="CANCELLED"
    CLOSED_UNVERIFIED="CLOSED_UNVERIFIED"
class AttemptState(StrEnum):
    DISPATCHING="DISPATCHING"; COMPLETED="COMPLETED"
    TRANSIENT="TRANSIENT"; AMBIGUOUS="AMBIGUOUS"; OPERATOR="OPERATOR"
class FailureKind(StrEnum): TRANSIENT="TRANSIENT"; AMBIGUOUS="AMBIGUOUS"; OPERATOR="OPERATOR"

FANOUT={Action.CATEGORY,Action.TEMPLATE}; FORCE=FANOUT|{Action.PAGE_LINKS}

@dataclass(frozen=True, slots=True, order=True)
class TargetIdentity: page_id:int; namespace_id:int; canonical_title:str
@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    kind:str; anchor_utc:datetime|None=None; interval_s:int|None=None
    month:int|None=None; day:int|None=None; hour:int|None=None; minute:int|None=None
@dataclass(frozen=True, slots=True)
class ControlEntry:
    request_id:str; action:Action; target:str; target_namespace:int
    schedule_key:str; schedule:ScheduleSpec; discussion_url:str|None; semantic_sha256:bytes
@dataclass(frozen=True, slots=True)
class ApiFailure(Exception): kind:FailureKind; code:str; retry_after_s:int|None=None

def canonical_bytes(v):
    return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
def digest(v): return sha256(canonical_bytes(v)).digest()
def claim_key(surface,id,slot,once):
    return digest([surface,id,None if once else slot.astimezone(UTC).isoformat(timespec="seconds")])

def latest_due_slot(s, introduced, now):
    if s.kind=="once": return introduced if introduced<=now else None
    if s.kind=="interval":
        if now<s.anchor_utc: return None
        n=int((now-s.anchor_utc).total_seconds()//s.interval_s)
        slot=s.anchor_utc+timedelta(seconds=n*s.interval_s)
        return slot if slot>=introduced else None
    for year in range(now.year,introduced.year-1,-1):
        try: slot=datetime(year,s.month,s.day,s.hour,s.minute,tzinfo=UTC)
        except ValueError: continue
        if introduced<=slot<=now: return slot
    return None


class SurfaceKind(StrEnum):
    SINGLE = "single"
    FANOUT = "fanout"


class SurfaceState(StrEnum):
    VALID = "VALID"
    PAUSED = "PAUSED"


TERMINAL_JOB_STATES = frozenset(
    {JobState.API_ACCEPTED, JobState.COMPLETED_NOOP, JobState.PARTIAL,
     JobState.REJECTED, JobState.CANCELLED}
)
OPEN_TARGET_STATES = frozenset(
    {TargetState.READY, TargetState.WAITING, TargetState.DISPATCHING, TargetState.UNKNOWN}
)
_JOB_END = frozenset(TERMINAL_JOB_STATES | {JobState.NEEDS_OPERATOR})
JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.QUEUED, JobState.RUNNING, JobState.WAITING} | _JOB_END),
    JobState.RUNNING: frozenset({JobState.RUNNING, JobState.WAITING} | _JOB_END),
    JobState.WAITING: frozenset({JobState.WAITING, JobState.RUNNING} | _JOB_END),
    JobState.NEEDS_OPERATOR: frozenset({JobState.WAITING, JobState.CANCELLED}),
    JobState.API_ACCEPTED: frozenset(),
    JobState.COMPLETED_NOOP: frozenset(),
    JobState.PARTIAL: frozenset(),
    JobState.REJECTED: frozenset(),
    JobState.CANCELLED: frozenset(),
}
TARGET_TRANSITIONS: dict[TargetState, frozenset[TargetState]] = {
    TargetState.READY: frozenset(
        {TargetState.DISPATCHING, TargetState.WAITING, TargetState.UNKNOWN, TargetState.CANCELLED}
    ),
    TargetState.WAITING: frozenset(
        {TargetState.DISPATCHING, TargetState.WAITING, TargetState.UNKNOWN, TargetState.CANCELLED}
    ),
    TargetState.DISPATCHING: frozenset(
        {TargetState.API_ACCEPTED, TargetState.FAILED, TargetState.WAITING, TargetState.UNKNOWN}
    ),
    TargetState.UNKNOWN: frozenset(
        {TargetState.DISPATCHING, TargetState.UNKNOWN,
         TargetState.FAILED, TargetState.CLOSED_UNVERIFIED}
    ),
    TargetState.API_ACCEPTED: frozenset(),
    TargetState.FAILED: frozenset(),
    TargetState.CANCELLED: frozenset(),
    TargetState.CLOSED_UNVERIFIED: frozenset(),
}
ATTEMPT_TRANSITIONS: dict[AttemptState, frozenset[AttemptState]] = {
    AttemptState.DISPATCHING: frozenset(
        {AttemptState.COMPLETED, AttemptState.TRANSIENT,
         AttemptState.AMBIGUOUS, AttemptState.OPERATOR}
    ),
    AttemptState.COMPLETED: frozenset(),
    AttemptState.TRANSIENT: frozenset(),
    AttemptState.AMBIGUOUS: frozenset(),
    AttemptState.OPERATOR: frozenset(),
}


def _require_transition(matrix, old, new, subject: str) -> None:
    if new not in matrix.get(old, frozenset()):
        raise InvariantViolation(f"illegal {subject} transition {old}->{new}")


def require_job_transition(old: JobState, new: JobState) -> None:
    _require_transition(JOB_TRANSITIONS, JobState(old), JobState(new), "job")


def require_target_transition(old: TargetState, new: TargetState) -> None:
    _require_transition(TARGET_TRANSITIONS, TargetState(old), TargetState(new), "target")


def require_attempt_transition(old: AttemptState, new: AttemptState) -> None:
    _require_transition(ATTEMPT_TRANSITIONS, AttemptState(old), AttemptState(new), "attempt")


def aggregate_terminal_state(target_states, attempt_count, unknown_replays_exhausted):
    """return the job state when work is done/needs review, otherwise return none"""
    states = tuple(TargetState(s) for s in target_states)
    if unknown_replays_exhausted:
        return JobState.NEEDS_OPERATOR
    if not states:
        require(attempt_count == 0, "COMPLETED_NOOP requires zero effect attempts")
        return JobState.COMPLETED_NOOP
    if any(s in OPEN_TARGET_STATES for s in states):
        return None
    if all(s is TargetState.API_ACCEPTED for s in states):
        return JobState.API_ACCEPTED
    if attempt_count == 0 and all(s is TargetState.CANCELLED for s in states):
        return JobState.CANCELLED
    return JobState.PARTIAL


def retry_delay_s(prior_attempts: int, delays: tuple[int, ...]) -> int:
    """return the retry delay and use the last value after the list ends"""
    require(delays, "retry_delays_s must not be empty")
    return delays[min(max(prior_attempts, 1), len(delays)) - 1]


@dataclass(frozen=True, slots=True)
class RawEntry:
    request_id: str
    action: Action
    target: str
    schedule_key: str
    schedule: ScheduleSpec
    discussion_url: str | None
    line: int


@dataclass(frozen=True, slots=True)
class PageInfo:
    page_id: int | None
    namespace_id: int
    canonical_title: str
    missing: bool
    redirect: bool


@dataclass(frozen=True, slots=True)
class ProtectionEntry:
    type: str
    level: str


@dataclass(frozen=True, slots=True)
class SurfaceRead:
    page_id: int
    title: str
    protection: tuple[ProtectionEntry, ...]
    revision_id: int
    author: str
    revision_timestamp: datetime
    wikitext: str
    content_sha256: bytes


@dataclass(frozen=True, slots=True)
class SurfaceSnapshot:
    page_id: int
    kind: SurfaceKind
    state: SurfaceState
    observed_title: str | None
    last_revision_id: int | None
    last_revision_author: str | None
    content_sha256: bytes | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class Authorization:
    allowed: bool
    code: str
    revision_id: int | None
    author: str | None


@dataclass(frozen=True, slots=True)
class RequestRecord:
    surface_page_id: int
    request_id: str
    action: Action
    target: str
    target_namespace: int
    schedule_key: str
    discussion_url: str | None
    semantic_sha256: bytes
    introduced_revision_id: int
    introduced_author: str
    introduced_at: datetime
    latest_revision_id: int
    active: bool
    suspended: bool


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: int
    surface_page_id: int
    request_id: str
    due_slot: datetime | None
    action: Action
    is_fanout: bool
    state: JobState
    selector_key: str
    target_namespace: int
    staging_failures: int
    not_before: datetime | None
    has_targets: bool


@dataclass(frozen=True, slots=True)
class TargetRecord:
    id: int
    job_id: int
    page_id: int
    namespace_id: int
    staged_title: str
    state: TargetState
    not_before: datetime | None
    retry_deadline: datetime | None
    singleton_replays: int
    last_code: str | None


@dataclass(frozen=True, slots=True)
class Reservation:
    attempt_id: int
    page_ids: tuple[int, ...]
    titles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WaitUntil:
    until: datetime
