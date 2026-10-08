"use strict";
const assert = require("node:assert/strict");
const { buildGraph, filterGraph, projectGraph, projectOverview, projectGroupPage, presetGraph, runLabel, wrapNodeLabel, nodePresentation, READABLE_MIN_ZOOM, layoutKey, validPositions } = require("../src/tasktra/portal_static/relationship-map.js");
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
check("cycles and special identifiers remain finite without mutating snapshots",()=>{
  const data={goals:[{id:"__proto__",title:"Special"}],agents:[{work_id:"a",parent_work_id:"b",goal_id:"__proto__",state:"succeeded"},{work_id:"b",parent_work_id:"a",goal_id:"__proto__",state:"succeeded"},{work_id:"constructor",parent_work_id:"constructor"}]};
  const before=JSON.stringify(data), view=projectGraph(buildGraph(data),{includeLineage:true});
  assert.ok(view.nodes.length>=4);
  assert.equal(JSON.stringify(data),before);
  assert.equal(JSON.stringify(fixture),original);
});
const history={goals:[{id:"g",title:"Goal"}],jobs:[{id:"j",goal_id:"g",title:"Job"}],agents:[
  {work_id:"one",role:"tester",job_id:"j",goal_id:"g",state:"succeeded"},
  {work_id:"two",role:"tester",job_id:"j",goal_id:"g",state:"failed"},
  {work_id:"three",role:"reviewer",goal_id:"g",state:"completed"},
  {work_id:"four",role:"reviewer",goal_id:"g",state:"cancelled"},
  {work_id:"active",role:"tester",job_id:"j",goal_id:"g",state:"started"},
  {work_id:"unlinked-one",role:"tester",state:"succeeded"},
  {work_id:"unlinked-two",role:"tester",state:"succeeded"}
]};
const historyGraph=buildGraph(history);
check("historical groups preserve exact members and do not absorb active or unlinked agents",()=>{
  const view=projectGraph(historyGraph,{groupHistorical:true});
  const group=view.nodes.find(n=>n.type==="agent-group" && n.memberIds.includes("agent:one"));
  assert.ok(group);assert.equal(group.groupedCount,2);
  assert.deepEqual(new Set(group.memberIds),new Set(["agent:one","agent:two"]));
  assert.equal(view.memberToDisplay["agent:one"],group.id);
  for(const id of ["agent:active","agent:unlinked-one","agent:unlinked-two"]) assert.ok(view.nodes.some(n=>n.id===id));
  const membership=view.edges.find(e=>e.source==="job:j" && e.target===group.id);
  assert.ok(membership);assert.equal(membership.count,2);
  assert.equal(new Set(membership.canonicalEdgeIds).size,2);
  assert.ok(membership.canonicalEdgeIds.every(id=>historyGraph.edges.some(e=>e.id===id)));
});
check("expanding a group restores original nodes and their recorded assignments",()=>{
  const compact=projectGraph(historyGraph,{groupHistorical:true});
  const group=compact.nodes.find(n=>n.type==="agent-group" && n.memberIds.includes("agent:one"));
  const expanded=projectGraph(historyGraph,{groupHistorical:true,expandedGroups:new Set([group.id])});
  assert.equal(expanded.nodes.some(n=>n.id===group.id),false);
  for(const id of group.memberIds) {
    assert.ok(expanded.nodes.some(n=>n.id===id));assert.ok(linked(expanded,"job:j",id,"job-agent"));
  }
  assert.equal(projectGraph(historyGraph,{groupHistorical:false}).nodes.some(n=>n.type==="agent-group"),false);
});
check("search and focus reveal exact historical members inside collapsed groups",()=>{
  for(const options of [{query:"one"},{focusId:"agent:two"}]) {
    const view=projectGraph(historyGraph,{groupHistorical:true,...options});
    const id=options.focusId || "agent:one";
    assert.ok(view.nodes.some(n=>n.id===id));
    assert.equal(view.memberToDisplay[id],id);
  }
});
check("lineage is optional and group summaries never invent recorded edges",()=>{
  const g=buildGraph({...history,agents:[...history.agents,{work_id:"child",role:"tester",goal_id:"g",parent_work_id:"one",state:"succeeded"}]});
  const off=projectGraph(g,{groupHistorical:true,includeLineage:false}),on=projectGraph(g,{groupHistorical:true,includeLineage:true});
  assert.equal(off.edges.some(e=>e.type==="parent-lineage"),false);
  assert.ok(on.nodes.some(n=>n.id==="agent:one"),"a recorded parent is not collapsed into a leaf group");
  assert.ok(on.edges.some(e=>e.type==="parent-lineage"));
  const canonical=new Map(g.edges.map(e=>[e.id,e]));
  for(const edge of on.edges) {
    for(const id of edge.canonicalEdgeIds || [edge.id]) {
      const source=canonical.get(id);assert.ok(source,"every displayed edge has recorded evidence");
      assert.equal(on.memberToDisplay[source.source],edge.source);
      assert.equal(on.memberToDisplay[source.target],edge.target);
    }
  }
});
check("grouping cannot merge distinct job assignments that share a role",()=>{
  const g=buildGraph({goals:[{id:"g"}],jobs:[{id:"j1",goal_id:"g"},{id:"j2",goal_id:"g"}],agents:[1,2].flatMap(j=>[1,2].map(i=>({work_id:`a${j}-${i}`,job_id:`j${j}`,goal_id:"g",role:"tester",state:"succeeded"})))});
  const view=projectGraph(g,{groupHistorical:true}), groups=view.nodes.filter(n=>n.type==="agent-group");
  assert.equal(groups.length,2);
  for(const group of groups) {
    const incoming=view.edges.filter(e=>e.target===group.id && e.type==="job-agent");
    assert.equal(incoming.length,1);
    assert.ok(group.memberIds.every(id=>g.nodes.find(n=>n.id===id).record.job_id===incoming[0].source.slice(4)));
  }
});
check("bounded projections have no dangling edges and disclose truncation",()=>{
  const g=buildGraph({agents:Array.from({length:600},(_,i)=>({work_id:"worker-"+i,role:i===599?"Unique target":"worker",state:"started"}))});
  const limited=projectGraph(g,{maxNodes:25});
  assert.ok(limited.nodes.length<=25);assert.equal(limited.meta.truncated,true);
  const ids=new Set(limited.nodes.map(n=>n.id));assert.ok(limited.edges.every(e=>ids.has(e.source)&&ids.has(e.target)));
  const searched=projectGraph(g,{query:"Unique target",maxNodes:25});assert.ok(searched.nodes.some(n=>n.id==="agent:worker-599"));
});
check("projection bounds keep the exact searched or focused crowded hub",()=>{
  const g=buildGraph({agents:[{work_id:"hub",role:"ZZZ unique hub"},...Array.from({length:300},(_,i)=>({work_id:"child-"+i,role:"AAA child",parent_work_id:"hub"}))]});
  for(const options of [{query:"ZZZ unique hub"},{focusId:"agent:hub"}]) {
    const view=projectGraph(g,{...options,includeLineage:true,maxNodes:25});
    assert.ok(view.nodes.some(n=>n.id==="agent:hub"));assert.ok(view.nodes.length<=25);assert.equal(view.meta.truncated,true);
  }
});
check("historical parents remain individual even when lineage is hidden",()=>{
  const g=buildGraph({...history,agents:[...history.agents,{work_id:"child",goal_id:"g",parent_work_id:"one",state:"succeeded"}]});
  assert.ok(projectGraph(g,{includeLineage:false}).nodes.some(n=>n.id==="agent:one"));
});
check("conflicting goal links never merge executions assigned to different jobs",()=>{
  const g=buildGraph({goals:[{id:"g"},{id:"other"}],jobs:[{id:"j1",goal_id:"other"},{id:"j2",goal_id:"other"}],agents:[{work_id:"a1",role:"tester",job_id:"j1",goal_id:"g",state:"succeeded"},{work_id:"a2",role:"tester",job_id:"j2",goal_id:"g",state:"succeeded"}]});
  const view=projectGraph(g);
  assert.notEqual(view.memberToDisplay["agent:a1"],view.memberToDisplay["agent:a2"]);
});
check("finished child runs collapse with lineage hidden while their parent stays visible",()=>{
  const g=buildGraph({goals:[{id:"g"}],agents:[{work_id:"parent",role:"coordinator",goal_id:"g",state:"started"},...Array.from({length:4},(_,i)=>({work_id:"child-"+i,role:"tester",goal_id:"g",parent_work_id:"parent",state:"succeeded"}))]});
  const view=projectGraph(g,{includeLineage:false});
  assert.ok(view.nodes.some(n=>n.id==="agent:parent"));
  const group=view.nodes.find(n=>n.type==="agent-group");assert.ok(group);assert.equal(group.groupedCount,4);
  const expanded=projectGraph(g,{includeLineage:true});assert.ok(expanded.nodes.every(n=>n.type!=="agent-group"));assert.equal(expanded.edges.filter(e=>e.type==="parent-lineage").length,4);
});
check("empty snapshots remain empty and safe to project",()=>{
  const empty=projectGraph(buildGraph({goals:[],jobs:[],agents:[]}));
  assert.equal(empty.nodes.length,0);assert.equal(empty.edges.length,0);
});
const overviewFixture=buildGraph({
  goals:[{id:"g1",title:"First"},{id:"g2",title:"Second"}],
  jobs:[{id:"j1",goal_id:"g1"},{id:"j2",goal_id:"g1"},{id:"j3",goal_id:"g2"}],
  agents:[
    {work_id:"d1",goal_id:"g1",role:"tester",state:"succeeded"},
    {work_id:"d2",goal_id:"g1",role:"reviewer",state:"succeeded"},
    {work_id:"w1",job_id:"j1",goal_id:"g1",role:"tester",state:"succeeded"},
    {work_id:"w2",job_id:"j2",goal_id:"g1",role:"reviewer",state:"failed"},
    {work_id:"active",job_id:"j1",goal_id:"g1",role:"implementer",state:"started"},
    {work_id:"ambiguous",job_id:"j1",goal_id:"g2",state:"succeeded"},
    {work_id:"orphan",state:"succeeded"}
  ]
});
check("overview keeps every canonical identity represented exactly once",()=>{
  const view=projectOverview(overviewFixture), represented=[];
  for(const node of view.nodes) represented.push(...(node.memberIds||[node.id]));
  assert.deepEqual(new Set(represented),new Set(overviewFixture.nodes.map(n=>n.id)));
  assert.equal(represented.length,new Set(represented).size);
  for(const node of view.nodes.filter(n=>n.summary)) assert.equal(node.groupedCount,node.memberIds.length);
  for(const id of ["agent:active","agent:ambiguous","agent:orphan"]) assert.ok(view.nodes.some(n=>n.id===id));
  assert.ok(view.nodes.length<overviewFixture.nodes.length);
});
check("overview edges reconcile exactly to recorded edges without invented goal assignments",()=>{
  const view=projectOverview(overviewFixture), byId=new Map(overviewFixture.edges.map(e=>[e.id,e]));
  for(const edge of view.edges) {
    assert.equal(edge.count,edge.canonicalEdgeIds.length);
    for(const id of edge.canonicalEdgeIds) {
      const original=byId.get(id);assert.ok(original);
      assert.equal(view.memberToDisplay[original.source],edge.source);
      assert.equal(view.memberToDisplay[original.target],edge.target);
    }
  }
  const jobOwned=view.memberToDisplay["agent:w1"];
  assert.equal(view.edges.some(e=>e.source==="goal:g1"&&e.target===jobOwned),false);
  assert.notEqual(jobOwned,view.memberToDisplay["agent:d1"]);
});
check("overview job groups stay within their recorded goals",()=>{
  const view=projectOverview(overviewFixture), first=view.nodes.find(n=>(n.memberIds||[]).includes("job:j1"));
  assert.ok(first.summary);assert.deepEqual(new Set(first.memberIds),new Set(["job:j1","job:j2"]));
  assert.notEqual(view.memberToDisplay["job:j3"],first.id);
});
check("overview terminal summaries combine roles while preserving active runs",()=>{
  const view=projectOverview(overviewFixture), direct=view.nodes.find(n=>(n.memberIds||[]).includes("agent:d1"));
  assert.ok(direct.summary);assert.deepEqual(new Set(direct.memberIds),new Set(["agent:d1","agent:d2"]));
  assert.equal(view.memberToDisplay["agent:active"],"agent:active");
});
check("overview limits are disclosed and cannot leave dangling edges",()=>{
  const g=buildGraph({goals:Array.from({length:60},(_,i)=>({id:"g"+i}))}), view=projectOverview(g,{maxNodes:20});
  assert.ok(view.nodes.length<=20);assert.equal(view.meta.truncated,true);
  const ids=new Set(view.nodes.map(n=>n.id));assert.ok(view.edges.every(e=>ids.has(e.source)&&ids.has(e.target)));
  assert.equal(projectOverview(buildGraph({})).nodes.length,0);
});
check("overview retains unloaded references and their recorded membership edges",()=>{
  const g=buildGraph({agents:[{work_id:"a",goal_id:"missing-goal",job_id:"missing-job",parent_work_id:"missing-parent",state:"started"}]}), view=projectOverview(g);
  assert.deepEqual(new Set(view.nodes.map(n=>n.id)),new Set(g.nodes.map(n=>n.id)));
  for(const edge of g.edges.filter(e=>e.type!=="parent-lineage")) assert.ok(view.edges.some(e=>e.canonicalEdgeIds.includes(edge.id)));
  assert.equal(view.meta.missingReferences,3);
});
check("overview never joins runs owned by different goals into a global history group",()=>{
  const g=buildGraph({goals:[{id:"g1"},{id:"g2"}],agents:[1,2].map(i=>({work_id:"a"+i,goal_id:"g"+i,role:"tester",state:"succeeded"}))}), view=projectOverview(g);
  assert.notEqual(view.memberToDisplay["agent:a1"],view.memberToDisplay["agent:a2"]);
});
check("group paging visits each exact run once without expanding the entire history",()=>{
  const g=buildGraph({goals:[{id:"g"}],agents:Array.from({length:25},(_,i)=>({work_id:"run-"+String(i).padStart(2,"0"),goal_id:"g",role:"tester",state:"succeeded"}))});
  const group=projectOverview(g).nodes.find(n=>n.summary), seen=[];
  for(let page=0;page<4;page++) {
    const view=projectGroupPage(g,group,{page});
    assert.equal(view.meta.paging.page,page);assert.equal(view.meta.paging.total,25);assert.equal(view.meta.paging.pages,4);
    const runs=view.nodes.filter(n=>n.type==="agent");assert.ok(runs.length<=8);seen.push(...runs.map(n=>n.id));
    assert.ok(view.nodes.some(n=>n.id==="goal:g"));
    for(const edge of view.edges) assert.ok(edge.canonicalEdgeIds.every(id=>g.edges.some(e=>e.id===id&&e.source===edge.source&&e.target===edge.target)));
  }
  assert.equal(seen.length,25);assert.equal(new Set(seen).size,25);
  assert.equal(projectGroupPage(g,group,{page:999}).meta.paging.page,3);
});
check("job-owned run pages retain their job summary without a speculative direct goal link",()=>{
  const group=projectOverview(overviewFixture).nodes.find(n=>(n.memberIds||[]).includes("agent:w1"));
  const page=projectGroupPage(overviewFixture,group);
  assert.ok(page.nodes.some(n=>n.type==="job-group"));
  assert.equal(page.edges.some(e=>e.type==="goal-agent"),false);
  assert.deepEqual(new Set(page.edges.flatMap(e=>e.canonicalEdgeIds)),new Set(overviewFixture.edges.filter(e=>e.type==="job-agent"&&group.memberIds.includes(e.target)).map(e=>e.id)));
});
check("job pages only show their recorded owner and requested members",()=>{
  const group=projectOverview(overviewFixture).nodes.find(n=>(n.memberIds||[]).includes("job:j1"));
  const page=projectGroupPage(overviewFixture,group,{pageSize:1});
  assert.equal(page.nodes.filter(n=>n.type==="job").length,1);assert.ok(page.nodes.some(n=>n.id==="goal:g1"));
  assert.equal(page.nodes.some(n=>n.type==="agent"),false);assert.equal(page.meta.paging.pages,2);
});
check("terminal runs with distinct missing job owners retain separate overview summaries",()=>{
  const g=buildGraph({agents:[{work_id:"a",job_id:"missing-a",state:"succeeded"},{work_id:"b",job_id:"missing-b",state:"succeeded"}]}),view=projectOverview(g);
  assert.notEqual(view.memberToDisplay["agent:a"],view.memberToDisplay["agent:b"]);
  assert.ok(linked(view,"stub-job:missing-a",view.memberToDisplay["agent:a"],"job-agent"));
  assert.ok(linked(view,"stub-job:missing-b",view.memberToDisplay["agent:b"],"job-agent"));
});
check("exact canonical self-parent links survive focus and paging without aggregate loops",()=>{
  const g=buildGraph({goals:[{id:"g"}],agents:[{work_id:"self",goal_id:"g",parent_work_id:"self",state:"succeeded"},{work_id:"other",goal_id:"g",state:"succeeded"}]});
  const focused=projectGraph(g,{focusId:"agent:self",includeLineage:true});
  assert.ok(linked(focused,"agent:self","agent:self","parent-lineage"));
  const overview=projectOverview(g,{includeLineage:true}),group=overview.nodes.find(n=>n.memberIds.includes("agent:self"));
  assert.equal(overview.edges.some(e=>e.source===e.target),false);
  assert.ok(linked(projectGroupPage(g,group,{includeLineage:true}),"agent:self","agent:self","parent-lineage"));
  assert.equal(projectGraph(g,{focusId:"agent:self"}).edges.some(e=>e.source===e.target),false);
});

check("presets retain only matching runs and their exact recorded goal or job context",()=>{
  const g=buildGraph({goals:[{id:"g"}],jobs:[{id:"j",goal_id:"g"}],agents:[{work_id:"active",job_id:"j",goal_id:"g",state:"working"},{work_id:"failed",job_id:"j",goal_id:"g",state:"failed"},{work_id:"sibling",job_id:"j",goal_id:"g",state:"succeeded"}]});
  const active=presetGraph(g,"active"), failed=presetGraph(g,"failed");
  assert.deepEqual(new Set(active.nodes.map(n=>n.id)),new Set(["goal:g","job:j","agent:active"]));
  assert.deepEqual(new Set(failed.nodes.map(n=>n.id)),new Set(["goal:g","job:j","agent:failed"]));
  assert.equal(active.edges.some(e=>e.source==="job:j"&&e.target==="agent:sibling"),false);
});
check("run labels are distinguishable and never infer a job title",()=>{
  assert.equal(runLabel({work_id:"long-run-123",role:"tester"},new Map()),"tester \u00b7 -run-123");
  assert.equal(runLabel({work_id:"work",job_id:"j"},new Map([["j",{title:"Exact job"}]])),"Exact job \u00b7 work");
});
check("visual node labels are bounded, categorized, and retain concise run identity",()=>{
  const agent={id:"agent:abcdefghijk",type:"agent",relationId:"abcdefghijk",label:"A very long job title that must remain canonical",record:{work_id:"abcdefghijk",role:"implementation specialist with a long role",model:"gpt-6.1-sol"}};
  const display=nodePresentation(agent);
  assert.equal(agent.label,"A very long job title that must remain canonical");
  assert.match(display.label,/^RUN\n/);
  assert.match(display.label,/defghijk/);
  assert.match(display.label,/gpt-6\.1-sol/);
  assert.ok(display.label.split("\n").length<=3);
  assert.ok(display.label.split("\n").every(line=>line.length<=26));
  assert.match(nodePresentation({id:"agent:r",type:"agent",record:{work_id:"review-run",role:"reviewer",model:"gpt-6.1-sol"}}).label,/\nreviewer\n/);
  assert.ok(display.width>display.textMaxWidth);
  assert.equal(nodePresentation({type:"goal",label:"A title"}).label,"GOAL\nA title");
  const job=nodePresentation({type:"job",label:"Readable ability telegraphs and accurate gameplay feedback"});
  const missing=nodePresentation({type:"stub-job",missing:true,label:"averylongunbrokenreferencethatmustnotescapeitsnode"});
  const group=nodePresentation({type:"agent-group",label:"implementation specialist · 127 historical runs"});
  for(const item of [job,missing,group]) assert.ok(item.label.split("\n").length<=3);
  assert.ok(job.label.includes("…"));assert.ok(missing.label.includes("…"));
  assert.equal(wrapNodeLabel("one two three four five six",3,2),"one\ntw…");
  assert.equal(wrapNodeLabel("abcdefghijklmnop",4,2),"abcd\nefg…");
  assert.ok(READABLE_MIN_ZOOM >= 14 / 17);
});
check("stored layouts are scoped and accept only finite loaded coordinates",()=>{
  assert.notEqual(layoutKey("p","g","all",false),layoutKey("p","g","failed",false));
  const points=validPositions({version:1,positions:[["agent:a",12,3],["agent:gone",1,1],["agent:a",Infinity,0]]},new Set(["agent:a"]));
  assert.deepEqual(points.get("agent:a"),{x:12,y:3}); assert.equal(points.size,1);
  assert.equal(validPositions({version:2,positions:[["agent:a",1,1]]},new Set(["agent:a"])).size,0);
});
console.log(checks + " relationship map checks passed");
