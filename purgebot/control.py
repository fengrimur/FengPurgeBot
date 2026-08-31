# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import unicodedata

import mwparserfromhell
from mwparserfromhell.nodes import ExternalLink, Text

from .config import MEDIAWIKI_MAX_BATCH
from .model import (
    FANOUT, Action, ApiFailure, Authorization, ControlEntry, IdentityUnknown, InvalidRevision,
    InvariantViolation, RawEntry, SurfaceState, SurfaceUnavailable, digest,
)

CATEGORY_NAMESPACE = 14
TEMPLATE_NAMESPACE = 10
SELECTOR_NAMESPACE = {Action.CATEGORY: CATEGORY_NAMESPACE, Action.TEMPLATE: TEMPLATE_NAMESPACE}
REASON_LIMIT = 255


def is_plain_node(node):
    return isinstance(node, Text) or (isinstance(node, ExternalLink) and not node.brackets)


def plain(code,line,field):
    if any(not is_plain_node(n) for n in code.nodes):
        raise InvalidRevision(line,f"{field}: markup/nesting")
    v=unicodedata.normalize("NFC",str(code).strip())
    if not v or any(ord(c)<32 for c in v): raise InvalidRevision(line,f"{field}: empty/control")
    return v

def parse_revision(raw,syntax,surface_kind,schedules):
    out=[]
    for no,line in enumerate(raw.splitlines(),1):
        if not line.strip(): continue
        code=mwparserfromhell.parse(line); top=code.filter_templates(recursive=False)
        if len(top)!=1 or str(code).strip()!=str(top[0]):
            raise InvalidRevision(no,"one template per line")
        t=top[0]; adapter=syntax.adapter_for(str(t.name).strip().replace("_"," "))
        values={}; seen=set()
        for p in t.params:
            name=str(p.name).strip()
            if not p.showkey or name in seen or name not in adapter.field_map:
                raise InvalidRevision(no,"positional/duplicate/unknown parameter")
            seen.add(name); values[adapter.field_map[name]]=plain(p.value,no,name)
        if set(values)!=adapter.required_fields(surface_kind):
            raise InvalidRevision(no,"missing/forbidden parameter")
        try: action=Action(values["action"])
        except ValueError: raise InvalidRevision(no,"unknown action")
        adapter.require_action(surface_kind,action)
        schedule=schedules.get(values["schedule"])
        if schedule is None: raise InvalidRevision(no,"unknown schedule")
        request_id=values["request_id"]
        if len(request_id)>128: raise InvalidRevision(no,"request_id too long")
        discussion=values.get("discussion"); adapter.require_permalink(action in FANOUT,discussion)
        out.append(RawEntry(request_id,action,values["target"],values["schedule"],
                            schedule,discussion,no))
    return out


def require_protection(protection, gate) -> None:
    levels = {entry.level for entry in protection if entry.type == "edit"}
    if not levels:
        raise SurfaceUnavailable("surface-has-no-edit-protection")
    if not levels <= gate.allowed_edit_levels:
        raise SurfaceUnavailable("surface-edit-protection-not-allowed")


def fetch_surface(client, gate):
    read = client.read_control_page(gate.page_id)
    if read.title != gate.expected_title:
        raise SurfaceUnavailable("surface-title-mismatch")
    require_protection(read.protection, gate)
    return read


def _chunks(values, size):
    return [values[i:i + size] for i in range(0, len(values), size)]


def bind_titles(client, entries, policy, surface_kind) -> tuple[ControlEntry, ...]:
    seen_ids: set[str] = set()
    for entry in entries:
        if entry.request_id in seen_ids:
            raise InvalidRevision(entry.line, "duplicate request_id in one revision")
        seen_ids.add(entry.request_id)
        if "|" in entry.target or "#" in entry.target:
            raise InvalidRevision(entry.line, "target: unusable character")
    resolved = {}
    for chunk in _chunks(sorted({entry.target for entry in entries}), MEDIAWIKI_MAX_BATCH):
        resolved.update(client.resolve_titles(chunk))
    bound = []
    digests: set[bytes] = set()
    for entry in entries:
        info = resolved[entry.target]
        expected_namespace = SELECTOR_NAMESPACE.get(entry.action)
        if expected_namespace is not None and info.namespace_id != expected_namespace:
            raise InvalidRevision(entry.line,
                                  f"selector must live in namespace {expected_namespace}")
        if entry.action not in FANOUT:
            if info.namespace_id not in policy.allowed_namespaces:
                raise InvalidRevision(entry.line, "target namespace not allowed")
            if info.redirect and policy.direct_page_redirect_policy == "reject":
                raise InvalidRevision(entry.line, "target is a redirect")
        semantic = digest([str(entry.action), info.namespace_id, info.canonical_title,
                           entry.schedule_key, entry.discussion_url])
        if semantic in digests:
            raise InvalidRevision(entry.line, "semantic duplicate of an earlier request")
        digests.add(semantic)
        bound.append(ControlEntry(
            request_id=entry.request_id, action=entry.action, target=info.canonical_title,
            target_namespace=info.namespace_id, schedule_key=entry.schedule_key,
            schedule=entry.schedule, discussion_url=entry.discussion_url,
            semantic_sha256=semantic))
    return tuple(bound)


def surface_gate(cfg, page_id):
    for gate in cfg.control_surfaces:
        if gate.page_id == page_id:
            return gate
    raise InvariantViolation(f"no configured control surface with page id {page_id}")


def is_already_reconciled(snapshot, read) -> bool:
    return (snapshot is not None and snapshot.state is SurfaceState.VALID
            and snapshot.last_revision_id == read.revision_id
            and snapshot.content_sha256 == read.content_sha256)


def reconcile_new_revision(mw, db, cfg, gate, read, now) -> str | None:
    """reconcile a revision and return a reason if it fails"""
    try:
        entries = parse_revision(read.wikitext, cfg.request_syntax, gate.kind,
                                 cfg.schedule_catalog)
        bound = bind_titles(mw, entries, cfg.selector_policy, gate.kind)
        db.reconcile_surface(gate, read, bound, now)
    except InvalidRevision as exc:
        return f"invalid-revision: {exc}"[:REASON_LIMIT]
    except IdentityUnknown as exc:
        return f"title-binding: {exc.code}"[:REASON_LIMIT]
    except ApiFailure as exc:
        return f"title-binding: {exc.code}"[:REASON_LIMIT]
    return None


def refresh_surface(mw, db, cfg, gate, now) -> None:
    try:
        read = fetch_surface(mw, gate)
    except SurfaceUnavailable as exc:
        db.pause_surface(gate, exc.code, now)
        return
    except ApiFailure as exc:
        db.pause_surface(gate, f"unreadable: {exc.code}"[:REASON_LIMIT], now)
        return
    if is_already_reconciled(db.surface_snapshot(gate.page_id), read):
        return
    reason = reconcile_new_revision(mw, db, cfg, gate, read, now)
    if reason is not None:
        db.pause_surface(gate, reason, now)


def authorize_now(mw, db, cfg, job, now) -> Authorization:
    gate = surface_gate(cfg, job.surface_page_id)
    try:
        read = fetch_surface(mw, gate)
    except SurfaceUnavailable as exc:
        db.pause_surface(gate, exc.code, now)
        return Authorization(False, exc.code, None, None)
    except ApiFailure as exc:
        db.pause_surface(gate, f"unreadable: {exc.code}"[:REASON_LIMIT], now)
        return Authorization(False, exc.code, None, None)
    if not is_already_reconciled(db.surface_snapshot(gate.page_id), read):
        reason = reconcile_new_revision(mw, db, cfg, gate, read, now)
        if reason is not None:
            db.pause_surface(gate, reason, now)
            return Authorization(False, "surface-paused", None, None)
    request = db.request_row(job.surface_page_id, job.request_id)
    if request is None or not request.active:
        return Authorization(False, "request-not-active", None, None)
    return Authorization(True, "authorized", read.revision_id, read.author)
