import unittest

from tasktra.worker_profiles import CliCapabilities, HostLaunchProfile, WorkerContext, WorkerProfileError


class WorkerProfilesTests(unittest.TestCase):
    def test_default_context_has_no_override_or_optimized_claim(self):
        preview = HostLaunchProfile.from_context(None, None).preview()
        self.assertEqual(preview, {
            "user_profile": None, "requested_config_overrides": [], "unavailable_capabilities": [],
            "focused_requested": False, "effective_tool_reduction": "not-requested",
        })

    def test_focused_context_is_bounded_to_observed_server_identities(self):
        context = WorkerContext(
            focused=True, observed_mcp_servers=("browser", "unused"),
            observed_plugin_mcp_servers=(("drive@marketplace.openai.com", "files"),),
            disable_mcp_servers=("unused",), disable_plugin_mcp_servers=(("drive@marketplace.openai.com", "files"),),
            enable_mcp_servers=("browser",),
        )
        profile = HostLaunchProfile.from_context(
            context, CliCapabilities(checked=True, supports_config_overrides=True),
        )
        self.assertEqual(profile.config_overrides, (
            "mcp_servers.unused.enabled=false",
            'plugins."drive@marketplace.openai.com".mcp_servers.files.enabled=false',
            "mcp_servers.browser.enabled=true",
        ))
        self.assertTrue(profile.preview()["focused_requested"])
        self.assertEqual(profile.preview()["effective_tool_reduction"], "unverified")

    def test_rejects_unobserved_unsafe_or_requested_capability_disables(self):
        with self.assertRaisesRegex(WorkerProfileError, "observed effective"):
            WorkerContext(focused=True, disable_mcp_servers=("missing",))
        with self.assertRaisesRegex(WorkerProfileError, "unsafe"):
            WorkerContext(focused=True, observed_mcp_servers=("bad.key",), disable_mcp_servers=("bad.key",))
        with self.assertRaisesRegex(WorkerProfileError, "requested capability"):
            WorkerContext(focused=True, observed_mcp_servers=("research",), disable_mcp_servers=("research",),
                          requested_capabilities=("research",))
        with self.assertRaisesRegex(WorkerProfileError, "requested capability"):
            WorkerContext(focused=True, observed_plugin_mcp_servers=(("drive@marketplace.openai.com", "files"),),
                          disable_plugin_mcp_servers=(("drive@marketplace.openai.com", "files"),),
                          requested_capabilities=("files",))

    def test_named_profile_and_overrides_fail_closed_when_help_was_not_checked(self):
        context = WorkerContext(
            focused=True, user_profile="local-worker", observed_mcp_servers=("unused",),
            disable_mcp_servers=("unused",),
        )
        profile = HostLaunchProfile.from_context(context, CliCapabilities())
        self.assertFalse(profile.available)
        self.assertEqual(profile.preview()["unavailable_capabilities"], ["named-user-profile", "bounded-mcp-overrides"])

    def test_named_profile_requires_exec_and_top_level_help_support(self):
        capabilities = CliCapabilities.from_help("--profile --config", "--profile --config")
        profile = HostLaunchProfile.from_context(WorkerContext(user_profile="focused"), capabilities)
        self.assertEqual(profile.user_profile, "focused")
        unsupported = CliCapabilities.from_help("--profile --config", "--config")
        self.assertFalse(HostLaunchProfile.from_context(WorkerContext(user_profile="focused"), unsupported).available)

    def test_mapping_accepts_only_closed_identity_fields(self):
        context = WorkerContext.from_mapping({
            "focused": True,
            "observed_mcp_servers": ["unused"],
            "disable_mcp_servers": ["unused"],
            "observed_plugin_mcp_servers": [{"plugin": "drive@marketplace.openai.com", "server": "files", "enabled": True}],
            "disable_plugin_mcp_servers": [{"plugin": "drive@marketplace.openai.com", "server": "files"}],
        })
        self.assertEqual(context.disable_plugin_mcp_servers, (("drive@marketplace.openai.com", "files"),))
        with self.assertRaisesRegex(WorkerProfileError, "unsupported fields"):
            WorkerContext.from_mapping({"config": "anything"})


if __name__ == "__main__":
    unittest.main()
