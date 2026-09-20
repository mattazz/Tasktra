"""Dependency-light, credential-free provider capability contracts."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass, field
import json
import math
import re
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlparse

from .contracts import ContractError, validate_named
from .identifiers import IdentifierError, require_identifier

MAX_PROVIDER_JSON_BYTES = 64 * 1024
MAX_PROVIDER_ITEMS = 64
MAX_PROVIDER_ITEM_BYTES = 8 * 1024
MAX_PROVIDER_SUMMARY_CHARS = 500
READ_ONLY = "read-only"
REMOTE_MUTATION = "remote-mutation"
EFFECTS = frozenset({"read-only", "local-reversible-write", "repository-history", "remote-mutation", "external-communication", "deployment", "merge", "destructive"})
# Provider adapters may make the six remote/consequential effect classes, but
# never infer one: every adapter registration carries an exact descriptor.
PROTECTED_EFFECTS = frozenset({"repository-history", "remote-mutation", "external-communication", "deployment", "merge", "destructive"})
HEALTH_STATES = frozenset({"available", "unavailable", "misconfigured", "degraded"})
# ``absent`` and ``conflict`` are reconciliation facts, never inferred from a
# human-written summary.  They are deliberately separate from an ordinary
# unsuccessful effect receipt so retry policy can remain mechanically safe.
RESULT_STATES = frozenset({"pending", "succeeded", "failed", "indeterminate", "unavailable", "reconciled", "absent", "conflict"})


class ProviderError(ContractError):
    """An untrusted provider payload or invocation is not safe to accept."""


_KEY = re.compile(r"[^a-z0-9]+")
_SENSITIVE = frozenset({"accesskey", "accesstoken", "apikey", "authorization", "bearer", "clientsecret", "cookie", "credential", "credentials", "password", "privatekey", "secret", "setcookie", "token"})
_CREDENTIAL_TEXT = re.compile(
    r"(?i)(?:\bbearer\s+\S+|\b(?:authorization|access[-_ ]?token|api[-_ ]?key|token|password|cookie)\b(?:\s*[:=]\s*|\s+)(?:bearer\s+)?\S+)"
)


def _identifier(value: Any, *, label: str) -> str:
    try:
        return require_identifier(value, label=label)
    except IdentifierError as error:
        raise ProviderError(str(error)) from error


def _sensitive_key(key: str) -> bool:
    name = _KEY.sub("", key.casefold())
    return name in _SENSITIVE or name.endswith("token") or name.endswith("secret")


def _reject_credential_url(value: str, *, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and (
        parsed.username is not None or parsed.password is not None
        or any(_sensitive_key(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
    ):
        raise ProviderError(f"{label} contains a credential-bearing URL")


def _reject_credential_text(value: str, *, label: str) -> None:
    _reject_credential_url(value, label=label)
    if _CREDENTIAL_TEXT.search(value):
        raise ProviderError(f"{label} contains credential text")


def _safe_json(value: Any, *, label: str) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        _reject_credential_text(value, label=label)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProviderError(f"{label} contains a non-finite number")
        return
    if isinstance(value, list) or isinstance(value, tuple):
        for index, item in enumerate(value):
            _safe_json(item, label=f"{label}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProviderError(f"{label} contains a non-string object key")
            if _sensitive_key(key):
                raise ProviderError(f"{label} contains credential-sensitive key {key!r}")
            _safe_json(item, label=f"{label}.{key}")
        return
    raise ProviderError(f"{label} is not JSON-compatible")


def _bounded_json(value: Any, *, label: str, limit: int = MAX_PROVIDER_JSON_BYTES) -> bytes:
    _safe_json(value, label=label)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ProviderError(f"{label} is not JSON-compatible: {error}") from error
    if len(encoded) > limit:
        raise ProviderError(f"{label} exceeds the {limit}-byte limit")
    return encoded


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError(f"duplicate provider JSON key: {key!r}")
        result[key] = value
    return result


def _non_finite(value: str) -> None:
    raise ProviderError(f"non-finite provider JSON value is not permitted: {value}")


def load_bounded_provider_json(payload: str | bytes) -> Any:
    """Load at most 64KiB of untrusted JSON, rejecting duplicates and secrets."""
    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ProviderError("provider text must be valid UTF-8") from error
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise ProviderError("provider payload must be text or UTF-8 bytes")
    if len(raw) > MAX_PROVIDER_JSON_BYTES:
        raise ProviderError(f"provider payload exceeds the {MAX_PROVIDER_JSON_BYTES}-byte limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_keys, parse_constant=_non_finite)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(f"invalid provider JSON: {error}") from error
    _bounded_json(value, label="provider payload")
    return value


@dataclass(frozen=True)
class ResourceScope:
    """Closed canonical provider scope: no paths, credentials, or extras."""

    provider: str
    host: str
    container: str
    resource_kind: str
    resource: str | None = None
    ref: str | None = None

    def __post_init__(self) -> None:
        try:
            validate_named(self.to_dict(), "provider-resource-scope")
        except ContractError as error:
            raise ProviderError(str(error)) from error
        _identifier(self.provider, label="scope provider")
        _identifier(self.resource_kind, label="scope resource_kind")
        if self.host != self.host.casefold() or "/" in self.host or "@" in self.host:
            raise ProviderError("scope host must be a canonical lowercase host name")
        for name in ("host", "container", "resource_kind", "resource", "ref"):
            value = getattr(self, name)
            if value is not None and (value != value.strip() or any(ord(char) < 32 for char in value)):
                raise ProviderError(f"scope {name} must be canonical visible text")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ResourceScope":
        if not isinstance(value, Mapping):
            raise ProviderError("resource scope must be an object")
        try:
            validate_named(dict(value), "provider-resource-scope")
        except ContractError as error:
            raise ProviderError(str(error)) from error
        return cls(**copy.deepcopy(dict(value)))

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "host": self.host, "container": self.container,
                "resource_kind": self.resource_kind, "resource": self.resource, "ref": self.ref}


@dataclass(frozen=True, init=False)
class OperationDescriptor:
    """A closed, provider-issued operation identity.

    The three-argument form remains for direct adapter tests and fixtures only.
    It is deliberately incomplete and therefore cannot be registered or sent to
    the durable effect ledger.  Registry-issued descriptors always include the
    action, effect, protocol, and exact resource scope that the provider fixed.
    """

    provider: str
    capability: str
    action: str | None
    effect_class: str
    resource_scope: ResourceScope | None
    protocol_version: int

    def __init__(self, provider: str, effect_class: str, request_kind: str | None = None, *,
                 capability: str | None = None, action: str | None = None,
                 resource_scope: ResourceScope | Mapping[str, Any] | None = None,
                 protocol_version: int = 2) -> None:
        """Create a full descriptor, or a legacy adapter-only descriptor.

        ``request_kind`` is a compatibility spelling of ``capability``.  A
        caller cannot use that compatibility form to create a trusted registry
        descriptor because registration and ledger preparation require scope
        and action explicitly.
        """
        if capability is not None and request_kind is not None and capability != request_kind:
            raise ProviderError("descriptor capability conflicts with request_kind")
        capability = capability if capability is not None else request_kind
        if not isinstance(capability, str):
            raise ProviderError("descriptor capability must be a string")
        scope = None if resource_scope is None else (
            resource_scope if isinstance(resource_scope, ResourceScope) else ResourceScope.from_mapping(resource_scope)
        )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "effect_class", effect_class)
        object.__setattr__(self, "resource_scope", scope)
        object.__setattr__(self, "protocol_version", protocol_version)
        self.__post_init__()

    def __post_init__(self) -> None:
        _identifier(self.provider, label="descriptor provider")
        _identifier(self.capability, label="descriptor capability")
        if self.effect_class not in EFFECTS:
            raise ProviderError(f"unsupported provider effect class: {self.effect_class!r}")
        if self.resource_scope is None:
            if self.action is not None:
                raise ProviderError("legacy adapter descriptor cannot include an action without resource scope")
            return
        if self.protocol_version != 2:
            raise ProviderError("provider operation descriptor must use protocol version 2")
        _identifier(self.action, label="descriptor action")
        if self.resource_scope.provider != self.provider:
            raise ProviderError("descriptor provider must match its resource scope")
        try:
            validate_named(self.to_dict(), "provider-operation-descriptor")
        except ContractError as error:
            raise ProviderError(str(error)) from error

    @property
    def request_kind(self) -> str:
        """Compatibility name consumed by existing provider adapters."""
        return self.capability

    @property
    def is_trusted_shape(self) -> bool:
        return self.action is not None and self.resource_scope is not None and self.protocol_version == 2

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OperationDescriptor":
        if not isinstance(value, Mapping):
            raise ProviderError("operation descriptor must be an object")
        copied = copy.deepcopy(dict(value))
        try:
            validate_named(copied, "provider-operation-descriptor")
        except ContractError as error:
            raise ProviderError(str(error)) from error
        return cls(
            copied["provider"], copied["effect_class"], capability=copied["capability"],
            action=copied["action"], resource_scope=copied["resource_scope"],
            protocol_version=copied["protocol_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        if not self.is_trusted_shape:
            return {"provider": self.provider, "effect_class": self.effect_class, "request_kind": self.capability}
        assert self.resource_scope is not None
        return {
            "provider": self.provider, "capability": self.capability, "action": self.action,
            "effect_class": self.effect_class, "resource_scope": self.resource_scope.to_dict(),
            "protocol_version": self.protocol_version,
        }


def _summary(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ProviderError(f"{label} must be a string")
    result = " ".join(value.split())
    if not result or len(result) > MAX_PROVIDER_SUMMARY_CHARS:
        raise ProviderError(f"{label} must contain 1 to {MAX_PROVIDER_SUMMARY_CHARS} visible characters")
    _reject_credential_text(result, label=label)
    return result


@dataclass(frozen=True)
class ProviderResult:
    """A bounded typed and sanitized read result or effect receipt."""

    state: str
    summary: str
    items: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in RESULT_STATES:
            raise ProviderError(f"unsupported provider result state: {self.state!r}")
        object.__setattr__(self, "summary", _summary(self.summary, label="provider result summary"))
        if not isinstance(self.items, tuple) or len(self.items) > MAX_PROVIDER_ITEMS:
            raise ProviderError(f"provider result items must contain at most {MAX_PROVIDER_ITEMS} entries")
        for index, item in enumerate(self.items):
            _bounded_json(item, label=f"provider result item {index}", limit=MAX_PROVIDER_ITEM_BYTES)
        _bounded_json(self.to_dict(), label="provider result")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProviderResult":
        if not isinstance(value, Mapping):
            raise ProviderError("provider result must be an object")
        copied = copy.deepcopy(dict(value))
        try:
            validate_named(copied, "provider-result")
        except ContractError as error:
            raise ProviderError(str(error)) from error
        return cls(copied["state"], copied["summary"], tuple(copied["items"]))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "tasktra.provider-result", "version": 1, "state": self.state, "summary": self.summary, "items": copy.deepcopy(list(self.items))}


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    state: str
    summary: str

    def __post_init__(self) -> None:
        _identifier(self.provider, label="health provider")
        if self.state not in HEALTH_STATES:
            raise ProviderError(f"unsupported provider health state: {self.state!r}")
        object.__setattr__(self, "summary", _summary(self.summary, label="provider health summary"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProviderHealth":
        if not isinstance(value, Mapping) or set(value) != {"provider", "state", "summary"}:
            raise ProviderError("provider health must contain only provider, state, and summary")
        return cls(value["provider"], value["state"], value["summary"])


class DiscoveryAdapter(Protocol):
    def discover(self) -> ProviderHealth | Mapping[str, Any]: ...


class ReadAdapter(Protocol):
    def read(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any]) -> ProviderResult | Mapping[str, Any]: ...


class EffectAdapter(Protocol):
    def execute(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any], idempotency_key: str) -> ProviderResult | Mapping[str, Any]: ...


class ReconciliationAdapter(Protocol):
    def reconcile(self, *, descriptor: OperationDescriptor, scope: ResourceScope,
                  request: Mapping[str, Any], idempotency_key: str) -> ProviderResult | Mapping[str, Any]: ...


@dataclass
class FakeProvider:
    """A replaceable, deterministic adapter for offline tests and fixtures."""

    health: ProviderHealth
    read_result: ProviderResult | Mapping[str, Any] = field(default_factory=lambda: ProviderResult("succeeded", "fake read completed"))
    effect_result: ProviderResult | Mapping[str, Any] = field(default_factory=lambda: ProviderResult("succeeded", "fake effect completed"))
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def discover(self) -> ProviderHealth:
        return self.health

    def read(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any]) -> ProviderResult | Mapping[str, Any]:
        self.calls.append(("read", {"descriptor": descriptor.to_dict(), "scope": scope.to_dict(), "request": copy.deepcopy(dict(request))}))
        return self.read_result

    def execute(self, descriptor: OperationDescriptor, scope: ResourceScope, request: Mapping[str, Any], idempotency_key: str) -> ProviderResult | Mapping[str, Any]:
        self.calls.append(("effect", {"descriptor": descriptor.to_dict(), "scope": scope.to_dict(), "request": copy.deepcopy(dict(request)), "idempotency_key": idempotency_key}))
        return self.effect_result

    def reconcile(self, *, descriptor: OperationDescriptor, scope: ResourceScope,
                  request: Mapping[str, Any], idempotency_key: str) -> ProviderResult | Mapping[str, Any]:
        self.calls.append(("reconcile", {"descriptor": descriptor.to_dict(), "scope": scope.to_dict(), "request": copy.deepcopy(dict(request)), "idempotency_key": idempotency_key}))
        return self.effect_result


class ProviderRegistry:
    """Separate discovery, read, and protected-effect adapter tables."""

    def __init__(self) -> None:
        self._discoveries: dict[str, DiscoveryAdapter] = {}
        self._reads: dict[OperationDescriptor, ReadAdapter] = {}
        self._effects: dict[OperationDescriptor, EffectAdapter] = {}

    def register_provider(self, provider: str, *, discovery: DiscoveryAdapter,
                          operations: Mapping[OperationDescriptor, ReadAdapter | EffectAdapter]) -> tuple[OperationDescriptor, ...]:
        """Bind each trusted descriptor to one adapter without inferring effects.

        Registration is intentionally all-or-nothing: a malformed later entry
        must not leave an earlier operation usable under a provider that was
        reported as unconfigured.
        """
        provider = _identifier(provider, label="provider")
        if provider in self._discoveries:
            raise ProviderError(f"provider is already registered: {provider}")
        if not callable(getattr(discovery, "discover", None)):
            raise ProviderError("provider discovery adapter must define discover()")
        if not isinstance(operations, Mapping):
            raise ProviderError("provider operations must be a descriptor mapping")
        descriptors: list[OperationDescriptor] = []
        pending_reads: dict[OperationDescriptor, ReadAdapter] = {}
        pending_effects: dict[OperationDescriptor, EffectAdapter] = {}
        for descriptor, adapter in operations.items():
            if (not isinstance(descriptor, OperationDescriptor) or descriptor.provider != provider
                    or not descriptor.is_trusted_shape):
                raise ProviderError("provider operation must use an explicit descriptor for this provider")
            if descriptor.effect_class == READ_ONLY:
                target, pending, method = self._reads, pending_reads, "read"
            elif descriptor.effect_class in PROTECTED_EFFECTS:
                target, pending, method = self._effects, pending_effects, "execute"
            else:
                raise ProviderError("local-reversible-write is not a provider operation")
            if not callable(getattr(adapter, method, None)):
                raise ProviderError(f"{descriptor.effect_class} adapter must define {method}()")
            if descriptor in target or descriptor in pending:
                raise ProviderError(f"conflicting provider operation: {descriptor.to_dict()}")
            # Copy through the closed representation so registration, not a
            # mutable caller-owned mapping, becomes the source of truth.
            issued = OperationDescriptor.from_mapping(descriptor.to_dict())
            pending[issued] = adapter
            descriptors.append(issued)
        # Commit only once every descriptor has been parsed, copied, and
        # validated.  Empty operation maps are useful for a credential-free
        # health-only provider registration.
        self._reads.update(pending_reads)
        self._effects.update(pending_effects)
        self._discoveries[provider] = discovery
        return tuple(sorted(descriptors, key=lambda item: json.dumps(item.to_dict(), sort_keys=True, separators=(",", ":"))))

    register = register_provider

    def health_report(self) -> dict[str, Any]:
        providers: list[dict[str, Any]] = []
        for name in sorted(self._discoveries):
            try:
                raw = self._discoveries[name].discover()
                health = raw if isinstance(raw, ProviderHealth) else ProviderHealth.from_mapping(raw)
                if health.provider != name:
                    raise ProviderError("discovery result names a different provider")
            except Exception:
                health = ProviderHealth(name, "unavailable", "provider discovery is unavailable")
            operations = [
                {"effect_class": item.effect_class, "request_kind": item.request_kind}
                for item in sorted(
                    (item for item in (*self._reads, *self._effects) if item.provider == name),
                    key=lambda item: (item.effect_class, item.request_kind, item.action or ""),
                )
            ]
            providers.append({"provider": name, "state": health.state, "summary": health.summary, "operations": operations})
        report = {"kind": "tasktra.provider-health-report", "version": 1, "providers": providers}
        try:
            validate_named(report, "provider-health-report")
        except ContractError as error:
            raise ProviderError(str(error)) from error
        _bounded_json(report, label="provider health report")
        return copy.deepcopy(report)

    def read(self, descriptor: OperationDescriptor | Mapping[str, Any], scope: ResourceScope | Mapping[str, Any], request: Mapping[str, Any]) -> ProviderResult:
        operation = self._trusted(descriptor, READ_ONLY, self._reads)
        resource_scope, safe_request = self._scope(operation, scope), self._request(request)
        try:
            return self._result(self._reads[operation].read(operation, resource_scope, safe_request))
        except ProviderError:
            raise
        except Exception:
            return ProviderResult("unavailable", "provider read is unavailable")

    def _execute_protected(self, descriptor: OperationDescriptor | Mapping[str, Any], scope: ResourceScope | Mapping[str, Any], request: Mapping[str, Any], *, idempotency_key: str) -> ProviderResult:
        """Internal half of a protected effect dispatch.

        This is deliberately not public API.  ``ProviderEffectExecutor`` is
        the sole supported caller and obtains its dispatch slot from the
        durable autonomy ledger before reaching this method.  Reads remain
        directly callable via :meth:`read` because they cannot make effects.
        """
        operation = self._trusted_effect(descriptor)
        resource_scope, safe_request = self._scope(operation, scope), self._request(request)
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        try:
            return self._result(self._effects[operation].execute(operation, resource_scope, safe_request, idempotency_key))
        except ProviderError:
            raise
        except Exception:
            return ProviderResult("indeterminate", "provider effect outcome is indeterminate")

    def _reconcile_protected(self, descriptor: OperationDescriptor | Mapping[str, Any], scope: ResourceScope | Mapping[str, Any], request: Mapping[str, Any], *, idempotency_key: str) -> ProviderResult:
        """Internal read-only reconciliation for an already-dispatched effect."""
        operation = self._trusted_effect(descriptor)
        resource_scope, safe_request = self._scope(operation, scope), self._request(request)
        adapter = self._effects[operation]
        reconcile = getattr(adapter, "reconcile", None)
        if not callable(reconcile):
            return ProviderResult("indeterminate", "provider does not support read-only reconciliation")
        idempotency_key = _identifier(idempotency_key, label="idempotency_key")
        try:
            return self._result(reconcile(descriptor=operation, scope=resource_scope, request=safe_request,
                                          idempotency_key=idempotency_key))
        except ProviderError:
            raise
        except Exception:
            return ProviderResult("indeterminate", "provider reconciliation outcome is indeterminate")

    @staticmethod
    def _result(value: ProviderResult | Mapping[str, Any]) -> ProviderResult:
        return value if isinstance(value, ProviderResult) else ProviderResult.from_mapping(value)

    @staticmethod
    def _request(value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ProviderError("provider request must be an object")
        copied = copy.deepcopy(dict(value))
        _bounded_json(copied, label="provider request")
        return copied

    @staticmethod
    def _scope(descriptor: OperationDescriptor, value: ResourceScope | Mapping[str, Any]) -> ResourceScope:
        scope = value if isinstance(value, ResourceScope) else ResourceScope.from_mapping(value)
        if descriptor.resource_scope is None:
            raise ProviderError("registry operation descriptor lacks its exact resource scope")
        if scope != descriptor.resource_scope:
            raise ProviderError("resource scope differs from the registered operation descriptor")
        return descriptor.resource_scope

    @staticmethod
    def _trusted(value: OperationDescriptor | Mapping[str, Any], expected: str, registered: Mapping[OperationDescriptor, Any]) -> OperationDescriptor:
        descriptor = value if isinstance(value, OperationDescriptor) else OperationDescriptor.from_mapping(value)
        if not descriptor.is_trusted_shape:
            raise ProviderError("operation descriptor is not a closed protocol-v2 descriptor")
        if descriptor.effect_class != expected:
            raise ProviderError(f"{expected} API cannot invoke {descriptor.effect_class} operations")
        if descriptor not in registered:
            raise ProviderError("operation descriptor is not registered and trusted")
        return descriptor

    def _trusted_effect(self, value: OperationDescriptor | Mapping[str, Any]) -> OperationDescriptor:
        descriptor = value if isinstance(value, OperationDescriptor) else OperationDescriptor.from_mapping(value)
        if not descriptor.is_trusted_shape:
            raise ProviderError("operation descriptor is not a closed protocol-v2 descriptor")
        if descriptor.effect_class not in PROTECTED_EFFECTS:
            raise ProviderError(f"protected effect API cannot invoke {descriptor.effect_class} operations")
        if descriptor not in self._effects:
            raise ProviderError("operation descriptor is not registered and trusted")
        return descriptor


def load_provider_result(payload: str | bytes) -> ProviderResult:
    return ProviderResult.from_mapping(load_bounded_provider_json(payload))
