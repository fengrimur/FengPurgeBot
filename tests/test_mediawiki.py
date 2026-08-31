# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
import requests

from conftest import FakeSession, Response, surface_page
from purgebot.mediawiki import (
    MediaWikiClient, authenticate_exactly_ratified_mode, build_session, correlate_purge,
    parse_retry_after,
)
from purgebot.model import (
    ApiFailure, ConfigError, FailureKind, IdentityUnknown, SurfaceUnavailable, TargetIdentity,
    TargetState,
)

PRE = {1: TargetIdentity(1, 0, "A"), 2: TargetIdentity(2, 0, "B")}


def client(cfg, *responses):
    session = FakeSession(responses)
    return MediaWikiClient(session, cfg), session


def purged(title, ns=0, force=True, **extra):
    item = {"ns": ns, "title": title, "purged": True}
    if force:
        item["linkupdate"] = True
    return item | extra


def test_a_purge_addresses_page_ids_and_sends_exactly_one_post(cfg):
    mw, session = client(cfg, Response(200, {"purge": [purged("A"), purged("B")]}))
    mw.purge_once((1, 2), True)
    method, body, timeout, redirects = session.calls[0]
    assert method == "POST" and len(session.posts()) == 1
    assert body["pageids"] == "1|2" and "titles" not in body
    assert body["action"] == "purge" and body["forcelinkupdate"] == "1"
    assert body["assert"] == "user" and body["assertuser"] == "PurgeBot"
    assert body["maxlag"] == "5" and body["formatversion"] == "2"
    assert timeout == (5.0, 25.0) and redirects is False
    assert "forcerecursivelinkupdate" not in body

    mw, session = client(cfg, Response(200, {"purge": [purged("A", force=False)]}))
    mw.purge_once((1,), False)
    assert "forcelinkupdate" not in session.calls[0][1]


def test_reads_are_gets_that_never_follow_redirects(cfg):
    mw, session = client(cfg, Response(200, {"query": {"pages": []}}))
    mw.get_json({"action": "query"})
    method, body, timeout, redirects = session.calls[0]
    assert method == "GET" and redirects is False and timeout == (5.0, 25.0)
    assert body["assert"] == "user" and body["maxlag"] == "5"


def test_assert_mode_switches_to_bot_only_when_configured(cfg):
    operator = dataclasses.replace(cfg.operator, assert_mode="bot")
    production = dataclasses.replace(cfg, operator=operator)
    mw, session = client(production, Response(200, {"query": {"pages": []}}))
    mw.get_json({"action": "query"})
    assert session.calls[0][1]["assert"] == "bot"
    assert session.calls[0][1]["assertuser"] == "PurgeBot"


def test_the_session_is_built_without_transport_retries_or_redirects(cfg, monkeypatch):
    monkeypatch.setattr("purgebot.mediawiki.authenticate_exactly_ratified_mode",
                        lambda session, operator: None)
    session = build_session(cfg)
    retries = session.get_adapter("https://x/").max_retries
    assert (retries.total, retries.connect, retries.read, retries.redirect, retries.status,
            retries.other) == (0, 0, 0, 0, 0, 0)
    assert session.headers["User-Agent"] == cfg.operator.contact_user_agent


def test_owner_only_oauth2_authentication_sets_only_a_bearer_header(cfg, monkeypatch):
    monkeypatch.setenv("PURGEBOT_SECRET", "access-token")
    session = requests.Session()
    authenticate_exactly_ratified_mode(session, cfg.operator)
    assert session.headers["Authorization"] == "Bearer access-token"


def test_unknown_authentication_mode_is_refused(cfg):
    operator = dataclasses.replace(cfg.operator, auth_mode="unknown")
    with pytest.raises(ConfigError, match="unsupported authentication mode"):
        authenticate_exactly_ratified_mode(requests.Session(), operator)


def test_oauth2_access_token_whitespace_is_refused(cfg, monkeypatch):
    monkeypatch.setenv("PURGEBOT_SECRET", "two tokens")
    with pytest.raises(ConfigError, match="contains whitespace"):
        authenticate_exactly_ratified_mode(requests.Session(), cfg.operator)


@pytest.mark.parametrize("status,payload,headers,side_effect,expected", [
    (503, {"errors": [{"code": "maxlag"}]}, {"Retry-After": "5"}, False,
     (FailureKind.TRANSIENT, "maxlag", 5)),
    (200, {"error": {"code": "readonly"}}, {}, True, (FailureKind.TRANSIENT, "readonly", None)),
    (200, {"errors": [{"code": "ratelimited"}]}, {}, True,
     (FailureKind.TRANSIENT, "ratelimited", None)),
    (200, {"warnings": [{"code": "maxlag"}], "purge": []}, {}, True,
     (FailureKind.TRANSIENT, "maxlag", None)),
    (429, "<html>", {"Retry-After": "120"}, False, (FailureKind.TRANSIENT, "http-429", 120)),
    (503, "<html>", {}, False, (FailureKind.TRANSIENT, "http-503-unstructured", None)),
    (503, "<html>", {}, True, (FailureKind.AMBIGUOUS, "http-503-unstructured", None)),
    (503, {"servedby": "x"}, {}, False, (FailureKind.TRANSIENT, "http-503", None)),
    (503, {"servedby": "x"}, {}, True, (FailureKind.AMBIGUOUS, "http-503", None)),
    (200, {"errors": [{"code": "assertuserfailed"}]}, {}, True,
     (FailureKind.OPERATOR, "assertuserfailed", None)),
    (302, {}, {}, True, (FailureKind.OPERATOR, "http-redirect", None)),
    (500, {}, {}, False, (FailureKind.OPERATOR, "http-500", None)),
    (500, {}, {}, True, (FailureKind.AMBIGUOUS, "http-500", None)),
    (200, "<html>", {}, False, (FailureKind.OPERATOR, "invalid-json", None)),
    (200, "<html>", {}, True, (FailureKind.AMBIGUOUS, "invalid-json", None)),
    (200, [1, 2], {}, False, (FailureKind.OPERATOR, "non-object-response", None)),
    (200, [1, 2], {}, True, (FailureKind.AMBIGUOUS, "non-object-response", None)),
])
def test_response_classification(status, payload, headers, side_effect, expected, cfg):
    mw, _ = client(cfg, Response(status, payload, headers))
    with pytest.raises(ApiFailure) as excinfo:
        mw.call("POST" if side_effect else "GET", {"action": "purge"}, side_effect)
    failure = excinfo.value
    assert (failure.kind, failure.code, failure.retry_after_s) == expected


@pytest.mark.parametrize("error,side_effect,expected", [
    (requests.exceptions.ConnectTimeout(), True, (FailureKind.TRANSIENT, "connect-timeout")),
    (requests.exceptions.ReadTimeout(), False, (FailureKind.TRANSIENT, "read-or-connection-loss")),
    (requests.exceptions.ReadTimeout(), True, (FailureKind.AMBIGUOUS, "read-or-connection-loss")),
    (requests.exceptions.ConnectionError(), True,
     (FailureKind.AMBIGUOUS, "read-or-connection-loss")),
])
def test_transport_failures_split_read_side_from_post_ambiguity(error, side_effect, expected, cfg):
    mw, _ = client(cfg, error)
    with pytest.raises(ApiFailure) as excinfo:
        mw.call("POST" if side_effect else "GET", {}, side_effect)
    assert (excinfo.value.kind, excinfo.value.code) == expected


@pytest.mark.parametrize("value,expected", [
    (None, None), ("  30 ", 30), ("nonsense", None),
])
def test_parse_retry_after_seconds(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    now = lambda: datetime(2026, 10, 21, 7, 27, 0, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=now) == 60
    assert parse_retry_after("Wed, 21 Oct 2026 07:26:00 GMT", now=now) == 0


def test_correlation_ignores_order_and_needs_the_documented_markers():
    raw = {"purge": [purged("B"), purged("A")]}
    assert correlate_purge(PRE, raw, PRE, True) == {1: TargetState.API_ACCEPTED,
                                                    2: TargetState.API_ACCEPTED}
    without_linkupdate = {"purge": [purged("A", force=False), purged("B", force=False)]}
    assert correlate_purge(PRE, without_linkupdate, PRE, True) == {1: TargetState.FAILED,
                                                                   2: TargetState.FAILED}
    assert correlate_purge(PRE, without_linkupdate, PRE, False) == {1: TargetState.API_ACCEPTED,
                                                                    2: TargetState.API_ACCEPTED}
    not_purged = {"purge": [{"ns": 0, "title": "A"}, {"ns": 0, "title": "B"}]}
    assert correlate_purge(PRE, not_purged, PRE, False) == {1: TargetState.FAILED,
                                                            2: TargetState.FAILED}


def test_the_join_is_by_title_and_the_outcome_key_is_the_reservation_page_id():
    reply = {"purge": [purged("B"), purged("A")]}
    stray = {"purge": [purged("A", pageid=999), purged("B", pageid=998)]}
    assert correlate_purge(PRE, stray, PRE, True) == {1: TargetState.API_ACCEPTED,
                                                      2: TargetState.API_ACCEPTED}
    swapped = {1: TargetIdentity(1, 0, "B"), 2: TargetIdentity(2, 0, "A")}
    assert correlate_purge(swapped, reply, swapped, True) == {1: TargetState.API_ACCEPTED,
                                                              2: TargetState.API_ACCEPTED}


def test_duplicate_titles_in_the_reservation_cannot_be_correlated():
    ambiguous = {1: TargetIdentity(1, 0, "A"), 2: TargetIdentity(2, 0, "A")}
    assert correlate_purge(ambiguous, {"purge": [purged("A")]}, ambiguous, True) \
        == {1: TargetState.UNKNOWN, 2: TargetState.UNKNOWN}


@pytest.mark.parametrize("raw,post", [
    ({"purge": [purged("A")]}, PRE),
    ({"purge": [purged("A"), purged("B"), purged("C")]}, PRE),
    ({"purge": [purged("A"), purged("A")]}, PRE),
    ({"purge": [purged("A"), purged("B", ns=1)]}, PRE),
    ({"purge": [purged("A"), purged("B")], "normalized": []}, PRE),
    ({"purge": [{"ns": 0, "purged": True}]}, PRE),
    ({"purge": "not a list"}, PRE),
    ({}, PRE),
    ("not an object", PRE),
    ({"purge": [purged("A"), purged("B")]}, {1: TargetIdentity(1, 0, "A")}),
])
def test_anything_uncorrelatable_becomes_unknown_for_every_target(raw, post):
    assert correlate_purge(PRE, raw, post, True) == {1: TargetState.UNKNOWN,
                                                     2: TargetState.UNKNOWN}


def test_resolve_pageids_returns_one_identity_per_requested_id(cfg):
    mw, session = client(cfg, Response(200, {"query": {"pages": [
        {"pageid": 2, "ns": 0, "title": "B"}, {"pageid": 1, "ns": 0, "title": "A"}]}}))
    assert mw.resolve_pageids((1, 2)) == PRE
    assert session.calls[0][1]["pageids"] == "1|2"


@pytest.mark.parametrize("payload,code", [
    ({"query": {"pages": [{"pageid": 1, "ns": 0, "title": "A"}]}}, "incomplete-resolution"),
    ({"query": {"pages": [{"pageid": 1, "ns": 0, "title": "A"},
                          {"pageid": 2, "missing": True}]}}, "page-missing"),
    ({"query": {"pages": [{"pageid": 1, "ns": 0, "title": "A"},
                          {"pageid": 2, "ns": 0, "title": "A"}]}}, "duplicate-title"),
    ({"query": {"pages": [{"pageid": 1, "ns": 0, "title": "A"},
                          {"pageid": 9, "ns": 0, "title": "C"}]}}, "pageid-mismatch"),
    ({"query": {"badpageids": [2], "pages": []}}, "bad-pageids"),
    ({"query": {"pages": "x"}}, "missing-pages"),
    ({}, "missing-query"),
])
def test_unresolvable_page_ids_are_reported_not_guessed(payload, code, cfg):
    mw, _ = client(cfg, Response(200, payload))
    with pytest.raises(IdentityUnknown) as excinfo:
        mw.resolve_pageids((1, 2))
    assert excinfo.value.code == code


def test_resolve_titles_maps_through_normalisation_without_following_redirects(cfg):
    mw, session = client(cfg, Response(200, {"query": {
        "normalized": [{"from": "main page", "to": "Main Page"}],
        "pages": [{"pageid": 1, "ns": 0, "title": "Main Page", "redirect": True}]}}))
    resolved = mw.resolve_titles(("main page",))["main page"]
    assert resolved.page_id == 1 and resolved.canonical_title == "Main Page"
    assert resolved.redirect is True and resolved.missing is False
    assert "redirects" not in session.calls[0][1]


def test_resolve_titles_reports_a_missing_page_without_a_page_id(cfg):
    mw, _ = client(cfg, Response(200, {"query": {
        "pages": [{"ns": 14, "title": "Category:Gone", "missing": True}]}}))
    resolved = mw.resolve_titles(("Category:Gone",))["Category:Gone"]
    assert resolved.missing and resolved.page_id is None and resolved.namespace_id == 14


@pytest.mark.parametrize("payload,code", [
    ({"query": {"interwiki": [{"title": "x"}], "pages": []}}, "interwiki-or-converted-title"),
    ({"query": {"pages": [{"ns": 0, "title": "Other"}]}}, "title-not-resolved"),
    ({"query": {"normalized": [{"from": "A"}], "pages": []}}, "malformed-normalization"),
    ({"query": {"pages": [{"ns": 0, "title": "A", "invalid": True}]}},
     "invalid-or-duplicate-title"),
])
def test_unresolvable_titles_are_reported(payload, code, cfg):
    mw, _ = client(cfg, Response(200, payload))
    with pytest.raises(IdentityUnknown) as excinfo:
        mw.resolve_titles(("A",))
    assert excinfo.value.code == code


def test_reading_a_control_page_extracts_protection_and_one_revision(cfg):
    body = surface_page(111, "User:PurgeBot/Pages", 1000, "sysop", "text")
    mw, session = client(cfg, Response(200, {"query": {"pages": [body]}}))
    read = mw.read_control_page(111)
    assert read.revision_id == 1000 and read.author == "Op" and read.wikitext == "text"
    assert read.protection[0].type == "edit" and read.protection[0].level == "sysop"
    params = session.calls[0][1]
    assert params["inprop"] == "protection" and params["rvlimit"] == "1"
    assert params["rvslots"] == "main"


@pytest.mark.parametrize("pages,code", [
    ([], "surface-not-unique"),
    ([{"pageid": 111, "missing": True}], "surface-missing-or-wrong-pageid"),
    ([{"pageid": 9, "ns": 2, "title": "X", "revisions": []}], "surface-missing-or-wrong-pageid"),
])
def test_an_unreadable_control_page_is_reported_not_guessed(pages, code, cfg):
    mw, _ = client(cfg, Response(200, {"query": {"pages": pages}}))
    with pytest.raises(SurfaceUnavailable) as excinfo:
        mw.read_control_page(111)
    assert excinfo.value.code == code


def test_two_revisions_on_a_control_page_are_refused(cfg):
    body = surface_page(111, "User:PurgeBot/Pages", 1000, "sysop", "text")
    body["revisions"] = body["revisions"] * 2
    mw, _ = client(cfg, Response(200, {"query": {"pages": [body]}}))
    with pytest.raises(SurfaceUnavailable, match="revision-not-unique"):
        mw.read_control_page(111)
