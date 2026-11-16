#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

// The CCA panel is exercised without a browser, using the app's fake-DOM pattern.
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.listeners = {}; this._value = ''; this.textContent = '';
    this.hidden = false; this.disabled = false; this.className = ''; this.files = []; this.style = {};
  }
  get value() { return this._value; }
  set value(v) { this._value = String(v); }
  append(...nodes) { this.children.push(...nodes); if (this.tag === 'select' && !this._value) this._value = this.firstOption()?.value || ''; }
  replaceChildren(...nodes) { this.children = []; if (this.tag === 'select') this._value = ''; this.append(...nodes); }
  firstOption() { for (const child of this.children) { if (child.tag === 'option') return child; const nested = child.firstOption?.(); if (nested) return nested; } return null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute(k, v) { (this.attrs ||= {})[k] = String(v); }
  getAttribute(k) { return this.attrs?.[k] ?? null; }
  dispatch(type) { for (const fn of this.listeners[type] || []) fn({target:this}); return this['on'+type]?.({target:this,preventDefault(){}}); }
  click() { return this.dispatch('click'); }
}
const elements = new Map();
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement(tag) { return new Element(tag); },
};
const get = id => document.getElementById('cca-' + id);
const source = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)),'cca.js'),'utf8');
const html = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)),'index.html'),'utf8');
assert.match(html, /id="tab-cca"/);
assert.match(html, /Passive CCX listener \(B1\) firmware/);
assert.match(html, /CCA end-device firmware/);
assert.match(html, /<script src="cca\.js"><\/script>/);
assert.doesNotMatch(source, /EventSource/);
const fields = [
  {name:'device_id',label:'Device ID',type:'uint32',min:0,max:4294967295,required:true},
  {name:'level_pct',label:'Level',type:'integer',min:0,max:100,default:50,required:true},
  {name:'direction',label:'Direction',type:'enum',min:0,max:1,default:1,options:[{value:0,label:'Down'},{value:1,label:'Up'}]},
];
const cmds = [
  {name:'bridge_level',title:'Set level',group:'lighting',fields},
  {name:'unpair',title:'Unpair',group:'pairing',fields:[fields[0]]},
  {name:'raw',title:'Raw',group:'configuration',fields:[{name:'raw_payload',label:'Frame',type:'hex',min:1,max:53,required:true}]},
  {name:'ota_full_tx',title:'Full OTA',group:'ota',fields:[fields[0]]},
  {name:'ota_digest',title:'Digest',group:'ota',fields:[]},
  ...Array.from({length:29},(_,i)=>({name:'native_'+i,title:'Command '+i,group:i%2?'configuration':'lighting',fields:[]})),
];
const caps = {schema_version:1,commands:cmds,max_raw_bytes:53,max_body_bytes:112640,ota_digest:true,native_encoder:false};
const initialConfig = {expected_mac:'10:bd:a3:b1:5b:1c',targets:[{id:'office',name:'Office',device_id:0x1234,target_id:5,subnet:7}],presets:[]};
const status = {supported:true,status_age_s:1,status:{ready:true,configuration:'ready',i2c:{online:true,status_age_ms:1000},c6:{mac:'10:bd:a3:b1:5b:1c',boot_id:4,radio_present:true,native_encoder:false,rx_packets:8,tx_packets:3}},ota:{state:'idle',upload:null}};
const requests = [], confirmations = [];
let confirmResult = true, failCommand = false, clipboard = '', download = null, nextRequest = 42;
const state = {caps, config:initialConfig, status, events:{schema_version:1,boot_id:55,cursor:0,events:[],stream:{state:'online'}}};
const context = {
  document, console, Date, Math, Number, String, Object, Array, Set, Map, JSON, Promise, Error,
  setInterval(){},
  navigator:{clipboard:{async writeText(v){clipboard=v;}}},
  Blob:class { constructor(parts, opts){this.parts=parts;this.type=opts.type;} },
  URL:{createObjectURL(blob){download=blob;return 'blob:test';},revokeObjectURL(){}},
  async confirmSheet(title, body, ok) { confirmations.push({title,body,ok}); return confirmResult; },
  async req(route, opts) {
    requests.push({route,opts});
    if (route === 'cca/capabilities') return state.caps;
    if (route === 'cca/config' && !opts) return state.config;
    if (route === 'cca/config' && opts?.method === 'PUT') { state.config = JSON.parse(opts.body); return state.config; }
    if (route === 'cca/status') return state.status;
    if (route.startsWith('cca/events?')) return state.events;
    if (route === 'cca/command') { if (failCommand) throw Error('connection lost'); return {accepted:true,request_id:nextRequest++}; }
    if (route === 'cca/state') return {accepted:true,revision:8};
    if (route.startsWith('cca/ota/file?')) return {state:'ready',upload:{size:3,sha256:'abcd'},expected:3,staged:0};
    if (route === 'cca/ota/action') return {state:'staging',upload:{size:3,sha256:'abcd'},expected:3,staged:0};
    throw Error('unexpected route '+route);
  },
};
vm.createContext(context);
vm.runInContext(source,context);
const ui = context.CCAUI, t = ui._test;
const plain = v => JSON.parse(JSON.stringify(v));
const count = route => requests.filter(r => r.route === route).length;
const tick = () => new Promise(resolve => setTimeout(resolve,0));
const walk = n => [n,...n.children.flatMap(walk)];
const text = n => walk(n).map(x => x.textContent).join(' ');

assert.equal(t.parseInteger('0xDEADBEEF'), 0xdeadbeef);
assert.equal(t.parseInteger('00042'), 42);
for (const invalid of ['-1','0x','1foo','1.2','0x123z','4294967296','']) assert.throws(()=>t.parseInteger(invalid));
assert.equal(t.asHex(4660),'0x1234');
assert.equal(t.parseField({name:'raw_payload',label:'Frame',type:'hex',min:1,max:3},'aa bb'),'AABB');
assert.throws(()=>t.parseField({name:'raw_payload',label:'Frame',type:'hex',min:1,max:3},'ABC'));

await ui.show();
assert.equal(requests.filter(r => r.opts?.method && r.opts.method !== 'GET').length,0,'opening reads cache only');
assert.equal(walk(get('command-select')).filter(n=>n.tag==='option').length,34,'all metadata commands are exposed');
assert.equal(get('status-pill').textContent,'Ready');
assert.match(text(get('status-facts')),/Matches expected MAC/);
t.setStatus({...status,status_age_s:30}); assert.equal(get('status-pill').textContent,'Unavailable or stale');
t.setStatus({...status,status:{...status.status,latest_operation:{cmd:'ota_full_tx',request_id:19,stage:'running',current:3,total:8,reason:'none'}}});
assert.match(text(get('status-facts')),/ota_full_tx · running · request 19/); t.setStatus(status);
get('target-select').value = 'office'; get('target-select').dispatch('change');
assert.deepEqual(plain(t.commandFromForm()),{cmd:'bridge_level',device_id:4660,level_pct:50,direction:1});
assert.match(get('command-preview').textContent,/"device_id": 4660/);
const fieldInputs = walk(get('command-fields')).filter(n=>n.tag==='input' || n.tag==='select');
fieldInputs[0].value='0xabcdef'; fieldInputs[0].dispatch('input');
fieldInputs[1].value='101'; fieldInputs[1].dispatch('input');
assert.equal(get('command-send').disabled,true);
fieldInputs[1].value='20'; fieldInputs[1].dispatch('input');
assert.deepEqual(plain(t.commandFromForm()),{cmd:'bridge_level',device_id:0xabcdef,level_pct:20,direction:1});
assert.equal(get('command-send').disabled,false);
get('save-preset').click();
assert.match(get('config-note').textContent,/Save settings/);
await get('config-save').click();
const saved = requests.findLast(r=>r.route==='cca/config' && r.opts?.method==='PUT');
assert.equal(JSON.parse(saved.opts.body).presets[0].command.device_id,0xabcdef);
assert.equal(JSON.parse(saved.opts.body).targets[0].subnet,7);

// Routine commands send once; event stages must match both request and node boot.
let before = confirmations.length;
t.eventView({boot_id:55,cursor:1,events:[{id:1,kind:'cca.command',node_boot_id:4,request_id:42,stage:'sequence_finished'}],stream:{state:'online'}});
await t.sendCommand({cmd:'bridge_level',device_id:4660,level_pct:20});
assert.equal(confirmations.length,before);
assert.equal(count('cca/command'),1);
assert.match(get('command-note').textContent,/Gateway accepted/,'old same-ID events do not complete a new request');
t.eventView({boot_id:55,cursor:2,events:[{id:2,kind:'cca.command',node_boot_id:5,request_id:42,stage:'sequence_finished'}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Gateway accepted/,'other boot must not complete this request');
t.eventView({boot_id:55,cursor:3,events:[{id:3,kind:'cca.command',node_boot_id:4,request_id:42,stage:'running'}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Running locally/);
t.eventView({boot_id:55,cursor:4,events:[{id:4,kind:'cca.command',node_boot_id:4,request_id:42,stage:'sequence_finished'}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Local sequence finished/);
assert.match(get('command-note').textContent,/does not confirm Lutron/);
t.eventView({boot_id:55,cursor:5,events:[{id:5,kind:'cca.command',node_boot_id:4,request_id:42,stage:'running'}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Local sequence finished/,'late cached stages must not undo a terminal result');
confirmResult=false; before=count('cca/command');
await t.sendCommand({cmd:'unpair',device_id:4660});
await t.sendCommand({cmd:'ota_full_tx',device_id:4660});
assert.equal(count('cca/command'),before); assert.equal(confirmations.length,2);
confirmResult=true; failCommand=true;
await t.sendCommand({cmd:'bridge_level',device_id:4660,level_pct:20});
assert.equal(count('cca/command'),before+1); assert.match(get('command-note').textContent,/will not be resent automatically/);
failCommand=false;

// A restarted stream clears its independent history and labels loss and unknown outcome.
await t.sendCommand({cmd:'bridge_level',device_id:4660,level_pct:20});
t.eventView({boot_id:56,cursor:601,lost:9,events:Array.from({length:600},(_,i)=>({id:i+1,kind:'cca.rx',raw_hex:i===599?'<img onerror=bad>':'ff'})),stream:{state:'online',missed:3}});
assert.equal(t.state().rows.length,500); assert.equal(t.state().cursor,601);
assert.match(get('events-note').textContent,/9 events missing/); assert.match(get('events-note').textContent,/3 stream events missed/);
assert.match(get('command-note').textContent,/unknown/);
assert.match(text(get('events')),/<img onerror=bad>/,'untrusted raw content remains plain text');
t.eventView({boot_id:56,after:601,cursor:601,lost:0,events:[],stream:{state:'online',missed:3}});
assert.match(get('events-note').textContent,/9 events missing/,'browser gaps remain visible on later polls');
get('events-download').click();
assert.equal(JSON.parse(download.parts[0]).events.length,500);
t.eventView({boot_id:56,after:0,cursor:2,lost:0,events:[{id:2,kind:'cca.tx',crc_valid:null}],stream:{state:'online'}});
assert.equal(t.state().cursor,2); assert.equal(t.state().rows.length,1,'same-boot cursor rewind resets the browser baseline');
assert.doesNotMatch(text(get('events')),/CRC invalid/,'unknown CRC is not a failure');
{ const viewed = get('events').children[0]; viewed.open = true; viewed.dispatch('toggle');
  t.eventView({boot_id:56,cursor:3,events:[{id:3,kind:'cca.rx',raw_hex:'aa'}],stream:{state:'online'}});
  const [fresh, kept] = get('events').children;
  assert.equal(kept.open, true, 'a newly arrived packet must not collapse the packet being viewed');
  assert.ok(!fresh.open, 'new packets arrive collapsed');
  kept.open = false; kept.dispatch('toggle');
  t.eventView({boot_id:56,cursor:4,events:[{id:4,kind:'cca.rx',raw_hex:'bb'}],stream:{state:'online'}});
  assert.ok(get('events').children.every(d => !d.open), 'a collapsed packet stays collapsed');
  t.eventView({boot_id:57,cursor:1,events:[{id:2,kind:'cca.tx'}],stream:{state:'online'}});
  assert.ok(!get('events').children[0].open, 'a new boot forgets expanded rows'); }

// Presence uses the state endpoint and a copyable HA MQTT automation; no RF encoding claim.
get('presence-serial').value='0x1234'; get('presence-serial').dispatch('input');
get('presence-state').value='occupied'; await get('presence-send').click();
const presence = requests.findLast(r=>r.route==='cca/state');
assert.deepEqual(JSON.parse(presence.opts.body),{serial:4660,state:'occupied'});
await get('presence-copy').click(); assert.match(clipboard,/ccx_bridge\/cca\/presence\/set/); assert.match(clipboard,/"serial":4660/);
assert.match(get('presence-note').textContent,/copied/i);

// Upload and staging are separate actions; reload reads snapshots without starting RF.
get('ota-file').files=[{name:'fixture.ldf',size:131}]; get('ota-format').value='ldf';
await get('ota-upload').click();
assert.equal(requests.findLast(r=>r.route.startsWith('cca/ota/file?')).route,'cca/ota/file?format=ldf');
assert.equal(count('cca/ota/action'),0,'upload must not stage or send');
await get('ota-stage').click(); assert.deepEqual(JSON.parse(requests.findLast(r=>r.route==='cca/ota/action').opts.body),{action:'stage'});
get('ota-device').value='0x1234'; get('ota-subnet').value='0x77';
confirmResult=false; before=count('cca/ota/action'); await get('ota-start').click(); assert.equal(count('cca/ota/action'),before);
confirmResult=true; await get('ota-start').click(); assert.deepEqual(JSON.parse(requests.findLast(r=>r.route==='cca/ota/action').opts.body),{action:'start',device_id:4660,subnet:119});
await get('ota-stop').click(); assert.deepEqual(JSON.parse(requests.findLast(r=>r.route==='cca/ota/action').opts.body),{action:'stop_staging'});
await ui.show(); assert.equal(count('cca/ota/action'),before+2,'tab reload must not replay an action');

// Required values without a meaningful default must be entered explicitly.
t.setCapabilities({commands:[{name:'requires_input',group:'controls',fields:[
  {name:'device_id',label:'Device ID',type:'integer',min:1,max:99,required:true,default:null},
  {name:'choice',label:'Choice',type:'integer',min:0,max:1,required:true,default:null,options:[{value:0,label:'No'},{value:1,label:'Yes'}]},
]}]});
assert.equal(get('command-send').disabled,true);
const requiredInputs=walk(get('command-fields')).filter(n=>n.tag==='input'||n.tag==='select');
assert.equal(requiredInputs[0].value,''); assert.equal(requiredInputs[1].value,'');
requiredInputs[0].value='7'; requiredInputs[0].dispatch('input'); assert.equal(get('command-send').disabled,true);
requiredInputs[1].value='0'; requiredInputs[1].dispatch('change');
assert.deepEqual(plain(t.commandFromForm()),{cmd:'requires_input',device_id:7,choice:0});

// New controls come only from firmware metadata; target defaults apply to subnet and group.
const shadeAddress = [
  {name:'subnet',label:'Subnet',type:'integer',min:0,max:65535,required:true},
  {name:'target_id',label:'Shade group',type:'integer',min:0,max:0xffffffff,required:true},
];
const motor = {name:'device_id',label:'Motor serial',type:'integer',min:1,max:0xffffffff,required:true};
const shadeCaps = {commands:[
  {name:'shade_level',group:'shade',fields:[...shadeAddress,{name:'level_pct',type:'integer',min:0,max:100,required:true},
    {name:'fade_qs',type:'integer',min:0,max:255},{name:'raw_repeat',type:'integer',min:1,max:255,default:6}]},
  {name:'feedback_map',group:'shade',fields:[motor,...shadeAddress,{name:'link_index',type:'integer',min:0,max:99,required:true},
    {name:'component',type:'integer',min:3,max:3,default:3}]},
  {name:'feedback_remove',group:'shade',fields:[motor,...shadeAddress]},
  {name:'feedback_list',group:'shade',fields:[]},
]};
t.setCapabilities(shadeCaps);
get('command-select').value='shade_level'; get('command-select').dispatch('change');
let inputs=walk(get('command-fields')).filter(n=>n.tag==='input'||n.tag==='select');
assert.equal(inputs[0].value,'7'); assert.equal(inputs[1].value,'5');
inputs[2].value='73'; inputs[2].dispatch('input');
assert.deepEqual(plain(t.commandFromForm()),{cmd:'shade_level',subnet:7,target_id:5,level_pct:73,raw_repeat:6});
get('command-select').value='feedback_map'; get('command-select').dispatch('change');
inputs=walk(get('command-fields')).filter(n=>n.tag==='input'||n.tag==='select');
assert.equal(inputs[0].value,'4660'); assert.equal(inputs[1].value,'7'); assert.equal(inputs[2].value,'5');
inputs[3].value='0'; inputs[3].dispatch('input');
assert.deepEqual(plain(t.commandFromForm()),{cmd:'feedback_map',device_id:4660,subnet:7,target_id:5,link_index:0,component:3});
inputs[3].value='100'; inputs[3].dispatch('input'); assert.equal(get('command-send').disabled,true);
get('command-select').value='feedback_remove'; get('command-select').dispatch('change');
assert.deepEqual(plain(t.commandFromForm()),{cmd:'feedback_remove',device_id:4660,subnet:7,target_id:5});
get('command-select').value='feedback_list'; get('command-select').dispatch('change');
assert.deepEqual(plain(t.commandFromForm()),{cmd:'feedback_list'});

// Motor results are visible without marking the overall feedback operation complete.
state.events={schema_version:1,boot_id:90,cursor:0,events:[],stream:{state:'online'}};
t.eventView(state.events);
await t.sendCommand({cmd:'shade_level',subnet:7,target_id:5,level_pct:73}); await tick();
const requestId=t.state().activeReceipt.id;
t.eventView({boot_id:90,cursor:1,events:[{id:1,kind:'cca.command',node_boot_id:4,request_id:requestId,stage:'sequence_finished'}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Local sequence finished/);
assert.match(get('command-note').textContent,/Overall feedback: final result not yet reported/);
t.eventView({boot_id:90,cursor:2,events:[{id:2,kind:'cca.feedback',node_boot_id:4,request_id:requestId,serial:4660,link_index:0,
  stage:'feedback_sent',reason:'none',ack_byte:0,quantized_target:0,attempt:1}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Latest motor 4660: Feedback sent locally/);
assert.match(get('command-note').textContent,/Overall feedback: final result not yet reported/);
assert.match(text(get('events')),/link 0 · ACK byte 0 · quantized target 0 · attempt 1/);
t.eventView({boot_id:90,cursor:3,events:[{id:3,kind:'cca.feedback',node_boot_id:5,request_id:requestId,serial:0,stage:'feedback_sent',current:1,total:1}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Overall feedback: final result not yet reported/,'a different node boot cannot finalize feedback');
t.eventView({boot_id:90,cursor:4,events:[{id:4,kind:'cca.feedback',node_boot_id:4,request_id:requestId,serial:0,stage:'feedback_sent',current:1,total:1}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Overall feedback: Feedback sent locally · 1 \/ 1/);
assert.match(get('command-note').textContent,/does not confirm RA3 state or measured shade position/);
t.eventView({boot_id:90,cursor:5,events:[{id:5,kind:'cca.feedback_mapping',node_boot_id:4,request_id:requestId+1,
  serial:4660,target_id:5,subnet:7,link_index:0,component:3,current:1,total:1}],stream:{state:'online'}});
assert.match(text(get('events')),/serial 4660 · group 5 · subnet 7 · link 0 · component 3 · 1 \/ 1/);

state.events={schema_version:1,boot_id:90,cursor:5,events:[],stream:{state:'online'}};
await t.sendCommand({cmd:'shade_level',subnet:7,target_id:5,level_pct:30}); await tick();
const nextId=t.state().activeReceipt.id;
t.eventView({boot_id:90,cursor:7,events:[{id:6,kind:'cca.command',node_boot_id:4,request_id:nextId,stage:'sequence_finished'},
  {id:7,kind:'cca.feedback',node_boot_id:4,request_id:nextId,serial:0,stage:'waiting'}],stream:{state:'online'}});
t.eventView({boot_id:91,cursor:0,events:[],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Local sequence finished/);
assert.match(get('command-note').textContent,/Feedback outcome unknown/,'stream restart leaves unfinished feedback unresolved');
for (const [stage,label] of [['partial','Feedback partially sent'],['ack_timeout','Shade ACK timed out'],
  ['superseded','Feedback superseded'],['not_applicable','Feedback not applicable']]) {
  assert.match(t.receiptText({cmd:'shade_level',stage:'sequence_finished',feedback:{stage,serial:0,current:1,total:2}}),new RegExp(label));
}
state.events={schema_version:1,boot_id:91,cursor:0,events:[],stream:{state:'online'}};
await t.sendCommand({cmd:'raw',raw_payload:'aa',raw_repeat:6}); await tick();
const rawId=t.state().activeReceipt.id;
t.eventView({boot_id:91,cursor:1,events:[{id:1,kind:'cca.feedback',node_boot_id:4,request_id:rawId,serial:4660,stage:'ack_received'}],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Latest motor 4660: Shade ACK observed/);
t.eventView({boot_id:92,cursor:0,events:[],stream:{state:'online'}});
assert.match(get('command-note').textContent,/Feedback outcome unknown/,'raw-command feedback is also invalidated on restart');

// Optional firmware-produced fixture exercises every real descriptor during integration.
if (process.env.CCA_CAPABILITIES_FIXTURE) {
  const actual = JSON.parse(fs.readFileSync(process.env.CCA_CAPABILITIES_FIXTURE,'utf8'));
  assert.ok(actual.commands.length>=34);
  t.setCapabilities(actual);
  assert.equal(walk(get('command-select')).filter(n=>n.tag==='option').length,actual.commands.length);
  for (const command of actual.commands) {
    get('command-select').value=command.name; get('command-select').dispatch('change');
    const controls=walk(get('command-fields')).filter(n=>n.tag==='input'||n.tag==='select');
    assert.equal(controls.length,command.fields.length,command.name+' renders every advertised field');
    command.fields.forEach((field,i)=>{
      controls[i].value=field.type==='hex' ? 'AABBCCDD' : field.options?.length ? String(field.options[0].value) : String(Math.max(field.min||0,field.required?1:0));
      controls[i].dispatch('input');
    });
    const value=plain(t.commandFromForm()); assert.equal(value.cmd,command.name);
    assert.deepEqual(Object.keys(value).sort(),['cmd',...command.fields.map(f=>f.name)].sort());
  }
}
await tick();

// Targets drawn as Picos: layout from the Lutron device whose serial is the target's device ID, else asked for.
{
  t.setCapabilities(caps);
  assert.equal(t.layoutOf({model:'PJ2-3BRL-GWH-L01'}),'pj2-3brl');
  assert.equal(t.layoutOf({model:'PJ2-4B-XXX-L31',type:'Pico4Button'}),'pj2-4b');
  assert.equal(t.layoutOf({type:'Pico2ButtonRaiseLower'}),'pj2-2brl');
  assert.equal(t.layoutOf({type:'Pico4ButtonScene'}),'pj2-4b');
  assert.equal(t.layoutOf({model:'HRST-HN4B-XX',type:'SunnataHybridKeypad'}),null,'a keypad is not a Pico');
  context.haData = {leap:{areas:{'330':{name:'Master Bedroom',path:'Master Bedroom'}},keypads:{
    '12742':{name:'Bedside',device:'Position 2',area:'330',model:'PJ2-4B-XXX-L31',type:'Pico4Button',serial:82324321},
    '3933':{name:'Bedside',device:'Position 1',area:'330',model:'PJ2-2B-XXX-L01',type:'Pico2Button',serial:139175899},
    '1208':{name:'stairs top',model:'HRST-HN4B-XX',type:'SunnataHybridKeypad',serial:103906374}}}};
  t.setConfig({expected_mac:'',targets:[{id:'bedside',name:'Bedside',device_id:82324321},{id:'mystery',name:'Mystery',device_id:5},{id:'stairs',name:'Stairs',device_id:103906374}],
    presets:[{id:'dim',name:'Dim',command:{cmd:'bridge_level',device_id:4660,level_pct:20}},{id:'off',name:'All off',command:{cmd:'bridge_level',device_id:4660,level_pct:0}}]});
  const cards = () => get('targets').children, keys = card => walk(card).filter(n => /\bpk\b/.test(n.className));
  const find = (root, pred) => walk(root).find(pred);
  assert.deepEqual(keys(cards()[0]).map(k => k.picoKey),['1','2','3','4'],'a PJ2-4B is drawn with four buttons');
  assert.match(text(cards()[0]),/Pico 4-button from Lutron: Bedside Position 2 · Master Bedroom · PJ2-4B-XXX-L31/);
  assert.equal(keys(cards()[1]).length,0); assert.match(text(cards()[1]),/No Lutron Pico has this serial\. Choose the layout/,'an unknown device asks for its layout');
  assert.match(text(cards()[2]),/is not a Pico/);
  assert.match(text(get('presets')),/Set level/,'presets show the command title from the catalogue');
  assert.match(text(get('presets')),/Level 20%/);

  // Select a button: its programming panel opens; assign a preset.
  keys(cards()[0])[1].click();
  let pick = find(cards()[0], n => n.id === 'cca-button-preset');
  assert.ok(pick,'selecting a button shows its programming'); assert.match(text(cards()[0]),/Button 2 on Bedside/);
  assert.match(text(cards()[0]),/Choose a preset for this button/);
  pick.value = 'dim'; pick.dispatch('change');
  assert.deepEqual(plain(t.config().targets[0].buttons),{'2':'dim'});
  assert.match(keys(cards()[0])[1].className,/\bset\b/); assert.match(keys(cards()[0])[1].className,/\bon\b/);
  assert.match(text(cards()[0]),/Set level.*Level 20%/s,'the panel shows what the preset sends');
  assert.match(text(get('presets').children[0]),/Programmed on Bedside · Button 2/);
  assert.match(text(get('presets').children[1]),/Not on any Pico button/);
  assert.equal(t.state().configDirty,true);

  const sent = count('cca/command');
  await find(cards()[0], n => n.tag === 'button' && n.textContent === 'Send now').click(); await tick();
  assert.equal(count('cca/command'),sent+1); assert.deepEqual(JSON.parse(requests.findLast(r => r.route === 'cca/command').opts.body),{cmd:'bridge_level',device_id:4660,level_pct:20});
  assert.match(get('button-note').textContent,/request \d+/,'the receipt is shown under the button');

  // Choose a layout for the unknown device.
  const layoutSelect = find(cards()[1], n => n.tag === 'select' && walk(n).some(o => o.value === 'pj2-3brl'));
  layoutSelect.value = 'pj2-3brl'; layoutSelect.dispatch('change');
  assert.deepEqual(keys(cards()[1]).map(k => k.picoKey),['on','raise','fav','lower','off']);
  assert.match(text(cards()[1]),/Shown as Pico 3-button with raise\/lower \(chosen here\)/);
  keys(cards()[1])[4].click(); pick = find(cards()[1], n => n.id === 'cca-button-preset'); pick.value = 'off'; pick.dispatch('change');
  assert.equal(find(cards()[0], n => n.id === 'cca-button-preset'),undefined,'one button panel is open at a time');

  let payload = t.configPayload();
  assert.deepEqual(plain(payload.targets[0]),{id:'bedside',name:'Bedside',device_id:82324321,buttons:{'2':'dim'}},'a detected layout is not stored');
  assert.deepEqual(plain(payload.targets[1]),{id:'mystery',name:'Mystery',device_id:5,layout:'pj2-3brl',buttons:{off:'off'}});

  // Renaming a preset ID keeps its buttons; removing a preset clears them.
  const idInput = walk(get('presets').children[0]).filter(n => n.tag === 'input')[1];
  idInput.value = 'dim2'; idInput.dispatch('input');
  assert.equal(t.config().targets[0].buttons['2'],'dim2');
  find(get('presets').children[0], n => n.textContent === 'Remove preset').click();
  assert.deepEqual(plain(t.config().targets[0].buttons),{}); assert.match(get('config-note').textContent,/cleared from 1 Pico button/);
  assert.equal(t.configPayload().targets[0].buttons,undefined);

  // A chosen layout drops buttons it does not have.
  t.config().targets[0].layout = 'pj2-2b'; t.config().targets[0].buttons = {'2':'off',on:'off'};
  assert.deepEqual(plain(t.configPayload().targets[0].buttons),{on:'off'});
  delete t.config().targets[0].layout; t.config().targets[0].buttons = {};

  // Add a Pico straight from Lutron: only Picos not already added are offered.
  t.selectButton(null);
  const offered = walk(get('add-pico')).filter(n => n.tag === 'option' && n.value).map(n => n.value);
  assert.deepEqual(offered,['139175899'],'added Picos and keypads are not offered');
  get('add-pico').value = '139175899'; get('add-pico').dispatch('change');
  const added = t.config().targets.at(-1);
  assert.deepEqual(plain(added),{id:'bedside-position-1',name:'Bedside Position 1',device_id:'139175899'});
  assert.deepEqual(keys(cards()[3]).map(k => k.picoKey),['on','off']); assert.ok(find(cards()[3], n => n.id === 'cca-button-preset'),'the new Pico opens on its first button');

  // Lutron data arriving later redraws the targets once; unchanged data does not.
  context.haData = null; ui.lutronChanged(); assert.match(text(cards()[0]),/Lutron data is not available/);
  const before = cards()[0]; ui.lutronChanged(); assert.equal(cards()[0],before);
  context.haData = {leap:{keypads:{a:{name:'Bedside',model:'PJ2-4B-XXX-L31',serial:82324321}}}}; ui.lutronChanged();
  assert.deepEqual(keys(cards()[0]).map(k => k.picoKey),['1','2','3','4']);
}
console.log('CCA dashboard metadata, commands, events, settings, presence and OTA tests passed');
