# SPDX-FileCopyrightText: 2026 Fengrímur
# SPDX-License-Identifier: AGPL-3.0-only
# See NOTICE for additional terms.

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from .model import Action, ConfigError, FANOUT, InvalidRevision, ScheduleSpec, SurfaceKind

RATIFIED_AUTH_MODES: frozenset[str] = frozenset({"oauth2-owner-only"})
ASSERT_MODES = frozenset({"user", "bot"})

REQUEST_FIELDS = frozenset({"request_id", "action", "target", "schedule", "discussion"})
MANDATORY_REQUEST_FIELDS = frozenset({"request_id", "action", "target", "schedule"})
SINGLE_ACTIONS = frozenset({Action.PURGE, Action.PAGE_LINKS})
FANOUT_ACTIONS = frozenset(FANOUT)
CATEGORY_MEMBER_TYPES = frozenset({"page", "subcat", "file"})
REDIRECT_FILTERS = frozenset({"all", "redirects", "nonredirects"})
DIRECT_REDIRECT_POLICIES = frozenset({"literal", "reject"})
SCHEDULE_KINDS = frozenset({"once", "interval", "annual"})
MEDIAWIKI_MAX_BATCH = 50

_PLACEHOLDER = re.compile(r"\A<.*>\Z", re.S)


@dataclass(frozen=True, slots=True)
class SecretRef:
    source: str
    location: str

    def resolve(self) -> str:
        if self.source == "file":
            try:
                value = Path(self.location).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"operator secret file unreadable: {exc.strerror}") from exc
        else:
            value = (os.environ.get(self.location) or "").strip()
        if not value:
            raise ConfigError(f"operator secret from {self.source} is empty")
        return value


@dataclass(frozen=True, slots=True)
class WikiConfig:
    api_url: str


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    host: str
    port: int
    database: str
    read_default_file: str
    lock_name: str
    connect_timeout_s: int
    read_timeout_s: int
    write_timeout_s: int


@dataclass(frozen=True, slots=True)
class OperatorGate:
    auth_mode: str
    assert_mode: str
    assert_user: str
    contact_user_agent: str
    secret: SecretRef


@dataclass(frozen=True, slots=True)
class SurfaceGate:
    page_id: int
    expected_title: str
    kind: SurfaceKind
    allowed_edit_levels: frozenset[str]


@dataclass(frozen=True, slots=True)
class SelectorPolicyGate:
    allowed_namespaces: tuple[int, ...]
    category_member_types: tuple[str, ...]
    embeddedin_redirect_filter: str
    direct_page_redirect_policy: str

    def category_params(self) -> dict[str, str]:
        return {"cmnamespace": "|".join(str(n) for n in self.allowed_namespaces),
                "cmtype": "|".join(self.category_member_types)}

    def embeddedin_params(self) -> dict[str, str]:
        return {"einamespace": "|".join(str(n) for n in self.allowed_namespaces),
                "eifilterredir": self.embeddedin_redirect_filter}


@dataclass(frozen=True, slots=True)
class Adapter:
    template_names: frozenset[str]
    field_map: Mapping[str, str]
    required_single: frozenset[str]
    required_fanout: frozenset[str]
    allowed_actions_single: frozenset[Action]
    allowed_actions_fanout: frozenset[Action]
    permalink_prefixes: tuple[str, ...]

    def required_fields(self, surface_kind: SurfaceKind) -> frozenset[str]:
        return self.required_single if surface_kind is SurfaceKind.SINGLE else self.required_fanout

    def require_action(self, surface_kind: SurfaceKind, action: Action) -> None:
        allowed = (self.allowed_actions_single if surface_kind is SurfaceKind.SINGLE
                   else self.allowed_actions_fanout)
        if action not in allowed:
            raise InvalidRevision(None, f"action {action} not allowed on {surface_kind} surface")

    def require_permalink(self, needed: bool, discussion: str | None) -> None:
        if not needed:
            if discussion is not None:
                raise InvalidRevision(None, "discussion permalink not allowed for this action")
            return
        if discussion is None:
            raise InvalidRevision(None, "missing discussion permalink")
        if not any(discussion.startswith(prefix) for prefix in self.permalink_prefixes):
            raise InvalidRevision(None, "discussion permalink outside ratified prefixes")


@dataclass(frozen=True, slots=True)
class RequestSyntaxGate:
    adapters: tuple[Adapter, ...]
    permalink_prefixes: tuple[str, ...]

    def adapter_for(self, template_name: str) -> Adapter:
        for adapter in self.adapters:
            if template_name in adapter.template_names:
                return adapter
        raise InvalidRevision(None, f"unknown template: {template_name}")


@dataclass(frozen=True, slots=True)
class ReportingGate:
    mode: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    wiki: WikiConfig; database: DatabaseConfig; operator: OperatorGate
    control_surfaces: tuple[SurfaceGate, ...]
    schedule_catalog: Mapping[str, ScheduleSpec]
    selector_policy: SelectorPolicyGate
    request_syntax: RequestSyntaxGate
    reporting: ReportingGate
    safety: SafetyGate

@dataclass(frozen=True, slots=True)
class SafetyGate:
    fanout_limit:int; open_fanout_targets:int
    target_attempts_24h:int; force_attempts_24h:int; effect_posts_24h:int
    normal_batch:int; force_batch:int; post_start_interval_s:int; maxlag:int
    attempts_per_target_24h:int; singleton_replays:int; retry_delays_s:tuple[int,...]
    retry_window_s:int; worker_runtime_s:int; platform_timeout_s:int; tick_cron:str
    http_connect_timeout_s: float
    http_read_timeout_s: float


class Reader:
    """read and validate toml table"""

    def __init__(self, table: object, path: str):
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: expected a table")
        self.path = path
        self.remaining = dict(table)

    def _take(self, key: str) -> object:
        if key not in self.remaining:
            raise ConfigError(f"{self.path}.{key}: missing")
        return self.remaining.pop(key)

    def _typed(self, key: str, kind: type, name: str) -> object:
        value = self._take(key)
        if not isinstance(value, kind) or isinstance(value, bool) is not (kind is bool):
            raise ConfigError(f"{self.path}.{key}: expected {name}")
        return value

    def text(self, key: str) -> str:
        value = self._typed(key, str, "a string")
        if not value.strip():
            raise ConfigError(f"{self.path}.{key}: empty")
        return value

    def integer(self, key: str, minimum: int) -> int:
        value = self._typed(key, int, "an integer")
        if value < minimum:
            raise ConfigError(f"{self.path}.{key}: must be >= {minimum}")
        return value

    def number(self, key: str, minimum: float) -> float:
        value = self._take(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{self.path}.{key}: expected a number")
        if value < minimum:
            raise ConfigError(f"{self.path}.{key}: must be >= {minimum}")
        return float(value)

    def text_list(self, key: str) -> tuple[str, ...]:
        value = self._typed(key, list, "a list of strings")
        if not value or not all(isinstance(x, str) and x.strip() for x in value):
            raise ConfigError(f"{self.path}.{key}: expected a non-empty list of strings")
        return tuple(value)

    def integer_list(self, key: str, minimum: int) -> tuple[int, ...]:
        value = self._typed(key, list, "a list of integers")
        if not value or not all(isinstance(x, int) and not isinstance(x, bool) for x in value):
            raise ConfigError(f"{self.path}.{key}: expected a non-empty list of integers")
        if any(x < minimum for x in value):
            raise ConfigError(f"{self.path}.{key}: every entry must be >= {minimum}")
        return tuple(value)

    def table(self, key: str) -> "Reader":
        return Reader(self._take(key), f"{self.path}.{key}")

    def tables(self, key: str) -> tuple["Reader", ...]:
        value = self._typed(key, list, "an array of tables")
        if not value:
            raise ConfigError(f"{self.path}.{key}: must not be empty")
        return tuple(Reader(item, f"{self.path}.{key}[{i}]") for i, item in enumerate(value))

    def string_map(self, key: str) -> Mapping[str, str]:
        value = self._typed(key, dict, "a table of strings")
        if not value or not all(isinstance(v, str) and v.strip() for v in value.values()):
            raise ConfigError(f"{self.path}.{key}: expected a non-empty table of strings")
        return dict(value)

    def done(self) -> None:
        if self.remaining:
            raise ConfigError(f"{self.path}: unknown key(s) {sorted(self.remaining)}")


def _reject_placeholders(value: object, path: str) -> None:
    if isinstance(value, str):
        if _PLACEHOLDER.match(value.strip()):
            raise ConfigError(f"{path}: unratified placeholder {value!r}")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_placeholders(key, path)
            _reject_placeholders(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_placeholders(item, f"{path}[{index}]")


def _utc_datetime(text: str, path: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"{path}: not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ConfigError(f"{path}: must carry an explicit UTC offset")
    return parsed.astimezone(UTC)


def _schedule(reader: Reader) -> ScheduleSpec:
    kind = reader.text("kind")
    if kind not in SCHEDULE_KINDS:
        raise ConfigError(f"{reader.path}.kind: must be one of {sorted(SCHEDULE_KINDS)}")
    if kind == "once":
        reader.done()
        return ScheduleSpec(kind="once")
    if kind == "interval":
        anchor = _utc_datetime(reader.text("anchor_utc"), f"{reader.path}.anchor_utc")
        interval = reader.integer("interval_s", 1)
        reader.done()
        return ScheduleSpec(kind="interval", anchor_utc=anchor, interval_s=interval)
    month = reader.integer("month", 1)
    day = reader.integer("day", 1)
    hour = reader.integer("hour", 0)
    minute = reader.integer("minute", 0)
    reader.done()
    if month > 12 or day > 31 or hour > 23 or minute > 59:
        raise ConfigError(f"{reader.path}: annual slot is not a calendar time")
    return ScheduleSpec(kind="annual", month=month, day=day, hour=hour, minute=minute)


def _actions(names: tuple[str, ...], path: str, allowed: frozenset[Action]) -> frozenset[Action]:
    out = set()
    for name in names:
        try:
            action = Action(name)
        except ValueError as exc:
            raise ConfigError(f"{path}: unknown action {name!r}") from exc
        if action not in allowed:
            raise ConfigError(f"{path}: action {name!r} is not valid for this surface kind")
        out.add(action)
    return frozenset(out)


def _adapter(reader: Reader, permalink_prefixes: tuple[str, ...]) -> Adapter:
    names = frozenset(n.strip().replace("_", " ") for n in reader.text_list("template_names"))
    field_map = reader.string_map("field_map")
    unknown = set(field_map.values()) - REQUEST_FIELDS
    if unknown:
        raise ConfigError(f"{reader.path}.field_map: unknown target field(s) {sorted(unknown)}")
    if len(set(field_map.values())) != len(field_map):
        raise ConfigError(f"{reader.path}.field_map: two parameters map to the same field")
    required_single = frozenset(reader.text_list("required_fields_single"))
    required_fanout = frozenset(reader.text_list("required_fields_fanout"))
    single = _actions(reader.text_list("allowed_actions_single"),
                      f"{reader.path}.allowed_actions_single", SINGLE_ACTIONS)
    fanout = _actions(reader.text_list("allowed_actions_fanout"),
                      f"{reader.path}.allowed_actions_fanout", FANOUT_ACTIONS)
    reader.done()
    for label, required in (("single", required_single), ("fanout", required_fanout)):
        if not required <= set(field_map.values()):
            raise ConfigError(f"{reader.path}.required_fields_{label}: not covered by field_map")
        if not MANDATORY_REQUEST_FIELDS <= required:
            raise ConfigError(f"{reader.path}.required_fields_{label}: "
                              f"must contain {sorted(MANDATORY_REQUEST_FIELDS)}")
    if "discussion" not in required_fanout:
        raise ConfigError(f"{reader.path}.required_fields_fanout: mass jobs need a permalink")
    if "discussion" in required_single:
        raise ConfigError(f"{reader.path}.required_fields_single: no permalink field here")
    return Adapter(names, field_map, required_single, required_fanout,
                   single, fanout, permalink_prefixes)


def _safety(reader: Reader) -> SafetyGate:
    gate = SafetyGate(
        fanout_limit=reader.integer("fanout_limit", 1),
        open_fanout_targets=reader.integer("open_fanout_targets", 1),
        target_attempts_24h=reader.integer("target_attempts_24h", 1),
        force_attempts_24h=reader.integer("force_attempts_24h", 1),
        effect_posts_24h=reader.integer("effect_posts_24h", 1),
        normal_batch=reader.integer("normal_batch", 1),
        force_batch=reader.integer("force_batch", 1),
        post_start_interval_s=reader.integer("post_start_interval_s", 0),
        maxlag=reader.integer("maxlag", 1),
        attempts_per_target_24h=reader.integer("attempts_per_target_24h", 1),
        singleton_replays=reader.integer("singleton_replays", 0),
        retry_delays_s=reader.integer_list("retry_delays_s", 1),
        retry_window_s=reader.integer("retry_window_s", 1),
        worker_runtime_s=reader.integer("worker_runtime_s", 1),
        platform_timeout_s=reader.integer("platform_timeout_s", 1),
        tick_cron=reader.text("tick_cron"),
        http_connect_timeout_s=reader.number("http_connect_timeout_s", 0.001),
        http_read_timeout_s=reader.number("http_read_timeout_s", 0.001),
    )
    reader.done()
    delays = gate.retry_delays_s
    if len(delays) != 5 or any(b <= a for a, b in zip(delays, delays[1:])):
        raise ConfigError("safety.retry_delays_s: expected five strictly increasing delays")
    if gate.attempts_per_target_24h != len(delays) + 1:
        raise ConfigError("safety.attempts_per_target_24h: must equal len(retry_delays_s)+1")
    if not 1 <= gate.force_batch <= gate.normal_batch <= MEDIAWIKI_MAX_BATCH:
        raise ConfigError("safety: expected 1 <= force_batch <= normal_batch <= 50")
    if gate.force_attempts_24h > gate.target_attempts_24h:
        raise ConfigError("safety.force_attempts_24h: must not exceed target_attempts_24h")
    if gate.worker_runtime_s >= gate.platform_timeout_s:
        raise ConfigError("safety.worker_runtime_s: must be shorter than platform_timeout_s")
    if gate.http_connect_timeout_s + gate.http_read_timeout_s >= gate.worker_runtime_s:
        raise ConfigError("safety: one HTTP call must fit inside worker_runtime_s")
    return gate


def _surfaces(readers: tuple[Reader, ...]) -> tuple[SurfaceGate, ...]:
    surfaces = []
    for reader in readers:
        kind = reader.text("kind")
        if kind not in tuple(SurfaceKind):
            raise ConfigError(f"{reader.path}.kind: must be 'single' or 'fanout'")
        gate = SurfaceGate(
            page_id=reader.integer("page_id", 1),
            expected_title=reader.text("expected_title"),
            kind=SurfaceKind(kind),
            allowed_edit_levels=frozenset(reader.text_list("allowed_edit_levels")),
        )
        reader.done()
        surfaces.append(gate)
    kinds = [s.kind for s in surfaces]
    if sorted(kinds) != sorted(SurfaceKind):
        raise ConfigError("control_surfaces: expected exactly one 'single' and one 'fanout' surface")
    if len({s.page_id for s in surfaces}) != len(surfaces):
        raise ConfigError("control_surfaces: page_id must be distinct")
    if len({s.allowed_edit_levels for s in surfaces}) != len(surfaces):
        raise ConfigError("control_surfaces: the two surfaces must be protected differently")
    return tuple(surfaces)


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"config file unreadable: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file is not valid TOML: {exc}") from exc
    _reject_placeholders(document, "config")

    root = Reader(document, "config")
    wiki_reader = root.table("wiki")
    wiki = WikiConfig(api_url=wiki_reader.text("api_url"))
    wiki_reader.done()
    if not wiki.api_url.startswith("https://"):
        raise ConfigError("wiki.api_url: must be https")

    db_reader = root.table("database")
    database = DatabaseConfig(
        host=db_reader.text("host"),
        port=db_reader.integer("port", 1),
        database=db_reader.text("database"),
        read_default_file=db_reader.text("read_default_file"),
        lock_name=db_reader.text("lock_name"),
        connect_timeout_s=db_reader.integer("connect_timeout_s", 1),
        read_timeout_s=db_reader.integer("read_timeout_s", 1),
        write_timeout_s=db_reader.integer("write_timeout_s", 1),
    )
    db_reader.done()

    operator_reader = root.table("operator")
    secret_reader = operator_reader.table("secret")
    source = secret_reader.text("source")
    if source not in ("file", "env"):
        raise ConfigError("operator.secret.source: must be 'file' or 'env'")
    secret = SecretRef(source=source, location=secret_reader.text("location"))
    secret_reader.done()
    operator = OperatorGate(
        auth_mode=operator_reader.text("auth_mode"),
        assert_mode=operator_reader.text("assert_mode"),
        assert_user=operator_reader.text("assert_user"),
        contact_user_agent=operator_reader.text("contact_user_agent"),
        secret=secret,
    )
    operator_reader.done()
    if operator.assert_mode not in ASSERT_MODES:
        raise ConfigError(
            f"operator.assert_mode: must be one of {sorted(ASSERT_MODES)}")
    if operator.auth_mode not in RATIFIED_AUTH_MODES:
        raise ConfigError(
            f"operator.auth_mode: {operator.auth_mode!r} has no implementation; the "
            "authentication mechanism is an unratified operator gate")
    secret.resolve()

    surfaces = _surfaces(root.tables("control_surfaces"))

    catalog_reader = root.table("schedule_catalog")
    catalog = {key: _schedule(catalog_reader.table(key))
               for key in sorted(catalog_reader.remaining)}
    catalog_reader.done()
    if not catalog:
        raise ConfigError("schedule_catalog: must contain at least one ratified schedule")

    policy_reader = root.table("selector_policy")
    policy = SelectorPolicyGate(
        allowed_namespaces=policy_reader.integer_list("allowed_namespaces", 0),
        category_member_types=policy_reader.text_list("category_member_types"),
        embeddedin_redirect_filter=policy_reader.text("embeddedin_redirect_filter"),
        direct_page_redirect_policy=policy_reader.text("direct_page_redirect_policy"),
    )
    policy_reader.done()
    if not set(policy.category_member_types) <= CATEGORY_MEMBER_TYPES:
        raise ConfigError(f"selector_policy.category_member_types: "
                          f"must be a subset of {sorted(CATEGORY_MEMBER_TYPES)}")
    if policy.embeddedin_redirect_filter not in REDIRECT_FILTERS:
        raise ConfigError(f"selector_policy.embeddedin_redirect_filter: "
                          f"must be one of {sorted(REDIRECT_FILTERS)}")
    if policy.direct_page_redirect_policy not in DIRECT_REDIRECT_POLICIES:
        raise ConfigError(f"selector_policy.direct_page_redirect_policy: "
                          f"must be one of {sorted(DIRECT_REDIRECT_POLICIES)}")

    syntax_reader = root.table("request_syntax")
    prefixes = syntax_reader.text_list("permalink_url_prefixes")
    if not all(p.startswith("https://") for p in prefixes):
        raise ConfigError("request_syntax.permalink_url_prefixes: every prefix must be https")
    adapters = tuple(_adapter(r, prefixes) for r in syntax_reader.tables("adapters"))
    syntax_reader.done()
    claimed: set[str] = set()
    for adapter in adapters:
        if claimed & adapter.template_names:
            raise ConfigError("request_syntax.adapters: template names must be disjoint")
        claimed |= adapter.template_names
    request_syntax = RequestSyntaxGate(adapters=adapters, permalink_prefixes=prefixes)

    reporting_reader = root.table("reporting")
    reporting = ReportingGate(mode=reporting_reader.text("mode"))
    reporting_reader.done()
    if reporting.mode != "log-only":
        raise ConfigError("reporting.mode: only 'log-only' is ratified; on-wiki status "
                          "edits need a BRFA gate and a publisher")

    safety = _safety(root.table("safety"))
    root.done()

    return AppConfig(wiki=wiki, database=database, operator=operator,
                     control_surfaces=surfaces, schedule_catalog=catalog,
                     selector_policy=policy, request_syntax=request_syntax,
                     reporting=reporting, safety=safety)
