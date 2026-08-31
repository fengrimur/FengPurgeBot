# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import os
import pathlib
from datetime import UTC, datetime

import pymysql
import pytest
import requests

from purgebot.config import (
    Adapter, AppConfig, DatabaseConfig, OperatorGate, ReportingGate, RequestSyntaxGate,
    SafetyGate, SecretRef, SelectorPolicyGate, SurfaceGate, WikiConfig,
)
from purgebot.ledger import Ledger
from purgebot.model import Action, ScheduleSpec, SurfaceKind

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "sql" / "01_init.sql"
TABLES = ("attempt_targets", "operator_events", "attempts", "targets", "jobs", "requests",
          "surfaces", "schema_version")
PERMALINK = "https://en.wikipedia.org/wiki/Special:PermanentLink/"

SAFETY = SafetyGate(
    fanout_limit=1500, open_fanout_targets=1500, target_attempts_24h=3000,
    force_attempts_24h=1500, effect_posts_24h=180, normal_batch=50, force_batch=25,
    post_start_interval_s=30, maxlag=5, attempts_per_target_24h=6, singleton_replays=1,
    retry_delays_s=(60, 300, 1800, 7200, 43200), retry_window_s=86400, worker_runtime_s=240,
    platform_timeout_s=300, tick_cron="*/5 * * * *", http_connect_timeout_s=5.0,
    http_read_timeout_s=25.0,
)
SINGLE_SURFACE = SurfaceGate(111, "User:PurgeBot/Pages", SurfaceKind.SINGLE, frozenset({"sysop"}))
FANOUT_SURFACE = SurfaceGate(222, "User:PurgeBot/Mass", SurfaceKind.FANOUT,
                             frozenset({"templateeditor"}))
ADAPTER = Adapter(
    template_names=frozenset({"PurgeBot request"}),
    field_map={"id": "request_id", "action": "action", "target": "target",
               "schedule": "schedule", "discussion": "discussion"},
    required_single=frozenset({"request_id", "action", "target", "schedule"}),
    required_fanout=frozenset({"request_id", "action", "target", "schedule", "discussion"}),
    allowed_actions_single=frozenset({Action.PURGE, Action.PAGE_LINKS}),
    allowed_actions_fanout=frozenset({Action.CATEGORY, Action.TEMPLATE}),
    permalink_prefixes=(PERMALINK,),
)


def make_config(database: DatabaseConfig | None = None) -> AppConfig:
    return AppConfig(
        wiki=WikiConfig("https://en.wikipedia.org/w/api.php"),
        database=database or DatabaseConfig("127.0.0.1", 3306, "purgebot", "/dev/null",
                                            "purgebot-dispatch", 10, 30, 30),
        operator=OperatorGate("oauth2-owner-only", "user", "PurgeBot", "PurgeBot/1.0 (contact)",
                              SecretRef("env", "PURGEBOT_SECRET")),
        control_surfaces=(SINGLE_SURFACE, FANOUT_SURFACE),
        schedule_catalog={
            "once": ScheduleSpec(kind="once"),
            "daily": ScheduleSpec(kind="interval", anchor_utc=datetime(2026, 1, 1, tzinfo=UTC),
                                  interval_s=86400)},
        selector_policy=SelectorPolicyGate((0, 10, 14), ("page",), "all", "literal"),
        request_syntax=RequestSyntaxGate((ADAPTER,), (PERMALINK,)),
        reporting=ReportingGate("log-only"),
        safety=SAFETY,
    )


@pytest.fixture
def cfg() -> AppConfig:
    return make_config()


class Response:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, str):
            raise requests.exceptions.JSONDecodeError("no json", "", 0)
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self):
        if not self.responses:
            raise AssertionError("the client made more HTTP calls than the test scripted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, params=None, timeout=None, allow_redirects=None):
        self.calls.append(("GET", dict(params), timeout, allow_redirects))
        return self._next()

    def post(self, url, data=None, timeout=None, allow_redirects=None):
        self.calls.append(("POST", dict(data), timeout, allow_redirects))
        return self._next()

    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]


class FakeWiki:
    def __init__(self, surfaces, pages, members, purge=None):
        self.surfaces = surfaces
        self.pages = pages
        self.members = members
        self.purge = purge
        self.calls = []

    def get(self, url, params=None, timeout=None, allow_redirects=None):
        self.calls.append(("GET", dict(params)))
        return Response(200, self._answer(params))

    def post(self, url, data=None, timeout=None, allow_redirects=None):
        self.calls.append(("POST", dict(data)))
        return self.purge([int(x) for x in data["pageids"].split("|")],
                          data.get("forcelinkupdate") == "1")

    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]

    def by_id(self, page_id):
        for page in self.pages.values():
            if page.get("pageid") == page_id:
                return page
        return None

    def _answer(self, p):
        if "pageids" in p and "revisions" in p.get("prop", ""):
            return {"query": {"pages": [self.surfaces[int(p["pageids"])]]}}
        if "pageids" in p:
            return {"query": {"pages": [self.by_id(int(x)) or {"pageid": int(x), "missing": True}
                                        for x in p["pageids"].split("|")]}}
        if "titles" in p:
            return {"query": {"pages": [self.pages.get(t) or {"ns": 0, "title": t, "missing": True}
                                        for t in p["titles"].split("|")]}}
        name = "categorymembers" if "cmtitle" in p else "embeddedin"
        return {"query": {name: list(self.members.get(p.get("cmtitle") or p.get("eititle"), []))}}


def surface_page(page_id, title, revision_id, level, wikitext, author="Op"):
    return {"pageid": page_id, "ns": 2, "title": title,
            "protection": [{"type": "edit", "level": level, "expiry": "infinity"}],
            "revisions": [{"revid": revision_id, "user": author,
                           "timestamp": "2026-08-29T10:00:00Z",
                           "slots": {"main": {"content": wikitext}}}]}


def database_config() -> DatabaseConfig | None:
    host = os.environ.get("PURGEBOT_TEST_DB_HOST")
    if not host:
        return None
    return DatabaseConfig(
        host=host, port=int(os.environ["PURGEBOT_TEST_DB_PORT"]),
        database=os.environ["PURGEBOT_TEST_DB_NAME"],
        read_default_file=os.environ["PURGEBOT_TEST_DB_DEFAULTS_FILE"],
        lock_name="purgebot-test", connect_timeout_s=10, read_timeout_s=30, write_timeout_s=30)


def reset_schema(db_cfg) -> None:
    statements = [s.strip() for s in
                  "\n".join(line for line in SCHEMA.read_text().splitlines()
                            if not line.lstrip().startswith("--")).split(";")]
    conn = pymysql.connect(host=db_cfg.host, port=db_cfg.port, database=db_cfg.database,
                           read_default_file=db_cfg.read_default_file, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in TABLES:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
            for statement in statements:
                if statement:
                    cur.execute(statement)
    finally:
        conn.close()


@pytest.fixture
def db_cfg():
    config = database_config()
    if config is None:
        pytest.skip("set PURGEBOT_TEST_DB_HOST/PORT/NAME/DEFAULTS_FILE for ledger tests")
    return config


@pytest.fixture
def ledger(db_cfg):
    reset_schema(db_cfg)
    db = Ledger.connect(db_cfg)
    assert db.acquire_mutex()
    try:
        yield db
    finally:
        db.close_releasing_mutex_if_healthy()


VALID_CONFIG_TOML = (pathlib.Path(__file__).with_name("valid_config.toml")
                     .read_text())
