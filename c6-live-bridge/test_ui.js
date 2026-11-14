#!/usr/bin/env node
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.attrs={}; this.style={setProperty(k,v){this[k]=v;}}; this.value=''; this.textContent=''; this.className=''; this.onclick=null; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=[...nodes]; }
  setAttribute(key,value) { this.attrs[key]=String(value); if(key==='class') this.className=String(value); }
  getAttribute(key) { return key==='class' ? this.className : this.attrs[key]??null; }
  addEventListener(name,fn) { this['on'+name]=fn; }
  click() { if(this.onclick) this.onclick({preventDefault(){}}); }
}
const elements=new Map();
const document={
  activeElement:null,
  getElementById(id) { if(!elements.has(id))elements.set(id,new Element()); return elements.get(id); },
  createElement(tag) { return new Element(tag); },
  createElementNS(_ns,tag) { return new Element(tag); },
};
const status={wifi_up:true,endpoint_online:true,config_match:true,thread_role:2,rloc16:1,
  endpoint_uptime_s:30,gateway_uptime_ms:10000,endpoint_rx_messages:2,endpoint_rx_duplicates:0,
  endpoint_commands_emitted:1,puts_ok:1,puts_failed:0,dropped_mismatch:0,uart_bytes:4,
  uart_frames_ok:1,uart_crc_errors:0,config_hash:1,monitoring:{age_s:0,delayed:false},
  endpoint_safety:{present:true,phase:'attached',faults:0,may_send:true,prefix_status:0,
    r1_role_fault:false,r3_prefix_fault:false,interrupted_session:false,
    mesh_local_prefix:'fd12:3456:789a:0000::/64',ext_pan_ok:true,active_timestamp_valid:true,gate_remaining_s:0},
  endpoint_power:{configured:false,physical_hold_available:false,enabled:true,held:false,hold_requested:false,
    state:'uncontrolled',boot_hold_remaining_s:0,timer_error:false,storage_error:false,hardware_error:false,watchdog_error:false,
    link_lost:false,persistence_pending:false,violation:'none',recovering:false,recovery_state:'idle'}};
const replies={
  status, devices:{config_hash:1,zones:[],monitoring:{age_s:0,delayed:false}}, events:{boot_id:'boot-a',next:1,next_sequence:1,oldest:1,lost:0,events:[]},
  zones:{config_hash:1,zones:[],monitoring:{age_s:0,delayed:false}},
  config:{generation:1,hash:1,editable:true,apply_status:'active',zones:[]},
};
const requests=[],failKeys=new Set(),waits=new Map();
const context={document,status,replies,requests,failKeys,waits,location:{href:'http://ha.local/api/hassio_ingress/token/'},URL,
  Date,Math,Number,String,Object,Array,Set,JSON,Promise,console,
  setInterval(){return 0;},setTimeout(){return 0;},confirm(){return true;},
  async fetch(url,opts){const path=new URL(url).pathname.split('/').slice(-2).join('/');const key=path.split('?')[0].split('/').pop();requests.push({key,opts});if(failKeys.has(key))throw Error('test request failed');const value=key in replies?replies[key]:{};if(waits.has(key))await waits.get(key);return {ok:true,headers:{get:()=> 'application/json'},async json(){return value;}};},
};
const html=fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)),'index.html'),'utf8');
if(!html.includes('Online again after 30 s')||!html.includes('Uptime decrease observed')||html.includes('Stays online 30 s'))throw Error('the firmware steps must describe sampled uptime observations');
const code=html.split('<script>',2)[1].split('</script>',1)[0];
const checks=`
const view=id=>document.getElementById(id);
const heroText=()=>view('headline').textContent+' '+view('headline-detail').textContent;
const safetyPatch=(safety={},power={})=>({...status,endpoint_safety:{...status.endpoint_safety,...safety},endpoint_power:{...status.endpoint_power,...power}});
const assertNotSafe=(s,why)=>{health(s);if(view('hero').className.includes('good')||view('node-thread').className.includes('good')||view('summary').children[1].className.includes('good'))throw Error(why+' must not be shown as safe');};
health(status);
if(!view('node-thread').className.includes('good')||!view('summary').children[1].className.includes('good'))throw Error('a fresh child with complete safety telemetry should show ready');
if(heroText().includes('No physical hold')||heroText().includes('does not confirm'))throw Error('a healthy bridge says so plainly, without hardware caveats: '+view('headline-detail').textContent);
if(firmwareBaseline(latestStatus)!==30||Number.isFinite(firmwareBaseline({...status,monitoring:{age_s:9,delayed:true}})))throw Error('firmware restart comparison requires a fresh pre-upload baseline');
latestNative={scene_cache_ready:false,scene_cache_error:'inventory_unavailable',bulbs:[{zone:30861,result:5},{zone:31467,result:0}],scene_cache:{not_ready_ms:300000,retry_in_ms:0}};
if(sceneCacheIssue(latestNative))throw Error('scene storage is not flagged before 10 minutes');
latestNative.scene_cache={not_ready_ms:1200000,retry_in_ms:120000};{const i=sceneCacheIssue(latestNative);
if(!i||i[0]!=='warn'||!i[1].includes('20 min')||!i[1].includes('Zone 30861 not answering')||!i[1].includes('next check in 2 min'))throw Error('stuck scene storage must name the lamp and the next check: '+i);}
health(status);if(!heroText().includes('Lamp scene storage not ready'))throw Error('stuck scene storage must reach the attention summary');
if(sceneCacheIssue({...latestNative,scene_cache_ready:true})||sceneCacheIssue({}))throw Error('ready or missing scene storage is not flagged');
latestNative=null;health(status);
if(overflowRow(160,900000)[1]||overflowRow(160,960000)[1])throw Error('a settled overflow count is not flagged');
if(overflowRow(0,10000)[1]||overflowRow(150,15000)[1])throw Error('the burst while the node restarts after boot is not flagged');
overflowLast=160;if(overflowRow(162,1000000)[1]!=='warn'||!overflowRow(162,1000000)[0].includes('rising'))throw Error('overflows rising in normal running are flagged');
overflowLast=null;overflowRoseAt=0;
for(const role of [3,4]){assertNotSafe({...status,thread_role:role},'router or leader');if(!view('node-thread').className.includes('bad'))throw Error('router or leader must be shown as a fault');}
for(const test of [
  {...status,endpoint_safety:undefined},safetyPatch({present:false}),safetyPatch({may_send:'true'}),
  safetyPatch({phase:'unknown'}),safetyPatch({mesh_local_prefix:'fdde:ad00:beef:0000::/64'}),
  safetyPatch({prefix_status:2}),safetyPatch({ext_pan_ok:false}),safetyPatch({active_timestamp_valid:false}),
  safetyPatch({may_send:false}),safetyPatch({faults:1}),safetyPatch({r1_role_fault:true}),safetyPatch({phase:'fault-latched'}),
  safetyPatch({}, {enabled:'true'}),safetyPatch({}, {boot_hold_remaining_s:5}),safetyPatch({}, {link_lost:true}),safetyPatch({}, {watchdog_error:true}),
  safetyPatch({}, {violation:'role'}),safetyPatch({}, {recovering:true,recovery_state:'waiting-hello'}),
  safetyPatch({}, {hold_requested:true}),safetyPatch({}, {enabled:false}),
  {...status,endpoint_online:false}, {...status,monitoring:undefined},
  {...status,monitoring:{age_s:9,delayed:false}}, {...status,monitoring:{age_s:0,delayed:true}}
])assertNotSafe(test,'unsafe or unavailable telemetry');
health(safetyPatch({phase:'listening',may_send:false,prefix_status:255,ext_pan_ok:false,active_timestamp_valid:false,mesh_local_prefix:'0000:0000:0000:0000::/64'},{ }));
if(!heroText().includes('waiting'))throw Error('startup should be shown as waiting');
health(safetyPatch({phase:'operator-stopped',may_send:false,prefix_status:255,ext_pan_ok:false,active_timestamp_valid:false,mesh_local_prefix:'0000:0000:0000:0000::/64'},{enabled:false,hold_requested:true}));
if(!heroText().includes('reports operator stop')||!heroText().includes('No physical hold is available')||!heroText().includes('power state is not verified'))throw Error('operator stop must be reported as telemetry, without promising physical power off');
health(safetyPatch({}, {enabled:false,hold_requested:true}));
if(!heroText().includes('has not reported operator stop'))throw Error('a gateway disable is not proof that the endpoint stopped');
health({...safetyPatch({}, {hold_requested:true,boot_hold_remaining_s:30}),endpoint_online:false});
if(!heroText().includes('startup hold'))throw Error('a held endpoint can be offline during startup');
health({...status,monitoring:{age_s:0,delayed:false}});statusReceivedAt-=6000;health(latestStatus);
if(!view('headline').textContent.includes('Monitoring delayed')||view('node-thread').className.includes('good')||view('summary').children.some(c=>c.className.includes('good')))throw Error('a snapshot must age out even while a refresh is pending');
const deviceSnapshot={config_hash:1,monitoring:{age_s:0,delayed:false},zones:[{zone:1,confirmed:{at_ms:9999,level_x100:5000}}]};
const matchZone={zone:1,name:'Office',lutron:{on:true,state:'on',level_x100:5000,cct:2800},sent:null,
  lamp:{level_x100:5000,source:'observed',age_ms:1,cct:2800,seq:1},show:{status:'not_selected'},
  checks:[{key:'lutron',status:'good',title:'Lamp matches Lutron'}],verdict:{tone:'good',title:'In sync'}};
const zoneSnapshot={config_hash:1,monitoring:{age_s:0,delayed:false},zones:[matchZone],home_assistant:true};
const zoneCard=()=>view('zones').children[0];
const cardVerdict=()=>zoneCard().children[0].children[1].children.at(-1);
health({...status});deviceView(deviceSnapshot);zonesView(zoneSnapshot);health(latestStatus);
if(!view('hero').className.includes('good')||!zoneCard().className.includes('good'))throw Error('fresh readback with complete comparisons should show in sync');
if(!view('hero').hidden)throw Error('a healthy bridge shows no banner');
if(view('headline').textContent!=='Everything is working'||!heroText().includes('1 of 1 lamps in sync'))throw Error('a healthy bridge says so plainly: '+view('headline').textContent+' / '+view('headline-detail').textContent);
health({...status,endpoint_safety:undefined,endpoint_power:undefined});
if(view('hero').className.includes('good')||view('node-thread').className.includes('good'))throw Error('green lamp comparisons must not hide unavailable endpoint safety');
if(view('hero').hidden)throw Error('the banner returns when something needs a look');
health({...status,monitoring:{age_s:9,delayed:true}});
if(zoneCard().className.includes('good')||!cardVerdict().textContent.includes('unavailable'))throw Error('stale status must invalidate the displayed lamp comparison');
health({...status});deviceView({...deviceSnapshot,monitoring:{age_s:9,delayed:false}});
if(zoneCard().className.includes('good')||!cardVerdict().textContent.includes('unavailable'))throw Error('a stale device snapshot must not show a current comparison');
deviceView({...deviceSnapshot,monitoring:undefined});
if(zoneCard().className.includes('good'))throw Error('device freshness without snapshot age must be unavailable');
deviceView(deviceSnapshot);zonesView({...zoneSnapshot,monitoring:{age_s:9,delayed:false}});
if(zoneCard().className.includes('good'))throw Error('a stale zone response must not show a current comparison');
zonesView({...zoneSnapshot,zones:[{...matchZone,lamp:{...matchZone.lamp,source:'applied'}}]});
if(zoneCard().className.includes('good')||!cardVerdict().textContent.includes('hub reading'))throw Error('accepted commands must not masquerade as lamp readback');
zonesView({...zoneSnapshot,zones:[{...matchZone,lamp:{...matchZone.lamp,age_ms:120001}}]});
if(zoneCard().className.includes('good'))throw Error('an old hub reading must not be displayed as a current comparison');
{const walk=n=>[n,...(n.children||[]).flatMap(walk)],lis=walk(zoneCard()).filter(n=>n.tag==='li'&&n.className==='wait');const txt=n=>[n.textContent||'',...(n.children||[]).map(txt)].join(' ');if(lis.length!==1||!txt(lis[0]).includes('No recent hub reading'))throw Error('a stale reading is explained once, not per check: '+lis.map(txt).join(' | '))}
zonesView({...zoneSnapshot,zones:[{...matchZone,checks:[{status:'unknown',title:'Colour unavailable'}]}]});
if(zoneCard().className.includes('good'))throw Error('incomplete checks must not be counted as in sync');
deviceView({...deviceSnapshot,zones:[{zone:1},{zone:2},{zone:3}]});
zonesView({...zoneSnapshot,zones:[matchZone,{...matchZone,zone:2}]});health(latestStatus);
if(view('hero').className.includes('good')||zoneCard().className.includes('good'))throw Error('two of three zone summaries must be unavailable');
zonesView({...zoneSnapshot,zones:[]});health(latestStatus);
if(view('hero').className.includes('good')||view('summary').children.at(-1).className.includes('good'))throw Error('empty zone summaries must be unavailable');
deviceView(deviceSnapshot);zonesView({...zoneSnapshot,config_hash:2});health(latestStatus);
if(view('hero').className.includes('good')||zoneCard().className.includes('good'))throw Error('same zone IDs with a different mapping hash must be unavailable');
zonesView({...zoneSnapshot,config_hash:undefined});
if(zoneCard().className.includes('good'))throw Error('a missing mapping hash must not enable comparisons');
zonesView({...zoneSnapshot,zones:[null]});
if(zoneCard().className.includes('good')||!cardVerdict().textContent.includes('unavailable'))throw Error('malformed zone records must replace any previous green card');
zonesView({...zoneSnapshot,zones:[{...matchZone,checks:{bad:'not an array'}}]});
if(zoneCard().className.includes('good'))throw Error('malformed checks must render unavailable without throwing');
deviceView({...deviceSnapshot,zones:[{zone:1},{zone:1}]});zonesView({...zoneSnapshot,zones:[matchZone,matchZone]});
if(zoneCard().className.includes('good'))throw Error('duplicate zone IDs must not enable comparisons');
deviceView(deviceSnapshot);zonesView(zoneSnapshot);health(latestStatus);
health({...status,monitoring:{age_s:9,delayed:true},diagnostics:{maintenance:false,wifi_disconnects:0}});
if(!document.getElementById('node-gateway').className.includes('warn')) throw Error('stale monitoring must not establish a Wi-Fi outage');
if(!document.getElementById('maintenance-toggle').textContent.includes('Enter')) throw Error('maintenance must be explicit');
health({...status,network_up:true,network_link:'ethernet',diagnostics:{maintenance:false,link_disconnects:0}});
if(document.getElementById('detail-gateway').textContent!=='Ethernet up') throw Error('gateway must show its reported Ethernet link');
if(document.getElementById('health-note').textContent.includes('Wi-Fi')) throw Error('an Ethernet gateway must not show Wi-Fi diagnostics');
health({...status,network_up:true,network_transport:'ethernet',diagnostics:{maintenance:false}});
if(document.getElementById('detail-gateway').textContent!=='Ethernet up'||document.getElementById('health-note').textContent.includes('Wi-Fi')) throw Error('S3 transport telemetry must identify Ethernet');
health({...status,network_up:false,network_link:'ethernet',diagnostics:{maintenance:false}});
if(document.getElementById('detail-gateway').textContent!=='Ethernet down') throw Error('a down Ethernet link must name Ethernet');
health({...status,network_up:true,network_link:'wifi',diagnostics:{maintenance:false}});
if(document.getElementById('detail-gateway').textContent!=='Wi-Fi up') throw Error('gateway must show its reported Wi-Fi link');
health({...status,diagnostics:{maintenance:false}});
if(document.getElementById('detail-gateway').textContent!=='Network up') throw Error('older firmware must not be assumed to use Wi-Fi');
if(STAGES['ethernet-disconnected'][1]!=='bad'||STAGES['wifi-connected'][1]!=='good') throw Error('link transitions must be labelled');
health({...status,diagnostics:{maintenance:true,maintenance_remaining_ms:300000}});
if(!document.getElementById('maintenance-toggle').textContent.includes('End')) throw Error('active maintenance must be visible');
if(STAGES.retrying[1]==='bad'||STAGES.superseded[1]==='bad') throw Error('retry and supersession must not claim lost delivery');
if(STAGES['delivery-failed'][1]!=='bad') throw Error('exhausted delivery must be distinct');

const sample={boot_id:'boot-a',next:20,next_sequence:21,oldest:4,lost:3,
  events:[{id:4,ts_ms:9000,stage:'requested',zone:2,requested:5000,confirmed:-1}]};
eventView(sample);
if(eventCursor!==4) throw Error('cursor must advance to delivered ID, never next_sequence');
if(!document.getElementById('events').children.some(row=>row.className==='gap')) throw Error('lost cursor gap should be highlighted');
if(![...document.getElementById('filter-stage').children].some(option=>option.value==='requested')) throw Error('stage filter should include observed stage');
latestStatus={...status,gateway_uptime_ms:10000};
eventView({...sample,lost:0,events:[{...sample.events[0],id:5,stage:'applied'}]});
if(!document.getElementById('node-n4').className.includes('good')) throw Error('applied event should mark N4 outcome good');
if(!document.getElementById('edge-gateway-n4').className.includes('flow-pulse')) throw Error('applied event should pulse Gateway→N4');
statusReceivedAt-=20000;renderFlow(latestStatus);
if(!view('flow-note').textContent.includes('21s ago')||view('edge-gateway-n4').className.includes('flow-pulse'))throw Error('last event age must advance and its pulse must stop while the snapshot ages');
health({...status});
eventView({boot_id:'boot-b',next:3,next_sequence:4,oldest:1,lost:0,events:[{id:1,ts_ms:200,stage:'report-sent',zone:0}]});
if(eventCursor!==1||eventRows.length!==1||eventBootId!=='boot-b') throw Error('boot change should reset rows and cursor');
if(!document.getElementById('node-report').className.includes('good')) throw Error('report-sent should mark observed report path');
if(document.getElementById('flow-note').textContent.includes('<img')) throw Error('flow labels must remain text');

const text=n=>typeof n==='string'?n:(n.textContent||'')+' '+(n.children||[]).map(text).join(' ');
for(const seq of [0,10,40,255,0]){
  const legacy={stage:'report-sent',requested:seq,confirmed:10000};
  if(!text({children:stepLevels(legacy)}).includes('100%')||reportSeq(legacy)!==seq) throw Error('legacy report counter must remain separate from level');
  const typed={schema_version:2,stage:'report-sent',requested:seq,confirmed:1234,reported:10000,report_seq:seq,command_seq:0,reason:'command-settled',endpoint_ts_ms:0};
  const detail=text(eventDetail({events:[typed]},10000));
  if(!detail.includes('Reported')||!detail.includes('100%')||!detail.includes('command seq 0')||!detail.includes('report seq '+seq)||!detail.includes('endpoint uptime')) throw Error('typed report fields must render in expanded steps: '+detail);
}
if(stepLevels({schema_version:2,stage:'report-sent',reported:null,confirmed:10000}).length) throw Error('empty query reply must not fabricate a reported level from legacy fields');
const typed=(id,stage,seq,extra={})=>({schema_version:2,id,ts_ms:id*1000,zone:20,stage,command_seq:seq,requested:10000,confirmed:-1,...extra});
const grouped=groupEvents([
  typed(1,'requested',0),typed(2,'requested',1),
  typed(3,'report-sent',null,{reason:'query-reply',report_seq:40,reported:10000}),
  typed(4,'confirmed',0,{confirmed:10000}),typed(5,'report-sent',0,{reason:'command-settled',report_seq:0,reported:10000}),
  typed(6,'report-sent',null,{reason:'periodic-refresh',report_seq:41,reported:10000})]);
const zero=grouped.find(g=>g.kind==='command'&&g.events[0].command_seq===0),one=grouped.find(g=>g.kind==='command'&&g.events[0].command_seq===1);
if(!zero.stages.has('report-sent')||one.stages.has('report-sent')||grouped.filter(g=>g.kind==='single').length!==2) throw Error('query/periodic reports must not finish a command or mix with command sequences');
const wrapped=groupEvents([typed(1,'requested',0),typed(2,'report-sent',0,{reported:10000}),typed(3,'requested',0),typed(4,'confirmed',0,{confirmed:10000})]);
if(wrapped.length!==2||wrapped[0].events.length!==2||wrapped[1].events.length!==2) throw Error('a reused command sequence must open a new command');
const rows=()=>document.getElementById('events').children.filter(r=>r.className!=='day'&&r.className!=='detail');
eventView({boot_id:'availability',events:['endpoint-online','endpoint-offline','config-mismatch','config-matched','endpoint-config-changed'].map((stage,i)=>({id:i+1,ts_ms:1000+i,zone:-1,stage,requested:-1,confirmed:-1}))});
if(rows().length!==5||!rows().every(row=>text(row.children[1]).includes('Bridge'))) throw Error('endpoint-wide transitions should be labeled Bridge');
document.getElementById('filter-problems').checked=true;renderEvents();
if(rows().length!==2||!rows().some(row=>text(row).includes('Endpoint disconnected'))||!rows().some(row=>text(row).includes('Endpoint configuration mismatch'))) throw Error('endpoint disconnect and mismatch must be visible in Problems only');
document.getElementById('filter-problems').checked=false;
const boot=Date.UTC(2026,9,26,19,0,0);
eventView({boot_id:'boot-c',next:7,oldest:1,lost:0,boot_epoch_ms:boot,events:[
  {id:1,ts_ms:1000,stage:'requested',zone:3,requested:5000,confirmed:-1},{id:2,ts_ms:1200,stage:'applied',zone:3,requested:5000,confirmed:-1},
  {id:3,ts_ms:3000,stage:'confirmed',zone:3,requested:5000,confirmed:5000},{id:4,ts_ms:3100,stage:'report-sent',zone:3,requested:5000,confirmed:5000},
  {id:5,ts_ms:4000,stage:'observed',zone:4,requested:-1,confirmed:2500},{id:6,ts_ms:5000,stage:'requested',zone:4,requested:0,confirmed:'??'}]});
if(rows().length!==3) throw Error('a command and its steps should collapse into one row, got '+rows().length);
const done=text(rows()[2]);if(!done.includes('Set to 50%')||!done.includes('Reported to Lutron')) throw Error('finished command should show its reported outcome: '+done);
if(!text(rows()[0]).includes('Turn off')) throw Error('level 0 should read as turning off');
const pacific=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',hour:'numeric',minute:'2-digit',second:'2-digit'}).format(new Date(boot+1000));
if(!text(rows()[2]).includes(pacific)) throw Error('time should default to Pacific: '+pacific);
if(!document.getElementById('events-tz').textContent.includes('Pacific')) throw Error('fallback time zone should be named');
if(text(rows()[1].children[3]).includes('-1')||!text(rows()[1].children[3]).includes('25%')) throw Error('a reading should show only the lights level, never the -1 placeholder: '+text(rows()[1].children[3]));
const unknown=rows()[0].children[3].children.find(l=>l.children.some(c=>c.className==='unknown'));
if(!unknown||!text(unknown).includes('??')) throw Error('non-numeric levels must be shown raw and marked unknown');
if(text(rows()[2]).includes('#1')) throw Error('event numbers belong in the details, not the row');
rows()[2].onclick();const detail=document.getElementById('events').children.find(r=>r.className==='detail');
if(!detail||!text(detail).includes('Event #4')||!text(detail).includes('+2.1 s')) throw Error('expanding a row should list each step');
// The screenshot case: no seq, a finished command, then reports driven by later hub readings.
const at=(id,ts,stage,requested,confirmed)=>({id,ts_ms:ts,stage,zone:20,requested,confirmed});
eventView({boot_id:'boot-d',next:40,oldest:1,lost:0,boot_epoch_ms:boot,events:[
  at(4,15000,'requested',10000,-1),at(9,15989,'applied',10000,10000),at(10,15989,'confirmed',-1,10000),at(13,16100,'report-sent',0,10000),
  at(22,29500,'report-sent',10,10000),at(26,33300,'report-sent',10,10000),at(38,56800,'report-sent',40,10000)]});
if(rows().length!==2) throw Error('later reports must not join the finished command, got '+rows().length+' rows');
const updates=text(rows()[0]),cmd=text(rows()[1]);
if(!updates.includes('Status updates')||!updates.includes('3 reports')||!updates.includes('Bridge → Lutron')) throw Error('later reports should merge into one status-update row: '+updates);
if(updates.includes('Asked')||updates.includes('0.4%')) throw Error('a report row must not present its requested field as an asked level: '+updates);
if(!cmd.includes('Set to 100%')||!cmd.includes('Reported to Lutron')||!cmd.includes('From Lutron')) throw Error('the command should show its reported outcome: '+cmd);
const marked=n=>typeof n!=='string'&&(n.className==='unknown'||(n.children||[]).some(marked));
rows()[1].onclick();const stepRow=document.getElementById('events').children.find(r=>r.className==='detail'),steps=text(stepRow);
for(const want of ['Lutron → Bridge','Bridge → Ketra hub','At Bridge','Bridge → Lutron','Bridge reported the level','raw requested 0']) if(!steps.includes(want)) throw Error('steps should include '+want+': '+steps);
if(steps.includes('Hub confirmed the level')||steps.includes('✓ as asked'))throw Error('command-result telemetry must not be presented as hub readback');
if(steps.includes('Event #22')||marked(stepRow)) throw Error('steps should hold only this command and no -1 placeholders: '+steps);
if(marked(rows()[1])) throw Error('the summary should hide -1 placeholders');
eventView({boot_id:'boot-e',next:3,oldest:1,lost:0,events:[at(1,1000,'requested',5000,-1),at(2,1100,'applied',5000,-1),at(3,1200,'correction',5000,4000)]});
rows()[0].onclick();const fixed=text(document.getElementById('events').children.find(r=>r.className==='detail'));
if(!fixed.includes('Expected')||!fixed.includes('40%')) throw Error('a correction should show the lights against what was asked: '+fixed);
expandedEvents.clear();
eventView({boot_id:'boot-c',next:7,oldest:1,lost:0,boot_epoch_ms:boot,events:[
  {id:1,ts_ms:1000,stage:'requested',zone:3,requested:5000,confirmed:-1},{id:2,ts_ms:1200,stage:'applied',zone:3,requested:5000,confirmed:-1},
  {id:3,ts_ms:3000,stage:'confirmed',zone:3,requested:5000,confirmed:5000},{id:4,ts_ms:3100,stage:'report-sent',zone:3,requested:5000,confirmed:5000},
  {id:5,ts_ms:4000,stage:'observed',zone:4,requested:-1,confirmed:2500},{id:6,ts_ms:5000,stage:'requested',zone:4,requested:0,confirmed:'??'}]});
document.getElementById('filter-problems').checked=true;renderEvents();
if(rows().length!==1||!text(rows()[0]).includes('No activity')) throw Error('problems filter should hide healthy rows');
document.getElementById('filter-problems').checked=false;
gatewayTimezone={timezone:'UTC0'};haData={time_zone:'America/Los_Angeles',lights:[]};
if(showZone()!=='UTC'||haMinute(new Date('2026-10-28T12:00:00Z'))!==720) throw Error('show chart must use gateway UTC rather than HA Pacific');
gatewayTimezone={timezone:'America/Los_Angeles'};
if(showZone()!=='America/Los_Angeles'||haMinute(new Date('2026-10-28T12:00:00Z'))!==300) throw Error('show chart must use gateway Pacific time');
gatewayTimezone={timezone:'unknown-rule'};if(showZone())throw Error('unknown gateway rules must not be mistaken for HA time');
gatewayTimezone={unsupported:true};if(showZone()!=='America/Los_Angeles')throw Error('old gateways should fall back to HA time');
haData={time_zone:'Europe/London',lights:[]};renderEvents();
if(!document.getElementById('events-tz').textContent.includes('Europe/London')) throw Error('Home Assistant time zone should win');
health({...status});deviceView({config_hash:1,zones:[{zone:0},{zone:1,requested:7500}],monitoring:{age_s:0,delayed:false}});
if(document.getElementById('device-count').textContent!=='2 zones') throw Error('device view should still count zones');
const office={zone:0,zone_id:30861,name:'Office A20',n4_group:'office',
  lutron:{name:'office lamp',area:'Office',on:false,state:'off',level_x100:0,cct:null},sent:null,
  lamp:{level_x100:10000,seq:0,source:'observed',age_ms:3000,cct:null,xy:null,vibrancy:null},
  show:{status:'not_selected',installed:[31779],override_x100:null},
  checks:[{key:'lutron',status:'bad',title:'Lamp is on at 100%, Lutron shows off',detail:'Changed outside Lutron.'},{key:'show',status:'info',title:'No Natural Show selected'}],
  verdict:{tone:'bad',title:'Lamp is on at 100%, Lutron shows off'}};
const curve=[0,360,720,1080,1440].map((minute,i)=>({minute,level_x100:[3000,3000,10000,10000,3000][i],cct:1800}));
const sitting={zone:1,zone_id:31467,name:'Sitting Room A20',n4_group:'Sitting Room',
  lutron:{on:true,state:'on',level_x100:10000,cct:3000,area:'Sitting Room'},
  sent:{level_x100:8840,cct:2803,xy:null,vibrancy:27,fade_ms:0,delay_ms:0,seq:16,age_ms:2000},
  lamp:{level_x100:8840,seq:16,source:'observed',age_ms:1000,cct:2803,xy:null,vibrancy:27},
  show:{status:'running',id:31779,override_x100:null,now_ms:33412000,curve,target:{level_x100:8840,show_level_x100:8840,cct:2803,vibrancy:27},next:{minute:600,level_x100:10000,cct:3000}},
  checks:[{key:'lutron',status:'info',title:"Lutron shows the show's next keyframe"},{key:'show',status:'good',title:'Following Natural Show 31779'},{key:'hub',status:'good',title:'Hub reading matches the last command'}],
  verdict:{tone:'good',title:'In sync · following Natural Show'}};
zonesView({config_hash:1,zones:[office,sitting],time_zone:'America/Los_Angeles',home_assistant:true,monitoring:{age_s:0,delayed:false}});
const cards=document.getElementById('zones').children;
if(cards.length!==2||!cards[0].className.includes('bad')||!cards[1].className.includes('good')) throw Error('zone cards should carry their verdict tone');
const all=cards.map(text).join(' | ');
for(const want of ['Lamp is on at 100%, Lutron shows off','Off','100%','Hub reading','Nothing yet','No Natural Show selected','88.4%','2803 K','vibrancy 27','Hub reading matches the last command','Calls for now: 88.4%','Following Natural Show 31779'])
  if(!all.includes(want)) throw Error('zone cards should show '+want+': '+all);
if(text(cards[0]).split('No Natural Show selected').length!==2) throw Error('show status should appear once per card');
if(all.includes('[object Object]')||all.includes('??')) throw Error('typed commands must render as values, not as unknown markers');
if(!document.getElementById('device-note').textContent.startsWith('1 of 2 lamps need attention')) throw Error('note should count lamps needing attention');
const savedHA=haData;haData=null;health(status);haData=savedHA;
if(!text(document.getElementById('summary')).includes('1 of 2 in sync')) throw Error('overview should summarise lamp sync');
if(!document.getElementById('headline').textContent.includes('Office A20')) throw Error('headline should name the lamp out of sync');
deviceView(deviceSnapshot);zonesView({...zoneSnapshot,zones:[{...sitting,lamp:{...sitting.lamp,age_ms:120001}}]});
const descendants=n=>[n,...(n.children||[]).flatMap(descendants)];
if(descendants(zoneCard()).some(n=>n.tag==='circle'&&(n.getAttribute('class')||'').startsWith('lamp ')))throw Error('a stale hub reading must not be plotted at the current show time');
zonesView({zones:[]});if(!text(document.getElementById('zones')).includes('No bridged zones')) throw Error('empty zone list needs a message');
applyHA({available:true,time_zone:'Europe/London',lights:[{entity_id:'light.k',name:'First Floor Kitchen kitchen',zone_id:'1379',serial:null}]});
if(haFor({zone_id:1379,name:'Kitchen'})?.by!=='zone') throw Error('lights should match by Lutron zone ID');
// Direct Zigbee output: the gateway holds commands and never reports them as delivered.
{
  const direct={...status,output_backend:'direct_zigbee',direct_zigbee:{output_admitted:false,qualified_for_deployment:false,security_continuity_ready:false,
    authenticated_readback_ready:false,endpoint_cache_epoch_ready:false,reason:'production_contract_unavailable',pins_configured:false,receiver_started:false,
    hello_online:false,unverified_role:0,uart_bytes:0,frames_ok:0,crc_errors:0,valid_hellos:0,rejected_hellos:0}};
  health(direct);
  if(!view('hero').className.includes('bad')||!(view('headline').textContent+view('headline-detail').textContent).includes('Lighting output held'))throw Error('a held direct Zigbee output must be the headline problem');
  if(!view('node-n4').className.includes('bad')||view('label-n4').textContent!=='Zigbee output'||!view('detail-n4').textContent.includes('held'))throw Error('the topology must show the Zigbee output as held');
  if(!text(view('summary')).includes('Held')||!text(view('health')).includes('Direct Zigbee output (reported)')||!text(view('health')).includes('production_contract_unavailable'))throw Error('overview must report the held backend and its reason');
  if(!text(view('summary')).includes('Not driven'))throw Error('lamps must not be summarised as compared while output is held');
  health({...direct,direct_zigbee:{...direct.direct_zigbee,output_admitted:'true'}});
  if(!view('hero').className.includes('bad'))throw Error('only a boolean admission opens the direct output');
  health({...status,output_backend:'something-new'});
  if(view('hero').className.includes('good')||!text(view('summary')).includes('Unknown'))throw Error('an unrecognised backend must not look healthy');
  health(status);
  if(view('label-n4').textContent!=='N4 output'||text(view('health')).includes('Direct Zigbee output (reported)')||!text(view('summary')).includes('N4 hub'))throw Error('gateways without output_backend write to N4');
  if(text(view('summary')).includes('Zigbee member'))throw Error('no Zigbee member tile without a reported link');
  health({...status,zigbee_uart:{enabled:true,online:false,bidirectional:false,timeouts:3}});
  if(!heroText().includes('Zigbee member link not responding')||!text(view('summary')).includes('Not responding'))throw Error('a silent Zigbee member must be reported');
  health({...status,zigbee_uart:{enabled:true,online:true,bidirectional:true,role:'end_device',state:'disarmed',radio:'off',identity_authenticated:false,rf_membership_verified:false,build_elf_sha256:'a'.repeat(64)}});
  const member=view('summary').children.find(c=>text(c).includes('Zigbee member'));
  if(!member?.className.includes('good')||!text(member).includes('not RF membership'))throw Error('a responding member is a serial link, not RF membership');
  if(!text(view('health')).includes('RF membership verified')||!text(view('health')).includes('Role (claimed)')||!text(view('health')).includes('aaaaaaaaaaaa…'))throw Error('member claims must be labelled as claims');
  health({...status,zigbee_uart:{enabled:false}});
  if(text(view('summary')).includes('Zigbee member')||!text(view('health')).includes('Not enabled'))throw Error('a disabled member link has no tile');
  // Activity: a held command stays held, without a "no progress" timeout.
  const heldGroups=groupEvents([typed(1,'requested',7,{requested:5000}),typed(2,'ketra-held',7,{requested:5000})]);
  health({...direct,gateway_uptime_ms:10_000_000});
  const heldSum=commandSummary(heldGroups[0]),heldSteps=commandSteps(heldGroups[0]);
  if(heldGroups.length!==1||heldSum.label!=='Held at the bridge · direct Zigbee output not admitted'||heldSum.tone!=='warn')throw Error('a held command must group with its request and say it is held: '+heldSum.label);
  if(heldSteps[1][0]!=='To lamps'||heldSteps[1][1]!=='warn'||heldSteps[2][1]!=='skip')throw Error('held steps must not wait for a hub');
  if(!text(stepLevels({stage:'ketra-held',requested:5000})[0]).includes('Held'))throw Error('the held step shows the held level');
  eventView({boot_id:'held',next:3,oldest:1,lost:0,events:[typed(1,'requested',7,{requested:5000}),typed(2,'ketra-held',7,{requested:5000})]});
  document.getElementById('filter-problems').checked=true;renderEvents();
  if(rows().length!==1||!text(rows()[0]).includes('Held at the bridge'))throw Error('held commands belong under Problems only');
  document.getElementById('filter-problems').checked=false;renderEvents();
  // Lamp card: the bridge is holding, and a held output outranks everything else.
  health(direct);deviceView(deviceSnapshot);
  zonesView({...zoneSnapshot,output:{backend:'direct_zigbee',admitted:false},zones:[{...matchZone,output:'direct_zigbee',lamp:null,
    sent:{level_x100:5000,held:true,seq:3,age_ms:1,fade_ms:0,delay_ms:0},checks:[{key:'output',status:'bad',title:'Lighting output held: direct Zigbee not admitted'}],
    verdict:{tone:'bad',title:'Lighting output held: direct Zigbee not admitted'}}]});
  if(!zoneCard().className.includes('bad')||!text(zoneCard()).includes('Bridge holding')||!text(zoneCard()).includes('Not sent')||!text(zoneCard()).includes('no lamp readings'))throw Error('a held lamp card must say the bridge is holding');
  if(!view('device-note').textContent.startsWith('Lighting output is held'))throw Error('the lights note must explain the held output');
  health(status);deviceView(deviceSnapshot);zonesView(zoneSnapshot);
}
// Hybrid build: N4 carrying output while direct Zigbee is unavailable is a fallback.
{
  const fallback={...status,output_backend:'n4',backend:{mode:'hybrid',phase:'n4',switches:2},direct_zigbee:{output_admitted:false,reason:'node_unavailable'},
    direct_routing:{session_ready:false,admitted:false,reason:'node_unavailable',lamps:3,resolved:0,uncertain:1,unreachable:0,external_changes:0,counter_next:'10',counter_upper:'4106'}};
  health(fallback);
  const tileText=text(view('summary').children.find(c=>text(c).includes('Lighting output')));
  if(!tileText.includes('N4 fallback'))throw Error('a hybrid gateway on N4 must show the fallback');
  if(!(view('headline').textContent+view('headline-detail').textContent).includes('N4 is carrying lighting'))throw Error('the fallback must be named in the overview');
  if(!text(view('health')).includes('Direct Zigbee routing (reported)')||!text(view('health')).includes('node unavailable'))throw Error('routing diagnostics must be shown');
  health({...fallback,output_backend:'direct_zigbee',backend:{mode:'hybrid',phase:'direct',switches:3},direct_zigbee:{output_admitted:true,reason:'admitted'}});
  if(text(view('summary')).includes('N4 fallback')||!text(view('summary')).includes('Direct Zigbee'))throw Error('a hybrid gateway on direct shows Direct Zigbee');
  health(status);
  if(text(view('summary')).includes('N4 fallback'))throw Error('an N4-only gateway is not a fallback');
  // The gateway-hosted node session: card, diagnostics and attention items.
  renderZigbeeNode(status);
  if(!view('zigbee-node-card').hidden)throw Error('the node card stays hidden without a gateway-hosted session');
  const mismatch={...fallback,direct_routing:{...fallback.direct_routing,arming:{state:'record_mismatch',enrolled:true,journal_ok:true,
    node_resets:0,reset_configured:true,boot_epoch:'5',committed_upper:'4096'},firmware:{transfer:'idle',offset:0,size:0}}};
  health(mismatch);renderZigbeeNode(mismatch);
  if(view('zigbee-node-card').hidden||view('zigbee-node-pill').textContent!=='Record mismatch (held)'||!view('zigbee-node-pill').className.includes('bad'))
    throw Error('a record mismatch must be shown as held');
  if(!(view('headline').textContent+view('headline-detail').textContent+text(view('summary'))).includes('does not match the gateway journal'))
    throw Error('a record mismatch needs attention in the overview');
  if(!text(view('health')).includes('Node session')||!text(view('health')).includes('Record mismatch'))throw Error('diagnostics show the node session');
  const fresh={...mismatch,direct_routing:{...mismatch.direct_routing,arming:{...mismatch.direct_routing.arming,state:'not_enrolled',enrolled:false},
    firmware:{transfer:'sending',offset:2048,size:4096}}};
  health(fresh);renderZigbeeNode(fresh);
  if(!(view('headline').textContent+view('headline-detail').textContent+text(view('summary'))).includes('not enrolled'))throw Error('an unenrolled node needs attention');
  if(!text(view('zigbee-node-facts')).includes('2,048 of 4,096'))throw Error('the transfer progress is shown');
  const running={...fresh,direct_routing:{...fresh.direct_routing,arming:{...fresh.direct_routing.arming,state:'running',enrolled:true}}};
  health(running);renderZigbeeNode(running);
  if(!view('zigbee-node-pill').className.includes('good')||(view('headline').textContent+view('headline-detail').textContent).includes('enrolled'))
    throw Error('a running node is healthy');
  health(status);
  // Lab inventory import maps zone names to the mapping's zone IDs.
  const lab={schema:'ketra-lab-inventory-v1',captured_at:1,source_sha256:'0'.repeat(64),zones:{
    Office:{group:22,members:[{eui_wire:'0102030405060708',logical:{pan:5,logical:40,interface:0,channel:0,subchannel:0}}]},
    Nursery:{group:23,members:[{eui_wire:'1112131415161718',logical:{pan:5,logical:41,interface:0,channel:0,subchannel:0}}]}}};
  const converted=labInventoryToKetra(lab,[{zone_id:30861,name:'office'}]);
  if(converted.zones.length!==1||converted.zones[0].zone_id!==30861||converted.zones[0].group.logical!==22||converted.zones[0].group.pan!==5||
     converted.unmatched.join()!=='Nursery'||converted.zones[0].lamps[0].logical.logical!==40)throw Error('lab inventory conversion: '+JSON.stringify(converted));
  let threw=false;try{labInventoryToKetra({schema:'other'},[])}catch(e){threw=true}
  if(!threw)throw Error('only lab inventory files convert');
  // The Ketra card shows each lamp's live state.
  ketraData={generation:4,hash:9,source:{pan:5,logical:17,interface:0,channel:0,subchannel:0},unmatched_zones:0,zones:[
    {zone_id:30861,group:{pan:5,logical:22},lamps:[{eui_wire:'0102030405060708',logical:{pan:5,logical:40,interface:0,channel:0,subchannel:0}},
                                              {eui_wire:'1112131415161718',logical:{pan:5,logical:41,interface:0,channel:0,subchannel:0}}]}],
    lamps_live:[{eui_wire:'0102030405060708',resolved:true,short_address:4660,uncertain:false,unreachable:false,external_change:false,power:true,level_x100:4500},
                {eui_wire:'1112131415161718',resolved:true,short_address:4661,uncertain:true,unreachable:false,external_change:false}]};
  renderKetra();
  const card=text(view('ketra-zones'));
  if(!card.includes('On 45%')||!card.includes('0x1234')||!card.includes('Uncertain')||view('ketra-pill').textContent!=='2 lamps')throw Error('Ketra card: '+card);
  if(!view('ketra-json').value.includes('"zone_id": 30861'))throw Error('the editor shows the inventory');
  ketraData={unsupported:true};renderKetra();
  if(view('ketra-pill').textContent!=='Not supported'||!view('ketra-apply').disabled)throw Error('old gateways have no Ketra inventory');
}
`;
const mappingChecks=`
(async()=>{
  // Settle the initial background loads before simulating an independent editor.
  for(let i=0;i<30;i++)await Promise.resolve();
  if(refreshing)throw Error('initial refresh did not settle');
  {
    const observed={id:1,kind:'ccx.local_level',source:{serial:71839249,zone_id:1594},source_event:{boot_id:3,sequence:4},
      fields:{level_x100:0},gateway_received_us:7000,timing:{source_rf_sfd_us:100,source_decode_done_us:300,source_uart_tx_us:500,
      source_to_gateway_us:900,gateway_decided_us:7300,gateway_submitted_us:7500}};
    ccxEventView({schema_version:1,boot_id:5,cursor:1,next:99,events:[observed],stream:{state:'online',missed:0},mqtt:{online:true,sent_to_socket:1,dropped:0,last_queue_us:100}});
    if(ccxCursor!==1||ccxRows.length!==1||view('ccx-stream-pill').textContent!=='Live')throw Error('CCX pagination follows delivered cursor, never producer next');
    let timing=view('ccx-latency').textContent;
    if(!timing.includes('C6 RF → decode 200 µs')||!timing.includes('Gateway receive → decision 300 µs')||timing.includes('C6 RF → gateway'))throw Error('clock domains are kept separate and mapped timing needs uncertainty: '+timing);
    observed.timing.clock_uncertainty_us=50;
    timing=ccxTiming(observed,null);
    if(!timing.includes('C6 RF → gateway 900 µs ± 50 µs'))throw Error('aligned latency requires and shows uncertainty: '+timing);
    ccxClock={boot_id:5,utc_anchor:{synced:true,mono_us:6000,unix_us:1700000000000000},monitoring:{age_s:1}};ccxClockAt=Date.now();
    if(ccxEventWall(observed)!==1700000000001||ccxEventWall({...observed,gateway_received_us:5999})!==null||ccxEventWall({...observed,gateway_received_us:null,stage_at_us:7000})!==1700000000001)throw Error('gateway wall time uses only a current, same-boot UTC anchor covering the event');
    ccxClock.boot_id=6;if(ccxEventWall(observed)!==null)throw Error('a clock from another boot cannot date an event');
    if(!text(view('ccx-events')).includes('71839249')||!text(view('ccx-events')).includes('level_x100'))throw Error('the generic inspector preserves decoded and raw fields');
    ccxEventView({schema_version:1,boot_id:6,cursor:0,next:1,events:[],stream:{state:'unsupported'}});
    if(ccxRows.length||view('ccx-stream-pill').textContent!=='Not supported')throw Error('new boot resets the CCX view and old gateways remain usable');
    {const prior=replies.status;replies.status={listener:{online:true},clock:{synced:true,uncertainty_us:12},monitoring:{age_s:100}};replies.clock={boot_id:6,utc_anchor:{synced:true},monitoring:{age_s:1}};ccxHealthAt=0;await loadCcxHealth();
      if(!view('ccx-clock-note').textContent.includes('stale'))throw Error('stale listener status must not claim the clock is currently aligned');
      replies.status.monitoring.age_s=1;ccxHealthAt=0;await loadCcxHealth();if(!view('ccx-clock-note').textContent.includes('aligned ± 12 µs'))throw Error('fresh listener status shows measured clock uncertainty');replies.status=prior}
    replies.rules={rules:[{id:'dining-touchbar',enabled:false,match:{kind:'ccx.local_level',serial:71839249},action:{type:'set_level',target_zone:38138,level:'event'}}]};
    await loadCcxRules();
    if(ccxRules[0].enabled||view('ccx-save-rules').disabled!==true)throw Error('loading a disabled rule must not enable it or create a change');
    ccxRules.push(ccxDraftRule({id:'show',enabled:false,match:{},action:{type:'select_show',target_zone:38138,show_id:31779}}));
    const candidate=ccxRulePayload();
    if(candidate.errors.length||candidate.rules[1].action.show_id!==31779||'level' in candidate.rules[1].action||Object.keys(candidate.rules[1].match).length)throw Error('Show default level and match-all rules must round trip');
    {const prior=ccxRules;ccxRules=[ccxDraftRule({id:'odd',enabled:false,match:{kind:''},action:{type:'set_level',target_zone:1,level:0}})];if(ccxRulePayload().rules[0].match.kind!=='')throw Error('an exact empty kind from the gateway must not silently become match-all');ccxRules=prior}
    ccxRules[1].show_id='0';if(!ccxRulePayload().errors.length)throw Error('Show ID zero must be rejected');
    ccxRules[1].show_id='31779';renderCcxRules();requests.length=0;await view('ccx-save-rules').onclick();
    const put=requests.find(r=>r.key==='rules'&&r.opts?.method==='PUT');
    if(!put||JSON.parse(put.opts.body).rules[0].enabled!==false||JSON.parse(put.opts.body).rules[1].action.show_id!==31779)throw Error('saving multiple rules preserves the disabled Dining rule');
    if(!view('perf-note').textContent.includes('cache requests')&&pollMs!==null)throw Error('browser cache duration must not be labelled gateway latency');
  }
  const realNow=Date.now;let now=realNow();Date.now=()=>now;
  try{
    const devices={config_hash:1,monitoring:{age_s:0,delayed:false},zones:[{zone:1,confirmed:{at_ms:9999,level_x100:5000}}]};
    replies.status={...status};replies.devices=devices;replies.zones=zoneSnapshot;health({...status});deviceView(devices);zonesView(zoneSnapshot);health(latestStatus);
    if(!zoneCard().className.includes('good'))throw Error('test setup must start with a fresh lamp comparison');
    let release;waits.set('devices',new Promise(resolve=>{release=resolve}));
    const delayed=refresh();for(let i=0;i<10;i++)await Promise.resolve();now+=30000;waits.delete('devices');release();await delayed;
    if(view('hero').className.includes('good')||zoneCard().className.includes('good')||!view('headline').textContent.includes('Monitoring delayed'))throw Error('a fast status response must age while a parallel device request is pending');
    for(const failed of ['devices','status','zones']){
      health({...status});deviceView(devices,now);zonesView(zoneSnapshot,now);health(latestStatus);failKeys.add(failed);await refresh();failKeys.delete(failed);
      if(view('hero').className.includes('good')||zoneCard().className.includes('good')||!cardVerdict().textContent.includes('unavailable'))throw Error('a failed '+failed+' request must invalidate the existing comparison');
    }
    for(const malformed of ['status','devices','zones']){
      health({...status});deviceView(devices,now);zonesView(zoneSnapshot,now);health(latestStatus);
      const saved=replies[malformed];replies[malformed]=null;await refresh();replies[malformed]=saved;
      if(view('hero').className.includes('good')||zoneCard().className.includes('good'))throw Error('a malformed '+malformed+' response must invalidate prior green status');
    }
    firmwarePending=true;firmwareBaselineUptime=30;firmwareStartedAt=now;firmwareOfflineSeen=true;firmwareReconnectAt=now-31000;
    replies.status={...status,monitoring:{age_s:9,delayed:true}};await refresh();
    if(!firmwarePending)throw Error('stale status must not complete a firmware stability watch');
    replies.status={...status,endpoint_online:false};await refresh();
    if(!firmwarePending||firmwareReconnectAt!==0)throw Error('an offline endpoint must reset the firmware stability watch');
    replies.status={...status,endpoint_uptime_s:31};await refresh();
    if(!firmwarePending||firmwareReconnectAt!==0||view('fw-step-restart').getAttribute('data-state')==='done'||!view('upload-note').textContent.includes('does not establish a restart'))throw Error('offline then online with increasing uptime must not establish a restart');
    replies.status={...status,endpoint_uptime_s:5};await refresh();
    if(view('fw-step-restart').getAttribute('data-state')!=='done')throw Error('a fresh uptime decrease should be shown as observed');
    now+=31000;await refresh();
    if(firmwarePending||!view('upload-note').textContent.includes('does not verify continuous connection'))throw Error('firmware observation must remain qualified');
  }finally{Date.now=realNow;failKeys.clear();waits.clear()}
  const initial={generation:4,hash:44,editable:true,apply_status:'active',zones:[{zone_id:1,name:'Office'}]};
  const changed={generation:5,hash:55,editable:true,apply_status:'active',zones:[{zone_id:1,name:'Other edit'}]};
  replies.config=initial;await loadConfig(true);draftZones[0].z.name='Draft';editorChanged(true);
  latestStatus.diagnostics={maintenance:true};requests.length=0;await view('save-config').onclick();
  const put=requests.find(r=>r.key==='config'&&r.opts?.method==='PUT');
  if(!put)throw Error('a valid draft should be sent');
  const payload=JSON.parse(put.opts.body);
  if(payload.generation!==4||payload.hash!==44||payload.zones[0].name!=='Draft')throw Error('the draft must be submitted with its original generation and hash');
  replies.config=initial;await loadConfig(true);draftZones[0].z.name='My unsaved edit';editorChanged(true);
  replies.config=changed;await loadConfig();await loadConfig();
  if(draftFrom.generation!==4||draftFrom.hash!==44||draftZones[0].z.name!=='My unsaved edit')throw Error('refresh must preserve the original baseline and unsaved draft');
  if(!view('save-config').disabled||!view('config-errors').textContent.includes('changed while you were editing')||!view('config-state').textContent.includes('changed while you were editing'))throw Error('a stale draft must remain blocked and visibly flagged on every refresh');
  requests.length=0;await view('save-config').onclick();
  if(requests.some(r=>r.key==='config'&&r.opts?.method==='PUT'))throw Error('a stale draft must not be submitted even by a direct click handler');
  replies.config=initial;await loadConfig(true);draftZones[0].z.name='Keep my edit';editorChanged(true);
  replies.config={...initial,hash:45,zones:[{zone_id:1,name:'Office'},{zone_id:2,name:'Sitting Room'},{zone_id:3,name:'Nursery'}]};
  await loadConfig();await loadConfig();
  if(draftFrom.hash!==44||draftFrom.generation!==4||draftZones[0].z.name!=='Keep my edit'||!view('save-config').disabled||!view('config-errors').textContent.includes('changed while you were editing'))throw Error('same generation with a different hash must preserve and block the stale draft');
  await loadConfig(true);view('config-json').value='{unfinished';view('config-json').oninput();
  replies.config={...changed,generation:6,hash:66};await loadConfig();
  if(view('config-json').value!=='{unfinished'||!isDirty()||!view('save-config').disabled)throw Error('an invalid JSON draft must survive refresh and remain unsaved');
  // Endpoint control: disable is always reachable; enable and clear need current evidence.
  const sent=key=>requests.filter(r=>r.key===key&&r.opts?.method);
  health(status);
  if(view('endpoint-toggle').disabled||!view('endpoint-toggle').textContent.includes('Disable')||!view('endpoint-clear').hidden)throw Error('an enabled endpoint should offer disable only');
  if(!view('endpoint-control-note').textContent.includes('Enabled'))throw Error('the control card must state the operator choice');
  health({...status,monitoring:{age_s:30,delayed:true}});
  if(view('endpoint-toggle').disabled)throw Error('disable must stay available while monitoring is stale');
  requests.length=0;replies.power={...status.endpoint_power,enabled:false};await view('endpoint-toggle').onclick();
  const off=sent('power')[0];
  if(!off||off.opts.method!=='PUT'||JSON.parse(off.opts.body).enabled!==false)throw Error('disable must PUT enabled:false');
  if(latestStatus.endpoint_power.enabled!==false||!view('endpoint-toggle').textContent.includes('Enable'))throw Error('the gateway reply must update the control at once');
  const disabled=safetyPatch({phase:'operator-stopped'},{enabled:false});
  health({...disabled,monitoring:{age_s:30,delayed:true}});
  if(!view('endpoint-toggle').disabled)throw Error('enable must need a current snapshot');
  health(disabled);
  if(view('endpoint-toggle').disabled||!view('endpoint-control-note').textContent.includes('reports its radio stopped'))throw Error('a fresh disabled snapshot should allow enable and report the radio stop');
  const latched=safetyPatch({phase:'fault-latched',faults:1,r1_role_fault:true},{enabled:false,violation:'role'});
  health(latched);
  if(view('endpoint-clear').hidden||view('endpoint-clear').disabled)throw Error('a disabled, latched endpoint should offer the clear');
  requests.length=0;await view('endpoint-clear').onclick();
  const clear=sent('clear')[0];
  if(!clear||clear.opts.method!=='POST'||JSON.parse(clear.opts.body).confirm!=='clear-r1-latch')throw Error('the clear must send the exact confirmation');
  for(const s of [safetyPatch({phase:'fault-latched',faults:1,r1_role_fault:true},{enabled:true}),{...latched,monitoring:{age_s:30,delayed:true}},safetyPatch({phase:'fault-latched',faults:1,r1_role_fault:true},{enabled:false,recovering:true,recovery_state:'waiting-ack'})]){
    health(s);if(!view('endpoint-clear').hidden)throw Error('the clear must not be offered while enabled, stale or already recovering');
    requests.length=0;await view('endpoint-clear').onclick();if(sent('clear').length)throw Error('the clear handler must re-check eligibility');
  }
  health(safetyPatch({},{recovery_state:'timeout'}));
  if(!view('endpoint-control-note').textContent.includes('did not complete'))throw Error('a failed clear must be reported');
  health({...status,endpoint_power:undefined});
  if(!view('endpoint-toggle').disabled||!view('endpoint-control-note').textContent.includes('does not report endpoint control'))throw Error('old firmware must not show controls');
  // Why no path owns output yet (backend.hold_reason), additive on newer gateways.
  const routing={session_ready:true,admitted:false,reason:'held',node_known:true,joined:true,lease_ok:true,table_committed:true,lamps:3,routed_zones:3,resolved:3};
  const routingRows=s=>{const g=routingGroup(s);return Object.fromEntries(g.children[1].children.map(r=>[r.children[0].textContent,r.children[1]]))};
  const heldStatus={...status,output_backend:'n4',direct_routing:routing,endpoint_power:{...status.endpoint_power,boot_hold_remaining_s:90},
    backend:{mode:'hybrid',phase:'waiting_epoch_drain',switches:0,hold_reason:'endpoint_admission',admission:'boot_hold',epoch_stage:'idle',epoch_frames_sent:0,output_owner:'none'}};
  let hold=backendHold(heldStatus);
  if(!hold||hold.reason!=='endpoint_admission'||!hold.text.includes('boot hold: 90 s left')||hold.owned)throw Error('a boot hold must be named with its countdown and not count as owned');
  let r2=routingRows(heldStatus);
  if(r2['Output owner'].textContent!=='none'||!r2['Output owner'].className.includes('warn')||!r2['Hold reason'].textContent.includes('boot hold: 90 s left')||
     r2['Endpoint admission'].textContent!=='boot hold'||r2['Epoch handshake'].textContent!=='idle'||r2['Epoch frames sent'].textContent!=='0')throw Error('routing diagnostics must show the hold fields');
  const owned={...heldStatus,backend:{...heldStatus.backend,phase:'direct',hold_reason:'none',admission:'ok',epoch_stage:'active',epoch_frames_sent:4,output_owner:'direct'}};
  if(backendHold(owned)!==null)throw Error('no hold means no hold note');
  r2=routingRows(owned);
  if(r2['Hold reason'].textContent!=='None'||r2['Output owner'].textContent!=='direct'||r2['Endpoint admission'].textContent!=='ok')throw Error('an owned output must read as healthy');
  const older={...status,output_backend:'n4',direct_routing:routing,backend:{mode:'hybrid',phase:'n4',switches:1}};
  if(backendHold(older)!==null||'Output owner' in routingRows(older))throw Error('gateways without the fields must be unchanged');
  for(const bad of [{hold_reason:7},{hold_reason:'none'},null,'x',{}])if(backendHold({...status,backend:bad})!==null)throw Error('malformed or absent hold data must be ignored');
  health(heldStatus);
  if(!heroText().includes('Lighting is not being delivered yet: the gateway is not yet admitting endpoint commands'))throw Error('the overview must warn while no path owns output');
  health(owned);
  if(heroText().includes('Lighting is not being delivered yet'))throw Error('the warning must clear once a path owns output');
  health(heldStatus);
  if(heroText().includes('N4 is carrying lighting')||!view('headline').textContent.length)throw Error('during the hold the fallback line must not claim N4 is carrying lighting');
  health({...heldStatus,backend:{...heldStatus.backend,hold_reason:'none',output_owner:'n4',phase:'n4'}});
  if(!heroText().includes('N4 is carrying lighting'))throw Error('a real fallback with an owned output must still be reported');
  if(backendHold({...heldStatus,backend:{...heldStatus.backend,hold_reason:'constructor'}}).text!=='constructor')throw Error('reason names must not resolve through object prototypes');
  // Startup sequence on the overview.
  const startup=()=>{const all=n=>[n,...(n.children||[]).flatMap(all)],g=all(view('health')).find(c=>c.children?.[0]?.textContent==='Startup sequence'&&c.children?.[1]?.className==='list');if(!g)throw Error('startup sequence missing');return Object.fromEntries(g.children[1].children.map(r=>[r.children[0].textContent,r.children[1]]))};
  const windowRow='Stability window (≥ 300 s, receive-only)';
  health(safetyPatch({phase:'start-delay',gate_remaining_s:120,gate_adverts:0},{boot_hold_remaining_s:40}));
  let rows=startup();
  if(rows['Gateway startup hold'].textContent!=='40 s remaining'||rows['Minimum start delay'].textContent!=='120 s remaining'||rows[windowRow].textContent!=='Pending'||rows['Command admission'].textContent!=='Closed')throw Error('start delay must show both countdowns and later steps pending');
  health(safetyPatch({phase:'listening',gate_remaining_s:0,gate_adverts:2}));rows=startup();
  if(rows['Minimum start delay'].textContent!=='Done'||!rows[windowRow].textContent.includes('2 of 3+ router adverts')||!rows[windowRow].className.includes('warn'))throw Error('the stability window must show the advert count');
  health(status);rows=startup();
  if(rows['Attaching as a child'].textContent!=='Done'||rows['Command admission'].textContent!=='Open')throw Error('an attached endpoint should show every step done');
  health(safetyPatch({phase:'regating'}));rows=startup();
  if(!rows['Endpoint phase'].textContent.includes('Regating')||rows['Command admission'].textContent!=='Closed')throw Error('regating must be explicit and keep admission closed');
  health({...status,monitoring:{age_s:30,delayed:true}});rows=startup();
  if(rows['Endpoint'].textContent!=='Current state unknown'||'Command admission' in rows)throw Error('stale snapshots must not show endpoint startup progress');
  // Stability window estimate, from endpoint uptime.
  const at=(phase,uptime,adverts)=>{health({...safetyPatch({phase,gate_remaining_s:0,gate_adverts:adverts}),endpoint_uptime_s:uptime});return startup()[windowRow].textContent};
  at('start-delay',290,0);
  if(!at('listening',300,0).startsWith('About 300 s remaining'))throw Error('the window must start exactly when listening follows the start delay');
  if(!at('listening',420,2).startsWith('About 180 s remaining'))throw Error('the window must count down with endpoint uptime');
  if(!at('listening',450,1).startsWith('About 300 s remaining'))throw Error('a drop in router adverts must restart the window');
  if(!at('listening',760,3).startsWith('5 min elapsed'))throw Error('an elapsed window must say it is waiting for evidence');
  if(!at('listening',20,0).startsWith('Up to 300 s remaining'))throw Error('a reboot seen mid-listening must give only an upper bound');
  at('attached',400,3);
  if(!at('listening',410,0).startsWith('Up to 300 s remaining'))throw Error('listening not preceded by the start delay is an upper bound');
  // Diagnostics: sections in command order, flagged rows listed first, no-hold builds not flagged.
  const tree=n=>[n,...(n.children||[]).flatMap(tree)];
  health({...status,uart_crc_errors:3,uart_frames_ok:997,diagnostics:{maintenance:false,heap_free:182000,sockets_used:19,socket_limit:20,reset_reason:4}});
  const secs=view('health').children;
  if(secs.length!==5||!secs.every(x=>x.className.startsWith('dsec')))throw Error('diagnostics must be one section per part of the bridge');
  const titles=secs.map(x=>text(x.children[0]));
  for(const want of ['Thread & endpoint','Endpoint ↔ gateway link','Gateway','Lighting output','Home Assistant'])if(!titles.some(t=>t.includes(want)))throw Error('missing diagnostics section '+want);
  const att=text(view('diag-attention'));
  if(!att.includes('CRC errors: 3')||!att.includes('Sockets in use')||!att.includes('Crash (panic)'))throw Error('flagged rows must be listed first: '+att);
  if(att.includes('Physical hold available'))throw Error('a build without a configured hold must not be flagged for it');
  if(!secs[1].open||!secs[1].className.includes('warn')||secs[0].open)throw Error('only sections with something flagged open themselves');
  if(!view('diag-pill').textContent.endsWith('to check'))throw Error('the diagnostics pill counts flagged rows');
  {const ha=haData,dev=lastDevices;haData={lights:[],integrations:['lutron'],time_zone:'UTC'};lastDevices={...dev,zones:[]};health(status);
  if(!text(view('diag-attention')).includes('Nothing flagged')||view('diag-pill').textContent!=='All normal')throw Error('a clean snapshot says so');haData=ha;lastDevices=dev}
  // Performance: hop timings from the event feed and the gateway's own timers.
  latestStatus={...status,puts_ok:99,puts_failed:1,diagnostics:{maintenance:false,accepted_count:5,acceptance_last_ms:150,acceptance_p95_ms:300,acceptance_max_ms:400,last_command_ms:210,max_command_ms:900,delivery_failed:0}};
  eventView({boot_id:'perf',boot_epoch_ms:Date.now()-100000,next:20,next_sequence:20,oldest:1,lost:0,events:[
    {id:1,ts_ms:1000,stage:'requested',zone:1,requested:5000,command_seq:1,schema_version:2},{id:2,ts_ms:1200,stage:'applied',zone:1,requested:5000,command_seq:1,schema_version:2},
    {id:3,ts_ms:1900,stage:'observed',zone:1,confirmed:5000,requested:5000},{id:4,ts_ms:2100,stage:'report-sent',zone:1,reported:5000,command_seq:1,report_seq:1,schema_version:2},
    {id:5,ts_ms:5000,stage:'requested',zone:2,requested:0,command_seq:2,schema_version:2},{id:6,ts_ms:9000,stage:'delivery-failed',zone:2,requested:0,command_seq:2,schema_version:2}]});
  const ptext=text(view('perf-stats'));
  for(const want of ['Lutron → hub accepted','200','Lutron → lamp reading','900','Round trip back to Lutron','1.10','Hub API response','Gateway queue → hub','99.0'])if(!ptext.includes(want))throw Error('performance should show '+want+': '+ptext);
  const wf=view('perf-waterfall').children;
  if(wf.length!==2||!text(wf[0]).includes('Failed')||!text(wf[1]).includes('1.10 s'))throw Error('the waterfall lists recent commands newest first with their totals');
  if(tree(wf[1]).filter(n=>/^seg-/.test(n.className)).map(n=>n.className).join()!=='seg-hub,seg-lamp,seg-back')throw Error('waterfall segments follow the hops in time order');
  if(view('perf-count').textContent!=='1 command timed · 1 without timing')throw Error('performance counts only commands with a timed hop: '+view('perf-count').textContent);
  {const n4=groupEvents([
    {id:1,ts_ms:1000,stage:'requested',zone:1,command_seq:7,schema_version:2},
    {id:2,ts_ms:1100,stage:'applied',zone:1,command_seq:7,schema_version:2},
    {id:3,ts_ms:1400,stage:'correction',zone:1,command_seq:7,schema_version:2}]);
    if(commandTimings(n4)[0].ms('lamp')!==400)throw Error('N4 corrections retain their existing timing behavior')}
  // Lights: level rail markers, show chart coloured by CCT, glance strip.
  zonesView({config_hash:1,zones:[sitting,{...sitting,zone:2,name:'Den'}],time_zone:'America/Los_Angeles',home_assistant:true,monitoring:{age_s:0,delayed:false}});
  const card=view('zones').children[0],nodes=tree(card);
  if(!nodes.some(n=>n.className==='rail-mk mk-lutron hi')||!nodes.some(n=>(n.className||'').startsWith('rail-mk mk-show'))||!nodes.some(n=>n.className==='rail-mk mk-sent'))throw Error('the rail marks Lutron, the bridge and the show');
  if(!nodes.some(n=>n.tag==='stop'&&n.getAttribute('stop-color')===kelvinRGB(1800)))throw Error('the show chart is coloured by the show colour temperature');
  if(!nodes.some(n=>n.className==='show-readout'))throw Error('the show chart has a readout');
  if(view('zones').children.length!==2||!text(view('zones').children[1].children[0]).includes('Den')||card.tag!=='details')throw Error('each lamp is one compact row that opens for details');
  // Direct Zigbee: lamp readings count for 7 minutes and are compared like N4 readings.
  {const z={...matchZone,output:'direct_zigbee',stale_ms:420000,lamp:{...matchZone.lamp,age_ms:300000},checks:[{key:'output',status:'info',title:'Direct Zigbee output'},...matchZone.checks]};
    deviceView(deviceSnapshot);zonesView({...zoneSnapshot,zones:[z]});
    if(!zoneCard().className.includes('good')||cardVerdict().textContent!=='In sync')throw Error('a direct Zigbee reading must be compared with Lutron: '+cardVerdict().textContent);
    zonesView({...zoneSnapshot,zones:[{...z,lamp:{...z.lamp,age_ms:430000}}]});
    if(zoneCard().className.includes('good')||cardVerdict().textContent!=='No recent lamp reading')throw Error('an old Zigbee reading is not current: '+cardVerdict().textContent);
    zonesView({...zoneSnapshot,zones:[{...z,lamp:null,verdict:{tone:'wait',title:'Waiting for a lamp reading'}}]});
    if(cardVerdict().textContent!=='Waiting for a lamp reading')throw Error('a direct lamp without a reading waits for one');
    zonesView({...zoneSnapshot,zones:[{...z,lutron:{...z.lutron,level_x100:7000},verdict:{tone:'warn',title:'Lamp at 50%, Lutron shows 70%'},checks:[{key:'lutron',status:'warn',title:'Lamp at 50%, Lutron shows 70%'}]}]});
    if(!zoneCard().className.includes('warn')||!cardVerdict().textContent.includes('Lutron shows 70%'))throw Error('a lamp out of step shows both levels in its row')}
  // Response times on direct Zigbee: no N4 figures, Zigbee labels, routing counters, and a reason when nothing can be timed.
  {const direct={...status,output_backend:'direct_zigbee',direct_zigbee:{output_admitted:true},direct_routing:{sends:40,applied:12,observed:30,unrouted:9,unreachable:3},puts_ok:99,puts_failed:1,diagnostics:{maintenance:false,accepted_count:5,acceptance_last_ms:150}};
    latestStatus=direct;eventView({boot_id:'zb',boot_epoch_ms:Date.now()-100000,next:5,next_sequence:5,oldest:1,lost:0,events:[
      {id:1,ts_ms:1000,stage:'requested',zone:1,requested:5000,command_seq:1,schema_version:2},{id:2,ts_ms:4000,stage:'observed',zone:1,confirmed:5000,requested:5000},
      {id:3,ts_ms:4500,stage:'report-sent',zone:1,reported:5000,command_seq:1,report_seq:1,schema_version:2}]});
    const z=text(view('perf-stats'));
    if(z.includes('Lutron → lamp reading')||!z.includes('Round trip back to Lutron')||!z.includes('Verified zone commands')||!z.includes('40 send requests, including polls')||!z.includes('30 zone readbacks')||!z.includes('9 without lamps')||!z.includes('3 lamps unreachable')||!z.includes('Command count excludes native Shows')||z.includes('Hub API response')||z.includes('Gateway queue'))throw Error('direct Zigbee performance: '+z);
    const aggregate=view('perf-stats').children.find(n=>text(n).includes('Verified zone commands'));
    if(aggregate.className!=='stat wait'||text(aggregate).includes('%'))throw Error('routing counts must be neutral, without a delivery ratio');
    if(commandSteps(groupEvents(eventRows).find(g=>g.kind==='command'))[2][1]!=='pending')throw Error('an uncorrelated reading is not a direct command readback');
    eventView({boot_id:'zb2',boot_epoch_ms:Date.now()-100000,next:2,next_sequence:2,oldest:1,lost:0,events:[{id:1,ts_ms:1000,stage:'requested',zone:1,requested:5000,command_seq:1,schema_version:2}]});
    if(!text(view('perf-stats')).includes('no command hop can be timed')||!text(view('perf-stats')).includes('Shows are sampled periodically')||text(view('perf-stats')).includes('firmware'))throw Error('an untimeable feed explains sampled Shows without implying a firmware problem: '+text(view('perf-stats')));
    if(view('perf-count').textContent!=='0 commands timed · 1 without timing'||!view('perf-note').textContent.includes('Missing confirmation does not imply a delay or failure'))throw Error('missing timing is counted and explained neutrally');
    latestStatus=status}
  // Show retags have no per-command lamp readback, even when a later periodic reading has the same zone.
  {latestStatus={...status,output_backend:'direct_zigbee',direct_zigbee:{output_admitted:true},direct_routing:{sends:1,applied:1,observed:1,unrouted:0}};
    const c=(id,ts,stage,seq,zone=1)=>({id,ts_ms:ts,stage,zone,requested:5000,confirmed:-1,command_seq:seq,schema_version:2});
    const show=[c(1,1000,'native-requested',41),c(2,1000,'native-pickup',41),c(3,1005,'selection-ready',41),
      {id:4,ts_ms:30672,stage:'observed',zone:1,requested:-1,confirmed:5000}];
    eventView({boot_id:'show',next:5,oldest:1,lost:0,events:show});
    let g=groupEvents(eventRows).find(x=>x.kind==='command'),t=commandTimings([g])[0],steps=commandSteps(g);
    if(t.marks.length||t.ms('lamp')!==null||steps[1][1]!=='pending'||steps[2][1]!=='pending'||commandSummary(g).label!=='Selection checked · lamp submission not recorded')throw Error('a 29.672 s Show sample must not imply lamp submission or confirmation');
    if(view('perf-count').textContent!=='0 commands timed · 1 without timing'||!text(view('perf-stats')).includes('their readbacks do not measure per-command latency'))throw Error('a Show sample is not timed as a command');
    eventView({boot_id:'show-multiple',next:20,oldest:1,lost:0,events:[...show.slice(0,3),
      c(4,2000,'native-requested',42),c(5,2000,'native-pickup',42),c(6,2005,'selection-ready',42),{...show[3],id:7},
      c(8,30680,'applied',null),c(9,30690,'applied',99),{...c(10,30700,'correction',42),confirmed:4000},
      c(11,32000,'native-requested',43),c(12,32020,'ketra-dispatch',43),c(13,32250,'applied',43)]});
    const groups=groupEvents(eventRows).filter(x=>x.kind==='command'&&x.events.some(e=>e.stage==='native-requested'));
    const times=commandTimings(groups);
    if(times.length!==3||times[0].ms('lamp')!==null||times[1].ms('lamp')!==null||times[2].ms('lamp')!==250)throw Error('only the matching applied confirms a same-zone request: '+times.map(x=>x.ms('lamp')));
    if(commandSteps(groups[0])[2][1]!=='pending'||commandSteps(groups[1])[2][1]!=='warn'||commandSummary(groups[1]).tone!=='warn'||commandSteps(groups[2])[2][1]!=='good')throw Error('missing confirmations wait, corrections warn, and matched applied confirms');
    if(view('perf-count').textContent!=='1 command timed · 2 without timing')throw Error('multiple Show commands count only marked timings: '+view('perf-count').textContent);
    // Exercise the correlation checks directly as a defense against permissive event grouping or incomplete feeds.
    const req=c(1,1000,'native-requested',5),valid=c(2,1200,'applied',5),group=(request,applied)=>({kind:'command',zone:1,events:[request,applied],stages:new Set([request.stage,applied.stage])});
    for(const bad of [{...valid,command_seq:null},{...valid,command_seq:6},{...valid,zone:2},{...valid,ts_ms:999},{...valid,ts_ms:null}]){
      g=group(req,bad);if(directApplied(g)||commandTimings([g])[0].ms('lamp')!==null||commandSteps(g)[2][1]==='good'||commandSummary(g).label.includes('Lamps at the commanded level'))throw Error('invalid applied event must not confirm a command: '+JSON.stringify(bad))}
    g=group({...req,command_seq:null},valid);if(directApplied(g)||commandTimings([g])[0].ms('lamp')!==null)throw Error('the request must have a sequence too');
    g=group(req,valid);if(directApplied(g)!==valid||commandTimings([g])[0].ms('lamp')!==200)throw Error('a correlated applied event remains a valid timing');
    latestStatus=status}
  // Native Ketra path (current direct gateways): native-requested starts the command; applied is the lamps' readback.
  {latestStatus={...status,output_backend:'direct_zigbee',direct_zigbee:{output_admitted:true},direct_routing:{sends:1,applied:1,observed:0,unrouted:0}};
    const c=(id,ts,stage,requested,confirmed)=>({id,ts_ms:ts,stage,zone:1,requested,confirmed,command_seq:5,schema_version:2});
    eventView({boot_id:'native',boot_epoch_ms:Date.now()-100000,next:20,next_sequence:20,oldest:1,lost:0,events:[
      c(1,1000,'native-requested',5000,-1),c(2,1010,'native-busy',3,40),c(3,1012,'native-pickup',5000,-1),c(4,1015,'selection-get',5000,-1),c(5,1100,'selection-ready',5000,-1),
      c(6,1105,'native-stream',5000,-1),c(7,1110,'native-queued',5000,-1),c(8,1115,'readback-policy',1,3),c(9,1150,'ketra-dispatch',5000,-1),c(10,1160,'ketra-uart',5000,-1),
      {id:11,ts_ms:1200,stage:'ketra-query',zone:1,requested:-1,confirmed:-1},c(12,1900,'applied',5000,5000),c(13,1950,'confirmed',-1,5000),
      {id:14,ts_ms:2300,stage:'report-sent',zone:1,reported:5000,command_seq:5,report_seq:9,schema_version:2},
      {id:15,ts_ms:5000,stage:'native-requested',zone:2,requested:2000,command_seq:6,schema_version:2},{id:16,ts_ms:5001,stage:'epoch-dropped',zone:2,requested:2000,command_seq:6,schema_version:2}]});
    const groups=groupEvents(eventRows).filter(g=>g.kind==='command');
    if(groups.length!==2||groups[0].events.length!==13)throw Error('a native command and all its steps form one row: '+groups.map(g=>g.events.length));
    const sum=commandSummary(groups[0]);if(sum.label!=='Reported to Lutron'||sum.req!==5000||sum.title!=='Set to 50%')throw Error('native command summary: '+JSON.stringify(sum));
    const steps=commandSteps(groups[0]).map(x=>x[1]).join();if(steps!=='good,good,good,good')throw Error('every native step is done: '+steps);
    const drop=commandSummary(groups[1]);if(drop.tone!=='bad'||!drop.label.includes('handshake'))throw Error('a gateway drop ends the command as a problem: '+drop.label);
    const z=text(view('perf-stats'));
    for(const want of ['Lutron → sent to lamps','150','Lutron → lamp reading','900','Round trip back to Lutron','1.30'])if(!z.includes(want))throw Error('native timings should show '+want+': '+z);
    if(stageInfo('applied')[0]!=='Every lamp at the commanded level')throw Error('applied means lamp readback on direct Zigbee');
    latestStatus=status}
  // Zone programming: members get names, presets read as what they do, show levels as percentages.
  {renderProgram({zone:1,programmed:true,selected:true,show:31779,members:[2710,30861],brightness_override_x100:null,
    presets:[{preset:4261,type:72,value:15826,field2:25,field5:null,fade_qs:8,delay_qs:0,xy:null,cct:2700,show_level:null},{preset:4262,type:72,value:0,fade_qs:40,delay_qs:0},
      {preset:4263,type:73,value:31779,fade_qs:8,delay_qs:4,show_level:null},{preset:4264,type:72,value:65279,field5:1,fade_qs:8,delay_qs:0},{preset:4270,type:1}],
    shows:[{id:31779,knots:[{minute:0,level:6000,cct:1900},{minute:720,level:65279,cct:4500}]}]});
    const v=text(view('program-viz'));
    for(const want of ['Answers to','Area or group','Scene preset 4261','25%','2700 K · vibrancy 25','Off','10 s fade','Starts Natural Show 31779','after 1 s delay','Halogen warm dim','Unaffected','100%','4 change this lamp · 1 leave it alone'])if(!v.includes(want))throw Error('zone programming should read '+want+': '+v);
    {const ha=haData;haData={lights:[],leap:{areas:{'2710':{name:'Office',path:'Second Floor › Office'}},zones:{'30861':{name:'office lamp',area:'2710',type:'SpectrumTune',tuning:[1400,10000],warm_dim:false}},keypads:{}}};
      const z={zone_id:30861};if(memberLabel(2710,z)[0]!=='Area: Second Floor › Office'||memberLabel(30861,z)[0]!=='This zone'||memberLabel(30864,z)[0]!=='This lamp’s device or load'||memberLabel(999,z)[0]!=='Group')throw Error('members are named from the Lutron processor');
      if(fixtureText(leapZone(30861))!=='SpectrumTune · 1400–10000 K')throw Error('fixture text: '+fixtureText(leapZone(30861)));haData=ha}
    if(level16(15826)!==2500||level16(65279)!==10000||level16(0)!==0)throw Error('16-bit levels convert as the endpoint does')}
  // Programming sync: rejected transfers, re-reading after a transfer, N4 lamps need nothing.
  {deviceView(deviceSnapshot);latestNative=null;const now=Date.now();
    zonesView({...zoneSnapshot,programming:{events:[{zone:1,stage:'program-installed',wall_ms:now-60000}]},zones:[{...matchZone,program:{programmed:true,selected:false,shows:1,age_s:30}}]});health(status);
    let st=syncState();if(st.tone!=='good'||!st.steps[0][2].includes('Last transfer received')||!st.steps[3][2].includes('Nothing to sync'))throw Error('a current copy after a transfer is up to date: '+JSON.stringify(st.steps));
    zonesView({...zoneSnapshot,programming:{events:[{zone:1,stage:'program-installed',wall_ms:now-10000}]},zones:[{...matchZone,program:{programmed:true,selected:false,shows:1,age_s:30}}]});
    st=syncState();if(st.tone!=='wait'||!st.steps[2][2].includes('re-reading Office'))throw Error('a copy older than the transfer is catching up: '+JSON.stringify(st.steps));
    zonesView({...zoneSnapshot,programming:{events:[{zone:1,stage:'program-rejected',wall_ms:now-10000}]},zones:[{...matchZone,program:{programmed:true,selected:false,shows:1,age_s:5}}]});health(status);
    if(syncState().tone!=='bad'||!heroText().includes('Programming from Lutron was rejected')||!view('program-badge').textContent)throw Error('a rejected transfer reaches the overview and the tab');
    latestNative={state:'ready',scene_cache_ready:true,used_bytes:2048,capacity_bytes:65536,last_scene_event:{valid:true,decision:'streamed',reason:'scene_cache_not_ready'},zones:[{zone_id:31467,ready:true}]};
    health({...status,direct_routing:{table_committed:true,unreachable:0}});st=syncState();
    if(st.steps[3][0]!=='good'||!st.steps[3][2].includes('2 of 64 KB')||!st.steps[3][2].includes('because scenes were still loading'))throw Error('the lamps step adds storage and the last recall: '+st.steps[3][2]);
    latestNative={...latestNative,state:'sync_failed',error:'bulb_timeout'};st=syncState();
    if(st.steps[3][0]!=='warn'||!st.steps[3][2].includes('bulb timeout'))throw Error('a failed scene sync needs a look: '+st.steps[3][2]);
    latestNative=null;zonesView(zoneSnapshot);health(status)}
})()
`;
await vm.runInNewContext(code+'\n'+checks+'\n'+mappingChecks,context,{timeout:2000});
console.log('management UI safety, mapping, event and flow tests passed');
