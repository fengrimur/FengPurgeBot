# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import pytest

from purgebot import selectors
from purgebot.model import (
    Action, InvalidEnumeration, PageInfo, SelectorDrift, SelectorOverCap, TargetIdentity,
    TargetMissing,
)


def member(page_id, title, ns=0):
    return {"pageid": page_id, "ns": ns, "title": title}


def page(items, continuation=None, key="categorymembers"):
    body = {"query": {key: items}}
    if continuation is not None:
        body["continue"] = continuation
    return body


class Client:
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
        item = self.titles
        if isinstance(item, Exception):
            raise item
        return {t: item[t] for t in titles}


def enumerate_with(responses, cap=100, action=Action.CATEGORY, policy=None):
    client = Client(responses)
    return client, selectors.enumerate_pass(client, action, "Category:Foo", policy,
                                            cap, lambda: None)


def test_enumeration_follows_every_continuation_including_empty_pages(cfg):
    client, found = enumerate_with(
        [page([member(1, "A")], {"cmcontinue": "x", "continue": "-||"}),
         page([], {"cmcontinue": "y", "continue": "-||"}),
         page([member(2, "B")])], policy=cfg.selector_policy)
    assert sorted(t.page_id for t in found) == [1, 2]
    assert len(client.requests) == 3
    assert client.requests[1]["cmcontinue"] == "x" and client.requests[1]["continue"] == "-||"


def test_the_selector_is_a_title_key_never_a_page_id(cfg):
    client, _ = enumerate_with([page([member(1, "A")])], policy=cfg.selector_policy)
    assert client.requests[0]["cmtitle"] == "Category:Foo"
    assert "cmpageid" not in client.requests[0]
    client = Client([page([member(1, "A")], key="embeddedin")])
    selectors.enumerate_pass(client, Action.TEMPLATE, "Template:Bar", cfg.selector_policy,
                             100, lambda: None)
    assert client.requests[0]["eititle"] == "Template:Bar"
    assert "eipageid" not in client.requests[0]
    assert client.requests[0]["einamespace"] == "0|10|14"
    assert client.requests[0]["eifilterredir"] == "all"


def test_the_namespace_and_member_gates_reach_the_query(cfg):
    client, _ = enumerate_with([page([member(1, "A")])], policy=cfg.selector_policy)
    assert client.requests[0]["cmnamespace"] == "0|10|14"
    assert client.requests[0]["cmtype"] == "page"


@pytest.mark.parametrize("responses,detail", [
    ([page([member(1, "A")], {"cmcontinue": "x"}), page([member(2, "B")], {"cmcontinue": "x"})],
     "continuation cycle"),
    ([page([member(1, "A")], {"cmtitle": "Category:Evil"})], "invalid continuation"),
    ([page([member(1, "A")], {"cmcontinue": ["x"]})], "invalid continuation"),
    ([page([member(1, "A"), member(2, "A")])], "duplicate title"),
    ([page([member(1, "A"), member(1, "B")])], "pageid drift"),
    ([page([{"ns": 0, "title": "A"}])], "malformed identity"),
    ([page([member(0, "A")])], "invalid identity"),
    ([{"query": {}}], "missing list"),
])
def test_malformed_enumerations_are_refused(responses, detail, cfg):
    with pytest.raises(InvalidEnumeration) as excinfo:
        enumerate_with(responses, policy=cfg.selector_policy)
    assert detail in str(excinfo.value)


def test_the_cap_plus_one_hit_stops_the_whole_enumeration(cfg):
    _, found = enumerate_with([page([member(i, f"P{i}") for i in (1, 2, 3)])], cap=3,
                              policy=cfg.selector_policy)
    assert len(found) == 3
    with pytest.raises(SelectorOverCap):
        enumerate_with([page([member(i, f"P{i}") for i in (1, 2, 3, 4)])], cap=3,
                       policy=cfg.selector_policy)


def test_two_identical_passes_stage_a_sorted_set_with_a_digest(cfg):
    responses = [page([member(2, "B"), member(1, "A")]), page([member(1, "A"), member(2, "B")])]
    targets, sha = selectors.stage_fanout(Client(responses), Action.CATEGORY, "Category:Foo",
                                          cfg.selector_policy, 100, lambda: None)
    assert [t.page_id for t in targets] == [1, 2] and len(sha) == 32
    same = [page([member(1, "A"), member(2, "B")]), page([member(2, "B"), member(1, "A")])]
    _, sha_again = selectors.stage_fanout(Client(same), Action.CATEGORY, "Category:Foo",
                                          cfg.selector_policy, 100, lambda: None)
    assert sha == sha_again


def test_a_second_pass_that_disagrees_is_drift(cfg):
    responses = [page([member(1, "A")]), page([member(1, "A"), member(2, "B")])]
    with pytest.raises(SelectorDrift):
        selectors.stage_fanout(Client(responses), Action.CATEGORY, "Category:Foo",
                               cfg.selector_policy, 100, lambda: None)


def test_the_read_guard_runs_before_every_page(cfg):
    seen = []
    client = Client([page([member(1, "A")], {"cmcontinue": "x"}), page([member(2, "B")])])
    selectors.enumerate_pass(client, Action.CATEGORY, "Category:Foo", cfg.selector_policy,
                             100, lambda: seen.append(len(client.requests)))
    assert seen == [0, 1]


def test_direct_staging_resolves_a_page_id_or_reports_the_page_missing():
    client = Client([], {"Main Page": PageInfo(1, 0, "Main Page", False, False)})
    targets, sha = selectors.stage_direct(client, "Main Page")
    assert targets == (TargetIdentity(1, 0, "Main Page"),) and len(sha) == 32
    gone = Client([], {"Gone": PageInfo(None, 0, "Gone", True, False)})
    with pytest.raises(TargetMissing):
        selectors.stage_direct(gone, "Gone")
