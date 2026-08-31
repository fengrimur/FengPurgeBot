# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

"""run one worker tick and verify every purge it starts"""

from __future__ import annotations

import time

from . import control, reporting
from .ledger import Ledger
from .mediawiki import MediaWikiClient, build_session, correlate_purge
from .model import (
    FORCE, ApiFailure, IdentityUnknown, InvalidEnumeration, LedgerUnavailable, SelectorDrift,
    SelectorOverCap, StopReached, TargetMissing, TargetState, WaitUntil, utc_now,
)
from .selectors import stage_direct, stage_fanout


class Stop:
    """leave time to finish request"""

    def __init__(self, deadline, reserve_s, monotonic=time.monotonic):
        self.deadline = deadline
        self.reserve_s = reserve_s
        self.monotonic = monotonic

    def remaining_s(self) -> float:
        return self.deadline - self.monotonic()

    def allow_read(self) -> bool:
        return self.remaining_s() > self.reserve_s

    def require_read(self) -> None:
        if not self.allow_read():
            raise StopReached("not enough time left to start a read")

    def require_effect_start(self) -> None:
        if not self.allow_read():
            raise StopReached("not enough time left to start an effect")

    def sleep_until(self, when, sleep, utcnow) -> None:
        if when is None:
            return
        delay = (when - utcnow()).total_seconds()
        if delay <= 0:
            return
        if delay > self.remaining_s() - self.reserve_s:
            raise StopReached("pace wait outruns this tick")
        sleep(delay)


def staging_code(error) -> str:
    if isinstance(error, SelectorDrift):
        return "selector-drift"
    if isinstance(error, InvalidEnumeration):
        return "invalid-enumeration"
    return error.code


def stage_job(job, mw, db, cfg, stop, utcnow) -> bool:
    """stage a job, return false when its targets put the OT count over limit"""
    try:
        if job.is_fanout:
            targets, selector_sha = stage_fanout(
                mw, job.action, job.selector_key, cfg.selector_policy,
                cfg.safety.fanout_limit, stop.require_read)
        else:
            targets, selector_sha = stage_direct(mw, job.selector_key)
    except SelectorOverCap:
        db.reject_job(job.id, "cap-exceeded", utcnow())
        return True
    except TargetMissing:
        db.reject_job(job.id, "direct-target-missing", utcnow())
        return True
    except (SelectorDrift, InvalidEnumeration, IdentityUnknown) as exc:
        db.note_staging_failure(job.id, staging_code(exc), cfg.safety, utcnow())
        return True
    except ApiFailure as exc:
        db.wait_after_api_failure(job.id, exc, cfg.safety, utcnow())
        return True
    return db.materialize_targets(job.id, targets, selector_sha,
                                  cfg.safety.open_fanout_targets, utcnow())


def run_tick(cfg,monotonic=time.monotonic,sleep=time.sleep,utcnow=utc_now):
    stop=Stop(monotonic()+cfg.safety.worker_runtime_s,
              cfg.safety.http_connect_timeout_s+cfg.safety.http_read_timeout_s+10,monotonic)
    db=Ledger.connect(cfg.database)
    try:
        if not db.acquire_mutex(): return 0
        db.require_schema(1); db.recover_dispatching(utcnow(),cfg.safety)
        mw=MediaWikiClient(build_session(cfg),cfg)
        for surface in cfg.control_surfaces:
            if stop.allow_read(): control.refresh_surface(mw,db,cfg,surface,utcnow())
        db.claim_current_due_slots(cfg.schedule_catalog,utcnow())
        blocked=set()
        while stop.allow_read():
            job=db.next_work(utcnow(),blocked)
            if job is None: break
            if not job.has_targets:
                if not stage_job(job,mw,db,cfg,stop,utcnow): blocked.add(job.id)
            else: dispatch_one(job,mw,db,cfg,stop,sleep,utcnow)
        reporting.emit_snapshot(db,utcnow()); return 0
    except StopReached: reporting.emit_snapshot(db,utcnow()); return 0
    except LedgerUnavailable: return 2
    finally: db.close_releasing_mutex_if_healthy()

def dispatch_one(job,mw,db,cfg,stop,sleep,utcnow):
    selected=db.select_batch(job.id,cfg.safety,utcnow())
    if not selected: db.aggregate_job(job.id,cfg.safety,utcnow()); return
    stop.require_read()
    try: pre=mw.resolve_pageids(tuple(t.page_id for t in selected))
    except IdentityUnknown as e: db.record_identity_unknown(selected,e.code,utcnow(),cfg.safety); return
    except ApiFailure as e: db.wait_after_api_failure(job.id,e,cfg.safety,utcnow()); return
    stop.require_read(); auth=control.authorize_now(mw,db,cfg,job,utcnow())
    if not auth.allowed: db.wait_for_authority(job.id,auth.code,cfg.safety,utcnow()); return
    stop.sleep_until(db.next_pace_time(cfg.safety),sleep,utcnow); stop.require_effect_start()
    r=db.reserve_dispatch(job,selected,{p:x.canonical_title for p,x in pre.items()},auth,cfg.safety,utcnow())
    if isinstance(r,WaitUntil): return
    db.assert_mutex()
    try: raw=mw.purge_once(r.page_ids,job.action in FORCE)
    except ApiFailure as e: db.finalize_failure(r.attempt_id,e,utcnow(),cfg.safety); return
    try: outcomes=correlate_purge(pre,raw,mw.resolve_pageids(tuple(pre)),job.action in FORCE)
    except (ApiFailure,IdentityUnknown): outcomes={p:TargetState.UNKNOWN for p in pre}
    db.finalize_outcomes(r.attempt_id,raw,outcomes,utcnow(),cfg.safety)
