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
  uart_frames_ok:1,uart_crc_errors:0,config_hash:1};
const replies={
  status, devices:{zones:[]}, events:{boot_id:'boot-a',next:1,next_sequence:1,oldest:1,lost:0,events:[]},
  config:{generation:1,hash:1,editable:true,apply_status:'active',zones:[]},
};
const context={document,status,location:{href:'http://ha.local/api/hassio_ingress/token/'},URL,
  Date,Math,Number,String,Object,Array,Set,JSON,Promise,console,
  setInterval(){return 0;},setTimeout(){return 0;},
  async fetch(url){const path=new URL(url).pathname.split('/').slice(-2).join('/');const key=path.split('?')[0].split('/').pop();const value=replies[key]??{};return {ok:true,headers:{get:()=> 'application/json'},async json(){return value;}};},
};
const html=fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)),'index.html'),'utf8');
const code=html.split('<script>',2)[1].split('</script>',1)[0];
const checks=`
health({...status,monitoring:{age_s:9,delayed:true},diagnostics:{maintenance:false,wifi_disconnects:0}});
if(!document.getElementById('node-gateway').className.includes('warn')) throw Error('stale monitoring must not establish a Wi-Fi outage');
if(!document.getElementById('maintenance-toggle').textContent.includes('Enter')) throw Error('maintenance must be explicit');
health({...status,network_up:true,network_link:'ethernet',diagnostics:{maintenance:false,link_disconnects:0}});
if(document.getElementById('detail-gateway').textContent!=='Ethernet up') throw Error('gateway must show its reported Ethernet link');
if(document.getElementById('health-note').textContent.includes('Wi-Fi')) throw Error('an Ethernet gateway must not show Wi-Fi diagnostics');
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
const done=text(rows()[2]);if(!done.includes('Set to 50%')||!done.includes('Done')) throw Error('finished command should read as done: '+done);
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
if(!cmd.includes('Set to 100%')||!cmd.includes('Done')||!cmd.includes('From Lutron')) throw Error('the command should read as done: '+cmd);
const marked=n=>typeof n!=='string'&&(n.className==='unknown'||(n.children||[]).some(marked));
rows()[1].onclick();const stepRow=document.getElementById('events').children.find(r=>r.className==='detail'),steps=text(stepRow);
for(const want of ['Lutron → Bridge','Bridge → Ketra hub','Ketra hub → Bridge','Bridge → Lutron','✓ as asked','raw requested 0']) if(!steps.includes(want)) throw Error('steps should include '+want+': '+steps);
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
haData={time_zone:'Europe/London',lights:[]};renderEvents();
if(!document.getElementById('events-tz').textContent.includes('Europe/London')) throw Error('Home Assistant time zone should win');
deviceView({zones:[{zone:1,requested:7500},{zone:2,requested:-1},{zone:3,requested:null}]});
const requestedCells=[...document.getElementById('devices').children].map(r=>text(r.children[2]));
if(!requestedCells[0].includes('75%')||requestedCells[0].includes('7500')) throw Error('requested level should read as a percent: '+requestedCells[0]);
if(!document.getElementById('devices').children[1].children[2].children.some(c=>c.className==='unknown')) throw Error('unrecognised requested level must be marked unknown');
if(!requestedCells[2].includes('—')) throw Error('missing requested level should read as a dash');
applyHA({available:true,time_zone:'Europe/London',lights:[{entity_id:'light.k',name:'First Floor Kitchen kitchen',zone_id:'1379',serial:null}]});
if(haFor({zone_id:1379,name:'Kitchen'})?.by!=='zone') throw Error('lights should match by Lutron zone ID');
`;
vm.runInNewContext(code+'\n'+checks,context,{timeout:2000});
console.log('management UI event/flow tests passed');
