# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

"""call the mediawiki api without retires/redirects"""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from math import ceil

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import MEDIAWIKI_MAX_BATCH, RATIFIED_AUTH_MODES
from .model import (
    ApiFailure, ConfigError, FailureKind, IdentityUnknown, PageInfo, ProtectionEntry,
    SurfaceRead, SurfaceUnavailable, TargetIdentity, TargetState, digest, require, utc_now,
)

from requests import exceptions as rx
TRANSIENT={"maxlag","ratelimited","readonly"}

def build_session(cfg):
    s=requests.Session()
    r=Retry(total=0,connect=0,read=0,redirect=0,status=0,other=0,
            raise_on_redirect=False,raise_on_status=False)
    s.mount("https://",HTTPAdapter(max_retries=r))
    s.headers.update({"User-Agent":cfg.operator.contact_user_agent,"Accept":"application/json"})
    authenticate_exactly_ratified_mode(s,cfg.operator)
    return s

def parse_retry_after(value,now=utc_now):
    if value is None: return None
    try:
        if value.strip().isdigit(): return int(value)
        return max(0,ceil((parsedate_to_datetime(value)-now()).total_seconds()))
    except (TypeError,ValueError,OverflowError): return None

class MediaWikiClient:
    def __init__(self,session,cfg):
        self.session=session; self.cfg=cfg
        self.timeout=(cfg.safety.http_connect_timeout_s,cfg.safety.http_read_timeout_s)
    def base(self):
        return {"format":"json","formatversion":"2","errorformat":"plaintext",
                "maxlag":str(self.cfg.safety.maxlag),"assert":self.cfg.operator.assert_mode,
                "assertuser":self.cfg.operator.assert_user}
    def get_json(self,p): return self.call("GET",self.base()|p,False)
    def purge_once(self,page_ids,force):
        p=self.base()|{"action":"purge","pageids":"|".join(str(x) for x in page_ids)}
        if force: p["forcelinkupdate"]="1"
        return self.call("POST",p,True)
    def call(self,method,p,side_effect):
        try:
            if method=="GET":
                r=self.session.get(self.cfg.wiki.api_url,params=p,timeout=self.timeout,allow_redirects=False)
            else:
                r=self.session.post(self.cfg.wiki.api_url,data=p,timeout=self.timeout,allow_redirects=False)
        except rx.ConnectTimeout as e:
            raise ApiFailure(FailureKind.TRANSIENT,"connect-timeout") from e
        except (rx.ReadTimeout,rx.ConnectionError) as e:
            k=FailureKind.AMBIGUOUS if side_effect else FailureKind.TRANSIENT
            raise ApiFailure(k,"read-or-connection-loss") from e
        if 300<=r.status_code<400: raise ApiFailure(FailureKind.OPERATOR,"http-redirect")
        retry=parse_retry_after(r.headers.get("Retry-After"))
        if r.status_code==429:
            raise ApiFailure(FailureKind.TRANSIENT,f"http-{r.status_code}",retry)
        try: data=r.json()
        except rx.JSONDecodeError as e:
            if r.status_code==503:
                k=FailureKind.AMBIGUOUS if side_effect else FailureKind.TRANSIENT
                raise ApiFailure(k,"http-503-unstructured",retry) from e
            k=FailureKind.AMBIGUOUS if side_effect else FailureKind.OPERATOR
            raise ApiFailure(k,"invalid-json",retry) from e
        if not isinstance(data,dict):
            k=FailureKind.AMBIGUOUS if side_effect else FailureKind.OPERATOR
            raise ApiFailure(k,"non-object-response",retry)
        code,kind=classify_api_response(data)
        if code:
            raise ApiFailure(kind,code,retry)
        if r.status_code==503:
            k=FailureKind.AMBIGUOUS if side_effect else FailureKind.TRANSIENT
            raise ApiFailure(k,"http-503",retry)
        if not 200<=r.status_code<300:
            k=FailureKind.AMBIGUOUS if side_effect and r.status_code>=500 else FailureKind.OPERATOR
            raise ApiFailure(k,f"http-{r.status_code}",retry)
        return data

    def resolve_pageids(self, page_ids) -> dict[int, TargetIdentity]:
        wanted = tuple(page_ids)
        if not wanted:
            return {}
        require(len(wanted) <= MEDIAWIKI_MAX_BATCH, "page-id batch exceeds the API limit")
        data = self.get_json({"action": "query", "prop": "info",
                              "pageids": "|".join(str(p) for p in wanted)})
        query = data.get("query")
        if not isinstance(query, dict):
            raise IdentityUnknown("missing-query")
        if query.get("badpageids"):
            raise IdentityUnknown("bad-pageids")
        identities: dict[int, TargetIdentity] = {}
        for page in _pages(query):
            page_id = page.get("pageid")
            if not isinstance(page_id, int) or page_id not in wanted or page_id in identities:
                raise IdentityUnknown("pageid-mismatch")
            if page.get("missing") or page.get("invalid"):
                raise IdentityUnknown("page-missing")
            namespace, title = page.get("ns"), page.get("title")
            if not isinstance(namespace, int) or not isinstance(title, str) or not title:
                raise IdentityUnknown("malformed-identity")
            identities[page_id] = TargetIdentity(page_id, namespace, title)
        if set(identities) != set(wanted):
            raise IdentityUnknown("incomplete-resolution")
        if len({i.canonical_title for i in identities.values()}) != len(identities):
            raise IdentityUnknown("duplicate-title")
        return identities

    def resolve_titles(self, titles) -> dict[str, PageInfo]:
        wanted = tuple(titles)
        if not wanted:
            return {}
        require(len(wanted) <= MEDIAWIKI_MAX_BATCH, "title batch exceeds the API limit")
        data = self.get_json({"action": "query", "prop": "info", "titles": "|".join(wanted)})
        query = data.get("query")
        if not isinstance(query, dict):
            raise IdentityUnknown("missing-query")
        if query.get("interwiki") or query.get("converted"):
            raise IdentityUnknown("interwiki-or-converted-title")
        normalized = {}
        for item in query.get("normalized") or ():
            if not isinstance(item, dict) or not isinstance(item.get("from"), str) \
                    or not isinstance(item.get("to"), str):
                raise IdentityUnknown("malformed-normalization")
            if item["from"] in normalized:
                raise IdentityUnknown("ambiguous-normalization")
            normalized[item["from"]] = item["to"]
        by_title = {}
        for page in _pages(query):
            title = page.get("title")
            namespace = page.get("ns")
            if not isinstance(title, str) or not isinstance(namespace, int):
                raise IdentityUnknown("malformed-identity")
            if page.get("invalid") or title in by_title:
                raise IdentityUnknown("invalid-or-duplicate-title")
            by_title[title] = page
        resolved = {}
        for raw in wanted:
            page = by_title.get(normalized.get(raw, raw))
            if page is None:
                raise IdentityUnknown("title-not-resolved")
            page_id = page.get("pageid")
            missing = bool(page.get("missing"))
            if missing != (page_id is None):
                raise IdentityUnknown("missing-flag-contradicts-pageid")
            resolved[raw] = PageInfo(page_id=page_id, namespace_id=page["ns"],
                                     canonical_title=page["title"], missing=missing,
                                     redirect=bool(page.get("redirect")))
        return resolved

    def read_control_page(self, page_id) -> SurfaceRead:
        data = self.get_json({
            "action": "query", "pageids": str(page_id),
            "prop": "info|revisions", "inprop": "protection",
            "rvprop": "ids|timestamp|user|content", "rvslots": "main", "rvlimit": "1"})
        query = data.get("query")
        if not isinstance(query, dict):
            raise SurfaceUnavailable("surface-missing-query")
        pages = query.get("pages")
        if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
            raise SurfaceUnavailable("surface-not-unique")
        page = pages[0]
        if page.get("missing") or page.get("invalid") or page.get("pageid") != page_id:
            raise SurfaceUnavailable("surface-missing-or-wrong-pageid")
        title = page.get("title")
        if not isinstance(title, str) or not title:
            raise SurfaceUnavailable("surface-without-title")
        protection = []
        for item in page.get("protection") or ():
            if not isinstance(item, dict) or not isinstance(item.get("type"), str) \
                    or not isinstance(item.get("level"), str):
                raise SurfaceUnavailable("surface-malformed-protection")
            protection.append(ProtectionEntry(type=item["type"], level=item["level"]))
        revisions = page.get("revisions")
        if not isinstance(revisions, list) or len(revisions) != 1:
            raise SurfaceUnavailable("surface-revision-not-unique")
        revision = revisions[0]
        content = (((revision.get("slots") or {}).get("main") or {}).get("content"))
        revision_id, author, stamp = (revision.get("revid"), revision.get("user"),
                                      revision.get("timestamp"))
        if not isinstance(revision_id, int) or not isinstance(author, str) or not author \
                or not isinstance(content, str) or not isinstance(stamp, str):
            raise SurfaceUnavailable("surface-malformed-revision")
        try:
            timestamp = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise SurfaceUnavailable("surface-malformed-timestamp") from exc
        return SurfaceRead(page_id=page_id, title=title, protection=tuple(protection),
                           revision_id=revision_id, author=author, revision_timestamp=timestamp,
                           wikitext=content, content_sha256=digest(content))


def _pages(query) -> list[dict]:
    pages = query.get("pages")
    if not isinstance(pages, list) or not all(isinstance(p, dict) for p in pages):
        raise IdentityUnknown("missing-pages")
    return pages


def _codes(data, key) -> tuple[str, ...]:
    value = data.get(key)
    if isinstance(value, dict):
        code = value.get("code")
        return (code,) if isinstance(code, str) else ()
    if isinstance(value, list):
        return tuple(item["code"] for item in value
                     if isinstance(item, dict) and isinstance(item.get("code"), str))
    return ()


def classify_api_response(data) -> tuple[str | None, FailureKind | None]:
    errors = _codes(data, "error") + _codes(data, "errors")
    for code in errors + _codes(data, "warnings"):
        if code in TRANSIENT:
            return code, FailureKind.TRANSIENT
    if errors:
        return errors[0], FailureKind.OPERATOR
    return None, None


def authenticate_exactly_ratified_mode(session, operator):
    if operator.auth_mode not in RATIFIED_AUTH_MODES:
        raise ConfigError(
            f"operator.auth_mode {operator.auth_mode!r}: unsupported authentication mode")
    if operator.auth_mode == "oauth2-owner-only":
        access_token = operator.secret.resolve()
        if any(character.isspace() for character in access_token):
            raise ConfigError("operator OAuth 2 access token contains whitespace")
        session.headers["Authorization"] = f"Bearer {access_token}"
        return
    raise ConfigError(
        f"operator.auth_mode {operator.auth_mode!r}: supported mode has no implementation")


def correlate_purge(pre,raw,post,force):
    """match purge results to the requested page ids"""
    unknown={pid:TargetState.UNKNOWN for pid in pre}
    if not isinstance(raw,dict): return unknown
    items=raw.get("purge")
    if pre!=post or not isinstance(items,list) or "normalized" in raw or "redirects" in raw: return unknown
    page_id_of={identity.canonical_title:pid for pid,identity in pre.items()}
    if len(page_id_of)!=len(pre): return unknown
    by_title={}
    for x in items:
        title=x.get("title") if isinstance(x,dict) else None
        if not isinstance(title,str) or title in by_title or title not in page_id_of: return unknown
        if x.get("ns")!=pre[page_id_of[title]].namespace_id: return unknown
        by_title[title]=x
    if set(by_title)!=set(page_id_of): return unknown
    return {page_id_of[title]:(TargetState.UNKNOWN if "missing" in x or "invalid" in x else
                 TargetState.API_ACCEPTED if "purged" in x and (not force or "linkupdate" in x)
                 else TargetState.FAILED) for title,x in by_title.items()}
