"""The sole supported composition for protected provider effects.

Provider adapters intentionally know nothing about authority or leases.  This
module joins the closed registry operation to the durable provider-effect
ledger, so an adapter is never reached until the ledger grants exactly one
live dispatch slot.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .autonomy import AutonomyError, AutonomyStore
from .providers import OperationDescriptor, ProviderError, ProviderRegistry, ProviderResult


class ProviderEffectExecutor:
    """Dispatch registered protected effects through authority and the ledger."""

    def __init__(self, store: AutonomyStore, registry: ProviderRegistry) -> None:
        if not isinstance(store, AutonomyStore) or not isinstance(registry, ProviderRegistry):
            raise TypeError("provider executor requires an AutonomyStore and ProviderRegistry")
        self._store = store
        self._registry = registry

    def execute(self, *, idempotency_key: str,
                operation_descriptor: OperationDescriptor | Mapping[str, Any],
                performer_id: str, lease_token: str,
                at: str | datetime | None = None) -> dict[str, Any]:
        """Claim, dispatch once, and persist a terminal sanitized receipt.

        Authority, descriptor, intent, and lease checks occur in
        ``begin_provider_effect_dispatch`` before the registry is touched.
        Thus a rejected call has no adapter side effect.  Once dispatch has
        begun, any malformed adapter result or transport fault becomes a
        durable indeterminate receipt rather than an implicit retry.
        """
        descriptor = self._descriptor(operation_descriptor)
        effect_attempt = self._store.begin_provider_effect_dispatch(
            idempotency_key=idempotency_key,
            operation_descriptor=descriptor,
            performer_id=performer_id,
            lease_token=lease_token,
            at=at,
        )
        if "id" not in effect_attempt or effect_attempt.get("status") != "executing":
            # The ledger can convert a stale executing claim to indeterminate
            # instead of returning a new attempt.  That is a recovery result,
            # not permission to call the adapter or fabricate a receipt.
            return {
                "effect_attempt": None,
                "outcome": "indeterminate",
                "receipt": None,
                "intent_status": effect_attempt.get("status", "indeterminate"),
            }
        intent = self._store.inspect_effect(idempotency_key)
        if intent is None:
            # Defensively terminalize the already claimed slot.  This should
            # be unreachable with one store, but it must not leave a retryable
            # unknown effect after a persistence anomaly.
            return self._receipt(
                idempotency_key, effect_attempt["id"], performer_id,
                ProviderResult("indeterminate", "provider effect intent was unavailable after dispatch"), at=at,
            )
        request = self._bound_request(intent, performer_id)
        try:
            result = self._registry._execute_protected(
                descriptor, descriptor.resource_scope, request,
                idempotency_key=idempotency_key,
            )
        except Exception:
            # Once the durable dispatch slot has been claimed, no thrown
            # adapter exception can prove the remote effect did not happen.
            # This includes ProviderError raised by adapter code after I/O.
            result = ProviderResult("indeterminate", "provider effect outcome is indeterminate")
        return self._receipt(idempotency_key, effect_attempt["id"], performer_id, result, at=at)

    def reconcile(self, *, idempotency_key: str,
                  operation_descriptor: OperationDescriptor | Mapping[str, Any],
                  performer_id: str) -> dict[str, Any]:
        """Run an adapter's read-only exact reconciliation for one intent.

        No caller supplies a resolution.  Only a closed adapter result can
        authorize absence, and the ledger marks that provenance separately
        from manual observations before any retry can become eligible.
        """
        descriptor = self._descriptor(operation_descriptor)
        intent = self._store.inspect_effect(idempotency_key)
        if intent is None:
            raise AutonomyError("unknown provider effect intent")
        if self._intent_descriptor(intent) != descriptor:
            raise AutonomyError("provider reconciliation descriptor differs from the prepared intent")
        if intent.get("status") != "indeterminate":
            raise AutonomyError("provider reconciliation requires an indeterminate effect")
        request = self._bound_request(intent, performer_id)
        try:
            result = self._registry._reconcile_protected(
                descriptor, descriptor.resource_scope, request, idempotency_key=idempotency_key,
            )
        except ProviderError:
            result = ProviderResult("indeterminate", "provider reconciliation outcome is indeterminate")

        resolution = {
            "reconciled": "applied",
            "absent": "absent",
            "conflict": "conflict",
        }.get(result.state)
        if resolution is not None:
            effect = self._store._record_adapter_provider_reconciliation(
                idempotency_key=idempotency_key, resolution=resolution,
                observation=result.to_dict(), performer_id=performer_id,
            )
            return {"resolution": resolution, "effect": effect, "result": result.to_dict()}
        # A missing, malformed, transport, or ordinary-success result is not
        # evidence of absence.  Preserve it as an indeterminate receipt so it
        # remains visible and must be reconciled explicitly later.
        event = self._store.append_provider_effect_receipt_event(
            idempotency_key=idempotency_key, event_type="observation",
            observation={"reconciliation": result.to_dict()}, performer_id=performer_id,
        )
        return {"resolution": "indeterminate", "observation": event, "result": result.to_dict()}

    @staticmethod
    def _descriptor(value: OperationDescriptor | Mapping[str, Any]) -> OperationDescriptor:
        try:
            descriptor = value if isinstance(value, OperationDescriptor) else OperationDescriptor.from_mapping(value)
        except ProviderError as error:
            # This intentionally happens before a ledger dispatch attempt.
            raise AutonomyError(f"provider operation descriptor is invalid: {error}") from error
        if not descriptor.is_trusted_shape:
            raise AutonomyError("provider operation descriptor must be a closed protocol-v2 descriptor")
        return descriptor

    @staticmethod
    def _bound_request(intent: Mapping[str, Any], performer_id: str) -> dict[str, Any]:
        try:
            bound = json.loads(intent["request_json"])
            if set(bound) != {"authorized_performer_id", "request"}:
                raise ValueError
            if bound["authorized_performer_id"] != performer_id or not isinstance(bound["request"], dict):
                raise ValueError
            return bound["request"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise AutonomyError("provider effect intent lacks a valid bound request") from error

    @staticmethod
    def _intent_descriptor(intent: Mapping[str, Any]) -> OperationDescriptor:
        try:
            scope = json.loads(intent["resource_scope"])
            return OperationDescriptor(
                intent["provider"], intent["effect_class"], capability=intent["capability"],
                action=intent["operation"], resource_scope=scope,
                protocol_version=intent["protocol_version"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ProviderError) as error:
            raise AutonomyError("provider effect intent lacks a valid descriptor identity") from error

    def _receipt(self, idempotency_key: str, effect_attempt_id: str, performer_id: str,
                 result: ProviderResult, *, at: str | datetime | None = None) -> dict[str, Any]:
        outcome = "succeeded" if result.state in {"succeeded", "reconciled"} else (
            "failed" if result.state == "failed" else "indeterminate"
        )
        event = self._store.record_provider_effect_receipt(
            idempotency_key=idempotency_key,
            effect_attempt_id=effect_attempt_id,
            outcome=outcome,
            receipt=result.to_dict(),
            performer_id=performer_id,
            at=at,
        )
        return {"effect_attempt": effect_attempt_id, "outcome": outcome, "receipt": event}
