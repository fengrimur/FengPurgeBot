# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

"""db access with short transactions"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pymysql
import pymysql.cursors
from pymysql.constants import CLIENT

from .model import (
    FANOUT, FORCE, OPEN_TARGET_STATES, TERMINAL_JOB_STATES, Action, AttemptState, FailureKind,
    InvalidRevision, InvariantViolation, JobRecord, JobState, LedgerUnavailable, LostMutex,
    OperatorRefused, RequestRecord, Reservation, SurfaceKind, SurfaceSnapshot, SurfaceState,
    TargetRecord, TargetState, WaitUntil, aggregate_terminal_state, as_utc, claim_key, digest,
    latest_due_slot, require, require_attempt_transition, require_job_transition,
    require_target_transition, retry_delay_s,
)

STAGING_PASS_PAIRS = 3
ACTIVE_TARGET_STATES = (
    TargetState.READY, TargetState.WAITING, TargetState.DISPATCHING, TargetState.UNKNOWN)
OPEN_JOB_STATES = (JobState.QUEUED, JobState.RUNNING, JobState.WAITING)


def one(cur):
    row = cur.fetchone()
    if row is None:
        raise InvariantViolation("expected exactly one row")
    return row


def _sql_in(values) -> str:
    names = sorted(str(v) for v in values)
    for name in names:
        require(name.isascii() and name.replace("_", "").isalpha(), f"unsafe enum value {name!r}")
    return "(" + ",".join(f"'{name}'" for name in names) + ")"


def _placeholders(count: int) -> str:
    require(count > 0, "empty IN () list")
    return ",".join(["%s"] * count)


TERMINAL_JOB_SQL = _sql_in(TERMINAL_JOB_STATES)
ACTIVE_TARGET_SQL = _sql_in(ACTIVE_TARGET_STATES)
OPEN_JOB_SQL = _sql_in(OPEN_JOB_STATES)


def release_time(rows,weight,need,cap):
    total=sum(weight(r) for r in rows)
    if total+need<=cap: return None
    for r in rows:
        total-=weight(r)
        if total+need<=cap: return r["reserved_at"]+timedelta(hours=24)
    raise InvariantViolation("reservation exceeds cap")


class Ledger:
    def __init__(self, conn, lock_name: str):
        self.conn = conn
        self.lock_name = lock_name
        self.has_mutex = False
        self.healthy = True

    @classmethod
    def connect(cls, cfg) -> "Ledger":
        try:
            conn = pymysql.connect(
                host=cfg.host, port=cfg.port, database=cfg.database,
                read_default_file=cfg.read_default_file,
                charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                connect_timeout=cfg.connect_timeout_s, read_timeout=cfg.read_timeout_s,
                write_timeout=cfg.write_timeout_s, init_command="SET time_zone='+00:00'",
                client_flag=CLIENT.FOUND_ROWS)
        except pymysql.MySQLError as exc:
            raise LedgerUnavailable(str(exc)) from exc
        ledger = cls(conn, cfg.lock_name)
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            conn.commit()
        except pymysql.MySQLError as exc:
            ledger.poison()
            raise LedgerUnavailable(str(exc)) from exc
        return ledger

    def poison(self) -> None:
        self.healthy = False
        self.has_mutex = False

    def close_releasing_mutex_if_healthy(self) -> None:
        try:
            if self.healthy and self.has_mutex:
                with self.conn.cursor() as cur:
                    cur.execute("SELECT RELEASE_LOCK(%s)", (self.lock_name,))
                self.conn.commit()
        except pymysql.MySQLError:
            self.poison()
        finally:
            self.has_mutex = False
            try:
                self.conn.close()
            except pymysql.MySQLError:
                pass

    @contextmanager
    def transaction(self):
        try:
            self.conn.begin()
            with self.conn.cursor() as cur:
                self.assert_mutex(cur); yield cur; self.assert_mutex(cur)
            self.conn.commit()
        except (pymysql.MySQLError,LostMutex) as exc:
            try: self.conn.rollback()
            except pymysql.MySQLError: pass
            self.poison(); raise LedgerUnavailable(str(exc)) from exc
        except Exception:
            try: self.conn.rollback()
            except pymysql.MySQLError: self.poison()
            raise

    def acquire_mutex(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s,0) acquired",(self.lock_name,)); value=cur.fetchone()["acquired"]
        self.conn.commit()
        if value==1: self.has_mutex=True; return True
        if value==0: return False
        self.poison(); raise LedgerUnavailable("GET_LOCK returned NULL")

    def assert_mutex(self,cur=None):
        if cur is not None:
            cur.execute("SELECT IS_USED_LOCK(%s)=CONNECTION_ID() held",(self.lock_name,))
            if cur.fetchone()["held"]!=1: raise LostMutex(self.lock_name)
            return
        try:
            with self.conn.cursor() as own:
                self.assert_mutex(own)
            self.conn.commit()
        except (pymysql.MySQLError,LostMutex) as exc:
            self.poison(); raise LedgerUnavailable(str(exc)) from exc

    def require_schema(self, expected: int) -> None:
        with self.transaction() as cur:
            cur.execute("SELECT version FROM schema_version")
            rows = cur.fetchall()
        require(len(rows) == 1 and rows[0]["version"] == expected,
                f"schema_version must hold exactly {expected}")

    # row mapping

    @staticmethod
    def _target(row) -> TargetRecord:
        require(row["page_id"] > 0 and row["staged_title"], "target identity is not usable")
        require(row["singleton_replays"] >= 0, "negative singleton replay count")
        return TargetRecord(
            id=row["id"], job_id=row["job_id"], page_id=row["page_id"],
            namespace_id=row["namespace_id"], staged_title=row["staged_title"],
            state=TargetState(row["state"]), not_before=as_utc(row["not_before"]),
            retry_deadline=as_utc(row["retry_deadline"]),
            singleton_replays=row["singleton_replays"], last_code=row["last_code"])

    @staticmethod
    def _job(row) -> JobRecord:
        action = Action(row["action"])
        require(bool(row["is_fanout"]) == (action in FANOUT), "is_fanout contradicts the action")
        return JobRecord(
            id=row["id"], surface_page_id=row["surface_page_id"], request_id=row["request_id"],
            due_slot=as_utc(row["due_slot"]), action=action, is_fanout=bool(row["is_fanout"]),
            state=JobState(row["state"]), selector_key=row["selector_key"],
            target_namespace=row["target_namespace"], staging_failures=row["staging_failures"],
            not_before=as_utc(row["not_before"]), has_targets=bool(row["has_targets"]))

    @staticmethod
    def _request(row) -> RequestRecord:
        return RequestRecord(
            surface_page_id=row["surface_page_id"], request_id=row["request_id"],
            action=Action(row["action"]), target=row["target"],
            target_namespace=row["target_namespace"], schedule_key=row["schedule_key"],
            discussion_url=row["discussion_url"], semantic_sha256=row["semantic_sha256"],
            introduced_revision_id=row["introduced_revision_id"],
            introduced_author=row["introduced_author"],
            introduced_at=as_utc(row["introduced_at"]),
            latest_revision_id=row["latest_revision_id"],
            active=bool(row["active"]), suspended=bool(row["suspended"]))

    JOB_COLUMNS = """j.id,j.surface_page_id,j.request_id,j.due_slot,j.action,j.is_fanout,
        j.state,j.staging_failures,j.not_before,r.target selector_key,r.target_namespace,
        EXISTS(SELECT 1 FROM targets t WHERE t.job_id=j.id) has_targets"""

    # control surfaces/requests

    def surface_snapshot(self, page_id: int) -> SurfaceSnapshot | None:
        with self.transaction() as cur:
            cur.execute("SELECT * FROM surfaces WHERE page_id=%s", (page_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return SurfaceSnapshot(
            page_id=row["page_id"], kind=SurfaceKind(row["kind"]),
            state=SurfaceState(row["state"]), observed_title=row["observed_title"],
            last_revision_id=row["last_revision_id"],
            last_revision_author=row["last_revision_author"],
            content_sha256=row["content_sha256"], reason_code=row["reason_code"])

    def _ensure_surface_row(self, cur, gate, now) -> None:
        cur.execute("SELECT page_id FROM surfaces WHERE page_id=%s FOR UPDATE", (gate.page_id,))
        if cur.fetchone() is None:
            cur.execute("""INSERT INTO surfaces(page_id,kind,state,created_at,updated_at)
                           VALUES(%s,%s,'PAUSED',%s,%s)""",
                        (gate.page_id, gate.kind, now, now))

    def pause_surface(self, gate, reason_code: str, now) -> None:
        with self.transaction() as cur:
            self._ensure_surface_row(cur, gate, now)
            cur.execute("""UPDATE surfaces SET state='PAUSED',reason_code=%s,updated_at=%s
                           WHERE page_id=%s""", (reason_code, now, gate.page_id))

    def request_row(self, surface_page_id: int, request_id: str) -> RequestRecord | None:
        with self.transaction() as cur:
            cur.execute("SELECT * FROM requests WHERE surface_page_id=%s AND request_id=%s",
                        (surface_page_id, request_id))
            row = cur.fetchone()
        return None if row is None else self._request(row)

    def reconcile_surface(self, gate, read, entries, now) -> None:
        with self.transaction() as cur:
            self._ensure_surface_row(cur, gate, now)
            cur.execute("SELECT * FROM requests WHERE surface_page_id=%s FOR UPDATE",
                        (gate.page_id,))
            existing = {row["request_id"]: row for row in cur.fetchall()}
            for entry in entries:
                old = existing.get(entry.request_id)
                if old is None:
                    cur.execute("""INSERT INTO requests(surface_page_id,request_id,action,target,
                        target_namespace,schedule_key,discussion_url,semantic_sha256,
                        introduced_revision_id,introduced_author,introduced_at,
                        latest_revision_id,active,suspended,created_at,updated_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,0,%s,%s)""",
                        (gate.page_id, entry.request_id, entry.action, entry.target,
                         entry.target_namespace, entry.schedule_key, entry.discussion_url,
                         entry.semantic_sha256, read.revision_id, read.author, now,
                         read.revision_id, now, now))
                    continue
                if old["semantic_sha256"] != entry.semantic_sha256:
                    raise InvalidRevision(
                        None, f"request_id {entry.request_id!r} reused with different semantics")
                cur.execute("""UPDATE requests SET latest_revision_id=%s,active=1,updated_at=%s
                    WHERE surface_page_id=%s AND request_id=%s""",
                    (read.revision_id, now, gate.page_id, entry.request_id))
            present = {entry.request_id for entry in entries}
            for request_id, row in existing.items():
                if request_id in present or not row["active"]:
                    continue
                cur.execute("""UPDATE requests SET active=0,updated_at=%s
                    WHERE surface_page_id=%s AND request_id=%s""", (now, gate.page_id, request_id))
                self._cancel_undispatched(cur, gate.page_id, request_id, now)
            cur.execute("""UPDATE surfaces SET state='VALID',observed_title=%s,last_revision_id=%s,
                last_revision_author=%s,last_revision_timestamp=%s,content_sha256=%s,
                reason_code=NULL,updated_at=%s WHERE page_id=%s""",
                (read.title, read.revision_id, read.author, read.revision_timestamp,
                 read.content_sha256, now, gate.page_id))

    def _cancel_undispatched(self, cur, surface_page_id, request_id, now) -> None:
        cur.execute(f"""SELECT id FROM jobs WHERE surface_page_id=%s AND request_id=%s
            AND state NOT IN {TERMINAL_JOB_SQL} FOR UPDATE""", (surface_page_id, request_id))
        for job in cur.fetchall():
            cur.execute("""SELECT * FROM targets WHERE job_id=%s AND state IN ('READY','WAITING')
                           ORDER BY id FOR UPDATE""", (job["id"],))
            targets = [self._target(row) for row in cur.fetchall()]
            if not targets:
                cur.execute("SELECT COUNT(*) n FROM targets WHERE job_id=%s", (job["id"],))
                if cur.fetchone()["n"] == 0:
                    self._set_job_state(cur, job["id"], JobState.CANCELLED, now,
                                        "request-removed", None)
                    continue
            for target in targets:
                self._set_target_state(cur, target, TargetState.CANCELLED, now,
                                       "request-removed", None)
            self._aggregate_open_or_terminal(cur, job["id"], now, False)

    # job claims

    def claim_current_due_slots(self, schedule_catalog, now) -> None:
        with self.transaction() as cur:
            cur.execute("""SELECT r.* FROM requests r JOIN surfaces s ON s.page_id=r.surface_page_id
                WHERE r.active=1 AND r.suspended=0 AND s.state='VALID'
                ORDER BY r.surface_page_id,r.request_id FOR UPDATE""")
            requests = [self._request(row) for row in cur.fetchall()]
            for request in requests:
                spec = schedule_catalog.get(request.schedule_key)
                if spec is None:
                    continue
                cur.execute(f"""SELECT COUNT(*) n FROM jobs WHERE surface_page_id=%s
                    AND request_id=%s AND state NOT IN {TERMINAL_JOB_SQL}""",
                    (request.surface_page_id, request.request_id))
                if cur.fetchone()["n"]:
                    continue
                slot = latest_due_slot(spec, request.introduced_at, now)
                if slot is None:
                    continue
                once = spec.kind == "once"
                key = claim_key(request.surface_page_id, request.request_id, slot, once)
                cur.execute("SELECT id FROM jobs WHERE claim_key=%s", (key,))
                if cur.fetchone() is not None:
                    continue
                cur.execute("""INSERT INTO jobs(claim_key,surface_page_id,request_id,due_slot,
                    action,is_fanout,state,staging_failures,created_at,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,'QUEUED',0,%s,%s)""",
                    (key, request.surface_page_id, request.request_id,
                     None if once else slot, request.action, request.action in FANOUT, now, now))

    # work selection

    def next_work(self, now, blocked) -> JobRecord | None:
        excluded = tuple(sorted(blocked))
        clause = f"AND j.id NOT IN ({_placeholders(len(excluded))})" if excluded else ""
        with self.transaction() as cur:
            cur.execute(f"""SELECT {self.JOB_COLUMNS} FROM jobs j
                JOIN requests r ON r.surface_page_id=j.surface_page_id AND r.request_id=j.request_id
                WHERE j.state IN {OPEN_JOB_SQL}
                  AND (j.not_before IS NULL OR j.not_before<=%s) {clause}
                  AND (NOT EXISTS(SELECT 1 FROM targets t WHERE t.job_id=j.id)
                       OR NOT EXISTS(SELECT 1 FROM targets t WHERE t.job_id=j.id
                                     AND t.state IN {ACTIVE_TARGET_SQL})
                       OR EXISTS(SELECT 1 FROM targets t WHERE t.job_id=j.id
                                 AND t.state IN ('READY','WAITING','UNKNOWN')
                                 AND (t.not_before IS NULL OR t.not_before<=%s)))
                ORDER BY j.not_before,j.id LIMIT 1""", (now, *excluded, now))
            row = cur.fetchone()
        return None if row is None else self._job(row)

    def select_batch(self, job_id, safety, now) -> tuple[TargetRecord, ...]:
        with self.transaction() as cur:
            cur.execute("""SELECT * FROM targets WHERE job_id=%s AND state='UNKNOWN'
                AND (not_before IS NULL OR not_before<=%s) ORDER BY id LIMIT 1""", (job_id, now))
            row = cur.fetchone()
            if row is not None:
                return (self._target(row),)
            cur.execute("SELECT COUNT(*) n FROM targets WHERE job_id=%s AND state='UNKNOWN'",
                        (job_id,))
            if cur.fetchone()["n"]:
                return ()
            cur.execute("SELECT action FROM jobs WHERE id=%s", (job_id,))
            action = Action(one(cur)["action"])
            limit = safety.force_batch if action in FORCE else safety.normal_batch
            cur.execute("""SELECT * FROM targets WHERE job_id=%s AND state IN ('READY','WAITING')
                AND (not_before IS NULL OR not_before<=%s)
                ORDER BY CASE state WHEN 'WAITING' THEN 0 ELSE 1 END,id LIMIT %s""",
                (job_id, now, limit))
            rows = cur.fetchall()
        return tuple(sorted((self._target(row) for row in rows), key=lambda t: t.id))

    # target staging

    def materialize_targets(self,job_id,targets,selector_sha,open_limit,now):
        with self.transaction() as cur:
            cur.execute("SELECT state,is_fanout FROM jobs WHERE id=%s FOR UPDATE",(job_id,)); job=one(cur)
            cur.execute("SELECT COUNT(*) n FROM targets WHERE job_id=%s",(job_id,))
            require(cur.fetchone()["n"]==0 and job["state"] in ("QUEUED","WAITING"))
            if job["is_fanout"]:
                cur.execute("""SELECT COUNT(*) n FROM targets t JOIN jobs j ON j.id=t.job_id
                  WHERE j.is_fanout=1 AND t.state IN ('READY','WAITING','DISPATCHING','UNKNOWN')""")
                if cur.fetchone()["n"]+len(targets)>open_limit: return False
            if targets:
                cur.executemany("""INSERT INTO targets
                  (job_id,page_id,namespace_id,staged_title,state,created_at,updated_at)
                  VALUES(%s,%s,%s,%s,'READY',%s,%s)""",
                  [(job_id,t.page_id,t.namespace_id,t.canonical_title,now,now) for t in targets])
                state,reason="RUNNING",None
            else: state,reason="COMPLETED_NOOP","empty-staged-set"
            cur.execute("UPDATE jobs SET state=%s,selector_sha256=%s,reason_code=%s,updated_at=%s WHERE id=%s",
                        (state,selector_sha,reason,now,job_id))
            return True

    def reject_job(self, job_id, reason_code, now) -> None:
        with self.transaction() as cur:
            cur.execute("SELECT COUNT(*) n FROM attempts WHERE job_id=%s", (job_id,))
            require(cur.fetchone()["n"] == 0, "REJECTED requires zero effect attempts")
            cur.execute("SELECT COUNT(*) n FROM targets WHERE job_id=%s", (job_id,))
            require(cur.fetchone()["n"] == 0, "REJECTED requires an unmaterialised job")
            self._set_job_state(cur, job_id, JobState.REJECTED, now, reason_code, None)

    def note_staging_failure(self, job_id, reason_code, safety, now) -> None:
        with self.transaction() as cur:
            cur.execute("SELECT staging_failures FROM jobs WHERE id=%s FOR UPDATE", (job_id,))
            failures = one(cur)["staging_failures"] + 1
            cur.execute("UPDATE jobs SET staging_failures=%s,updated_at=%s WHERE id=%s",
                        (failures, now, job_id))
            if failures >= STAGING_PASS_PAIRS:
                self._escalate_to_operator(cur, job_id, reason_code, now)
                return
            self._defer_job(cur, job_id, reason_code,
                            now + timedelta(seconds=retry_delay_s(failures,
                                                                  safety.retry_delays_s)),
                            safety, now)

    def wait_after_api_failure(self, job_id, failure, safety, now) -> None:
        with self.transaction() as cur:
            if failure.kind is not FailureKind.TRANSIENT:
                self._escalate_to_operator(cur, job_id, failure.code, now)
                return
            delay = max(retry_delay_s(1, safety.retry_delays_s), failure.retry_after_s or 0)
            self._defer_job(cur, job_id, failure.code, now + timedelta(seconds=delay), safety, now)

    # api dispatch

    def lock_targets(self, cur, job_id, ids) -> list[TargetRecord]:
        cur.execute(f"""SELECT * FROM targets WHERE job_id=%s AND id IN ({_placeholders(len(ids))})
            ORDER BY id FOR UPDATE""", (job_id, *ids))
        return [self._target(row) for row in cur.fetchall()]

    def attempt_rows_since(self, cur, since) -> list[dict]:
        cur.execute("""SELECT id,target_count,force_link_update,reserved_at,post_started_at
            FROM attempts WHERE reserved_at>=%s ORDER BY reserved_at,id""", (since,))
        return [{"id": row["id"], "target_count": row["target_count"],
                 "force_link_update": bool(row["force_link_update"]),
                 "reserved_at": as_utc(row["reserved_at"]),
                 "post_started_at": as_utc(row["post_started_at"])} for row in cur.fetchall()]

    def per_target_release_times(self, cur, locked, safety, now) -> list:
        ids = tuple(t.id for t in locked)
        cur.execute(f"""SELECT x.target_id,a.reserved_at FROM attempt_targets x
            JOIN attempts a ON a.id=x.attempt_id
            WHERE x.target_id IN ({_placeholders(len(ids))}) AND a.reserved_at>=%s
            ORDER BY a.reserved_at,a.id""", (*ids, now - timedelta(hours=24)))
        history: dict[int, list[dict]] = {target_id: [] for target_id in ids}
        for row in cur.fetchall():
            history[row["target_id"]].append({"reserved_at": as_utc(row["reserved_at"])})
        return [release_time(history[t.id], lambda r: 1, 1, safety.attempts_per_target_24h)
                for t in locked]

    def next_pace_time(self, safety):
        with self.transaction() as cur:
            cur.execute("SELECT MAX(post_started_at) last_post FROM attempts")
            last = as_utc(one(cur)["last_post"])
        return None if last is None else last + timedelta(seconds=safety.post_start_interval_s)

    def defer(self, cur, targets, wait, reason, now) -> None:
        require(targets, "defer needs at least one target")
        job_id = targets[0].job_id
        overdue = any(t.retry_deadline is not None and wait > t.retry_deadline for t in targets)
        for target in targets:
            state = (TargetState.UNKNOWN if target.state is TargetState.UNKNOWN
                     else TargetState.WAITING)
            self._set_target_state(cur, target, state, now, reason, wait)
        if overdue:
            self._escalate_to_operator(cur, job_id, "retry-deadline-exceeded", now)
        else:
            self._set_job_state(cur, job_id, JobState.WAITING, now, reason, wait)

    def reserve_dispatch(self,job,selected,titles,auth,safety,now):
        force=job.action in FORCE
        payload={"action":"purge","force":force,
                 "targets":[[t.page_id,titles[t.page_id]] for t in sorted(selected,key=lambda x:x.page_id)]}
        with self.transaction() as cur:
            locked=self.lock_targets(cur,job.id,tuple(t.id for t in selected))
            require([t.id for t in locked]==[t.id for t in selected])
            rows=self.attempt_rows_since(cur,now-timedelta(hours=24))
            waits=[release_time(rows,lambda r:1,1,safety.effect_posts_24h),
                   release_time(rows,lambda r:r["target_count"],len(locked),safety.target_attempts_24h)]
            if force:
                waits.append(release_time(rows,lambda r:r["target_count"] if r["force_link_update"] else 0,
                                          len(locked),safety.force_attempts_24h))
            waits+=self.per_target_release_times(cur,locked,safety,now)
            if rows: waits.append(rows[-1]["post_started_at"]+timedelta(seconds=safety.post_start_interval_s))
            wait=max((x for x in waits if x and x>now),default=None)
            if wait:
                self.defer(cur,locked,wait,"budget-or-pace",now); return WaitUntil(wait)
            cur.execute("""INSERT INTO attempts
              (job_id,state,force_link_update,target_count,payload_sha256,
               authorizing_revision_id,authorizing_author,reserved_at,post_started_at)
              VALUES(%s,'DISPATCHING',%s,%s,%s,%s,%s,%s,%s)""",
              (job.id,force,len(locked),digest(payload),auth.revision_id,auth.author,now,now))
            attempt_id=cur.lastrowid
            cur.executemany("INSERT INTO attempt_targets(attempt_id,target_id,request_title) VALUES(%s,%s,%s)",
                            [(attempt_id,t.id,titles[t.page_id]) for t in locked])
            for t in locked:
                replay=t.singleton_replays+(t.state==TargetState.UNKNOWN)
                cur.execute("""UPDATE targets SET state='DISPATCHING',singleton_replays=%s,
                  retry_deadline=COALESCE(retry_deadline,%s),updated_at=%s WHERE id=%s AND state=%s""",
                  (replay,now+timedelta(seconds=safety.retry_window_s),now,t.id,t.state))
                require(cur.rowcount==1)
            cur.execute("UPDATE jobs SET state='RUNNING',updated_at=%s WHERE id=%s",(now,job.id))
        return Reservation(attempt_id,tuple(t.page_id for t in locked),tuple(titles[t.page_id] for t in locked))

    # recovery/completion

    def recover_dispatching(self,now,safety):
        with self.transaction() as cur:
            cur.execute("SELECT id,job_id FROM attempts WHERE state='DISPATCHING' FOR UPDATE")
            for a in cur.fetchall():
                cur.execute("UPDATE attempts SET state='AMBIGUOUS',api_code='worker-crash',finished_at=%s WHERE id=%s",
                            (now,a["id"]))
                cur.execute("""SELECT t.id,t.singleton_replays FROM targets t
                  JOIN attempt_targets x ON x.target_id=t.id WHERE x.attempt_id=%s FOR UPDATE""",(a["id"],))
                ts=cur.fetchall()
                cur.executemany("UPDATE targets SET state='UNKNOWN',last_code='worker-crash',updated_at=%s WHERE id=%s",
                                [(now,t["id"]) for t in ts])
                self.set_after_ambiguity(cur,a["job_id"],
                    any(t["singleton_replays"]>=safety.singleton_replays for t in ts),now,safety)

    def set_after_ambiguity(self, cur, job_id, exhausted, now, safety) -> None:
        if exhausted:
            self._escalate_to_operator(cur, job_id, "ambiguous-replays-exhausted", now)
        else:
            self._aggregate_open_or_terminal(cur, job_id, now, False)

    def finalize_failure(self, attempt_id, failure, now, safety) -> None:
        with self.transaction() as cur:
            attempt = self._lock_attempt(cur, attempt_id)
            targets = self._lock_attempt_targets(cur, attempt_id)
            job_id = attempt["job_id"]
            if failure.kind is FailureKind.TRANSIENT:
                self._finish_attempt(cur, attempt, AttemptState.TRANSIENT, now, failure.code,
                                     failure.retry_after_s, None, None)
                self._write_outcomes(cur, attempt_id, targets, "TRANSIENT")
                overdue = False
                for target in targets:
                    prior = self._attempts_for_target(cur, target.id, now)
                    delay = max(retry_delay_s(prior, safety.retry_delays_s),
                                failure.retry_after_s or 0)
                    when = now + timedelta(seconds=delay)
                    overdue = overdue or (target.retry_deadline is not None
                                          and when > target.retry_deadline)
                    self._set_target_state(cur, target, TargetState.WAITING, now,
                                           failure.code, when)
                if overdue:
                    self._escalate_to_operator(cur, job_id, "retry-deadline-exceeded", now)
                else:
                    self._aggregate_open_or_terminal(cur, job_id, now, False)
                return
            if failure.kind is FailureKind.AMBIGUOUS:
                self._finish_attempt(cur, attempt, AttemptState.AMBIGUOUS, now, failure.code,
                                     failure.retry_after_s, None, None)
                self._write_outcomes(cur, attempt_id, targets, "UNKNOWN")
                exhausted = False
                for target in targets:
                    prior = self._attempts_for_target(cur, target.id, now)
                    when = now + timedelta(seconds=retry_delay_s(prior, safety.retry_delays_s))
                    self._set_target_state(cur, target, TargetState.UNKNOWN, now,
                                           failure.code, when)
                    exhausted = exhausted or target.singleton_replays >= safety.singleton_replays
                self.set_after_ambiguity(cur, job_id, exhausted, now, safety)
                return
            self._finish_attempt(cur, attempt, AttemptState.OPERATOR, now, failure.code,
                                 failure.retry_after_s, None, None)
            self._write_outcomes(cur, attempt_id, targets, "OPERATOR")
            for target in targets:
                self._set_target_state(cur, target, TargetState.WAITING, now, failure.code, now)
            self._escalate_to_operator(cur, job_id, failure.code, now)

    def finalize_outcomes(self, attempt_id, raw, outcomes, now, safety) -> None:
        with self.transaction() as cur:
            attempt = self._lock_attempt(cur, attempt_id)
            targets = self._lock_attempt_targets(cur, attempt_id)
            require({t.page_id for t in targets} == set(outcomes),
                    "outcome set does not cover the attempt exactly")
            exhausted = False
            ambiguous = False
            for target in targets:
                outcome = TargetState(outcomes[target.page_id])
                require(outcome in (TargetState.API_ACCEPTED, TargetState.FAILED,
                                    TargetState.UNKNOWN), f"illegal purge outcome {outcome}")
                when = None
                if outcome is TargetState.UNKNOWN:
                    ambiguous = True
                    exhausted = exhausted or target.singleton_replays >= safety.singleton_replays
                    prior = self._attempts_for_target(cur, target.id, now)
                    when = now + timedelta(seconds=retry_delay_s(prior, safety.retry_delays_s))
                self._set_target_state(cur, target, outcome, now, "correlated", when)
                cur.execute("""UPDATE attempt_targets SET outcome=%s
                    WHERE attempt_id=%s AND target_id=%s""", (outcome, attempt_id, target.id))
            self._finish_attempt(
                cur, attempt,
                AttemptState.AMBIGUOUS if ambiguous else AttemptState.COMPLETED,
                now, "correlated", None, 200, digest(raw))
            if ambiguous:
                self.set_after_ambiguity(cur, attempt["job_id"], exhausted, now, safety)
            else:
                self._aggregate_open_or_terminal(cur, attempt["job_id"], now, False)

    def record_identity_unknown(self, targets, code, now, safety) -> None:
        require(targets, "identity failure needs at least one target")
        with self.transaction() as cur:
            locked = self.lock_targets(cur, targets[0].job_id, tuple(t.id for t in targets))
            require([t.id for t in locked] == [t.id for t in targets],
                    "target set changed while recording an identity failure")
            exhausted = False
            for target in locked:
                replays = target.singleton_replays + (target.state is TargetState.UNKNOWN)
                exhausted = exhausted or replays >= safety.singleton_replays
                prior = self._attempts_for_target(cur, target.id, now)
                when = now + timedelta(seconds=retry_delay_s(prior, safety.retry_delays_s))
                require_target_transition(target.state, TargetState.UNKNOWN)
                cur.execute("""UPDATE targets SET state='UNKNOWN',singleton_replays=%s,
                    retry_deadline=COALESCE(retry_deadline,%s),not_before=%s,last_code=%s,
                    updated_at=%s WHERE id=%s AND state=%s""",
                    (replays, now + timedelta(seconds=safety.retry_window_s), when, code,
                     now, target.id, target.state))
                require(cur.rowcount == 1, "target changed while recording an identity failure")
            self.set_after_ambiguity(cur, locked[0].job_id, exhausted, now, safety)

    def wait_for_authority(self, job_id, code, safety, now) -> None:
        with self.transaction() as cur:
            cur.execute("SELECT state FROM jobs WHERE id=%s FOR UPDATE", (job_id,))
            state = JobState(one(cur)["state"])
            if state in TERMINAL_JOB_STATES or state is JobState.NEEDS_OPERATOR:
                return
            when = now + timedelta(seconds=retry_delay_s(1, safety.retry_delays_s))
            self._defer_job(cur, job_id, code, when, safety, now)

    def aggregate_job(self, job_id, safety, now) -> None:
        with self.transaction() as cur:
            cur.execute("""SELECT COUNT(*) n FROM targets WHERE job_id=%s AND state='UNKNOWN'
                AND singleton_replays>=%s""", (job_id, safety.singleton_replays))
            exhausted = bool(cur.fetchone()["n"])
            self._aggregate_open_or_terminal(cur, job_id, now, exhausted)

    # state updates

    def _lock_attempt(self, cur, attempt_id) -> dict:
        cur.execute("SELECT * FROM attempts WHERE id=%s FOR UPDATE", (attempt_id,))
        attempt = one(cur)
        require(attempt["state"] == AttemptState.DISPATCHING, "attempt is already finished")
        return attempt

    def _lock_attempt_targets(self, cur, attempt_id) -> list[TargetRecord]:
        cur.execute("""SELECT t.* FROM targets t JOIN attempt_targets x ON x.target_id=t.id
            WHERE x.attempt_id=%s ORDER BY t.id FOR UPDATE""", (attempt_id,))
        targets = [self._target(row) for row in cur.fetchall()]
        require(targets, "attempt without targets")
        return targets

    def _write_outcomes(self, cur, attempt_id, targets, outcome) -> None:
        cur.executemany("""UPDATE attempt_targets SET outcome=%s
            WHERE attempt_id=%s AND target_id=%s""",
            [(outcome, attempt_id, t.id) for t in targets])

    def _finish_attempt(self, cur, attempt, new_state, now, api_code, retry_after_s,
                        http_status, response_sha) -> None:
        require_attempt_transition(AttemptState(attempt["state"]), new_state)
        cur.execute("""UPDATE attempts SET state=%s,api_code=%s,retry_after_s=%s,http_status=%s,
            response_sha256=%s,finished_at=%s WHERE id=%s AND state='DISPATCHING'""",
            (new_state, api_code, retry_after_s, http_status, response_sha, now, attempt["id"]))
        require(cur.rowcount == 1, "attempt changed while being finalised")

    def _attempts_for_target(self, cur, target_id, now) -> int:
        cur.execute("""SELECT COUNT(*) n FROM attempt_targets x JOIN attempts a ON a.id=x.attempt_id
            WHERE x.target_id=%s AND a.reserved_at>=%s""", (target_id, now - timedelta(hours=24)))
        return one(cur)["n"]

    def _set_target_state(self, cur, target, new_state, now, code, not_before) -> None:
        require_target_transition(target.state, new_state)
        cur.execute("""UPDATE targets SET state=%s,last_code=%s,not_before=%s,updated_at=%s
            WHERE id=%s AND state=%s""",
            (new_state, code, not_before, now, target.id, target.state))
        require(cur.rowcount == 1, f"target {target.id} changed concurrently")

    def _set_job_state(self, cur, job_id, new_state, now, reason, not_before) -> None:
        cur.execute("SELECT state FROM jobs WHERE id=%s FOR UPDATE", (job_id,))
        old = JobState(one(cur)["state"])
        require_job_transition(old, new_state)
        cur.execute("""UPDATE jobs SET state=%s,reason_code=%s,not_before=%s,updated_at=%s
            WHERE id=%s AND state=%s""", (new_state, reason, not_before, now, job_id, old))
        require(cur.rowcount == 1, f"job {job_id} changed concurrently")

    def _defer_job(self, cur, job_id, reason, when, safety, now) -> None:
        cur.execute("SELECT retry_deadline FROM jobs WHERE id=%s FOR UPDATE", (job_id,))
        deadline = as_utc(one(cur)["retry_deadline"])
        if deadline is None:
            deadline = now + timedelta(seconds=safety.retry_window_s)
            cur.execute("UPDATE jobs SET retry_deadline=%s,updated_at=%s WHERE id=%s",
                        (deadline, now, job_id))
        if when > deadline:
            self._escalate_to_operator(cur, job_id, "retry-deadline-exceeded", now)
            return
        self._set_job_state(cur, job_id, JobState.WAITING, now, reason, when)

    def _clear_retry_window(self, cur, job_id, now) -> None:
        cur.execute("UPDATE jobs SET retry_deadline=NULL,updated_at=%s WHERE id=%s", (now, job_id))
        cur.execute("""UPDATE targets SET retry_deadline=NULL,not_before=NULL,updated_at=%s
                       WHERE job_id=%s AND state IN ('READY','WAITING','UNKNOWN')""",
                    (now, job_id))

    def _escalate_to_operator(self, cur, job_id, reason, now) -> None:
        self._set_job_state(cur, job_id, JobState.NEEDS_OPERATOR, now, reason, None)
        cur.execute("""UPDATE requests r JOIN jobs j
            ON j.surface_page_id=r.surface_page_id AND j.request_id=r.request_id
            SET r.suspended=1,r.updated_at=%s WHERE j.id=%s""", (now, job_id))

    def _aggregate_open_or_terminal(self, cur, job_id, now, exhausted) -> None:
        cur.execute("SELECT state FROM jobs WHERE id=%s FOR UPDATE", (job_id,))
        state = JobState(one(cur)["state"])
        if state in TERMINAL_JOB_STATES or state is JobState.NEEDS_OPERATOR:
            return
        cur.execute("SELECT state,not_before FROM targets WHERE job_id=%s", (job_id,))
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) n FROM attempts WHERE job_id=%s", (job_id,))
        attempts = cur.fetchone()["n"]
        final = aggregate_terminal_state([row["state"] for row in rows], attempts, exhausted)
        if final is JobState.NEEDS_OPERATOR:
            self._escalate_to_operator(cur, job_id, "ambiguous-replays-exhausted", now)
            return
        if final is not None:
            self._set_job_state(cur, job_id, final, now, f"aggregated-{final}", None)
            return
        unknown_waits = [as_utc(row["not_before"]) for row in rows
                         if TargetState(row["state"]) is TargetState.UNKNOWN]
        waits = unknown_waits or [as_utc(row["not_before"]) for row in rows
                                  if TargetState(row["state"]) in OPEN_TARGET_STATES]
        due = None if any(w is None for w in waits) else min(waits)
        if due is None or due <= now:
            self._set_job_state(cur, job_id, JobState.RUNNING, now, None, None)
        else:
            self._set_job_state(cur, job_id, JobState.WAITING, now, "targets-waiting", due)

    # operator actions

    def _log_operator_event(self, cur, job_id, target_id, operation, operator, reason, now) -> None:
        cur.execute("""INSERT INTO operator_events(job_id,target_id,operation,operator,reason,
            created_at) VALUES(%s,%s,%s,%s,%s,%s)""",
            (job_id, target_id, operation, operator, reason, now))

    def operator_resume_job(self, job_id, operator, reason, safety, now) -> None:
        with self.transaction() as cur:
            cur.execute("SELECT state FROM jobs WHERE id=%s FOR UPDATE", (job_id,))
            row = cur.fetchone()
            if row is None or JobState(row["state"]) is not JobState.NEEDS_OPERATOR:
                raise OperatorRefused(f"job {job_id} is not in NEEDS_OPERATOR")
            cur.execute("""SELECT COUNT(*) n FROM targets WHERE job_id=%s AND state='UNKNOWN'
                AND singleton_replays>=%s""", (job_id, safety.singleton_replays))
            if cur.fetchone()["n"]:
                raise OperatorRefused(
                    f"job {job_id} has UNKNOWN targets with no replay left; resolve them first")
            self._clear_retry_window(cur, job_id, now)
            self._set_job_state(cur, job_id, JobState.WAITING, now, "operator-resume", now)
            self._log_operator_event(cur, job_id, None, "resume-job", operator, reason, now)

    def operator_resolve_target(self, target_id, outcome, operator, reason, now) -> None:
        resolved = {"failed": TargetState.FAILED,
                    "closed-unverified": TargetState.CLOSED_UNVERIFIED}.get(outcome)
        if resolved is None:
            raise OperatorRefused(f"unknown outcome {outcome!r}")
        with self.transaction() as cur:
            cur.execute("SELECT * FROM targets WHERE id=%s FOR UPDATE", (target_id,))
            row = cur.fetchone()
            if row is None or TargetState(row["state"]) is not TargetState.UNKNOWN:
                raise OperatorRefused(f"target {target_id} is not UNKNOWN")
            target = self._target(row)
            self._set_target_state(cur, target, resolved, now, f"operator-{outcome}", None)
            self._log_operator_event(cur, target.job_id, target_id, "resolve-target",
                                     operator, reason, now)
            self._aggregate_open_or_terminal(cur, target.job_id, now, False)

    def operator_cancel_job(self, job_id, operator, reason, now) -> None:
        with self.transaction() as cur:
            cur.execute("SELECT state FROM jobs WHERE id=%s FOR UPDATE", (job_id,))
            row = cur.fetchone()
            if row is None or JobState(row["state"]) in TERMINAL_JOB_STATES:
                raise OperatorRefused(f"job {job_id} is missing or already terminal")
            cur.execute("""SELECT COUNT(*) n FROM targets WHERE job_id=%s
                AND state IN ('DISPATCHING','UNKNOWN')""", (job_id,))
            if cur.fetchone()["n"]:
                raise OperatorRefused(
                    f"job {job_id} has DISPATCHING or UNKNOWN targets; resolve them first")
            cur.execute("""SELECT * FROM targets WHERE job_id=%s AND state IN ('READY','WAITING')
                ORDER BY id FOR UPDATE""", (job_id,))
            for target in [self._target(r) for r in cur.fetchall()]:
                self._set_target_state(cur, target, TargetState.CANCELLED, now,
                                       "operator-cancel", None)
            self._set_job_state(cur, job_id, JobState.CANCELLED, now, "operator-cancel", None)
            self._log_operator_event(cur, job_id, None, "cancel-job", operator, reason, now)

    # reporting snapshot

    def snapshot(self, now, attention_limit) -> dict:
        with self.transaction() as cur:
            cur.execute("SELECT version FROM schema_version")
            version = one(cur)["version"]
            cur.execute("SELECT state,COUNT(*) n FROM jobs GROUP BY state")
            jobs_by_state = {row["state"]: row["n"] for row in cur.fetchall()}
            cur.execute("SELECT state,COUNT(*) n FROM targets GROUP BY state")
            targets_by_state = {row["state"]: row["n"] for row in cur.fetchall()}
            cur.execute(f"""SELECT COUNT(*) n FROM targets t JOIN jobs j ON j.id=t.job_id
                WHERE j.is_fanout=1 AND t.state IN {ACTIVE_TARGET_SQL}""")
            open_fanout_targets = cur.fetchone()["n"]
            since = now - timedelta(hours=24)
            cur.execute("""SELECT COUNT(*) posts,COALESCE(SUM(target_count),0) target_attempts,
                COALESCE(SUM(CASE WHEN force_link_update=1 THEN target_count ELSE 0 END),0)
                force_attempts,MAX(post_started_at) last_post
                FROM attempts WHERE reserved_at>=%s""", (since,))
            budget = one(cur)
            cur.execute("SELECT page_id,kind,state,last_revision_id,reason_code FROM surfaces")
            surfaces = cur.fetchall()
            attention_filter = f"""j.state NOT IN {TERMINAL_JOB_SQL} OR j.state='PARTIAL'
                   OR EXISTS(SELECT 1 FROM targets t WHERE t.job_id=j.id AND t.state='UNKNOWN')"""
            cur.execute(f"SELECT COUNT(*) n FROM jobs j WHERE {attention_filter}")
            attention_total = cur.fetchone()["n"]
            cur.execute(f"""SELECT {self.JOB_COLUMNS},j.selector_sha256,j.reason_code,
                    j.retry_deadline,j.updated_at
                FROM jobs j
                JOIN requests r ON r.surface_page_id=j.surface_page_id AND r.request_id=j.request_id
                WHERE {attention_filter}
                ORDER BY j.id DESC LIMIT %s""", (attention_limit,))
            jobs = cur.fetchall()
            details = []
            for job in jobs:
                cur.execute("""SELECT state,COUNT(*) n FROM targets WHERE job_id=%s
                               GROUP BY state""", (job["id"],))
                counts = {row["state"]: row["n"] for row in cur.fetchall()}
                cur.execute("""SELECT id,page_id,state,last_code,singleton_replays,not_before,
                    retry_deadline FROM targets WHERE job_id=%s ORDER BY id LIMIT %s""",
                    (job["id"], attention_limit))
                targets = cur.fetchall()
                cur.execute("""SELECT id,state,force_link_update,target_count,payload_sha256,
                    authorizing_revision_id,authorizing_author,reserved_at,post_started_at,
                    finished_at,http_status,api_code,retry_after_s,response_sha256
                    FROM attempts WHERE job_id=%s ORDER BY id DESC LIMIT %s""",
                    (job["id"], attention_limit))
                attempts = cur.fetchall()
                cur.execute("""SELECT introduced_revision_id,introduced_author,latest_revision_id,
                    active,suspended,schedule_key,discussion_url,semantic_sha256
                    FROM requests WHERE surface_page_id=%s AND request_id=%s""",
                    (job["surface_page_id"], job["request_id"]))
                details.append({"job": job, "request": one(cur),
                                "target_counts": {state: counts.get(state, 0) for state in
                                                  sorted(TargetState)},
                                "targets_shown": len(targets), "targets": targets,
                                "attempts": attempts})
        return {"schema_version": version, "jobs_by_state": jobs_by_state,
                "targets_by_state": targets_by_state,
                "open_fanout_targets": open_fanout_targets, "budget_24h": budget,
                "surfaces": surfaces, "attention_total": attention_total,
                "attention": details}
