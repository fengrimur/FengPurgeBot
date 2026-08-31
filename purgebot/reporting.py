# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

"""log audit snapshots without affecting job results"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal

LOG = logging.getLogger("purgebot.audit")

ATTENTION_LIMIT = 50
SECRET_WORDS = ("password", "passwd", "secret", "token", "cookie", "credential",
                "api_key", "apikey", "private_key", "authorization")


def plain_json(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, dict):
        return {str(key): plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(item) for item in value]
    return str(value)


def require_no_secret_fields(value, path="snapshot") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in SECRET_WORDS):
                raise ValueError(f"{path}.{key}: secret field name in an audit record")
            require_no_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            require_no_secret_fields(item, f"{path}[{index}]")


def build_snapshot(db, now) -> dict:
    raw = db.snapshot(now, ATTENTION_LIMIT)
    snapshot = {
        "kind": "purgebot-snapshot",
        "emitted_at": now,
        "attention_limit": ATTENTION_LIMIT,
        "schema_version": raw["schema_version"],
        "jobs_by_state": raw["jobs_by_state"],
        "targets_by_state": raw["targets_by_state"],
        "open_fanout_targets": raw["open_fanout_targets"],
        "budget_24h": raw["budget_24h"],
        "surfaces": raw["surfaces"],
        "attention_total": raw["attention_total"],
        "attention_shown": len(raw["attention"]),
        "attention_truncated": raw["attention_total"] > len(raw["attention"]),
        "attention": raw["attention"],
    }
    return plain_json(snapshot)


def emit_snapshot(db, now) -> None:
    try:
        snapshot = build_snapshot(db, now)
        require_no_secret_fields(snapshot)
        LOG.info("%s", json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    except Exception:
        LOG.exception("snapshot reporting failed; job outcomes are unaffected")
