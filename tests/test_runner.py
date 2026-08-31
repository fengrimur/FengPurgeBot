# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from conftest import PERMALINK, SAFETY, FakeWiki, Response, reset_schema, surface_page
from purgebot import runner
from purgebot.ledger import Ledger
from purgebot.model import (
    Action, ApiFailure, Authorization, FailureKind, IdentityUnknown, JobRecord, JobState,
    PageInfo, Reservation, StopReached, TargetIdentity, TargetRecord, TargetState,
)
from purgebot.runner import Stop, dispatch_one, run_tick, stage_job

T0 = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
FANOUT_TEXT = ("{{PurgeBot request|id=m1|action=refresh-category-members|target=Category:Foo"
               "|schedule=once|discussion=" + PERMALINK + "9}}")
SINGLE_TEXT = ("{{PurgeBot request|id=p1|action=refresh-page-links|target=Main Page"
               "|schedule=once}}")


class Clock:
    def __init__(self):
        self.monotonic_s = 0.0
        self.now = T0
        self.slept = []

    def utcnow(self):
        return self.now

    def tick(self, seconds):
        self.monotonic_s += seconds
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.tick(seconds)


def make_stop(clock, remaining=240.0, reserve=40.0):
    return Stop(clock.monotonic_s + remaining, reserve, lambda: clock.monotonic_s)


def test_stop_refuses_to_start_work_inside_the_reserve(clock=None):
    clock = Clock()
    stop = make_stop(clock)
    assert stop.allow_read() and stop.remaining_s() == 240.0
    stop.require_read()
    stop.require_effect_start()
    clock.tick(201)
    assert not stop.allow_read()
    with pytest.raises(StopReached):
        stop.require_read()
    with pytest.raises(StopReached):
        stop.require_effect_start()


def test_stop_sleeps_to_the_pace_time_but_never_past_the_deadline():
    clock = Clock()
    stop = make_stop(clock)
    stop.sleep_until(None, clock.sleep, clock.utcnow)
    stop.sleep_until(clock.now - timedelta(seconds=5), clock.sleep, clock.utcnow)
    assert clock.slept == []
    stop.sleep_until(clock.now + timedelta(seconds=30), clock.sleep, clock.utcnow)
    assert clock.slept == [30.0]
    with pytest.raises(StopReached):
        stop.sleep_until(clock.now + timedelta(seconds=300), clock.sleep, clock.utcnow)


class StagingClient:
    def __init__(self, responses, titles=None):
        self.responses = list(responses)
        self.titles = titles or {}
        self.requests = []

    def get_json(self, params):
        self.requests.append(dict(params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def resolve_titles(self, titles):
        self.requests.append({"titles": list(titles)})
        if isinstance(self.titles, Exception):
            raise self.titles
        return {title: self.titles[title] for title in titles}


class StagingLedger:
    def __init__(self, materialize=True):
        self.materialize = materialize
        self.events = []

    def reject_job(self, job_id, code, now):
        self.events.append(("reject", code))

    def note_staging_failure(self, job_id, code, safety, now):
        self.events.append(("staging-failure", code))

    def wait_after_api_failure(self, job_id, failure, safety, now):
        self.events.append(("api-wait", failure.code))

    def materialize_targets(self, job_id, targets, sha, open_limit, now):
        self.events.append(("materialize", len(targets), open_limit))
        return self.materialize


def selector_member(page_id, title):
    return {"pageid": page_id, "ns": 0, "title": title}


def selector_page(items):
    return {"query": {"categorymembers": items}}


def staging_job(is_fanout=True, action=Action.CATEGORY, key="Category:Foo"):
    return JobRecord(1, 222, "m1", None, action, is_fanout, JobState.QUEUED, key, 14, 0,
                     None, False)


def stage(cfg, client, ledger=None, record=None, **kwargs):
    ledger = ledger or StagingLedger(**kwargs)
    result = stage_job(staging_job(**(record or {})), client, ledger, cfg, make_stop(Clock()),
                       lambda: T0)
    return ledger, result


def test_stage_job_materialises_a_clean_fan_out(cfg):
    client = StagingClient([selector_page([selector_member(1, "A")])] * 2)
    ledger, progressed = stage(cfg, client)
    assert progressed and ledger.events == [("materialize", 1,
                                              cfg.safety.open_fanout_targets)]


def test_stage_job_blocks_when_the_open_fan_out_cap_is_full(cfg):
    client = StagingClient([selector_page([selector_member(1, "A")])] * 2)
    _, progressed = stage(cfg, client, materialize=False)
    assert progressed is False


def test_stage_job_rejects_an_over_cap_selector_without_any_effect(cfg):
    big = [selector_member(i, f"P{i}") for i in range(1, cfg.safety.fanout_limit + 2)]
    ledger, _ = stage(cfg, StagingClient([selector_page(big)]))
    assert ledger.events == [("reject", "cap-exceeded")]


def test_stage_job_routes_drift_and_transients_to_their_own_authorities(cfg):
    drift = [selector_page([selector_member(1, "A")]),
             selector_page([selector_member(2, "B")])]
    ledger, _ = stage(cfg, StagingClient(drift))
    assert ledger.events == [("staging-failure", "selector-drift")]

    ledger, _ = stage(cfg, StagingClient([{"query": {}}]))
    assert ledger.events == [("staging-failure", "invalid-enumeration")]

    ledger, _ = stage(cfg, StagingClient([ApiFailure(FailureKind.TRANSIENT, "maxlag", 30)]))
    assert ledger.events == [("api-wait", "maxlag")]

    client = StagingClient([], IdentityUnknown("title-not-resolved"))
    ledger, _ = stage(cfg, client, record={"is_fanout": False, "action": Action.PURGE,
                                           "key": "Main Page"})
    assert ledger.events == [("staging-failure", "title-not-resolved")]


def test_a_direct_page_job_never_consults_the_fan_out_selectors(cfg):
    client = StagingClient([], {"Main Page": PageInfo(7, 0, "Main Page", False, False)})
    ledger, _ = stage(cfg, client, record={"is_fanout": False, "action": Action.PURGE,
                                           "key": "Main Page"})
    assert ledger.events == [("materialize", 1, cfg.safety.open_fanout_targets)]
    assert client.requests == [{"titles": ["Main Page"]}]


def target(target_id, page_id, state=TargetState.READY, replays=0):
    return TargetRecord(target_id, 1, page_id, 0, f"P{page_id}", state, None, None, replays, None)


def job(action=Action.CATEGORY):
    return JobRecord(1, 222, "m1", None, action, True, JobState.RUNNING, "Category:Foo", 14,
                     0, None, True)


class RecordingLedger:
    def __init__(self, batch, reservation=None, pace=None):
        self.batch = batch
        self.reservation = reservation
        self.pace = pace
        self.events = []

    def select_batch(self, job_id, safety, now):
        return self.batch

    def record_identity_unknown(self, targets, code, now, safety):
        self.events.append(("identity-unknown", code, tuple(t.id for t in targets)))

    def wait_after_api_failure(self, job_id, failure, safety, now):
        self.events.append(("api-wait", failure.code))

    def wait_for_authority(self, job_id, code, safety, now):
        self.events.append(("authority-wait", code))

    def next_pace_time(self, safety):
        return self.pace

    def reserve_dispatch(self, job, selected, titles, auth, safety, now):
        self.events.append(("reserve", tuple(titles[t.page_id] for t in selected)))
        return self.reservation

    def assert_mutex(self):
        self.events.append(("assert-mutex",))

    def finalize_failure(self, attempt_id, failure, now, safety):
        self.events.append(("finalize-failure", failure.kind, failure.code))

    def finalize_outcomes(self, attempt_id, raw, outcomes, now, safety):
        self.events.append(("finalize-outcomes", tuple(sorted(outcomes.items()))))


class Wiki:
    def __init__(self, identities, purge):
        self.identities = identities
        self.purge = purge
        self.purge_calls = []
        self.resolve_calls = 0

    def resolve_pageids(self, page_ids):
        self.resolve_calls += 1
        if isinstance(self.identities, Exception):
            raise self.identities
        return {p: self.identities[p] for p in page_ids}

    def purge_once(self, page_ids, force):
        self.purge_calls.append((tuple(page_ids), force))
        if isinstance(self.purge, Exception):
            raise self.purge
        return self.purge


def authorised(mw, db, cfg, current_job, now):
    return Authorization(True, "authorized", 2000, "Op")


def refused(mw, db, cfg, current_job, now):
    return Authorization(False, "surface-paused", None, None)


def run_dispatch(cfg, monkeypatch, db, wiki, authorize=authorised, clock=None):
    clock = clock or Clock()
    monkeypatch.setattr(runner.control, "authorize_now", authorize)
    dispatch_one(job(), wiki, db, cfg, make_stop(clock), clock.sleep, clock.utcnow)
    return clock


def test_a_failed_identity_resolution_never_sends_a_post(cfg, monkeypatch):
    db = RecordingLedger((target(1, 5),))
    wiki = Wiki(IdentityUnknown("page-missing"), None)
    run_dispatch(cfg, monkeypatch, db, wiki)
    assert db.events == [("identity-unknown", "page-missing", (1,))]
    assert wiki.purge_calls == []


def test_a_failed_identity_read_waits_without_sending_a_post(cfg, monkeypatch):
    db = RecordingLedger((target(1, 5),))
    wiki = Wiki(ApiFailure(FailureKind.TRANSIENT, "maxlag", 30), None)
    run_dispatch(cfg, monkeypatch, db, wiki)
    assert db.events == [("api-wait", "maxlag")]
    assert wiki.purge_calls == []


def test_lost_authority_waits_without_sending(cfg, monkeypatch):
    db = RecordingLedger((target(1, 5),))
    wiki = Wiki({5: TargetIdentity(5, 0, "A")}, None)
    run_dispatch(cfg, monkeypatch, db, wiki, authorize=refused)
    assert db.events == [("authority-wait", "surface-paused")]
    assert wiki.purge_calls == []


def test_a_failed_post_is_persisted_once_and_never_retried_in_place(cfg, monkeypatch):
    failure = ApiFailure(FailureKind.AMBIGUOUS, "read-or-connection-loss")
    db = RecordingLedger((target(1, 5),), reservation=Reservation(7, (5,), ("A",)))
    wiki = Wiki({5: TargetIdentity(5, 0, "A")}, failure)
    run_dispatch(cfg, monkeypatch, db, wiki)
    assert len(wiki.purge_calls) == 1
    assert db.events[-1] == ("finalize-failure", failure.kind, failure.code)


def test_an_unverifiable_response_makes_every_target_unknown(cfg, monkeypatch):

    class Flaky(Wiki):
        def resolve_pageids(self, page_ids):
            self.resolve_calls += 1
            if self.resolve_calls == 1:
                return {p: self.identities[p] for p in page_ids}
            raise IdentityUnknown("page-missing")

    db = RecordingLedger((target(1, 5),), reservation=Reservation(7, (5,), ("A",)))
    wiki = Flaky({5: TargetIdentity(5, 0, "A")},
                 {"purge": [{"ns": 0, "title": "A", "purged": True, "linkupdate": True}]})
    run_dispatch(cfg, monkeypatch, db, wiki)
    assert db.events[-1] == ("finalize-outcomes", ((5, TargetState.UNKNOWN),))


# full tick integration tests

PAGES = {"Main Page": {"pageid": 1, "ns": 0, "title": "Main Page"},
         "Category:Foo": {"pageid": 50, "ns": 14, "title": "Category:Foo"},
         "A": {"pageid": 10, "ns": 0, "title": "A"},
         "B": {"pageid": 11, "ns": 0, "title": "B"}}
MEMBERS = {"Category:Foo": [{"pageid": 10, "ns": 0, "title": "A"},
                            {"pageid": 11, "ns": 0, "title": "B"}]}


def purge_reversed(page_ids, force):
    items = [{"ns": 0, "title": next(t for t, v in PAGES.items() if v.get("pageid") == p),
              "purged": True, **({"linkupdate": True} if force else {})}
             for p in reversed(page_ids)]
    return Response(200, {"purge": items})


def wiki_for(members=MEMBERS, purge=purge_reversed, single_text=SINGLE_TEXT):
    return FakeWiki(
        {111: surface_page(111, "User:PurgeBot/Pages", 1000, "sysop", single_text),
         222: surface_page(222, "User:PurgeBot/Mass", 2000, "templateeditor", FANOUT_TEXT)},
        PAGES, members, purge)


@pytest.fixture
def tick(cfg, db_cfg, monkeypatch):
    reset_schema(db_cfg)
    live = dataclasses.replace(cfg, database=db_cfg)

    def run(wiki, clock=None, safety=None, monotonic=None):
        config = live if safety is None else dataclasses.replace(live, safety=safety)
        clock = clock or Clock()
        monkeypatch.setattr(runner, "build_session", lambda _: wiki)
        code = run_tick(config, monotonic=monotonic or (lambda: clock.monotonic_s),
                        sleep=clock.sleep, utcnow=clock.utcnow)
        return code, clock
    return run


@pytest.fixture
def rows(db_cfg):
    def query(sql):
        db = Ledger.connect(db_cfg)
        assert db.acquire_mutex()
        try:
            with db.transaction() as cur:
                cur.execute(sql)
                return cur.fetchall()
        finally:
            db.close_releasing_mutex_if_healthy()
    return query


def test_a_whole_tick_stages_and_purges_both_surfaces(tick, rows):
    wiki = wiki_for()
    code, clock = tick(wiki)
    assert code == 0
    assert [r["state"] for r in rows("SELECT state FROM jobs ORDER BY id")] \
        == ["API_ACCEPTED", "API_ACCEPTED"]
    assert [r["state"] for r in rows("SELECT state FROM targets ORDER BY id")] \
        == ["API_ACCEPTED"] * 3
    assert len(wiki.posts()) == 2
    assert clock.slept and clock.slept[0] <= SAFETY.post_start_interval_s
    assert all("titles" not in body and "pageids" in body for _, body in wiki.posts())
    assert rows("SELECT authorizing_revision_id FROM attempts ORDER BY id")[0] \
        == {"authorizing_revision_id": 1000}


def test_a_second_tick_repeats_no_work_and_adds_no_post(tick):
    tick(wiki_for())
    clock = Clock()
    clock.now = T0 + timedelta(minutes=5)
    second = wiki_for()
    code, _ = tick(second, clock)
    assert code == 0 and second.posts() == []
