# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from conftest import FANOUT_SURFACE, PERMALINK, SINGLE_SURFACE, VALID_CONFIG_TOML
from purgebot import control
from purgebot.config import load_config
from purgebot.model import (
    Action, ApiFailure, ConfigError, FailureKind, IdentityUnknown, InvalidRevision,
    InvariantViolation, PageInfo, ProtectionEntry, SurfaceKind, SurfaceRead, SurfaceState,
    SurfaceSnapshot, SurfaceUnavailable, digest,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
SINGLE = "{{PurgeBot request|id=p1|action=purge-page-cache|target=Main Page|schedule=once}}"
FANOUT = ("{{PurgeBot request|id=m1|action=refresh-category-members|target=Category:Foo"
          "|schedule=once|discussion=" + PERMALINK + "9}}")


def parse(text, cfg, kind=SurfaceKind.SINGLE):
    return control.parse_revision(text, cfg.request_syntax, kind, cfg.schedule_catalog)


def test_valid_lines_parse_on_their_own_surface(cfg):
    assert [e.request_id for e in parse(SINGLE, cfg)] == ["p1"]
    assert [e.request_id for e in parse("\n\n" + SINGLE + "\n", cfg)] == ["p1"]
    entries = parse(FANOUT, cfg, SurfaceKind.FANOUT)
    assert entries[0].discussion_url == PERMALINK + "9"
    assert entries[0].action is Action.CATEGORY


@pytest.mark.parametrize("text,kind", [
    (SINGLE + SINGLE, SurfaceKind.SINGLE),
    (SINGLE + " trailing", SurfaceKind.SINGLE),
    ("free text", SurfaceKind.SINGLE),
    ("{{PurgeBot request|p1|action=purge-page-cache|target=X|schedule=once}}", SurfaceKind.SINGLE),
    ("{{PurgeBot request|id=a|id=b|action=purge-page-cache|target=X|schedule=once}}",
     SurfaceKind.SINGLE),
    ("{{PurgeBot request|id=a|cap=99|action=purge-page-cache|target=X|schedule=once}}",
     SurfaceKind.SINGLE),
    ("{{PurgeBot request|id=a|action=purge-page-cache|target=X}}", SurfaceKind.SINGLE),
    ("{{Unknown|id=a}}", SurfaceKind.SINGLE),
    ("{{PurgeBot request|id=a|action=edit-page|target=X|schedule=once}}", SurfaceKind.SINGLE),
    ("{{PurgeBot request|id=a|action=purge-page-cache|target=X|schedule=hourly}}",
     SurfaceKind.SINGLE),
    ("{{PurgeBot request|id=a|action=purge-page-cache|target={{X}}|schedule=once}}",
     SurfaceKind.SINGLE),
    (FANOUT, SurfaceKind.SINGLE),
    (SINGLE, SurfaceKind.FANOUT),
    (FANOUT.replace(PERMALINK + "9", "https://evil.example/x"), SurfaceKind.FANOUT),
    (FANOUT.replace("discussion=" + PERMALINK + "9",
                    "discussion=[" + PERMALINK + "9 t]"), SurfaceKind.FANOUT),
])
def test_rejected_syntax(text, kind, cfg):
    with pytest.raises(InvalidRevision):
        parse(text, cfg, kind)


def test_request_id_length_is_bounded(cfg):
    long_id = SINGLE.replace("id=p1", "id=" + "z" * 129)
    with pytest.raises(InvalidRevision):
        parse(long_id, cfg)


def test_template_name_underscores_normalise(cfg):
    assert parse(SINGLE.replace("PurgeBot request", "PurgeBot_request"), cfg)


@pytest.mark.parametrize("protection,allowed", [
    ((ProtectionEntry("edit", "sysop"),), True),
    ((ProtectionEntry("edit", "autoconfirmed"),), False),
    ((ProtectionEntry("move", "sysop"),), False),
])
def test_protection_validation(protection, allowed):
    if allowed:
        control.require_protection(protection, SINGLE_SURFACE)
    else:
        with pytest.raises(SurfaceUnavailable):
            control.require_protection(protection, SINGLE_SURFACE)


def read_of(wikitext, revision_id=1000, title="User:PurgeBot/Pages"):
    return SurfaceRead(page_id=111, title=title,
                       protection=(ProtectionEntry("edit", "sysop"),),
                       revision_id=revision_id, author="Op",
                       revision_timestamp=NOW, wikitext=wikitext,
                       content_sha256=digest(wikitext))


class Client:
    def __init__(self, page, titles=None):
        self.page = page
        self.titles = titles or {}
        self.title_calls = 0

    def read_control_page(self, page_id):
        if isinstance(self.page, Exception):
            raise self.page
        return self.page

    def resolve_titles(self, titles):
        self.title_calls += 1
        return {t: self.titles[t] for t in titles}


class Recorder:
    def __init__(self, snapshot=None, request=None):
        self._snapshot = snapshot
        self._request = request
        self.paused = []
        self.reconciled = []

    def surface_snapshot(self, page_id):
        return self._snapshot

    def pause_surface(self, gate, reason, now):
        self.paused.append((gate.page_id, reason))

    def reconcile_surface(self, gate, read, entries, now):
        self.reconciled.append((read.revision_id, tuple(e.request_id for e in entries)))

    def request_row(self, surface_page_id, request_id):
        return self._request


def snapshot_of(read, state=SurfaceState.VALID):
    return SurfaceSnapshot(111, SurfaceKind.SINGLE, state, read.title, read.revision_id,
                           read.author, read.content_sha256, None)


def test_fetch_surface_rejects_a_renamed_control_page():
    renamed = read_of(SINGLE, title="User:Someone/Else")
    with pytest.raises(SurfaceUnavailable):
        control.fetch_surface(Client(renamed), SINGLE_SURFACE)
    assert control.fetch_surface(Client(read_of(SINGLE)), SINGLE_SURFACE).revision_id == 1000


def test_binding_applies_the_namespace_and_redirect_gates(cfg):
    entries = parse(SINGLE, cfg)
    client = Client(None, {"Main Page": PageInfo(1, 0, "Main Page", False, False)})
    bound = control.bind_titles(client, entries, cfg.selector_policy, SurfaceKind.SINGLE)
    assert bound[0].target == "Main Page" and bound[0].target_namespace == 0

    forbidden = Client(None, {"Main Page": PageInfo(1, 3, "User talk:X", False, False)})
    with pytest.raises(InvalidRevision):
        control.bind_titles(forbidden, entries, cfg.selector_policy, SurfaceKind.SINGLE)

    fan = parse(FANOUT, cfg, SurfaceKind.FANOUT)
    wrong_ns = Client(None, {"Category:Foo": PageInfo(50, 0, "Foo", False, False)})
    with pytest.raises(InvalidRevision):
        control.bind_titles(wrong_ns, fan, cfg.selector_policy, SurfaceKind.FANOUT)


def test_binding_rejects_redirects_duplicate_ids_and_semantic_duplicates(cfg):
    strict = dataclasses.replace(cfg.selector_policy, direct_page_redirect_policy="reject")
    redirect = Client(None, {"Main Page": PageInfo(1, 0, "Main Page", False, True)})
    with pytest.raises(InvalidRevision):
        control.bind_titles(redirect, parse(SINGLE, cfg), strict, SurfaceKind.SINGLE)
    client = Client(None, {"Main Page": PageInfo(1, 0, "Main Page", False, False)})
    for text in (SINGLE + "\n" + SINGLE, SINGLE + "\n" + SINGLE.replace("id=p1", "id=p2")):
        with pytest.raises(InvalidRevision):
            control.bind_titles(client, parse(text, cfg), cfg.selector_policy, SurfaceKind.SINGLE)


def test_refresh_skips_parsing_a_revision_already_reconciled(cfg):
    read = read_of(SINGLE)
    client = Client(read, {"Main Page": PageInfo(1, 0, "Main Page", False, False)})
    db = Recorder(snapshot=snapshot_of(read))
    control.refresh_surface(client, db, cfg, SINGLE_SURFACE, NOW)
    assert db.reconciled == [] and client.title_calls == 0


def test_refresh_reconciles_a_new_revision(cfg):
    read = read_of(SINGLE, revision_id=1001)
    client = Client(read, {"Main Page": PageInfo(1, 0, "Main Page", False, False)})
    db = Recorder(snapshot=snapshot_of(read_of(SINGLE, revision_id=1000)))
    control.refresh_surface(client, db, cfg, SINGLE_SURFACE, NOW)
    assert db.reconciled == [(1001, ("p1",))] and db.paused == []


@pytest.mark.parametrize("failure", [
    SurfaceUnavailable("surface-title-mismatch"),
    ApiFailure(FailureKind.TRANSIENT, "maxlag"),
])
def test_an_unreadable_surface_only_pauses_that_surface(failure, cfg):
    db = Recorder()
    control.refresh_surface(Client(failure), db, cfg, SINGLE_SURFACE, NOW)
    assert len(db.paused) == 1 and db.paused[0][0] == 111
    assert db.reconciled == []


def test_a_broken_revision_pauses_without_reconciling(cfg):
    read = read_of("{{Unknown|id=a}}", revision_id=1002)
    db = Recorder()
    control.refresh_surface(Client(read), db, cfg, SINGLE_SURFACE, NOW)
    assert db.reconciled == [] and "invalid-revision" in db.paused[0][1]


def test_title_binding_failure_pauses_the_surface(cfg):
    class Failing(Client):
        def resolve_titles(self, titles):
            raise IdentityUnknown("ambiguous-normalization")

    db = Recorder()
    control.refresh_surface(Failing(read_of(SINGLE, revision_id=1003)), db, cfg,
                            SINGLE_SURFACE, NOW)
    assert db.reconciled == [] and "ambiguous-normalization" in db.paused[0][1]


class Job:
    surface_page_id = 111
    request_id = "p1"


class Request:
    def __init__(self, active=True, suspended=False):
        self.active = active
        self.suspended = suspended


def test_authorize_now_reconciles_a_newer_valid_revision_before_reserving(cfg):
    read = read_of(SINGLE, revision_id=1004)
    client = Client(read, {"Main Page": PageInfo(1, 0, "Main Page", False, False)})
    db = Recorder(snapshot=snapshot_of(read_of(SINGLE, revision_id=1000)),
                  request=Request())
    auth = control.authorize_now(client, db, cfg, Job(), NOW)
    assert auth.allowed and auth.revision_id == 1004 and auth.author == "Op"
    assert db.reconciled == [(1004, ("p1",))]


def test_authorize_now_refuses_a_removed_request_but_allows_a_suspended_one(cfg):
    read = read_of(SINGLE)
    client = Client(read, {"Main Page": PageInfo(1, 0, "Main Page", False, False)})
    snapshot = snapshot_of(read)
    removed = control.authorize_now(client, Recorder(snapshot, Request(active=False)), cfg,
                                    Job(), NOW)
    assert not removed.allowed and removed.code == "request-not-active"
    resumed = control.authorize_now(client, Recorder(snapshot, Request(suspended=True)), cfg,
                                    Job(), NOW)
    assert resumed.allowed


def test_authorize_now_refuses_and_pauses_on_weaker_protection(cfg):
    weak = SurfaceRead(111, "User:PurgeBot/Pages", (ProtectionEntry("edit", "autoconfirmed"),),
                       1000, "Op", NOW, SINGLE, digest(SINGLE))
    db = Recorder(snapshot_of(read_of(SINGLE)), Request())
    auth = control.authorize_now(Client(weak), db, cfg, Job(), NOW)
    assert not auth.allowed and db.paused[0][1] == "surface-edit-protection-not-allowed"


def test_surface_gate_lookup_is_exact(cfg):
    assert control.surface_gate(cfg, 222) is FANOUT_SURFACE
    with pytest.raises(InvariantViolation):
        control.surface_gate(cfg, 999)


def load_toml(tmp_path, monkeypatch, *replacements):
    text = VALID_CONFIG_TOML
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("PURGEBOT_TEST_SECRET", "not-a-real-secret")
    return load_config(path)


def test_a_placeholder_cannot_start_the_bot(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="placeholder"):
        load_toml(tmp_path, monkeypatch,
                  ('database = "purgebot"', 'database = "<pending>"'))


def test_a_fully_ratified_config_loads(tmp_path, monkeypatch):
    load_toml(tmp_path, monkeypatch)


def test_the_auth_mode_gate_fails_before_any_database_or_http(tmp_path, monkeypatch):
    monkeypatch.setenv("PURGEBOT_TEST_SECRET", "x")
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG_TOML.replace(
        'auth_mode = "oauth2-owner-only"', 'auth_mode = "unsupported"'))
    with pytest.raises(ConfigError, match="auth_mode"):
        load_config(path)


def test_a_missing_secret_stops_the_start(tmp_path, monkeypatch):
    monkeypatch.delenv("PURGEBOT_TEST_SECRET", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG_TOML)
    with pytest.raises(ConfigError, match="secret"):
        load_config(path)


@pytest.mark.parametrize("replacement,message", [
    (("maxlag = 5\n", ""), "missing"),
    (("mode = \"log-only\"", "mode = \"on-wiki\""), "log-only"),
    (("tick_cron = \"*/5 * * * *\"", "tick_cron = \"*/5 * * * *\"\nextra = 1"), "unknown key"),
    (("api_url = \"https://en.wikipedia.org/w/api.php\"",
      "api_url = \"http://en.wikipedia.org/w/api.php\""), "https"),
    (("retry_delays_s = [60, 300, 1800, 7200, 43200]", "retry_delays_s = [60, 300, 300, 7200, 43200]"),
     "increasing"),
    (("attempts_per_target_24h = 6", "attempts_per_target_24h = 4"), "len(retry_delays_s)+1"),
    (("force_batch = 25", "force_batch = 51"), "force_batch"),
    (("worker_runtime_s = 240", "worker_runtime_s = 300"), "platform_timeout_s"),
    (("force_attempts_24h = 1500", "force_attempts_24h = 4000"), "target_attempts_24h"),
    (("assert_mode = \"user\"", "assert_mode = \"admin\""), "assert_mode"),
    (("allowed_edit_levels = [\"extendedconfirmed\", \"templateeditor\", \"sysop\"]",
      "allowed_edit_levels = [\"autoconfirmed\", \"extendedconfirmed\", \"templateeditor\", \"sysop\"]"),
     "protected differently"),
    (("kind = \"fanout\"", "kind = \"single\""), "exactly one"),
])
def test_inconsistent_or_incomplete_configuration_is_refused(replacement, message, tmp_path,
                                                             monkeypatch):
    with pytest.raises(ConfigError) as excinfo:
        load_toml(tmp_path, monkeypatch, replacement)
    assert message in str(excinfo.value)
