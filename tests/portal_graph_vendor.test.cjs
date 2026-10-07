"use strict";
// Execute the browser UMD assets, without npm dependencies or a network connection.
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), crypto = require("node:crypto");
const root = path.resolve(__dirname, "../src/tasktra/portal_static");
const assets = [
  {
    "file": "vendor-cytoscape-3.34.3.min.js",
    "sha256": "5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91"
  },
  {
    "file": "vendor-layout-base-2.0.1.js",
    "sha256": "ec15ab5df9af3f20708f4faab994accf91cda71848cd5bb10a23432cc50b6745"
  },
  {
    "file": "vendor-cose-base-2.2.0.js",
    "sha256": "7cae9509bd36235a63a85e71c8d9fa2cd0bc1d0c1ecc5b5a737976f39d040ddf"
  },
  {
    "file": "vendor-cytoscape-fcose-2.2.0.js",
    "sha256": "4b1cab218d74996aa59cd8473f9239cc6398b8c1774d84d7e59ad9a68959cb57"
  }
];
const notices = fs.readFileSync(path.join(root, "vendor-graph-licenses.js"));
assert.equal(crypto.createHash("sha256").update(notices).digest("hex"), "fd98150a270c46b11ac0779648e8f0df855faa023831a4a25b9c77a99b7e83f9", "required upstream license notices must ship intact");
for(const packageName of ["cytoscape 3.34.3", "layout-base 2.0.1", "cose-base 2.2.0", "cytoscape-fcose 2.2.0"]) assert.ok(notices.toString("utf8").includes(packageName));
const context = vm.createContext({setTimeout, clearTimeout, console});
for (const asset of assets) {
  const bytes = fs.readFileSync(path.join(root, asset.file));
  assert.equal(crypto.createHash("sha256").update(bytes).digest("hex"), asset.sha256, asset.file + " must retain verified upstream bytes");
  vm.runInContext(bytes.toString("utf8"), context, {filename:asset.file});
}
assert.equal(context.cytoscape.version, "3.34.3");
vm.runInContext(`
  var cy = cytoscape({headless:true, styleEnabled:true, elements:[
    {data:{id:'goal'}}, {data:{id:'job'}}, {data:{id:'agent'}}, {data:{id:'isolated'}},
    {data:{id:'goal-job',source:'goal',target:'job'}}, {data:{id:'job-agent',source:'job',target:'agent'}}
  ]});
  cy.layout({name:'fcose',quality:'default',animate:false,randomize:true,nodeDimensionsIncludeLabels:true}).run();
`, context);
assert.equal(context.cy.nodes().length, 4);
assert.ok(context.cy.nodes().every(n=>Number.isFinite(n.position('x')) && Number.isFinite(n.position('y'))));
context.cy.destroy();
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
let previous = -1;
for(const asset of assets) {
  const position = html.indexOf('src="/' + asset.file + '"');
  assert.ok(position > previous, "browser scripts must load dependencies in order: " + asset.file);
  previous = position;
}
assert.ok(html.indexOf('src="/relationship-map.js"') > previous);
assert.doesNotMatch(html, /<script[^>]+src=["']https?:/i);
console.log("vendored graph engine checks passed");
