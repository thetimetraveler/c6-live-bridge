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
const rows=()=>document.getElementById('events').children.filter(r=>r.className!=='day'&&r.className!=='detail');
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
const unknown=rows()[1].children[3].children.find(l=>l.children.some(c=>c.className==='unknown'));
if(!unknown||!text(unknown).includes('-1')) throw Error('unrecognised levels must be shown raw and marked unknown');
if(!text(rows()[0]).includes('??')) throw Error('non-numeric levels must be shown raw');
if(text(rows()[2]).includes('#1')) throw Error('event numbers belong in the details, not the row');
rows()[2].onclick();const detail=document.getElementById('events').children.find(r=>r.className==='detail');
if(!detail||!text(detail).includes('Event #4')||!text(detail).includes('+2.1 s')) throw Error('expanding a row should list each step');
document.getElementById('filter-problems').checked=true;renderEvents();
if(rows().length!==1||!text(rows()[0]).includes('No activity')) throw Error('problems filter should hide healthy rows');
document.getElementById('filter-problems').checked=false;
haData={time_zone:'Europe/London',lights:[]};renderEvents();
if(!document.getElementById('events-tz').textContent.includes('Europe/London')) throw Error('Home Assistant time zone should win');
applyHA({available:true,time_zone:'Europe/London',lights:[{entity_id:'light.k',name:'First Floor Kitchen kitchen',zone_id:'1379',serial:null}]});
if(haFor({zone_id:1379,name:'Kitchen'})?.by!=='zone') throw Error('lights should match by Lutron zone ID');
`;
vm.runInNewContext(code+'\n'+checks,context,{timeout:2000});
console.log('management UI event/flow tests passed');
