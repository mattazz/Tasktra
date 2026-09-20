import unittest

from tasktra.providers import (
    MAX_PROVIDER_JSON_BYTES,
    FakeProvider,
    OperationDescriptor,
    ProviderError,
    ProviderHealth,
    ProviderRegistry,
    ProviderResult,
    ResourceScope,
    load_bounded_provider_json,
    load_provider_result,
)


SCOPE = {
    "provider": "github", "host": "github.com", "container": "acme/widgets",
    "resource_kind": "issue", "resource": "12", "ref": "main",
}


def descriptor(provider, effect_class, capability, scope=SCOPE):
    return OperationDescriptor(
        provider, effect_class, capability=capability, action=capability,
        resource_scope=scope,
    )


class Stage4ProviderTests(unittest.TestCase):
    def test_deterministic_health_report_and_replaceable_fake(self):
        registry = ProviderRegistry()
        jira = FakeProvider(ProviderHealth("jira", "misconfigured", "Jira host is not configured"))
        github = FakeProvider(ProviderHealth("github", "degraded", "GitHub reads are limited"))
        jira_scope = {**SCOPE, "provider": "jira", "host": "jira.example"}
        registry.register_provider("jira", discovery=jira, operations={descriptor("jira", "read-only", "issue-get", jira_scope): jira})
        registry.register_provider("github", discovery=github, operations={
            descriptor("github", "read-only", "issue-get"): github,
            descriptor("github", "remote-mutation", "issue-comment"): github,
        })
        report = registry.health_report()
        self.assertEqual([item["provider"] for item in report["providers"]], ["github", "jira"])
        self.assertEqual(report["providers"][0]["state"], "degraded")
        self.assertEqual(report["providers"][0]["operations"], [
            {"effect_class": "read-only", "request_kind": "issue-get"},
            {"effect_class": "remote-mutation", "request_kind": "issue-comment"},
        ])

    def test_read_cannot_invoke_a_registered_protected_effect(self):
        fake = FakeProvider(
            ProviderHealth("github", "available", "GitHub is available"),
            read_result=ProviderResult("succeeded", "issue returned", ({"number": 12},)),
            effect_result=ProviderResult("pending", "comment accepted for processing"),
        )
        registry = ProviderRegistry()
        descriptors = registry.register_provider("github", discovery=fake, operations={
            descriptor("github", "read-only", "issue-get"): fake,
            descriptor("github", "remote-mutation", "issue-comment"): fake,
        })
        read = next(item for item in descriptors if item.effect_class == "read-only")
        effect = next(item for item in descriptors if item.effect_class == "remote-mutation")
        self.assertEqual(registry.read(read, SCOPE, {"number": 12}).state, "succeeded")
        with self.assertRaisesRegex(ProviderError, "read-only API"):
            registry.read(effect, SCOPE, {"body": "must not execute"})
        self.assertEqual([call[0] for call in fake.calls], ["read"])
        self.assertFalse(hasattr(registry, "execute"))
        self.assertEqual([call[0] for call in fake.calls], ["read"])

    def test_closed_scope_secrets_bounded_json_and_result_bytes_fail_closed(self):
        self.assertEqual(ResourceScope.from_mapping(SCOPE).to_dict(), SCOPE)
        with self.assertRaisesRegex(ProviderError, "allowed property"):
            ResourceScope.from_mapping({**SCOPE, "token": "not allowed"})
        with self.assertRaisesRegex(ProviderError, "canonical lowercase"):
            ResourceScope.from_mapping({**SCOPE, "host": "GitHub.com"})
        with self.assertRaisesRegex(ProviderError, "credential-sensitive"):
            load_bounded_provider_json('{"authorization":"Bearer should-not-serialize"}')
        with self.assertRaisesRegex(ProviderError, "credential-bearing URL"):
            load_bounded_provider_json('{"link":"https://host.example/a?access_token=secret"}')
        with self.assertRaisesRegex(ProviderError, "duplicate provider JSON key"):
            load_bounded_provider_json('{"x":1,"x":2}')
        with self.assertRaisesRegex(ProviderError, "exceeds"):
            load_bounded_provider_json("x" * (MAX_PROVIDER_JSON_BYTES + 1))
        bad = {"kind": "tasktra.provider-result", "version": 1, "state": "succeeded", "summary": "ok", "items": ["x" * 9000]}
        with self.assertRaisesRegex(ProviderError, "item 0 exceeds"):
            ProviderResult.from_mapping(bad)

    def test_conflicts_unknown_descriptors_and_offline_adapters_are_safe(self):
        class Offline(FakeProvider):
            def read(self, descriptor, scope, request):
                raise RuntimeError("Bearer leaked-token")

            def execute(self, descriptor, scope, request, idempotency_key):
                raise TimeoutError("token=leaked")

        offline = Offline(ProviderHealth("jira", "unavailable", "Jira is offline"))
        registry = ProviderRegistry()
        descriptors = registry.register_provider("jira", discovery=offline, operations={
            descriptor("jira", "read-only", "issue-get", {**SCOPE, "provider": "jira", "host": "jira.example"}): offline,
            descriptor("jira", "remote-mutation", "issue-transition", {**SCOPE, "provider": "jira", "host": "jira.example"}): offline,
        })
        read = next(item for item in descriptors if item.effect_class == "read-only")
        effect = next(item for item in descriptors if item.effect_class == "remote-mutation")
        jira_scope = {**SCOPE, "provider": "jira", "host": "jira.example"}
        self.assertEqual(registry.read(read, jira_scope, {}).state, "unavailable")
        self.assertFalse(hasattr(registry, "execute"))
        with self.assertRaisesRegex(ProviderError, "already registered"):
            registry.register_provider("jira", discovery=offline, operations={descriptor("jira", "read-only", "issue-get", jira_scope): offline})
        with self.assertRaisesRegex(ProviderError, "not registered"):
            registry.read(descriptor("jira", "read-only", "issue-list", jira_scope), jira_scope, {})
        good = '{"kind":"tasktra.provider-result","version":1,"state":"succeeded","summary":"ok","items":[]}'
        self.assertEqual(load_provider_result(good).state, "succeeded")

    def test_registry_requires_full_descriptor_and_rejects_credential_text_in_benign_fields(self):
        fake = FakeProvider(ProviderHealth("github", "available", "GitHub is available"))
        registry = ProviderRegistry()
        with self.assertRaisesRegex(ProviderError, "explicit descriptor"):
            registry.register_provider("github", discovery=fake, operations={OperationDescriptor("github", "remote-mutation", "issue-comment"): fake})
        effect = registry.register_provider("github", discovery=fake, operations={
            descriptor("github", "remote-mutation", "issue-comment"): fake,
        })[0]
        for request in (
            {"note": "Bearer do-not-store"},
            {"note": "password=hunter2"},
            {"link": "https://user:pass@github.com/acme/widgets"},
            {"link": "https://github.com/acme/widgets?token=do-not-store"},
        ):
            with self.subTest(request=request), self.assertRaisesRegex(ProviderError, "credential"):
                registry._execute_protected(effect, SCOPE, request, idempotency_key="comment-credential")
        self.assertEqual(fake.calls, [])

    def test_registration_is_atomic_when_a_later_operation_is_invalid(self):
        fake = FakeProvider(ProviderHealth("github", "available", "GitHub is available"))
        registry = ProviderRegistry()
        valid = descriptor("github", "read-only", "issue-get")
        invalid = OperationDescriptor("github", "remote-mutation", "issue-comment")
        with self.assertRaisesRegex(ProviderError, "explicit descriptor"):
            registry.register_provider("github", discovery=fake, operations={valid: fake, invalid: fake})
        self.assertEqual(registry.health_report()["providers"], [])
        with self.assertRaisesRegex(ProviderError, "not registered"):
            registry.read(valid, SCOPE, {})


if __name__ == "__main__":
    unittest.main()
