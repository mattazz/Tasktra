---
id = "tasktra-packs"
title = "Plan ecosystem packs"
family = "core"
---
# Plan ecosystem packs

Use this skill when a project needs specialist or ecosystem-pack recommendations, capability preflight, or migration preview.

## Procedure

1. Run `tasktra packs --root <root> recommend` to inspect bounded local evidence. Treat the result as a preview, never as authorization or automatic activation.
2. Present the recommended top-level packs, evidence, observation counts, truncation, and uncertainty. Do not infer token savings from evidence volume.
3. Before changing `packs.enabled`, run `tasktra packs --root <root> preflight --pack <pack>` with only credential-free capability facts supplied by the trusted host.
4. Missing optional capabilities degrade only their feature. Missing required capabilities or untrusted executable packs block that pack, not unrelated local work.
5. Run `tasktra packs --root <root> migration-preview --pack <pack>` before a version change. Stage 5 validates migrations but never executes them.
6. Change the project-owned profile only after explicit review, then run compile preview/check and the configured validation commands.

Pack detection must not write files, execute project code, access credentials, use the network, or grant authority.
