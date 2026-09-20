# Portable examples

These compact fixtures show how a project can start locally, compile managed
projections, and run direct-argument validation without a network connection or
an account. Copy one fixture into a new directory, install Tasktra, then run:

```text
python -m tasktra compile --root . --catalog <tasktra-checkout>/catalog --trust-catalog
python -m tasktra compile --root . --catalog <tasktra-checkout>/catalog --trust-catalog --check
python -m tasktra validate --root .
python -m tasktra validate --root . --run
```

The profile comments in each `project.toml` distinguish a ready-to-use pack
selection from a customized selection. Validation commands deliberately use
only the Python interpreter and local fixture files, keeping every example
portable even when language-specific tooling is absent.

| Fixture | Profile | Pack selection |
| --- | --- | --- |
| `application` | ready-to-use | generic |
| `service-api` | customized | Python |
| `web` | ready-to-use | TypeScript/web |
| `python` | customized | Python |
| `typescript` | ready-to-use | TypeScript/web |
| `monorepo` | customized | Python, TypeScript/web, monorepo |

See [optional capability behavior](optional-capabilities.md) for the offline
fallbacks shared by all six fixtures.
