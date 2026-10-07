# Portal graph dependencies

The relationship map bundles these MIT-licensed browser libraries locally. It makes no CDN requests and requires no npm installation to run. The exact upstream license notices ship in `src/tasktra/portal_static/vendor-graph-licenses.js`.

| Package | Version | Upstream browser asset | SHA-256 of vendored file |
| --- | --- | --- | --- |
| [cytoscape](https://registry.npmjs.org/cytoscape/-/cytoscape-3.34.3.tgz) | 3.34.3 | `dist/cytoscape.min.js` | `5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91` |
| [layout-base](https://registry.npmjs.org/layout-base/-/layout-base-2.0.1.tgz) | 2.0.1 | `layout-base.js` | `ec15ab5df9af3f20708f4faab994accf91cda71848cd5bb10a23432cc50b6745` |
| [cose-base](https://registry.npmjs.org/cose-base/-/cose-base-2.2.0.tgz) | 2.2.0 | `cose-base.js` | `7cae9509bd36235a63a85e71c8d9fa2cd0bc1d0c1ecc5b5a737976f39d040ddf` |
| [cytoscape-fcose](https://registry.npmjs.org/cytoscape-fcose/-/cytoscape-fcose-2.2.0.tgz) | 2.2.0 | `cytoscape-fcose.js` | `4b1cab218d74996aa59cd8473f9239cc6398b8c1774d84d7e59ad9a68959cb57` |

## Updating the bundle

Fetch an exact package tarball from the npm registry and verify its SHA-512 integrity before extracting only the browser asset and license. Do not run package scripts. Preserve the upstream asset bytes, record the new filename and hashes here, and retain its license notices. Update the fixed HTTP routes, script order, and integrity tests together. The package includes `portal_static/*.js`; no runtime network access is needed.

Load order: Cytoscape, layout-base, cose-base, then cytoscape-fcose. The fCoSE browser bundle registers itself with Cytoscape. Run the graph tests and configured project checks, then inspect the actual project map in a browser, including layout, polling stability, reduced motion, and keyboard navigation.

Verified tarball integrity:

- `cytoscape@3.34.3`: `sha512-yfYGhRcGAntq6YBD583j4n0Eg3jIxvWmZtz/5uz9UYkeIStSlMxuUja+ec5j3iBD8nv1rwaOAYMW09tBdkSeaQ==`
- `layout-base@2.0.1`: `sha512-dp3s92+uNI1hWIpPGH3jK2kxE2lMjdXdr+DH8ynZHpd6PUlH6x6cbuXnoMmiNumznqaNO31xu9e79F0uuZ0JFg==`
- `cose-base@2.2.0`: `sha512-AzlgcsCbUMymkADOJtQm3wO9S3ltPfYOFD5033keQn9NJzIbtnZj+UdBJe7DYml/8TdbtHJW3j58SOnKhWY/5g==`
- `cytoscape-fcose@2.2.0`: `sha512-ki1/VuRIHFCzxWNrsshHYPs6L7TvLu3DL+TyIGEsRcvVERmxokbf5Gdk7mFxZnTdiGtnA4cfSmjZJMviqSuZrQ==`

The nested `portal_static/.gitattributes` marks these pinned vendor files as `-text` so Git preserves their verified upstream bytes, including mixed line endings. Combined license notice SHA-256: `fd98150a270c46b11ac0779648e8f0df855faa023831a4a25b9c77a99b7e83f9`.
