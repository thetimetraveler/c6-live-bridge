#!/usr/bin/env node
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.attrs={}; this.style={}; this.value=''; this.textContent=''; this.className=''; this.onclick=null; }
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
const safetyPatch=(safety={},power={})=>({...status,endpoint_safety:{...status.endpoint_safety,...safety},endpoint_power:{...status.endpoint_power,...power}});
const assertNotSafe=(s,why)=>{health(s);if(view('hero').className.includes('good')||view('node-thread').className.includes('good')||view('summary').children[1].className.includes('good'))throw Error(why+' must not be shown as safe');};
health(status);
if(!view('node-thread').className.includes('good')||!view('summary').children[1].className.includes('good'))throw Error('a fresh child with complete safety telemetry should show ready');
if(!view('headline-detail').textContent.includes('No physical hold is available')||!view('headline-detail').textContent.includes('does not confirm current command delivery'))throw Error('good status must explain the hardware and snapshot limits');
if(firmwareBaseline(latestStatus)!==30||Number.isFinite(firmwareBaseline({...status,monitoring:{age_s:9,delayed:true}})))throw Error('firmware restart comparison requires a fresh pre-upload baseline');
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
if(!view('headline-detail').textContent.includes('waiting'))throw Error('startup should be shown as waiting');
health(safetyPatch({phase:'operator-stopped',may_send:false,prefix_status:255,ext_pan_ok:false,active_timestamp_valid:false,mesh_local_prefix:'0000:0000:0000:0000::/64'},{enabled:false,hold_requested:true}));
if(!view('headline-detail').textContent.includes('reports operator stop')||!view('headline-detail').textContent.includes('No physical hold is available')||!view('headline-detail').textContent.includes('power state is not verified'))throw Error('operator stop must be reported as telemetry, without promising physical power off');
health(safetyPatch({}, {enabled:false,hold_requested:true}));
if(!view('headline-detail').textContent.includes('has not reported operator stop'))throw Error('a gateway disable is not proof that the endpoint stopped');
health({...safetyPatch({}, {hold_requested:true,boot_hold_remaining_s:30}),endpoint_online:false});
if(!view('headline-detail').textContent.includes('startup hold'))throw Error('a held endpoint can be offline during startup');
health({...status,monitoring:{age_s:0,delayed:false}});statusReceivedAt-=6000;health(latestStatus);
if(!view('headline').textContent.includes('Monitoring delayed')||view('node-thread').className.includes('good')||view('summary').children.some(c=>c.className.includes('good')))throw Error('a snapshot must age out even while a refresh is pending');
const deviceSnapshot={config_hash:1,monitoring:{age_s:0,delayed:false},zones:[{zone:1,confirmed:{at_ms:9999,level_x100:5000}}]};
const matchZone={zone:1,name:'Office',lutron:{on:true,state:'on',level_x100:5000,cct:2800},sent:null,
  lamp:{level_x100:5000,source:'observed',age_ms:1,cct:2800,seq:1},show:{status:'not_selected'},
  checks:[{key:'lutron',status:'good',title:'Lamp matches Lutron'}],verdict:{tone:'good',title:'In sync'}};
const zoneSnapshot={config_hash:1,monitoring:{age_s:0,delayed:false},zones:[matchZone],home_assistant:true};
const zoneCard=()=>view('zones').children[0];
const cardVerdict=()=>zoneCard().children[0].children[1];
health({...status});deviceView(deviceSnapshot);zonesView(zoneSnapshot);health(latestStatus);
if(!view('hero').className.includes('good')||!zoneCard().className.includes('good'))throw Error('fresh readback with complete comparisons should show in sync');
health({...status,endpoint_safety:undefined,endpoint_power:undefined});
if(view('hero').className.includes('good')||view('node-thread').className.includes('good'))throw Error('green lamp comparisons must not hide unavailable endpoint safety');
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
  if(!view('headline-detail').textContent.includes('Zigbee member link not responding')||!text(view('summary')).includes('Not responding'))throw Error('a silent Zigbee member must be reported');
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
`;
const mappingChecks=`
(async()=>{
  // Settle the initial background loads before simulating an independent editor.
  for(let i=0;i<30;i++)await Promise.resolve();
  if(refreshing)throw Error('initial refresh did not settle');
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
  // Startup sequence on the overview.
  const startup=()=>{const g=view('health').children.find(c=>c.children[0]?.textContent==='Startup sequence');if(!g)throw Error('startup sequence missing');return Object.fromEntries(g.children[1].children.map(r=>[r.children[0].textContent,r.children[1]]))};
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
})()
`;
await vm.runInNewContext(code+'\n'+checks+'\n'+mappingChecks,context,{timeout:2000});
console.log('management UI safety, mapping, event and flow tests passed');
