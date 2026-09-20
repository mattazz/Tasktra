import copy
import unittest

from tasktra.authority import (
    AuthorityError,
    authority_envelope_sha256,
    load_authority_envelope,
    resource_scope_within,
    serialize_authority_envelope,
    serialize_transition_approval,
    transition_approval_sha256,
    validate_authority_envelope,
    validate_transition_approval,
)
from tests.test_authority import approval, envelope


RESOURCE_SCOPE = {
    "provider": "github",
    "host": "github.com",
    "container": "tasktra",
    "resource_kind": "repository",
    "resource": "tasktra",
    "ref": "refs/heads/main",
}


def envelope_v2():
    value = copy.deepcopy(envelope())
    value["version"] = 2
    value["allowed_effects"] = ["read-only", "remote-mutation"]
    value["resource_scopes"] = [copy.deepcopy(RESOURCE_SCOPE)]
    return value


def approval_v2():
    value = copy.deepcopy(approval())
    value["version"] = 2
    value["effect"] = "remote-mutation"
    value["resource_scope"] = copy.deepcopy(RESOURCE_SCOPE)
    value["envelope_sha256"] = authority_envelope_sha256(envelope_v2())
    return value


class AuthorityV2Tests(unittest.TestCase):
    def test_v1_bytes_and_hashes_are_unchanged(self):
        self.assertEqual(
            authority_envelope_sha256(envelope()),
            "58e8199f8b1ba0824fe5bd521a1343faddbc37f361bd893085e2b9dead45a412",
        )
        self.assertEqual(
            transition_approval_sha256(approval()),
            "98abb6121f7fef0a3ae7e4a1e588c850dab74a7a97748ef5b014436d5fcbb872",
        )

    def test_v2_round_trip_and_hash_are_stable(self):
        value = envelope_v2()
        payload = serialize_authority_envelope(value)
        self.assertEqual(payload, serialize_authority_envelope(load_authority_envelope(payload)))
        self.assertEqual(
            authority_envelope_sha256(value),
            authority_envelope_sha256(load_authority_envelope(payload)),
        )
        transition = approval_v2()
        transition_payload = serialize_transition_approval(transition)
        self.assertEqual(
            transition_payload,
            serialize_transition_approval(validate_transition_approval(transition)),
        )
        self.assertEqual(
            transition_approval_sha256(transition),
            transition_approval_sha256(validate_transition_approval(transition)),
        )

    def test_resource_scope_containment_is_exact_except_parent_wildcards(self):
        child = copy.deepcopy(RESOURCE_SCOPE)
        parent = copy.deepcopy(RESOURCE_SCOPE)
        parent["resource"] = None
        parent["ref"] = None
        self.assertTrue(resource_scope_within(child, parent))
        parent["container"] = "another-container"
        self.assertFalse(resource_scope_within(child, parent))
        parent = copy.deepcopy(RESOURCE_SCOPE)
        child["resource"] = None
        self.assertFalse(resource_scope_within(child, parent))

    def test_remote_and_local_effect_scope_rules_are_fail_closed(self):
        transition = approval_v2()
        transition["resource_scope"] = None
        with self.assertRaisesRegex(AuthorityError, "Consequential effects require"):
            validate_transition_approval(transition)
        transition = approval_v2()
        transition["effect"] = "read-only"
        with self.assertRaisesRegex(AuthorityError, "require null resource_scope"):
            validate_transition_approval(transition)
        authority = envelope_v2()
        authority["resource_scopes"] = []
        with self.assertRaisesRegex(AuthorityError, "Consequential allowed_effects require"):
            validate_authority_envelope(authority)
        authority = envelope_v2()
        authority["allowed_effects"] = ["read-only"]
        with self.assertRaisesRegex(AuthorityError, "resource_scopes require"):
            validate_authority_envelope(authority)

    def test_credential_bearing_and_unsafe_resource_values_are_rejected(self):
        authority = envelope_v2()
        authority["resource_scopes"][0]["host"] = "https://user:token@github.com"
        with self.assertRaisesRegex(AuthorityError, "userinfo"):
            validate_authority_envelope(authority)
        transition = approval_v2()
        transition["resource_scope"]["resource"] = "tasktra?access_token=secret"
        with self.assertRaisesRegex(AuthorityError, "userinfo or query"):
            validate_transition_approval(transition)
        transition = approval_v2()
        transition["resource_scope"]["ref"] = "token: secret"
        with self.assertRaisesRegex(AuthorityError, "control characters or whitespace"):
            validate_transition_approval(transition)

    def test_v2_requires_closed_scope_shape_and_supported_version(self):
        authority = envelope_v2()
        authority["resource_scopes"][0]["unexpected"] = "value"
        with self.assertRaises(AuthorityError):
            validate_authority_envelope(authority)
        authority = envelope_v2()
        authority["version"] = 3
        with self.assertRaisesRegex(AuthorityError, "Unsupported authority envelope version"):
            validate_authority_envelope(authority)

    def test_v1_cannot_authorize_consequential_effects(self):
        authority = envelope()
        authority["allowed_effects"] = ["remote-mutation"]
        with self.assertRaises(AuthorityError):
            validate_authority_envelope(authority)

        transition = approval()
        transition["effect"] = "external-communication"
        with self.assertRaises(AuthorityError):
            validate_transition_approval(transition)
