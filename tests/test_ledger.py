# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from conftest import FANOUT_SURFACE, PERMALINK, SAFETY
from purgebot.ledger import Ledger, release_time
from purgebot.model import (
    FORCE, Action, ApiFailure, Authorization, ControlEntry, FailureKind, InvalidRevision,
    InvariantViolation, JobState, LedgerUnavailable, OperatorRefused, ProtectionEntry,
    ScheduleSpec, SurfaceRead, TargetIdentity, TargetState, WaitUntil, aggregate_terminal_state,
    claim_key, digest, latest_due_slot, require_job_transition, require_target_transition,
    retry_delay_s,
)

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
ONCE = ScheduleSpec(kind="once")
DAILY = ScheduleSpec(kind="interval", anchor_utc=datetime(2026, 1, 1, tzinfo=UTC),
                     interval_s=86400)
CATALOG = {"once": ONCE, "daily": DAILY}
AUTH = Authorization(True, "authorized", 2000, "Op")


# state model tests

def rows(*reserved_at):
    return [{"reserved_at": t, "target_count": 1} for t in reserved_at]


def test_release_time_frees_capacity_when_the_oldest_debit_ages_out():
    assert release_time(rows(), lambda r: 1, 1, 3) is None
    assert release_time(rows(T0, T0 + timedelta(minutes=1)), lambda r: 1, 1, 3) is None
    full = rows(T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    assert release_time(full, lambda r: 1, 1, 3) == T0 + timedelta(hours=24)
    assert release_time(full, lambda r: 1, 2, 3) == T0 + timedelta(hours=25)
    with pytest.raises(InvariantViolation):
        release_time(full, lambda r: 1, 4, 3)


def test_aggregation_precedence_is_defined_in_exactly_one_place():
    accepted, failed = TargetState.API_ACCEPTED, TargetState.FAILED
    unknown, cancelled = TargetState.UNKNOWN, TargetState.CANCELLED
    closed = TargetState.CLOSED_UNVERIFIED
    assert aggregate_terminal_state([], 0, False) is JobState.COMPLETED_NOOP
    assert aggregate_terminal_state([unknown], 1, True) is JobState.NEEDS_OPERATOR
    assert aggregate_terminal_state([accepted, unknown], 1, True) is JobState.NEEDS_OPERATOR
    assert aggregate_terminal_state([accepted, unknown], 1, False) is None
    assert aggregate_terminal_state([accepted, TargetState.READY], 1, False) is None
    assert aggregate_terminal_state([accepted, accepted], 1, False) is JobState.API_ACCEPTED
    assert aggregate_terminal_state([cancelled], 0, False) is JobState.CANCELLED
    assert aggregate_terminal_state([cancelled], 1, False) is JobState.PARTIAL
    assert aggregate_terminal_state([accepted, failed], 1, False) is JobState.PARTIAL
    assert aggregate_terminal_state([accepted, closed], 1, False) is JobState.PARTIAL
    assert aggregate_terminal_state([failed], 1, False) is JobState.PARTIAL


def test_terminal_states_are_immutable_and_illegal_transitions_are_invariants():
    require_job_transition(JobState.QUEUED, JobState.RUNNING)
    require_job_transition(JobState.NEEDS_OPERATOR, JobState.WAITING)
    for terminal in (JobState.API_ACCEPTED, JobState.COMPLETED_NOOP, JobState.PARTIAL,
                     JobState.REJECTED, JobState.CANCELLED):
        with pytest.raises(InvariantViolation):
            require_job_transition(terminal, JobState.RUNNING)
    with pytest.raises(InvariantViolation):
        require_job_transition(JobState.RUNNING, JobState.QUEUED)
    require_target_transition(TargetState.UNKNOWN, TargetState.CLOSED_UNVERIFIED)
    require_target_transition(TargetState.UNKNOWN, TargetState.DISPATCHING)
    for illegal in ((TargetState.UNKNOWN, TargetState.API_ACCEPTED),
                    (TargetState.API_ACCEPTED, TargetState.READY),
                    (TargetState.CANCELLED, TargetState.DISPATCHING),
                    (TargetState.READY, TargetState.API_ACCEPTED),
                    (TargetState.DISPATCHING, TargetState.CANCELLED)):
        with pytest.raises(InvariantViolation):
            require_target_transition(*illegal)


def test_retry_delays_walk_the_configured_scale_and_stop_at_the_last():
    delays = SAFETY.retry_delays_s
    assert [retry_delay_s(n, delays) for n in (1, 2, 3, 4, 5)] == list(delays)
    assert retry_delay_s(6, delays) == delays[-1] and retry_delay_s(0, delays) == delays[0]


def test_recurrence_produces_only_the_latest_due_anchor():
    introduced = datetime(2026, 8, 1, tzinfo=UTC)
    assert latest_due_slot(DAILY, introduced, datetime(2026, 8, 6, 3, tzinfo=UTC)) \
        == datetime(2026, 8, 6, tzinfo=UTC)
    midday = datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert latest_due_slot(DAILY, midday, datetime(2026, 8, 1, 18, tzinfo=UTC)) is None
    assert latest_due_slot(DAILY, introduced, datetime(2026, 8, 1, 3, tzinfo=UTC)) == introduced
    assert latest_due_slot(ONCE, introduced, introduced) == introduced
    annual = ScheduleSpec(kind="annual", month=2, day=29, hour=0, minute=0)
    assert latest_due_slot(annual, datetime(2020, 1, 1, tzinfo=UTC),
                           datetime(2026, 8, 1, tzinfo=UTC)) == datetime(2024, 2, 29, tzinfo=UTC)


def test_one_off_and_recurrence_claim_keys_differ_in_the_documented_way():
    once_key = claim_key(222, "m1", T0, True)
    assert once_key == claim_key(222, "m1", T0 + timedelta(days=5), True)
    assert claim_key(222, "m1", T0, False) != claim_key(222, "m1", T0 + timedelta(days=1), False)
    assert claim_key(222, "m1", T0, True) != claim_key(111, "m1", T0, True)


# database integration tests

def entry(request_id, action, target, namespace, schedule_key, schedule, discussion=None):
    return ControlEntry(request_id, action, target, namespace, schedule_key, schedule, discussion,
                        digest([str(action), namespace, target, schedule_key, discussion]))


def read(revision_id, wikitext, gate=FANOUT_SURFACE):
    return SurfaceRead(gate.page_id, gate.expected_title,
                       (ProtectionEntry("edit", sorted(gate.allowed_edit_levels)[0]),),
                       revision_id, "Op", T0, wikitext, digest(wikitext))


def fanout_request(db, request_id="m1", target="Category:Foo", schedule="once", revision=2000):
    db.reconcile_surface(FANOUT_SURFACE, read(revision, f"rev-{revision}"),
                         (entry(request_id, Action.CATEGORY, target, 14, schedule,
                                CATALOG[schedule], PERMALINK + "9"),), T0)


def staged(db, targets, now=T0, safety=SAFETY):
    db.claim_current_due_slots(CATALOG, now)
    job = db.next_work(now, set())
    assert job is not None and not job.has_targets
    db.materialize_targets(job.id, targets, digest([1]), safety.open_fanout_targets, now)
    return db.next_work(now, set())


def query(db, sql, args=()):
    with db.transaction() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def dispatch(db, job, batch, now=T0, safety=SAFETY):
    titles = {t.page_id: t.staged_title for t in batch}
    return db.reserve_dispatch(job, batch, titles, AUTH, safety, now)


def test_an_empty_staged_set_is_a_no_op_that_leaves_a_permanent_tombstone(ledger):
    fanout_request(ledger)
    ledger.claim_current_due_slots(CATALOG, T0)
    job = ledger.next_work(T0, set())
    assert ledger.materialize_targets(job.id, (), digest([]), SAFETY.open_fanout_targets, T0)
    assert query(ledger, "SELECT state,reason_code FROM jobs")[0] \
        == {"state": "COMPLETED_NOOP", "reason_code": "empty-staged-set"}
    assert query(ledger, "SELECT COUNT(*) n FROM attempts")[0]["n"] == 0

    fanout_request(ledger, revision=2001)
    ledger.claim_current_due_slots(CATALOG, T0 + timedelta(days=3))
    assert len(query(ledger, "SELECT id FROM jobs")) == 1


def test_a_recurring_request_never_accumulates_catch_up_jobs(ledger):
    fanout_request(ledger, schedule="daily")
    job = staged(ledger, (TargetIdentity(5, 0, "A"),), now=T0 + timedelta(days=1, hours=3))
    ledger.claim_current_due_slots(CATALOG, T0 + timedelta(days=6, hours=3))
    assert len(query(ledger, "SELECT id FROM jobs")) == 1

    later = T0 + timedelta(days=6, hours=3)
    batch = ledger.select_batch(job.id, SAFETY, later)
    reservation = dispatch(ledger, job, batch, later)
    ledger.finalize_outcomes(reservation.attempt_id, {"purge": []},
                             {5: TargetState.API_ACCEPTED}, later, SAFETY)
    ledger.claim_current_due_slots(CATALOG, later)
    jobs = query(ledger, "SELECT state,due_slot FROM jobs ORDER BY id")
    assert len(jobs) == 2 and jobs[1]["due_slot"] == datetime(2026, 8, 7)


def test_the_open_fan_out_cap_is_all_or_nothing(ledger):
    tiny = dataclasses.replace(SAFETY, open_fanout_targets=1)
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    ledger.reconcile_surface(
        FANOUT_SURFACE, read(2002, "rev-2002"),
        (entry("m1", Action.CATEGORY, "Category:Foo", 14, "once", ONCE, PERMALINK + "9"),
         entry("m2", Action.TEMPLATE, "Template:Bar", 10, "once", ONCE, PERMALINK + "9")), T0)
    ledger.claim_current_due_slots(CATALOG, T0)
    second = ledger.next_work(T0, {job.id})
    assert not ledger.materialize_targets(second.id, (TargetIdentity(6, 0, "B"),), digest([2]),
                                          tiny.open_fanout_targets, T0)
    assert query(ledger, "SELECT COUNT(*) n FROM targets WHERE job_id=%s",
                 (second.id,))[0]["n"] == 0
    assert query(ledger, "SELECT state FROM jobs WHERE id=%s", (second.id,))[0]["state"] == "QUEUED"


def test_rejecting_an_unmaterialised_job_sets_terminal_state(ledger):
    fanout_request(ledger)
    ledger.claim_current_due_slots(CATALOG, T0)
    job = ledger.next_work(T0, set())
    ledger.reject_job(job.id, "cap-exceeded", T0)
    assert query(ledger, "SELECT state FROM jobs")[0]["state"] == "REJECTED"


def test_a_reservation_debits_the_budget_and_a_wait_does_not(ledger):
    fanout_request(ledger)
    job = staged(ledger, tuple(TargetIdentity(i, 0, f"P{i}") for i in (1, 2, 3, 4)))
    first = ledger.select_batch(job.id, SAFETY, T0)[:2]
    reservation = dispatch(ledger, job, first)
    ledger.finalize_outcomes(reservation.attempt_id, {"purge": []},
                             {t.page_id: TargetState.API_ACCEPTED for t in first}, T0, SAFETY)

    job = ledger.next_work(T0, set())
    waited = dispatch(ledger, job, ledger.select_batch(job.id, SAFETY, T0))
    assert isinstance(waited, WaitUntil)
    assert waited.until == T0 + timedelta(seconds=SAFETY.post_start_interval_s)
    assert query(ledger, "SELECT COUNT(*) n FROM attempts")[0]["n"] == 1
    assert ledger.next_work(T0, set()) is None
    assert ledger.next_work(T0 + timedelta(seconds=31), set()) is not None


def test_the_post_budget_defers_instead_of_overdrawing(ledger):
    one_post = dataclasses.replace(SAFETY, effect_posts_24h=1, post_start_interval_s=0)
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(1, 0, "A"), TargetIdentity(2, 0, "B")))
    first = ledger.select_batch(job.id, one_post, T0)[:1]
    reservation = dispatch(ledger, job, first, safety=one_post)
    ledger.finalize_outcomes(reservation.attempt_id, {"purge": []},
                             {1: TargetState.API_ACCEPTED}, T0, one_post)
    job = ledger.next_work(T0, set())
    waited = dispatch(ledger, job, ledger.select_batch(job.id, one_post, T0), safety=one_post)
    assert isinstance(waited, WaitUntil) and waited.until == T0 + timedelta(hours=24)


def test_a_target_that_exhausted_its_daily_attempts_waits_for_the_oldest_to_age_out(ledger):
    two_a_day = dataclasses.replace(SAFETY, attempts_per_target_24h=2, post_start_interval_s=0,
                                    retry_window_s=10 ** 7)
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    at = T0
    for _ in range(2):
        batch = ledger.select_batch(job.id, two_a_day, at)
        reservation = dispatch(ledger, job, batch, at, two_a_day)
        ledger.finalize_failure(reservation.attempt_id,
                                ApiFailure(FailureKind.TRANSIENT, "http-500"), at, two_a_day)
        at += timedelta(hours=1)
        job = ledger.next_work(at, set())
    waited = dispatch(ledger, job, ledger.select_batch(job.id, two_a_day, at), at, two_a_day)
    assert isinstance(waited, WaitUntil) and waited.until == T0 + timedelta(hours=24)
    assert query(ledger, "SELECT COUNT(*) n FROM attempts")[0]["n"] == 2


def test_a_wait_past_the_retry_deadline_escalates_instead_of_sleeping(ledger):
    short_window = dataclasses.replace(SAFETY, retry_window_s=60, attempts_per_target_24h=1,
                                       post_start_interval_s=0)
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    reservation = dispatch(ledger, job, ledger.select_batch(job.id, short_window, T0),
                           safety=short_window)
    ledger.finalize_failure(reservation.attempt_id,
                            ApiFailure(FailureKind.TRANSIENT, "http-500"), T0, short_window)
    later = T0 + timedelta(seconds=61)
    job = ledger.next_work(later, set())
    outcome = dispatch(ledger, job, ledger.select_batch(job.id, short_window, later), later,
                       short_window)
    assert isinstance(outcome, WaitUntil)
    assert query(ledger, "SELECT state,reason_code FROM jobs")[0] \
        == {"state": "NEEDS_OPERATOR", "reason_code": "retry-deadline-exceeded"}
    assert query(ledger, "SELECT suspended FROM requests")[0]["suspended"] == 1


def test_a_transient_failure_waits_for_the_larger_of_backoff_and_retry_after(ledger):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    reservation = dispatch(ledger, job, ledger.select_batch(job.id, SAFETY, T0))
    ledger.finalize_failure(reservation.attempt_id,
                            ApiFailure(FailureKind.TRANSIENT, "maxlag", 900), T0, SAFETY)
    target = query(ledger, "SELECT state,not_before,last_code FROM targets")[0]
    assert target["state"] == "WAITING" and target["last_code"] == "maxlag"
    assert target["not_before"] == datetime(2026, 8, 1, 0, 15)
    attempt = query(ledger, "SELECT state,api_code,retry_after_s FROM attempts")[0]
    assert (attempt["state"], attempt["api_code"], attempt["retry_after_s"]) \
        == ("TRANSIENT", "maxlag", 900)
    assert query(ledger, "SELECT outcome FROM attempt_targets")[0]["outcome"] == "TRANSIENT"


def test_ambiguity_gets_exactly_one_singleton_replay_then_the_operator(ledger):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"), TargetIdentity(6, 0, "B")))
    reservation = dispatch(ledger, job, ledger.select_batch(job.id, SAFETY, T0))
    ledger.finalize_failure(reservation.attempt_id,
                            ApiFailure(FailureKind.AMBIGUOUS, "read-or-connection-loss"),
                            T0, SAFETY)
    assert [t["state"] for t in query(ledger, "SELECT state FROM targets")] == ["UNKNOWN"] * 2
    assert query(ledger, "SELECT state FROM jobs")[0]["state"] == "WAITING"

    later = T0 + timedelta(minutes=2)
    job = ledger.next_work(later, set())
    batch = ledger.select_batch(job.id, SAFETY, later)
    assert len(batch) == 1 and batch[0].state is TargetState.UNKNOWN

    reservation = dispatch(ledger, job, batch, later)
    ledger.finalize_outcomes(reservation.attempt_id, {"purge": []},
                             {batch[0].page_id: TargetState.UNKNOWN}, later, SAFETY)
    assert query(ledger, "SELECT state,reason_code FROM jobs")[0] \
        == {"state": "NEEDS_OPERATOR", "reason_code": "ambiguous-replays-exhausted"}
    assert query(ledger, "SELECT suspended FROM requests")[0]["suspended"] == 1


def test_force_actions_use_the_smaller_batch(ledger):
    fanout_request(ledger)
    job = staged(ledger, tuple(TargetIdentity(i, 0, f"P{i}") for i in range(1, 40)))
    assert job.action in FORCE
    assert len(ledger.select_batch(job.id, SAFETY, T0)) == SAFETY.force_batch


def test_an_unknown_target_blocks_the_rest_of_the_job(ledger):
    fanout_request(ledger)
    job = staged(ledger, tuple(TargetIdentity(i, 0, f"P{i}")
                               for i in range(1, SAFETY.force_batch + 2)))
    batch = ledger.select_batch(job.id, SAFETY, T0)
    reservation = dispatch(ledger, job, batch)
    outcomes = {target.page_id: TargetState.API_ACCEPTED for target in batch}
    outcomes[batch[0].page_id] = TargetState.UNKNOWN
    ledger.finalize_outcomes(reservation.attempt_id, {"purge": []},
                             outcomes, T0, SAFETY)
    assert ledger.next_work(T0, set()) is None
    later = T0 + timedelta(minutes=2)
    current = ledger.next_work(later, set())
    remaining = ledger.select_batch(current.id, SAFETY, later)
    assert [t.page_id for t in remaining] == [batch[0].page_id]


def test_an_authentication_failure_stops_the_job_without_a_retry(ledger):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    reservation = dispatch(ledger, job, ledger.select_batch(job.id, SAFETY, T0))
    ledger.finalize_failure(reservation.attempt_id,
                            ApiFailure(FailureKind.OPERATOR, "assertuserfailed"), T0, SAFETY)
    assert query(ledger, "SELECT state,reason_code FROM jobs")[0] \
        == {"state": "NEEDS_OPERATOR", "reason_code": "assertuserfailed"}
    assert ledger.next_work(T0, set()) is None


def test_a_crash_after_dispatching_is_recovered_conservatively(ledger, db_cfg):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    dispatch(ledger, job, ledger.select_batch(job.id, SAFETY, T0))
    ledger.close_releasing_mutex_if_healthy()

    restarted = Ledger.connect(db_cfg)
    assert restarted.acquire_mutex()
    try:
        restarted.recover_dispatching(T0 + timedelta(minutes=5), SAFETY)
        assert query(restarted, "SELECT state,api_code FROM attempts")[0] \
            == {"state": "AMBIGUOUS", "api_code": "worker-crash"}
        assert query(restarted, "SELECT state,last_code FROM targets")[0] \
            == {"state": "UNKNOWN", "last_code": "worker-crash"}
        restarted.recover_dispatching(T0 + timedelta(minutes=6), SAFETY)
        assert query(restarted, "SELECT COUNT(*) n FROM attempts")[0]["n"] == 1
    finally:
        restarted.close_releasing_mutex_if_healthy()


def test_identity_failures_before_a_post_consume_a_replay_without_an_attempt(ledger):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    batch = ledger.select_batch(job.id, SAFETY, T0)
    ledger.record_identity_unknown(batch, "page-missing", T0, SAFETY)
    assert query(ledger, "SELECT COUNT(*) n FROM attempts")[0]["n"] == 0
    assert query(ledger, "SELECT state,singleton_replays FROM targets")[0] \
        == {"state": "UNKNOWN", "singleton_replays": 0}

    later = T0 + timedelta(minutes=2)
    again = ledger.select_batch(job.id, SAFETY, later)
    ledger.record_identity_unknown(again, "page-missing", later, SAFETY)
    assert query(ledger, "SELECT singleton_replays FROM targets")[0]["singleton_replays"] == 1
    assert query(ledger, "SELECT state FROM jobs")[0]["state"] == "NEEDS_OPERATOR"


def test_removing_a_request_cancels_only_undispatched_work(ledger):
    fanout_request(ledger)
    staged(ledger, (TargetIdentity(5, 0, "A"),))
    ledger.reconcile_surface(FANOUT_SURFACE, read(2003, "rev-2003"), (), T0)
    assert query(ledger, "SELECT active FROM requests")[0]["active"] == 0
    assert query(ledger, "SELECT state FROM targets")[0]["state"] == "CANCELLED"
    assert query(ledger, "SELECT state FROM jobs")[0]["state"] == "CANCELLED"


def test_removing_a_request_before_staging_cancels_the_job(ledger):
    fanout_request(ledger)
    ledger.claim_current_due_slots(CATALOG, T0)
    job = ledger.next_work(T0, set())
    ledger.reconcile_surface(FANOUT_SURFACE, read(2003, "rev-2003"), (), T0)
    assert query(ledger, "SELECT state,reason_code FROM jobs WHERE id=%s", (job.id,))[0] \
        == {"state": "CANCELLED", "reason_code": "request-removed"}


def test_reusing_a_request_id_with_other_semantics_invalidates_the_revision(ledger):
    fanout_request(ledger)
    with pytest.raises(InvalidRevision):
        ledger.reconcile_surface(
            FANOUT_SURFACE, read(2004, "rev-2004"),
            (entry("m1", Action.TEMPLATE, "Template:Other", 10, "once", ONCE, PERMALINK + "9"),),
            T0)
    row = query(ledger, "SELECT action,target,latest_revision_id FROM requests")[0]
    assert row["action"] == "refresh-category-members" and row["latest_revision_id"] == 2000


def test_the_operator_cannot_manufacture_an_api_acceptance(ledger):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"), TargetIdentity(6, 0, "B")))
    reservation = dispatch(ledger, job, ledger.select_batch(job.id, SAFETY, T0))
    ledger.finalize_outcomes(reservation.attempt_id, {"purge": []},
                             {5: TargetState.API_ACCEPTED, 6: TargetState.UNKNOWN}, T0, SAFETY)
    for refused in ("api-accepted", "API_ACCEPTED", "accepted", "purged"):
        with pytest.raises(OperatorRefused):
            ledger.operator_resolve_target(2, refused, "op", "reason", T0)
    ledger.operator_resolve_target(2, "closed-unverified", "op", "human accepted", T0)
    assert [t["state"] for t in query(ledger, "SELECT state FROM targets ORDER BY id")] \
        == ["API_ACCEPTED", "CLOSED_UNVERIFIED"]
    assert query(ledger, "SELECT state FROM jobs")[0]["state"] == "PARTIAL"
    assert query(ledger, "SELECT operation FROM operator_events")[0]["operation"] \
        == "resolve-target"


def test_operator_commands_refuse_the_states_they_must_not_touch(ledger):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    with pytest.raises(OperatorRefused):
        ledger.operator_resume_job(job.id, "op", "reason", SAFETY, T0)
    with pytest.raises(OperatorRefused):
        ledger.operator_resolve_target(1, "failed", "op", "reason", T0)

    ledger.operator_cancel_job(job.id, "op", "not needed", T0)
    assert query(ledger, "SELECT state FROM jobs")[0]["state"] == "CANCELLED"
    assert query(ledger, "SELECT state FROM targets")[0]["state"] == "CANCELLED"
    assert query(ledger, "SELECT operator,reason FROM operator_events")[0] \
        == {"operator": "op", "reason": "not needed"}


def test_cancelling_is_refused_while_an_effect_may_still_be_in_flight(ledger):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    reservation = dispatch(ledger, job, ledger.select_batch(job.id, SAFETY, T0))
    ledger.finalize_outcomes(reservation.attempt_id, {"purge": []},
                             {5: TargetState.UNKNOWN}, T0, SAFETY)
    with pytest.raises(OperatorRefused):
        ledger.operator_cancel_job(job.id, "op", "reason", T0)


def test_resume_returns_a_job_to_work_but_leaves_the_request_suspended(ledger):
    fanout_request(ledger)
    job = staged(ledger, (TargetIdentity(5, 0, "A"),))
    reservation = dispatch(ledger, job, ledger.select_batch(job.id, SAFETY, T0))
    ledger.finalize_failure(reservation.attempt_id,
                            ApiFailure(FailureKind.OPERATOR, "assertuserfailed"), T0, SAFETY)
    ledger.operator_resume_job(job.id, "op", "password rotated", SAFETY, T0)
    row = query(ledger, "SELECT state,retry_deadline FROM jobs")[0]
    assert row["state"] == "WAITING" and row["retry_deadline"] is None
    assert query(ledger, "SELECT suspended FROM requests")[0]["suspended"] == 1
    assert ledger.next_work(T0, set()) is not None
    ledger.claim_current_due_slots(CATALOG, T0 + timedelta(days=2))
    assert len(query(ledger, "SELECT id FROM jobs")) == 1


def test_only_one_worker_holds_the_mutex(ledger, db_cfg):
    other = Ledger.connect(db_cfg)
    try:
        assert other.acquire_mutex() is False
    finally:
        other.close_releasing_mutex_if_healthy()


def test_losing_the_mutex_poisons_the_ledger_and_nothing_reconnects(ledger):
    with ledger.conn.cursor() as cur:
        cur.execute("SELECT RELEASE_LOCK(%s)", (ledger.lock_name,))
    ledger.conn.commit()
    with pytest.raises(LedgerUnavailable):
        with ledger.transaction() as cur:
            cur.execute("SELECT 1")
    assert ledger.healthy is False and ledger.has_mutex is False


def test_a_database_error_poisons_but_an_application_error_only_rolls_back(ledger):
    with pytest.raises(InvariantViolation):
        with ledger.transaction() as cur:
            cur.execute("""INSERT INTO surfaces(page_id,kind,state,created_at,updated_at)
                           VALUES(9,'single','PAUSED',%s,%s)""", (T0, T0))
            raise InvariantViolation("deliberate")
    assert ledger.healthy is True
    assert query(ledger, "SELECT COUNT(*) n FROM surfaces WHERE page_id=9")[0]["n"] == 0

    with pytest.raises(LedgerUnavailable):
        with ledger.transaction() as cur:
            cur.execute("SELECT * FROM no_such_table")
    assert ledger.healthy is False


def test_the_schema_version_is_verified_before_any_work(ledger):
    ledger.require_schema(1)
    with pytest.raises(InvariantViolation):
        ledger.require_schema(2)
