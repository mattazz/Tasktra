(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.TasktraPortalWorkspace = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const MAX_VIEWS = 20, MAX_BYTES = 65536, MAX_FRAGMENT = 4096;
  const OPAQUE = /^[A-Za-z0-9_-]{1,128}$/, VIEW_ID = /^[a-f0-9]{16,64}$/;
  const STATIC = { view:["overview","goals","jobs","agents","map","timeline","efficiency"], sort:["recent","tokens"], since:["","P1D","P7D","P30D"], timeline_kind:["","job_attempt","agent"], map:["all","active","failed"], activity:["all","progress","tool","status","final"], selected:["goal","job","agent"], severity:["","error","warning","info"] };
  const result = (ok, value, issues=[]) => ({ok, value, issues:[...new Set(issues)]});
  const obj = v => v !== null && typeof v === "object" && !Array.isArray(v);
  const text = (v,max,trim=false) => typeof v === "string" && v.length <= max && !/[\x00-\x1f\x7f-\x9f\uD800-\uDFFF]/u.test(v) && (trim || v === v.trim()) ? (trim ? v.trim() : v) : null;
  const id = v => text(v,256) || null;
  const key = v => typeof v === "string" && OPAQUE.test(v) ? v : null;
  const mode = v => v === "live" || v === "demo" ? v : null;
  const safeInt = v => Number.isSafeInteger(v) && v >= 0;
  const defaults = () => ({view:"overview",goal_id:"",jobs:{query:"",status:""},agents:{query:"",role:"",model:"",state:"",sort:"recent"},usage:{model:"",role:"",since:""},attention:{severity:"",category:""},timeline:{kind:"",state:"",model:""},map:{preset:"all"},activity:{kind:"all",follow:true},selected:null,comparison:{work_ids:[]}});

  // Omitted collections mean structural validation; supplied collections mean
  // exact membership in the newly fetched scope. Empty filters are always valid.
  function normalizeSettings(raw, allowed={}) {
    const out=defaults(), bad=[];
    if (!obj(raw)) return result(false,out,["settings"]);
    if (!obj(allowed)) return result(false,out,["allowed"]);
    const section=name=>{if(raw[name]===undefined)return {};if(!obj(raw[name])){bad.push(name);return {};}return raw[name];};
    const collection=name=>{if(allowed[name]===undefined)return null;if(!Array.isArray(allowed[name])){bad.push(`allowed.${name}`);return new Set();}return new Set(allowed[name].filter(x=>id(x)));};
    const member=(value,name)=>{const set=collection(name);return !set||set.has(value);};
    const choice=(value,fallback,values,name)=>{if(value===undefined)return fallback;if(typeof value!=="string"||!values.includes(value)){bad.push(name);return fallback;}return value;};
    const dynamic=(value,name)=>{if(value===undefined||value==="")return "";if(!id(value)||!member(value,name)){bad.push(name);return "";}return value;};
    const query=(value,name)=>{if(value===undefined)return "";const v=text(value,200,true);if(v===null){bad.push(name);return "";}return v;};
    out.view=choice(raw.view,"overview",STATIC.view,"view");out.goal_id=dynamic(raw.goal_id,"goal_ids");
    const jobs=section("jobs"), agents=section("agents"), usage=section("usage"), attention=section("attention"), timeline=section("timeline"), map=section("map"), activity=section("activity"), comparison=section("comparison");
    out.jobs={query:query(jobs.query,"jobs.query"),status:dynamic(jobs.status,"job_states")};
    out.agents={query:query(agents.query,"agents.query"),role:dynamic(agents.role,"roles"),model:dynamic(agents.model,"models"),state:dynamic(agents.state,"agent_states"),sort:choice(agents.sort,"recent",STATIC.sort,"agents.sort")};
    out.usage={model:dynamic(usage.model,"models"),role:dynamic(usage.role,"roles"),since:choice(usage.since,"",STATIC.since,"usage.since")};
    out.attention={severity:choice(attention.severity,"",STATIC.severity,"attention.severity"),category:dynamic(attention.category,"attention_categories")};
    out.timeline={kind:choice(timeline.kind,"",STATIC.timeline_kind,"timeline.kind"),state:dynamic(timeline.state,"timeline_states"),model:dynamic(timeline.model,"models")};
    out.map.preset=choice(map.preset,"all",STATIC.map,"map.preset");out.activity.kind=choice(activity.kind,"all",STATIC.activity,"activity.kind");
    if(activity.follow!==undefined){if(typeof activity.follow==="boolean")out.activity.follow=activity.follow;else bad.push("activity.follow");}
    if(raw.selected!==undefined&&raw.selected!==null){const s=raw.selected, names={goal:"goal_ids",job:"job_ids",agent:"agent_work_ids"};if(obj(s)&&STATIC.selected.includes(s.type)&&id(s.id)&&member(s.id,names[s.type]))out.selected={type:s.type,id:s.id};else bad.push("selected");}
    if(comparison.work_ids!==undefined){const work=comparison.work_ids;if(!Array.isArray(work)||work.length>2||work.some(v=>!id(v))||new Set(work).size!==work.length||work.some(v=>!member(v,"agent_work_ids")))bad.push("comparison.work_ids");else out.comparison.work_ids=[...work];}
    return result(!bad.length,out,bad);
  }
  function makeEnvelope(settings, context){const p=key(context?.projectKey),m=mode(context?.mode),n=normalizeSettings(settings);return !p||!m?result(false,null,["context"]):result(n.ok,{v:1,project_key:p,mode:m,settings:n.value},n.issues);}
  const enc=s=>typeof Buffer!=="undefined"?Buffer.from(s,"utf8").toString("base64url"):btoa(unescape(encodeURIComponent(s))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
  const dec=s=>typeof Buffer!=="undefined"?Buffer.from(s,"base64url").toString("utf8"):decodeURIComponent(escape(atob(s.replace(/-/g,"+").replace(/_/g,"/"))));
  function serializeFragment(envelope){
    if(!obj(envelope)||envelope.v!==1||!key(envelope.project_key)||!mode(envelope.mode))return result(false,"",["envelope"]);
    const n=normalizeSettings(envelope.settings);if(!n.ok)return result(false,"",n.issues);
    const value="#workspace="+enc(JSON.stringify({v:1,project_key:envelope.project_key,mode:envelope.mode,settings:n.value}));
    return value.length>MAX_FRAGMENT?result(false,"",["fragment-too-large"]):result(true,value);
  }
  function parseFragment(hash,allowed={}){
    if(typeof hash!=="string"||!hash.startsWith("#workspace=")||hash.length>MAX_FRAGMENT)return result(false,null,["fragment"]);
    try{const encoded=hash.slice(11);if(!/^[A-Za-z0-9_-]+$/.test(encoded))return result(false,null,["fragment"]);const decoded=dec(encoded);if(enc(decoded)!==encoded)return result(false,null,["fragment"]);const e=JSON.parse(decoded);if(!obj(e)||e.v!==1||!key(e.project_key)||!mode(e.mode))return result(false,null,["envelope"]);const n=normalizeSettings(e.settings,allowed);return result(n.ok,{v:1,project_key:e.project_key,mode:e.mode,settings:n.value},n.issues);}catch(_){return result(false,null,["fragment"]);}
  }
  function storageKey(projectKey,m){const p=key(projectKey),q=mode(m);return p&&q?result(true,`tasktra.portal.workspace.v1.${q}.${p}`):result(false,null,["context"]);}
  const bytes=s=>new TextEncoder().encode(s).length;
  function timestamp(value){
    if(typeof value!=="string"||value.length>40)return null;
    const match=/^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):([0-5]\d):([0-5]\d)(?:\.\d{1,3})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.exec(value);
    if(!match)return null;const y=Number(match[1]),m=Number(match[2]),d=Number(match[3]);const days=[31,y%4===0&&(y%100!==0||y%400===0)?29:28,31,30,31,30,31,31,30,31,30,31];const time=Date.parse(value);
    if(d>days[m-1]||!Number.isFinite(time))return null;const canonical=new Date(time).toISOString();
    return /^\d{4}-/.test(canonical)?canonical:null;
  }
  function normalizeEntry(entry){
    if(!obj(entry)||typeof entry.id!=="string"||!VIEW_ID.test(entry.id))return result(false,null,["entry"]);
    const name=text(entry.name,80,true),at=timestamp(entry.updated_at),n=normalizeSettings(entry.settings);
    return !name||!at?result(false,null,["entry"]):result(n.ok,{id:entry.id,name,settings:n.value,updated_at:at},n.issues);
  }
  const sortViews=(a,b)=>Date.parse(b.updated_at)-Date.parse(a.updated_at)||a.id.localeCompare(b.id);
  function readViews(storage,p,m){
    const k=storageKey(p,m);if(!k.ok)return result(false,{views:[],available:false},k.issues);
    let raw;try{raw=storage.getItem(k.value);}catch(_){return result(false,{views:[],available:false},["storage-unavailable"]);}
    if(raw===null)return result(true,{views:[],available:true});
    if(typeof raw!=="string"||bytes(raw)>MAX_BYTES)return result(false,{views:[],available:false},["storage-invalid"]);
    let data;try{data=JSON.parse(raw);}catch(_){return result(false,{views:[],available:false},["storage-invalid"]);}
    if(!Array.isArray(data)||data.length>MAX_VIEWS)return result(false,{views:[],available:false},["storage-invalid"]);
    const views=[],ids=new Set();for(const entry of data){const normalized=normalizeEntry(entry);if(!normalized.ok||ids.has(normalized.value.id))return result(false,{views:[],available:false},["storage-invalid"]);ids.add(normalized.value.id);views.push(normalized.value);}
    return result(true,{views:views.sort(sortViews),available:true});
  }
  function upsertView(storage,p,m,entry){
    const current=readViews(storage,p,m);if(!current.ok)return result(false,{views:[],saved:null,available:false},current.issues);
    const n=normalizeEntry(entry);if(!n.ok)return result(false,{views:current.value.views,saved:null,available:true},n.issues);
    const views=[...current.value.views.filter(x=>x.id!==n.value.id),n.value].sort(sortViews),serialized=JSON.stringify(views);
    if(views.length>MAX_VIEWS||bytes(serialized)>MAX_BYTES)return result(false,{views:current.value.views,saved:null,available:true},["storage-full"]);
    try{storage.setItem(storageKey(p,m).value,serialized);return result(true,{views,saved:n.value,available:true});}catch(_){return result(false,{views:current.value.views,saved:null,available:false},["storage-unavailable"]);}
  }
  function removeView(storage,p,m,entryId){
    const current=readViews(storage,p,m);if(!current.ok)return result(false,{views:[],removed:false,available:false},current.issues);
    if(typeof entryId!=="string"||!VIEW_ID.test(entryId))return result(false,{views:current.value.views,removed:false,available:true},["id"]);
    const views=current.value.views.filter(x=>x.id!==entryId);if(views.length===current.value.views.length)return result(true,{views,removed:false,available:true});
    try{storage.setItem(storageKey(p,m).value,JSON.stringify(views));return result(true,{views,removed:true,available:true});}catch(_){return result(false,{views:current.value.views,removed:false,available:false},["storage-unavailable"]);}
  }
  const unknownUsage=()=>({total_tokens:null,input_tokens:null,cached_input_tokens:null,output_tokens:null});
  function measuredUsage(raw){if(!obj(raw))return null;const t=raw.total_tokens,i=raw.input_tokens,c=raw.cached_input_tokens,o=raw.output_tokens;return [t,i,c,o].every(safeInt)&&t===i+o&&c<=i?{total_tokens:t,input_tokens:i,cached_input_tokens:c,output_tokens:o}:null;}
  function imported(a,nested=false){return {work_id:id(a.work_id),role:text(a.role,128)||null,model:text(a.model,128)||null,effort:text(a.effort,64)||null,state:text(a.state,64)||null,goal_id:id(a.goal_id),job_id:id(a.job_id),started_at:timestamp(a.started_at),last_observed_at:timestamp(a.last_observed_at),outcome:text(a.outcome,64)||null,provenance:text(a.provenance,128)||null,usage:measuredUsage(nested?a.usage:a)||unknownUsage()};}
  function normalizeComparison(workIds,agents){
    if(!Array.isArray(workIds)||workIds.length>2||!Array.isArray(agents))return result(false,{requested:[],pins:[],ready:false},["comparison"]);
    const requested=workIds.map(v=>id(v)||""),counts=new Map();for(const a of agents){if(obj(a)&&id(a.work_id))counts.set(a.work_id,(counts.get(a.work_id)||0)+1);}
    const pins=requested.map(w=>{if(!w)return{work_id:"",found:false,agent:null,reason:"invalid"};if(requested.filter(x=>x===w).length>1||counts.get(w)>1)return{work_id:w,found:false,agent:null,reason:"duplicate"};const a=agents.find(x=>obj(x)&&x.work_id===w);return a?{work_id:w,found:true,agent:imported(a),reason:"present"}:{work_id:w,found:false,agent:null,reason:"missing"};});
    const ok=pins.every(x=>x.found),ready=ok&&pins.length===2;return result(ok,{requested,pins,ready},ok?[]:["comparison"]);
  }
  function validPin(pin){
    if(!obj(pin)||!id(pin.work_id))return null;
    if(pin.found!==true||pin.reason!=="present"||!obj(pin.agent)||pin.agent.work_id!==pin.work_id)return{work_id:pin.work_id,found:false,agent:null,reason:["missing","duplicate","invalid"].includes(pin.reason)?pin.reason:"invalid"};
    return{work_id:pin.work_id,found:true,agent:imported(pin.agent,true),reason:"present"};
  }
  function comparisonRows(left,right){
    const lp=validPin(left),rp=validPin(right),l=lp?.agent?.usage||unknownUsage(),r=rp?.agent?.usage||unknownUsage();
    const stale=!lp?.found||!rp?.found||lp.work_id===rp.work_id;const bad=[];if(stale)bad.push("comparison");
    for(const [original,pin] of [[left,lp],[right,rp]])if(pin?.found&&obj(original?.agent?.usage)&&!measuredUsage(original.agent.usage)&&Object.values(original.agent.usage).some(v=>v!==null))bad.push("comparison-usage");
    const rows=["total_tokens","input_tokens","cached_input_tokens","output_tokens"].map(k=>{const comparable=!stale&&safeInt(l[k])&&safeInt(r[k]);return{key:k,label:k.replace(/_/g," "),left:l[k],right:r[k],difference:comparable?r[k]-l[k]:null,comparable};});
    const frac=u=>safeInt(u.input_tokens)&&u.input_tokens>0&&safeInt(u.cached_input_tokens)?u.cached_input_tokens/u.input_tokens:null,lf=frac(l),rf=frac(r),comparable=!stale&&lf!==null&&rf!==null;
    rows.push({key:"cache_fraction",label:"cache fraction",left:lf,right:rf,difference:comparable?rf-lf:null,comparable});
    return result(!bad.length,{left:lp,right:rp,stale:stale||bad.length>0,metrics:rows},bad);
  }
  // Callers may supply malformed objects or adapters. All public boundaries
  // retain a usable safe result even when property access itself throws.
  const guard=(fn,fallback,issue="invalid-input")=>(...args)=>{try{return fn(...args);}catch(_){return result(false,fallback(),[issue]);}};
  return {
    normalizeSettings:guard(normalizeSettings,defaults),makeEnvelope:guard(makeEnvelope,()=>null),parseFragment:guard(parseFragment,()=>null),serializeFragment:guard(serializeFragment,()=>""),storageKey:guard(storageKey,()=>null),
    readViews:guard(readViews,()=>({views:[],available:false}),"storage-unavailable"),upsertView:guard(upsertView,()=>({views:[],saved:null,available:false}),"storage-unavailable"),removeView:guard(removeView,()=>({views:[],removed:false,available:false}),"storage-unavailable"),
    normalizeComparison:guard(normalizeComparison,()=>({requested:[],pins:[],ready:false})),comparisonRows:guard(comparisonRows,()=>comparisonRows(null,null).value)
  };
});
