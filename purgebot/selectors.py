# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

"""read fanout selectors twice and reject any that change"""

from __future__ import annotations

from .model import (
    Action, InvalidEnumeration, SelectorDrift, SelectorOverCap, TargetIdentity, TargetMissing,
    canonical_bytes, digest,
)


def enumerate_pass(client,action,key,policy,cap,before_read):
    if action==Action.CATEGORY:
        fixed={"action":"query","list":"categorymembers","cmtitle":key,
               "cmprop":"ids|title|type","cmlimit":"max"}|policy.category_params(); result="categorymembers"
    else:
        fixed={"action":"query","list":"embeddedin","eititle":key,
               "eilimit":"max"}|policy.embeddedin_params(); result="embeddedin"
    cont={}; seen_cont=set(); by_id={}; by_title={}
    while True:
        before_read(); data=client.get_json(fixed|cont); items=data.get("query",{}).get(result)
        if not isinstance(items,list): raise InvalidEnumeration("missing list")
        for item in items:
            try: t=TargetIdentity(int(item["pageid"]),int(item["ns"]),str(item["title"]))
            except (KeyError,TypeError,ValueError): raise InvalidEnumeration("malformed identity")
            if t.page_id<=0 or not t.canonical_title: raise InvalidEnumeration("invalid identity")
            if t.page_id in by_id and by_id[t.page_id]!=t: raise InvalidEnumeration("pageid drift")
            if t.canonical_title in by_title and by_title[t.canonical_title]!=t.page_id:
                raise InvalidEnumeration("duplicate title")
            by_id[t.page_id]=t; by_title[t.canonical_title]=t.page_id
            if len(by_id)>cap: raise SelectorOverCap(cap)
        if "continue" not in data: return frozenset(by_id.values())
        nxt=data["continue"]
        if not isinstance(nxt,dict) or not nxt or any(not isinstance(k,str) or
           not isinstance(v,(str,int)) or k in fixed for k,v in nxt.items()):
            raise InvalidEnumeration("invalid continuation")
        marker=canonical_bytes(nxt)
        if marker in seen_cont: raise InvalidEnumeration("continuation cycle")
        seen_cont.add(marker); cont=dict(nxt)

def stage_fanout(client,action,key,policy,cap,before_read):
    a=enumerate_pass(client,action,key,policy,cap,before_read)
    b=enumerate_pass(client,action,key,policy,cap,before_read)
    if a!=b: raise SelectorDrift()
    targets=tuple(sorted(a,key=lambda x:x.page_id))
    return targets,digest([[t.page_id,t.namespace_id,t.canonical_title] for t in targets])


def stage_direct(client, title):
    info = client.resolve_titles((title,))[title]
    if info.missing or info.page_id is None:
        raise TargetMissing(title)
    identity = TargetIdentity(info.page_id, info.namespace_id, info.canonical_title)
    targets = (identity,)
    return targets, digest([[t.page_id, t.namespace_id, t.canonical_title] for t in targets])
