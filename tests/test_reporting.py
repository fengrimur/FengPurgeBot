# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

from purgebot import reporting

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

RAW = {
    "schema_version": 1,
    "jobs_by_state": {"API_ACCEPTED": 2, "NEEDS_OPERATOR": 1},
    "targets_by_state": {"API_ACCEPTED": 40, "UNKNOWN": 1},
    "open_fanout_targets": 1,
    "budget_24h": {"posts": 3, "target_attempts": Decimal("41"),
                   "force_attempts": Decimal("41"), "last_post": NOW},
    "surfaces": [{"page_id": 111, "kind": "single", "state": "VALID",
                  "last_revision_id": 1000, "reason_code": None}],
    "attention_total": 3,
    "attention": [{
        "job": {"id": 7, "state": "NEEDS_OPERATOR", "selector_sha256": b"\x01" * 32,
                "reason_code": "ambiguous-replays-exhausted", "action": "refresh-category-members"},
        "request": {"introduced_revision_id": 2000, "introduced_author": "Op",
                    "latest_revision_id": 2001, "semantic_sha256": b"\x02" * 32},
        "target_counts": {"API_ACCEPTED": 0, "FAILED": 4, "UNKNOWN": 1, "CANCELLED": 0,
                          "CLOSED_UNVERIFIED": 0, "READY": 0, "WAITING": 0, "DISPATCHING": 0},
        "targets_shown": 1,
        "targets": [{"id": 3, "page_id": 10, "state": "UNKNOWN", "last_code": "worker-crash",
                     "singleton_replays": 1, "not_before": NOW, "retry_deadline": NOW}],
        "attempts": [{"id": 5, "state": "AMBIGUOUS", "api_code": "http-503",
                      "retry_after_s": 30, "payload_sha256": b"\x03" * 32,
                      "response_sha256": None, "authorizing_revision_id": 2000,
                      "authorizing_author": "Op", "post_started_at": NOW,
                      "finished_at": NOW, "http_status": None}],
    }],
}


class FakeLedger:
    def __init__(self, raw=RAW, error=None):
        self.raw = raw
        self.error = error
        self.limits = []

    def snapshot(self, now, attention_limit):
        self.limits.append(attention_limit)
        if self.error is not None:
            raise self.error
        return self.raw


def emitted(db, caplog):
    with caplog.at_level(logging.INFO, logger="purgebot.audit"):
        reporting.emit_snapshot(db, NOW)
    return caplog.records


def test_a_snapshot_is_deterministic_json_with_the_audit_fields(caplog):
    db = FakeLedger()
    records = emitted(db, caplog)
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload["kind"] == "purgebot-snapshot"
    assert payload["emitted_at"] == NOW.isoformat()
    assert payload["schema_version"] == 1
    assert payload["open_fanout_targets"] == 1
    assert (payload["attention_total"], payload["attention_shown"],
            payload["attention_truncated"]) == (3, 1, True)
    assert payload["budget_24h"] == {"posts": 3, "target_attempts": 41, "force_attempts": 41,
                                     "last_post": NOW.isoformat()}
    attention = payload["attention"][0]
    assert attention["job"]["id"] == 7
    assert attention["job"]["selector_sha256"] == "01" * 32
    assert attention["request"]["introduced_revision_id"] == 2000
    assert attention["attempts"][0]["authorizing_revision_id"] == 2000
    assert attention["attempts"][0]["api_code"] == "http-503"
    assert attention["attempts"][0]["retry_after_s"] == 30
    assert attention["targets"][0]["singleton_replays"] == 1
    assert attention["target_counts"]["FAILED"] == 4
    assert attention["target_counts"]["API_ACCEPTED"] == 0
    assert db.limits == [reporting.ATTENTION_LIMIT]


def test_secret_field_names_are_refused_before_anything_is_written(caplog):
    poisoned = dict(RAW, surfaces=[{"page_id": 111, "api_token": "s3cret"}])
    records = emitted(FakeLedger(poisoned), caplog)
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert "s3cret" not in records[0].getMessage()


def test_a_reporter_failure_is_only_a_logging_failure(caplog):
    db = FakeLedger(error=RuntimeError("boom"))
    records = emitted(db, caplog)
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert "job outcomes are unaffected" in records[0].getMessage()
