"use strict";
const assert = require("node:assert/strict");
const { buildGraph, filterGraph, layoutGraph } = require("../src/tasktra/portal_static/relationship-map.js");
let checks = 0;
function check(name, run) { run(); checks++; console.log("PASS " + name); }
const fixture = {
  goals: [{id:"g",title:"Ship puzzle",status:"active"},{id:"shared",title:"Second goal"}],
  jobs: [{id:"job",goal_id:"g",title:"Implement puzzle"},{id:"shared",goal_id:"g",title:"Shared identifier job"}],
  agents: [
    {work_id:"parent",id:"reused-agent",role:"coordinator",goal_id:"g",state:"started"},
    {work_id:"child",id:"reused-agent",role:"implementer",goal_id:"g",job_id:"job",parent_work_id:"parent"},
    {work_id:"unassigned",role:"reviewer",goal_id:"g"},
    {work_id:"shared",role:"tester",goal_id:"g"},
    {work_id:"job-parent",role:"tester",goal_id:"g",job_id:"shared",parent_work_id:"shared"}
  ]
};
const original = JSON.stringify(fixture), graph = buildGraph(fixture);
const linked = (g, source, target, type) => g.edges.some(e=>e.source===source && e.target===target && (!type || e.type===type));
check("distinct typed records and repeated agent identities do not merge",()=>{
  assert.equal(new Set(graph.nodes.map(n=>n.id)).size,9);
  for(const id of ["goal:shared","job:shared","agent:shared","agent:parent","agent:child"]) assert.ok(graph.nodes.some(n=>n.id===id));
});
check("edges follow recorded goal, job, and parent relationships",()=>{
  assert.ok(linked(graph,"goal:g","job:job","goal-job"));
  assert.ok(linked(graph,"job:job","agent:child","job-agent"));
  assert.equal(linked(graph,"goal:g","agent:child","goal-agent"),false);
  assert.ok(linked(graph,"goal:g","agent:unassigned","goal-agent"));
  assert.ok(linked(graph,"agent:parent","agent:child","parent-lineage"));
  assert.ok(linked(graph,"job:shared","agent:job-parent"));
  assert.ok(linked(graph,"agent:shared","agent:job-parent","parent-lineage"));
  assert.equal(linked(graph,"job:shared","agent:job-parent","parent-lineage"),false);
  assert.equal(graph.edges.some(e=>e.type==="job-agent" && e.target==="agent:unassigned"),false);
});
check("missing references stay explicit without inventing their record type",()=>{
  const g=buildGraph({goals:[],jobs:[],agents:[{work_id:"orphan",goal_id:"missing-goal",job_id:"missing-job",parent_work_id:"missing-parent"}]});
  for(const type of ["stub-goal","stub-job","unresolved-parent"]) assert.ok(g.nodes.some(n=>n.type===type && n.missing));
  assert.equal(g.nodes.some(n=>n.id==="agent:missing-parent"),false);
  const view=filterGraph(g);assert.equal(view.meta.loadedRecords,1);assert.equal(view.meta.missingReferences,3);assert.equal(view.meta.totalNodes,4);
  const ids=new Set(g.nodes.map(n=>n.id)); assert.ok(g.edges.every(e=>ids.has(e.source)&&ids.has(e.target)));
});
check("hidden parent anchors require an explicit job link",()=>{
  const g=buildGraph({goals:[],jobs:[{id:"anchor"}],agents:[{work_id:"linked",job_id:"anchor",parent_work_id:"anchor"},{work_id:"unlinked",parent_work_id:"anchor"}]});
  assert.ok(linked(g,"job:anchor","agent:linked","parent-lineage"));
  assert.equal(linked(g,"job:anchor","agent:unlinked","parent-lineage"),false);
  assert.ok(g.nodes.some(n=>n.type==="unresolved-parent" && n.relationId==="anchor"));
});
check("an agent goal remains visible when its job goal is absent or conflicting",()=>{
  for(const job of [{id:"j"},{id:"j",goal_id:"other"}]) {
    const g=buildGraph({goals:[{id:"g"}],jobs:[job],agents:[{work_id:"a",job_id:"j",goal_id:"g"}]});
    assert.ok(linked(g,"goal:g","agent:a","goal-agent"));
  }
});
check("unassigned agents and name similarities do not create speculative links",()=>{
  const g=buildGraph({goals:[{id:"g",title:"same"}],jobs:[{id:"job",goal_id:"g",title:"same"}],agents:[{work_id:"job-worker",id:"job",role:"same"}]});
  assert.equal(g.edges.some(e=>e.target==="agent:job-worker"),false);
});
check("search shows only matching nodes and immediate relationship context",()=>{
  const found=filterGraph(graph,{query:"unassigned",maxNodes:250});
  assert.deepEqual(new Set(found.nodes.map(n=>n.id)),new Set(["agent:unassigned","goal:g"]));
  assert.equal(filterGraph(graph,{query:"not a recorded identifier"}).nodes.length,0);
});
check("focus shows only selected node and direct connections",()=>{
  const view=filterGraph(graph,{focusId:"agent:child"});
  assert.deepEqual(new Set(view.nodes.map(n=>n.id)),new Set(["agent:child","agent:parent","job:job"]));
});
check("search can find records beyond the default display limit",()=>{
  const all=buildGraph({goals:[{id:"g",title:"Goal"}],jobs:[],agents:Array.from({length:600},(_,i)=>({work_id:"worker-"+String(i).padStart(3,"0"),goal_id:"g",role:i===599?"Unique target":"worker"}))});
  const limited=filterGraph(all,{maxNodes:25});
  assert.ok(limited.nodes.length<=25);assert.equal(limited.meta.truncated,true);assert.ok(limited.meta.hiddenNodes>0);
  const found=filterGraph(all,{query:"Unique target",maxNodes:25});
  assert.ok(found.nodes.some(n=>n.id==="agent:worker-599"));
  assert.ok(found.nodes.length<=25);
  const ids=new Set(limited.nodes.map(n=>n.id));assert.ok(limited.edges.every(e=>ids.has(e.source)&&ids.has(e.target)));
});
check("a crowded neighborhood cannot displace the searched or focused node",()=>{
  const hub=buildGraph({goals:[],jobs:[],agents:[{work_id:"hub",role:"ZZZ unique hub"},...Array.from({length:300},(_,i)=>({work_id:"child-"+i,role:"AAA child",parent_work_id:"hub"}))]});
  for(const options of [{query:"ZZZ unique hub",maxNodes:25},{focusId:"agent:hub",maxNodes:25}]) {
    const view=filterGraph(hub,options);assert.ok(view.nodes.some(n=>n.id==="agent:hub"));assert.ok(view.nodes.length<=25);assert.equal(view.meta.truncated,true);
  }
});
check("cycles, self references, and special identifiers remain finite",()=>{
  const cyclic=buildGraph({goals:[{id:"__proto__",title:"Special"}],jobs:[],agents:[{work_id:"a",parent_work_id:"b",goal_id:"__proto__"},{work_id:"b",parent_work_id:"a",goal_id:"__proto__"},{work_id:"constructor",parent_work_id:"constructor"}]});
  const positioned=layoutGraph(filterGraph(cyclic,{}));
  assert.ok(positioned.nodes.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
  assert.ok(Number.isFinite(positioned.bounds.width)&&Number.isFinite(positioned.bounds.height));
});
check("layout is deterministic and model operations do not mutate snapshots",()=>{
  const view=filterGraph(graph,{}); assert.deepEqual(layoutGraph(view),layoutGraph(view));
  assert.equal(JSON.stringify(fixture),original);
  const positioned=layoutGraph(view);assert.equal(new Set(positioned.nodes.map(n=>n.x+","+n.y)).size,positioned.nodes.length);
});
check("small neighborhoods fit compact bounds and large rosters use multiple columns",()=>{
  const small=layoutGraph(filterGraph(buildGraph({goals:[{id:"g"}],agents:[{work_id:"a",goal_id:"g"}]})));
  assert.ok(small.bounds.height<300);assert.ok(small.bounds.width<1000);
  const large=layoutGraph(filterGraph(buildGraph({agents:Array.from({length:200},(_,i)=>({work_id:"a"+i}))})));
  assert.ok(new Set(large.nodes.map(n=>n.x)).size>1);assert.ok(large.bounds.height<1300);
});
check("empty snapshots remain empty and safe to lay out",()=>{
  const empty=filterGraph(buildGraph({goals:[],jobs:[],agents:[]}));
  assert.equal(empty.nodes.length,0);assert.equal(empty.edges.length,0);assert.deepEqual(layoutGraph(empty).nodes,[]);
});
console.log(checks + " relationship map checks passed");
