/* CCA dashboard. All network reads use the add-on cache; there is no browser SSE. */
var CCAUI = (() => {
  const byId = id => document.getElementById('cca-' + id);
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = String(text);
    return n;
  };
  const display = value => value === null || value === undefined ? '—' :
    typeof value === 'object' ? JSON.stringify(value) : String(value);
  const jsonOptions = value => ({method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(value)});
  const clone = value => JSON.parse(JSON.stringify(value));
  const commands = () => Array.isArray(capabilities?.commands) ? capabilities.commands : [];
  const descriptor = name => commands().find(c => c.name === name);
  const addressFields = ['device_id', 'target_id', 'subnet'];
  const maxRows = 500;
  const nodeMac = '10:bd:a3:b1:5b:1c', nodeMin = 8192, nodeMax = 1048576, hashPattern = /^[0-9a-f]{64}$/i;
  let capabilities = null, config = null, status = null, statusAt = 0, ota = null;
  let boot = null, cursor = 0, rows = [], keys = new Set(), stream = {}, lost = 0;
  // Rows the user has expanded, kept across re-renders so new packets do not collapse them.
  const openEvents = new Set(), eventListeners = [];
  let activeReceipt = null, formInputs = new Map(), presetDrafts = [];
  let loaded = false, loading = false, fetchingEvents = false, fetchingStatus = false, fetchingCaps = false;
  let sending = false, otaSending = false, configDirty = false, capsAt = 0, attemptAt = 0;
  let nodeFile = null, nodeFileValid = false, nodeSending = false, nodeMessage = '';

  // Never use parseInt: it would accept trailing garbage or a partial hexadecimal ID.
  function parseInteger(value, min = 0, max = 0xffffffff, label = 'Value') {
    const text = String(value ?? '').trim();
    if (!/^(?:\d+|0x[0-9a-f]+)$/i.test(text)) throw Error(`${label} must be a decimal or 0x hexadecimal integer.`);
    const n = Number(text);
    if (!Number.isSafeInteger(n) || n < min || n > max) throw Error(`${label} must be between ${min} and ${max}.`);
    return n;
  }
  function asHex(n) { return Number.isSafeInteger(n) && n >= 0 ? '0x' + n.toString(16).toUpperCase() : ''; }
  function option(value, label) {
    const n = node('option', '', label); n.value = String(value); return n;
  }
  function selectOptions(select, items, selected) {
    select.replaceChildren(...items);
    if (selected !== undefined && selected !== null) select.value = String(selected);
  }
  function facts(title, entries) {
    const wrap = node('div');
    wrap.append(node('h3', '', title));
    const list = node('div', 'list');
    for (const [label, value] of entries) {
      const row = node('div', 'row'); row.append(node('span', '', label), node('strong', '', display(value))); list.append(row);
    }
    wrap.append(list); return wrap;
  }
  function note(id, text) { byId(id).textContent = text; }
  function pill(id, text, tone = 'wait') { byId(id).textContent = text; byId(id).className = 'pill ' + tone; }
  function nodeBusy() {
    return nodeSending || status?.node_upload?.in_flight === true || status?.node_upload?.awaiting_status === true || status?.status?.node_update?.busy === true;
  }
  function nodeReady() {
    const wrapper = status || {}, s = wrapper.status || {}, peer = s.c6 || {}, u = s.node_update || {};
    const age = Number.isFinite(wrapper.status_age_s) ? wrapper.status_age_s + Math.max(0, Date.now() - statusAt) / 1000 : Infinity;
    return wrapper.error == null && age <= 15 && s.capabilities?.node_update === true && u.supported === true && u.online === true &&
      String(config?.expected_mac || '').toLowerCase() === nodeMac && String(peer.mac || '').toLowerCase() === nodeMac &&
      String(u.mac || '').toLowerCase() === nodeMac && u.busy === false && u.retry_allowed === true && !nodeBusy();
  }
  function updateCcaControls() {
    const busy = nodeBusy();
    for (const id of ['advanced-send','presence-send','ota-upload','ota-stage','ota-resume','ota-stop','ota-clear','ota-start']) byId(id).disabled = busy;
    updatePreview();
  }
  function renderNodeUpdate() {
    const s = status?.status || {}, u = s.node_update || {}, supported = s.capabilities?.node_update === true && u.supported === true;
    const busy = nodeBusy(), phase = u.phase || 'idle', total = Number(u.total), offset = Number(u.offset);
    const progress = Number.isFinite(total) && total > 0 && Number.isFinite(offset) ? Math.max(0, Math.min(100, 100 * offset / total)) : 0;
    pill('node-pill', !status?.status ? 'Waiting' : busy ? 'Updating' : !supported ? 'Unavailable' : u.online ? 'Online' : 'Offline',
      busy ? 'info' : supported && u.online ? 'good' : 'wait');
    byId('node-facts').replaceChildren(facts('CCA node update', [['Board MAC',u.mac], ['Version',u.version], ['Slot',u.slot],
      ['Boot ID',u.boot_id], ['Running ELF SHA-256',u.running_elf_sha256 || null], ['Candidate ELF SHA-256',u.candidate_elf_sha256 || null],
      ['Confirmed',u.confirmed === true ? 'Yes' : u.pending_verify === true ? 'Pending verification' : null], ['Local ready',u.local_ready], ['Phase',u.phase], ['Error',u.error || null]]));
    note('node-progress', u.total > 0 ? `${display(phase)} · ${display(u.offset)} of ${display(u.total)} bytes` : supported ? display(phase) : '');
    byId('node-progress-bar').style.width = progress + '%';
    const age = Number.isFinite(status?.status_age_s) ? status.status_age_s + Math.max(0, Date.now() - statusAt) / 1000 : Infinity;
    const confirmed = status?.error == null && age <= 15 && supported && u.online === true && u.busy === false && !busy && u.phase === 'confirmed' &&
      String(config?.expected_mac || '').toLowerCase() === nodeMac && String(u.mac || '').toLowerCase() === nodeMac &&
      u.confirmed === true && u.pending_verify === false && u.local_ready === true && !u.error &&
      Number.isSafeInteger(u.boot_id) && u.boot_id > 0 && (u.slot === 0 || u.slot === 1) &&
      hashPattern.test(u.running_elf_sha256 || '') && String(u.running_elf_sha256).toLowerCase() === String(u.candidate_elf_sha256 || '').toLowerCase();
    let message = nodeMessage;
    if (!status?.status) message = 'Waiting for CCA node status.';
    else if (busy) message = 'CCA update in progress or awaiting fresh status. CCA controls pause during the update; CCX and the event feed continue.';
    else if (!supported) message = 'CCA node updater unavailable. The gateway and CCA node need compatible firmware and the one-time USB bootstrap.';
    else if (['failed','uncertain','interrupted'].includes(phase)) message = `Update ${phase}${u.error ? ': ' + display(u.error) : ''}. Check status before any manual retry; the app will not resend automatically.`;
    else if (confirmed) message = 'The CCA node confirmed the candidate image; the full running ELF hash matches.';
    else if (phase === 'confirmed') message = 'Confirmation details are incomplete or inconsistent. Check the node identity, readiness and ELF hashes.';
    else if (!u.online) message = 'CCA updater offline; use USB recovery if it cannot return.';
    else if (!nodeReady()) message = 'Update unavailable. Verify the saved CCA MAC and wait for fresh updater status.';
    note('node-note', message || 'Choose a signed CCA node image.');
    byId('node-upload').disabled = !(nodeFile && nodeFileValid && nodeReady());
    updateCcaControls();
  }
  function statusView() {
    const wrapper = status || {}, s = wrapper.status || {}, peer = s.c6 || s.peer || {}, link = s.i2c || {}, control = s.control || {}, operation = s.latest_operation;
    const expected = String(config?.expected_mac || '').trim(), observed = String(peer.mac || '').trim();
    const identity = !expected ? 'Expected MAC not set' : !observed ? 'Waiting for board identity' :
      expected.toLowerCase() === observed.toLowerCase() ? 'Matches expected MAC' : 'MAC does not match expected board';
    const elapsed = Math.max(0, Date.now() - statusAt) / 1000;
    const ageSeconds = Number.isFinite(link.status_age_ms) && Number.isFinite(wrapper.status_age_s) ?
      link.status_age_ms / 1000 + wrapper.status_age_s + elapsed : Infinity;
    const age = Number.isFinite(ageSeconds) ? Math.round(ageSeconds) + ' s' : 'Unavailable';
    const available = wrapper.supported === true && link.online === true && ageSeconds <= 15;
    const isMatch = !!expected && !!observed && expected.toLowerCase() === observed.toLowerCase();
    pill('status-pill', wrapper.supported === false ? 'Unsupported' : !available ? 'Unavailable or stale' :
      !isMatch ? 'Check identity' : peer.radio_present === true && s.ready === true ? 'Ready' : 'Not ready',
      wrapper.supported === false || !available ? 'wait' : !isMatch || peer.radio_present !== true || s.ready !== true ? 'warn' : 'good');
    byId('status-facts').replaceChildren(
      facts('Identity and link', [['Observed MAC', peer.mac], ['Expected MAC', expected || null], ['Identity', identity],
        ['Node boot', peer.boot_id], ['Link online', link.online], ['Status age', age], ['Gateway ready', s.ready], ['Radio present', peer.radio_present], ['Configuration', s.configuration]]),
      facts('Radio counters and control', [['Received packets', peer.rx_packets], ['Transmitted packets', peer.tx_packets], ['CRC errors', peer.crc_errors], ['I/O errors', peer.io_errors],
        ['Link reads failed', link.reads_failed], ['Bad frames', link.bad_frames], ['Link writes failed', link.writes_failed], ['Receipt', control.receipt], ['Transfer', control.transfer],
        ['Latest local operation', operation ? `${display(operation.cmd)} · ${display(operation.stage)} · request ${display(operation.request_id)}` : null],
        ['Local progress', operation ? `${display(operation.current)} / ${display(operation.total)} · ${display(operation.reason)}` : null],
        ['Native LRF occupancy', peer.native_encoder === false ? 'Unsupported' : display(peer.native_encoder)]]));
    note('status-note', wrapper.error ? 'Status unavailable: ' + wrapper.error :
      !status ? 'Waiting for CCA status.' : wrapper.supported === false ? 'This gateway does not expose CCA controls.' :
      !isMatch ? identity + '. Verify the attached board and save its expected MAC.' :
      !available ? 'Status is offline or stale; displayed fields are the last cached snapshot.' : 'Fresh status from the add-on cache.');
    renderNodeUpdate();
  }
  function receiptText(receipt) {
    if (!receipt) return '';
    const req = receipt.id === undefined ? '' : ` · request ${receipt.id}`;
    const labels = {queued:'Queued locally', running:'Running locally', sequence_finished:'Local sequence finished', failed:'Local sequence failed', unknown:'Execution outcome unknown'};
    const stage = labels[receipt.stage] || 'Gateway accepted the request';
    let text = `${stage}${req}${receipt.reason ? ' · ' + receipt.reason : ''}.`;
    if (receipt.cmd === 'shade_level' || receipt.feedback || receipt.motorFeedback) {
      const feedbackLabels = {waiting:'Waiting for shade ACK', ack_received:'Shade ACK observed', feedback_sent:'Feedback sent locally',
        partial:'Feedback partially sent', ack_timeout:'Shade ACK timed out', superseded:'Feedback superseded',
        not_applicable:'Feedback not applicable', unknown:'Feedback outcome unknown'};
      const describe = e => (feedbackLabels[e.stage] || display(e.stage)) +
        (e.reason && e.reason !== 'none' ? ' · ' + display(e.reason) : '') +
        (e.current !== undefined && e.total !== undefined ? ` · ${display(e.current)} / ${display(e.total)}` : '');
      text += ' Overall feedback: ' + (receipt.feedback ? describe(receipt.feedback) : 'final result not yet reported') + '.';
      if (receipt.motorFeedback) text += ` Latest motor ${display(receipt.motorFeedback.serial)}: ${describe(receipt.motorFeedback)}.`;
      text += ' Feedback does not confirm RA3 state or measured shade position.';
    } else text += ' This does not confirm Lutron device receipt or state.';
    return text;
  }
  function showReceipt() {
    const message = receiptText(activeReceipt);
    note('command-note', message);
    if (activeReceipt?.noteId && activeReceipt.noteId !== 'command-note') note(activeReceipt.noteId, message);
  }
  function updateReceipt(e) {
    if (!activeReceipt || !['cca.command','cca.feedback'].includes(e.kind) || String(e.request_id) !== String(activeReceipt.id)) return;
    if (activeReceipt.boot !== null && e.node_boot_id !== undefined && String(e.node_boot_id) !== String(activeReceipt.boot)) return;
    if (Number.isInteger(e.id) && activeReceipt.eventBoot === boot && e.id <= activeReceipt.after) return;
    if (e.kind === 'cca.feedback') {
      if (!Number.isInteger(e.serial) || e.serial < 0) return;
      if (e.serial === 0) activeReceipt.feedback = e;
      else activeReceipt.motorFeedback = e;
      showReceipt(); return;
    }
    const stage = e.stage;
    const ranks = {accepted:0,queued:1,running:2,sequence_finished:3,failed:3,unknown:3};
    if (ranks[stage] !== undefined && ranks[stage] >= (ranks[activeReceipt.stage] ?? 0) &&
        ((ranks[activeReceipt.stage] ?? 0) < 3 || activeReceipt.stage === stage)) {
      activeReceipt.stage = stage; activeReceipt.reason = e.reason && e.reason !== 'none' ? display(e.reason) : '';
      showReceipt();
    }
  }
  function invalidateReceipt() {
    if (!activeReceipt) return;
    if (!['sequence_finished','failed'].includes(activeReceipt.stage)) activeReceipt.stage = 'unknown';
    if ((activeReceipt.cmd === 'shade_level' || activeReceipt.feedback || activeReceipt.motorFeedback) &&
        !['feedback_sent','partial','ack_timeout','superseded','not_applicable'].includes(activeReceipt.feedback?.stage))
      activeReceipt.feedback = {stage:'unknown',reason:'stream or node restarted before the final result'};
    showReceipt();
  }
  function eventView(data) {
    if (!data || !Array.isArray(data.events)) throw Error('Invalid CCA activity response');
    const nextBoot = data.boot_id ?? null;
    const rewound = nextBoot === boot && cursor > 0 && data.after === 0 && Number.isInteger(data.cursor) && data.cursor < cursor;
    if (nextBoot !== boot || data.reset === true || rewound) {
      if (boot !== null) invalidateReceipt();
      boot = nextBoot; cursor = 0; rows = []; keys.clear(); openEvents.clear(); lost = 0;
    }
    for (const e of data.events) {
      if (!e || !Number.isInteger(e.id) || e.id < 0) continue;
      const key = String(boot) + ':' + e.id;
      if (keys.has(key)) continue;
      keys.add(key); rows.push(e); cursor = Math.max(cursor, e.id); updateReceipt(e);
    }
    if (Number.isInteger(data.cursor) && data.cursor >= 0) cursor = Math.max(cursor, data.cursor);
    if (rows.length > maxRows) { rows = rows.slice(-maxRows); keys = new Set(rows.map(e => String(boot) + ':' + e.id)); for (const k of openEvents) if (!keys.has(k)) openEvents.delete(k); }
    stream = data.stream || {}; lost += Math.max(0,Number(data.lost) || 0);
    const state = stream.state || 'connecting';
    pill('events-pill', ({online:'Live', unsupported:'Unsupported', disconnected:'Disconnected', connecting:'Connecting'})[state] || state,
      state === 'online' ? 'good' : state === 'disconnected' ? 'warn' : 'wait');
    note('events-note', `Stream ${state} · boot ${display(boot)} · cursor ${cursor} · ${rows.length} browser records` +
      (Number.isFinite(stream.age_s) ? ` · last contact ${Math.round(stream.age_s)} s ago` : '') +
      (stream.missed ? ` · ${stream.missed} stream events missed` : '') + (lost ? ` · ${lost} events missing from this view` : '') +
      ' · starts at the add-on live baseline; earlier gateway events are not replayed');
    renderEvents();
    if (data.events.some(e => e?.kind === 'cca.feedback_mapping')) { refreshTargets(); for (const f of eventListeners) f(); }
  }
  // Hold the reader's place while packets arrive (live.js); without it the list just redraws.
  const liveRender = (box, draw) => typeof LiveFeed === 'undefined' ? draw() : LiveFeed.render(box, box, draw);
  function packetView(e) {
    if (!['cca.rx','cca.tx'].includes(e.kind)) return null;
    if (typeof CCAProtocol === 'undefined') return {description:'Packet decoder unavailable'};
    if (typeof e.raw_hex !== 'string' || !/^(?:[0-9a-f]{2}){1,53}$/i.test(e.raw_hex)) return {description:'Invalid packet bytes'};
    if (e.truncated) return {description:'Truncated packet; raw bytes retained'};
    const hex = e.raw_hex.toLowerCase().match(/../g), bytes = hex.map(v => Number.parseInt(v,16));
    const info = CCAProtocol.identifyPacket(bytes);
    const packet = {type:info.typeName, category:info.category, description:info.description, fields:{}};
    if (info.category === 'unknown') return packet;
    if ((info.typeName === 'SENSOR_LEVEL' && bytes[8] === 3) ||
        (info.typeName === 'SET_LEVEL' && bytes[14] === 6 && bytes[15] === 0)) {
      return {type:'FORMAT_' + hex[7].toUpperCase(), category:info.category,
        description:'Ambiguous protocol variant; inspect the raw event.', fields:{}};
    }
    const fields = info.fields.filter(f => !(e.kind === 'cca.tx' && f.name === 'crc' && f.offset === bytes.length));
    if (fields.length !== info.fields.length) packet.crc = 'Not included in TX payload telemetry';
    if (fields.some(f => f.offset + f.size > bytes.length)) return {...packet, description:'Incomplete ' + info.typeName + ' frame'};
    const covered = new Set();
    for (const f of fields) {
      const value = CCAProtocol.parseFieldValue(hex,f.offset,f.size,f.format,f.name);
      // Source IDs can be routing addresses, with inconsistent endian conventions.
      if (f.name === 'source_id') value.decoded = null;
      packet.fields[f.name] = {offset:f.offset, raw:value.raw, value:value.decoded};
      for (let i=f.offset; i<f.offset+f.size; i++) covered.add(i);
    }
    const source = ['SENSOR','BUTTON'].includes(info.category) ? packet.fields.device_id : null;
    if (source) {
      packet.serial = Number.parseInt(source.value,16);
      const target = (config?.targets || []).find(t => { try { return parseInteger(t.device_id) === packet.serial; } catch { return false; } });
      if (target?.name) packet.name = target.name;
    }
    packet.undecoded_bytes = Object.fromEntries(hex.map((v,i) => [i,v]).filter(([i]) => !covered.has(i)));
    if (info.category === 'ACK') packet.check = bytes.length === 5 ? 'ACK check valid' : 'ACK prefix check valid; trailing bytes unclassified';
    if (info.typeName === 'SENSOR_VACANT') packet.type = 'BUTTON_SENSOR_FORMAT_0C';
    if (info.category === 'SENSOR') packet.description = 'Sensor RF packet; occupancy state is not inferred.';
    if (info.category === 'SENSOR') packet.note = 'Sensor flags are raw values; occupied/vacant meanings are not established for every frame.';
    if (e.crc_valid === false) packet.description = 'Unverified frame; best-effort decode. ' + packet.description;
    return packet;
  }
  function renderEvents() {
    const filter = byId('events-filter').value.trim().toLowerCase();
    const matching = rows.map(e => ({e,packet:packetView(e)})).filter(row => !filter || JSON.stringify(row).toLowerCase().includes(filter));
    const rendered = matching.slice().reverse().map(({e,packet}) => {
      const d = node('details'), summary = node('summary');
      const parts = [e.kind || 'CCA event', packet?.type || (packet ? packet.description : e.cmd), packet?.name,
        packet?.serial !== undefined ? 'serial ' + packet.serial : '',
        packet && e.cmd && e.cmd !== 'unknown' ? 'command ' + e.cmd : '', e.request_id ? 'request ' + e.request_id : '',
        e.stage, Number.isFinite(e.at_ms) ? 'node uptime ' + e.at_ms + ' ms' : '',
        e.rssi !== undefined ? 'RSSI ' + e.rssi : '', packet?.check,
        e.crc_valid === true && packet?.category !== 'ACK' ? 'CRC valid' : e.crc_valid === false ? 'CRC invalid' : ''];
      if (e.kind === 'cca.feedback' || e.kind === 'cca.feedback_mapping') {
        if (e.serial !== undefined) parts.push(e.serial === 0 && e.kind === 'cca.feedback' ? 'overall' : 'serial ' + display(e.serial));
        if (e.target_id !== undefined) parts.push('group ' + display(e.target_id));
        if (e.subnet !== undefined) parts.push('subnet ' + display(e.subnet));
        if (e.link_index !== undefined) parts.push('link ' + display(e.link_index));
        if (e.component !== undefined) parts.push('component ' + display(e.component));
        if (e.ack_byte !== undefined) parts.push('ACK byte ' + display(e.ack_byte));
        if (e.quantized_target !== undefined) parts.push('quantized target ' + display(e.quantized_target));
        if (e.attempt !== undefined) parts.push('attempt ' + display(e.attempt));
        if (e.current !== undefined && e.total !== undefined) parts.push(display(e.current) + ' / ' + display(e.total));
        if (e.reason && e.reason !== 'none') parts.push(display(e.reason));
      }
      summary.textContent = parts.filter(Boolean).join(' · ');
      d.append(summary);
      if (packet) d.append(node('p','small muted',packet.description));
      d.append(node('pre', 'cca-json', JSON.stringify(packet ? {packet,event:e} : e, null, 2)));
      const key = String(boot) + ':' + e.id;
      d.rowKey = key; d.open = openEvents.has(key);
      d.addEventListener('toggle', () => { if (d.open) openEvents.add(key); else openEvents.delete(key); });
      return d;
    });
    const box = byId('events');
    liveRender(box, () => box.replaceChildren(...(rendered.length ? rendered : [node('p', 'empty', rows.length ? 'No activity matches this filter.' : 'Waiting for CCA activity…')])));
  }
  async function fetchEvents() {
    if (fetchingEvents) return;
    fetchingEvents = true;
    try { eventView(await req('cca/events?after=' + cursor + (boot === null ? '' : '&boot_id=' + encodeURIComponent(boot)))); }
    catch (e) { pill('events-pill', 'Unavailable'); note('events-note', 'CCA activity unavailable: ' + e.message); }
    finally { fetchingEvents = false; }
  }
  async function fetchStatus() {
    if (fetchingStatus) return;
    fetchingStatus = true;
    try {
      const s = await req('cca/status'); status = s; statusAt = Date.now();
      if (s?.ota) { ota = s.ota; renderOta(); }
      const peerBoot = (s?.status?.c6 || s?.status?.peer)?.boot_id;
      if (activeReceipt && activeReceipt.boot !== null && peerBoot !== undefined && peerBoot !== activeReceipt.boot) {
        invalidateReceipt();
      }
      const operation = s?.status?.latest_operation;
      if (activeReceipt && operation) updateReceipt({...operation,kind:'cca.command',node_boot_id:peerBoot});
      statusView();
    } catch (e) { status = {...(status || {}), status_age_s:null, error:e.message}; statusAt = Date.now(); statusView(); }
    finally { fetchingStatus = false; }
  }

  function enumItems(field) {
    return (field.options || []).map(o => {
      if (o && typeof o === 'object') return {value:o.value ?? o.id ?? o.code, label:o.label ?? o.title ?? o.name ?? o.value};
      return {value:o, label:o};
    });
  }
  function isHexField(field) {
    return ['hex', 'bytes', 'hex_bytes', 'raw', 'hex_string'].includes(String(field.type).toLowerCase()) || field.name === 'raw_payload';
  }
  function isNumberField(field) {
    return !isHexField(field) && (['number','integer','int','uint','u8','u16','u32','id','enum'].includes(String(field.type).toLowerCase()) ||
      typeof field.min === 'number' || typeof field.max === 'number' || typeof field.default === 'number' || enumItems(field).some(o => typeof o.value === 'number'));
  }
  function parseField(field, value) {
    const label = field.label || field.name, text = String(value ?? '').trim();
    if (!text) { if (field.required) throw Error(label + ' is required.'); return undefined; }
    if (isHexField(field)) {
      const compact = text.replace(/\s+/g, '');
      if (!/^(?:[0-9a-f]{2})+$/i.test(compact)) throw Error(label + ' must contain complete hexadecimal bytes.');
      const len = compact.length / 2;
      if (typeof field.min === 'number' && len < field.min || typeof field.max === 'number' && len > field.max)
        throw Error(`${label} must be between ${field.min ?? 0} and ${field.max ?? 'the supported limit'} bytes.`);
      return compact.toUpperCase();
    }
    if (isNumberField(field)) {
      const n = parseInteger(text, field.min ?? 0, field.max ?? 0xffffffff, label);
      if (field.options?.length && !enumItems(field).some(o => Number(o.value) === n)) throw Error(label + ' is not an available option.');
      return n;
    }
    if (field.type === 'boolean' || field.type === 'bool') return text === 'true';
    if (field.options?.length && !enumItems(field).some(o => String(o.value) === text)) throw Error(label + ' is not an available option.');
    return text;
  }
  function commandFromForm() {
    const command = descriptor(byId('command-select').value);
    if (!command) throw Error('Choose a supported command.');
    const result = {cmd:command.name};
    for (const field of command.fields || []) {
      const input = formInputs.get(field.name); if (!input) continue;
      const value = parseField(field, input.value);
      if (value !== undefined) result[field.name] = value;
    }
    return result;
  }
  function updatePreview() {
    try {
      const command = commandFromForm();
      byId('command-preview').textContent = JSON.stringify(command, null, 2);
      byId('command-send').disabled = sending || nodeBusy();
      byId('save-preset').disabled = false;
      return command;
    } catch (e) {
      byId('command-preview').textContent = e.message;
      byId('command-send').disabled = true; byId('save-preset').disabled = true; return null;
    }
  }
  function renderCommandFields(values = {}) {
    const command = descriptor(byId('command-select').value); formInputs = new Map();
    if (!command) { byId('command-fields').replaceChildren(); note('command-help', 'Command metadata is unavailable.'); updatePreview(); return; }
    const fields = (command.fields || []).map(field => {
      const label = node('label', '', field.label || field.name); let input;
      if (field.options?.length) {
        input = node('select');
        const options = enumItems(field).filter(o => o.value !== undefined).map(o => option(o.value, o.label));
        if (field.default === undefined || field.default === null) options.unshift(option('', field.required ? 'Choose an option' : 'Not set'));
        selectOptions(input, options);
      } else if (field.type === 'boolean' || field.type === 'bool') {
        input = node('select'); selectOptions(input, [option('true','Yes'),option('false','No')]);
      } else {
        input = node('input'); input.type = 'text';
        input.placeholder = isHexField(field) ? 'Hexadecimal bytes' : isNumberField(field) ? 'Decimal or 0x hexadecimal' : '';
        input.autocomplete = 'off';
      }
      const value = values[field.name] ?? field.default;
      if (value !== undefined && value !== null) input.value = String(value);
      if (field.required) input.required = true;
      input.addEventListener('input', updatePreview); input.addEventListener('change', updatePreview);
      formInputs.set(field.name, input); label.append(input);
      if (isNumberField(field)) {
        const hint = node('span','small muted');
        const refresh = () => { try { const n = parseInteger(input.value, field.min ?? 0, field.max ?? 0xffffffff); hint.textContent = `${n} · ${asHex(n)}`; } catch { hint.textContent = field.required ? 'Required' : 'Optional'; } };
        input.addEventListener('input', refresh); input.addEventListener('change', refresh); refresh(); label.append(hint);
      }
      return label;
    });
    byId('command-fields').replaceChildren(...fields);
    note('command-help', command.name === 'raw' ? 'Raw payload is the complete pre-CRC frame. The radio does not construct its header from other fields.' :
      command.name === 'shade_level' ? 'Shade movement and feedback have separate results. Feedback does not measure shade position.' :
      command.name === 'feedback_map' ? 'Store a feedback mapping on the CCA node: device ID is the motor serial and target ID is the shade group.' :
      command.name === 'feedback_remove' ? 'Remove the CCA node mapping for this motor serial, shade group and subnet.' :
      command.name === 'feedback_list' ? 'List the CCA node mappings in activity.' :
      command.name.startsWith('ota_') ? 'Native OTA commands act directly. The firmware workflow below handles staging and prefix reconciliation.' : '');
    updatePreview();
  }
  function renderCommandChoices() {
    const select = byId('command-select'), previous = select.value, groups = new Map();
    for (const c of commands()) {
      const group = c.group || 'Other'; if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(c);
    }
    const options = [];
    for (const [group, list] of groups) {
      const g = node('optgroup'); g.label = group.charAt(0).toUpperCase() + group.slice(1);
      g.append(...list.map(c => option(c.name, `${c.title || c.name} (${c.name})`))); options.push(g);
    }
    select.replaceChildren(...options);
    const next = descriptor(previous) ? previous : commands()[0]?.name;
    if (next) select.value = next;
    renderCommandFields();
  }
  function uniqueId(base, existing) {
    const slug = String(base || 'item').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'item';
    let value = slug, i = 2; while (existing.some(x => x.id === value)) value = slug + '-' + i++;
    return value;
  }
  function targetDefaults() {
    const t = config?.targets?.find(x => x.id === byId('target-select').value);
    return t || {};
  }
  function applyTarget() {
    const target = targetDefaults();
    for (const [name, input] of formInputs) if (target[name] !== undefined && target[name] !== '') input.value = String(target[name]);
    if (target.device_id !== undefined) byId('ota-device').value = String(target.device_id);
    if (target.subnet !== undefined) byId('ota-subnet').value = String(target.subnet);
    updatePreview();
  }
  function renderSelectors() {
    const target = byId('target-select'), preset = byId('preset-select'), oldTarget = target.value, oldPreset = preset.value;
    selectOptions(target, [option('', 'No target defaults'), ...(config?.targets || []).map(t => option(t.id, t.name || t.id))], oldTarget);
    if (!(config?.targets || []).some(t => t.id === oldTarget)) target.value = '';
    selectOptions(preset, [option('', 'Choose a preset'), ...presetDrafts.map(p => option(p.id, p.name || p.id))], oldPreset);
    if (!presetDrafts.some(p => p.id === oldPreset)) preset.value = '';
  }
  function inputFor(value, label, onChange, placeholder = '') {
    const wrap = node('label', '', label), input = node('input');
    input.type = 'text'; input.value = value ?? ''; input.placeholder = placeholder; input.autocomplete = 'off';
    input.addEventListener('input', () => { onChange(input.value); configDirty = true; }); wrap.append(input); return wrap;
  }
  // Pico layouts, top to bottom as on the remote. Keys match the add-on's PICO_LAYOUTS.
  const PICO = {
    'pj2-2b':   {name:'Pico 2-button', buttons:[['on','On'],['off','Off']]},
    'pj2-2brl': {name:'Pico 2-button with raise/lower', buttons:[['on','On'],['raise','Raise'],['lower','Lower'],['off','Off']]},
    'pj2-3b':   {name:'Pico 3-button', buttons:[['on','On'],['fav','Favorite'],['off','Off']]},
    'pj2-3brl': {name:'Pico 3-button with raise/lower', buttons:[['on','On'],['raise','Raise'],['fav','Favorite'],['lower','Lower'],['off','Off']]},
    'pj2-4b':   {name:'Pico 4-button', buttons:[['1','Button 1'],['2','Button 2'],['3','Button 3'],['4','Button 4']]},
  };
  // Other device kinds a target can be drawn as. A shade is detected when the target ID is a Lutron shade zone.
  const KINDS = {shade:{name:'Shade', cmd:'shade_level'}, dimmer:{name:'Light (dimmer)', cmd:'bridge_level'}};
  const layoutName = l => PICO[l]?.name || KINDS[l]?.name || '';
  const PICO_TYPES = {Pico2Button:'pj2-2b', Pico2ButtonRaiseLower:'pj2-2brl', Pico3Button:'pj2-3b', Pico3ButtonRaiseLower:'pj2-3brl'};
  let selectedButton = null, lutronSig = '';
  // Keypads from Home Assistant's Lutron integration (read by the page); a Pico's serial is its CCA device ID.
  function lutronKeypads() {
    const k = typeof haData !== 'undefined' ? haData?.leap?.keypads : null;
    return k && typeof k === 'object' ? Object.values(k).filter(d => d && typeof d === 'object') : [];
  }
  function layoutOf(device) {
    const m = /^PJ2-([234])B(RL)?(?:-|$)/i.exec(String(device?.model || ''));
    if (m) return 'pj2-' + m[1] + 'b' + (m[2] ? 'rl' : '');
    if (/^Pico4Button/.test(device?.type || '')) return 'pj2-4b';
    return PICO_TYPES[device?.type] || null;
  }
  const lutronPicos = () => lutronKeypads().filter(d => Number.isSafeInteger(d.serial) && layoutOf(d));
  function deviceOf(target) {
    let id; try { id = parseInteger(target.device_id, 0, 0xffffffff); } catch { return null; }
    return lutronKeypads().find(d => d.serial === id) || null;
  }
  // The layout to draw: an explicit choice, else what the Lutron device says, else unknown (null).
  function zoneOf(target) {
    let id; try { id = parseInteger(target.target_id, 0, 0xffffffff); } catch { return null; }
    const z = typeof haData !== 'undefined' ? haData?.leap?.zones?.[String(id)] : null;
    return z && typeof z === 'object' ? {...z, id} : null;
  }
  function resolveLayout(target) {
    const device = deviceOf(target), zone = zoneOf(target);
    if (target.layout) return {layout: target.layout === 'none' ? null : target.layout, device, zone, source: 'chosen'};
    const layout = layoutOf(device);
    if (layout) return {layout, device, zone, source: 'lutron'};
    if (zone?.type === 'Shade') return {layout: 'shade', device, zone, source: 'lutron-zone'};
    return {layout: null, device, zone, source: device ? 'not-pico' : 'unknown'};
  }
  const zoneLabel = z => [...new Set([z.name || 'Zone ' + z.id, lutronArea(z.area)].filter(Boolean))].join(' · ') + ` · Lutron zone ${z.id}`;
  // Shade feedback mappings from the latest feedback_list listing in CCA activity (stored on the CCA node).
  function feedbackMappings() {
    const listed = rows.filter(e => e.kind === 'cca.feedback_mapping');
    const ids = listed.map(e => e.request_id).filter(Number.isInteger), latest = ids.length ? Math.max(...ids) : null;
    const seen = new Map();
    for (const e of listed) if (latest === null || e.request_id === latest)
      seen.set([e.serial, e.target_id, e.subnet, e.link_index, e.component].join(':'), e);
    return {listed: listed.length > 0, request: latest, mappings: [...seen.values()]};
  }
  // A command from the catalogue for this target: addresses from the target, level from the control.
  function levelCommand(target, cmd, level) {
    const d = descriptor(cmd);
    if (!d) return {error: `This CCA firmware does not offer ${cmd}.`};
    const command = {cmd};
    for (const f of d.fields || []) {
      if (f.name === 'level_pct') { command.level_pct = level; continue; }
      const raw = target[f.name];
      if (addressFields.includes(f.name) && raw !== undefined && raw !== null && String(raw).trim() !== '') {
        try { command[f.name] = parseInteger(raw, f.min ?? 0, f.max ?? 0xffffffff, f.label || f.name); } catch (e) { return {error: e.message}; }
      } else if (f.required && f.default === undefined) return {error: `Set this target's ${(f.label || f.name).toLowerCase()} first.`};
    }
    return {command};
  }
  const lastLevel = new WeakMap();
  function levelView(target, layout, refresh) {
    const kind = KINDS[layout], wrap = node('div', 'level-wrap'), shade = layout === 'shade';
    const state = lastLevel.get(target) || {level: shade ? 100 : 50, sent: null}; lastLevel.set(target, state);
    const picture = node('div', shade ? 'shade-window' : 'cca-bulb'), fill = node('div', shade ? 'shade-fabric' : 'bulb-glow');
    if (shade) { picture.append(fill, node('div', 'shade-sill')); } else picture.append(fill, node('div', 'bulb-base'));
    const readout = node('strong', 'level-readout'), slider = node('input');
    slider.type = 'range'; slider.min = '0'; slider.max = '100'; slider.step = '1'; slider.value = String(state.level);
    slider.setAttribute?.('aria-label', shade ? 'Shade level (100 is open)' : 'Light level');
    const draw = () => {
      const v = state.level;
      if (shade) fill.style.height = (100 - v) + '%'; else fill.style.opacity = String(v / 100);
      readout.textContent = shade ? (v >= 100 ? 'Open' : v <= 0 ? 'Closed' : v + '% open') : (v <= 0 ? 'Off' : v + '%');
    };
    slider.addEventListener('input', () => { state.level = Number(slider.value); draw(); });
    const controls = node('div', 'level-controls'), quick = node('div', 'actions');
    const out = node('p', 'small muted'); out.id = 'cca-device-note-' + (target.id || 'target'); out.setAttribute?.('role', 'status');
    const send = level => {
      if (level !== undefined) { state.level = level; slider.value = String(level); draw(); }
      const built = levelCommand(target, kind.cmd, state.level);
      if (built.error) { out.textContent = built.error; return; }
      state.sent = state.level; sendCommand(built.command, 'device-note-' + (target.id || 'target'));
    };
    const a = node('button', 'secondary', shade ? 'Open' : 'On'), b = node('button', 'secondary', shade ? 'Close' : 'Off'), go = node('button', '', 'Send level');
    a.onclick = () => send(100); b.onclick = () => send(0); go.onclick = () => send();
    quick.append(a, b, go);
    const preview = levelCommand(target, kind.cmd, state.level);
    controls.append(readout, slider, quick, node('p', 'small muted', preview.error || `Sends ${kind.cmd} once with this target's addresses.` + (shade ? ' Feedback, when mapped, acknowledges the target; it does not measure the shade.' : '')), out);
    if (shade) controls.append(motorsView(target));
    draw(); wrap.append(picture, controls); return wrap;
  }
  function motorsView(target) {
    const box = node('div', 'shade-motors'), {listed, mappings} = feedbackMappings();
    let group, subnet; try { group = parseInteger(target.target_id); } catch {} try { subnet = parseInteger(target.subnet); } catch {}
    const mine = mappings.filter(m => m.target_id === group && (subnet === undefined || m.subnet === subnet));
    box.append(node('strong', 'small', 'Feedback motors on the CCA node'));
    if (!listed) box.append(node('p', 'small muted', 'Not listed yet. List the CCA node\'s mappings to see which motors report for this shade.'));
    else if (!mine.length) box.append(node('p', 'small muted', 'No motor is mapped for this shade group' + (subnet === undefined ? '.' : ' and subnet.')));
    else { const list = node('ul', 'motor-list'); for (const m of mine) list.append(node('li', '', `Motor ${display(m.serial)} · link ${display(m.link_index)}` + (m.component !== undefined ? ` · component ${display(m.component)}` : ''))); box.append(list); }
    if (descriptor('feedback_list')) { const list = node('button', 'plain', listed ? 'List again' : 'List mappings'); list.onclick = () => listMappings('device-note-' + (target.id || 'target')); box.append(list); }
    return box;
  }
  function listMappings(noteId) { return sendCommand({cmd:'feedback_list'}, noteId); }
  const deviceLabel = d => [d.name, d.device].filter(Boolean).join(' ') + (lutronArea(d.area) ? ' · ' + lutronArea(d.area) : '') + (d.model ? ' · ' + d.model : '');
  function lutronArea(id) { const a = typeof haData !== 'undefined' ? haData?.leap?.areas?.[String(id)] : null; return a?.path || a?.name || ''; }
  const presetById = id => presetDrafts.find(p => p.id === id);
  function presetCommand(preset) { try { const c = JSON.parse(preset.text); return c && typeof c === 'object' && !Array.isArray(c) ? c : null; } catch { return null; } }
  // A preset's command as short readable chips: its title, then each field.
  function commandChips(command) {
    const wrap = node('div', 'cca-chips');
    if (!command) { wrap.append(node('span', 'cca-chip bad', 'Invalid JSON')); return wrap; }
    const d = descriptor(command.cmd);
    wrap.append(node('span', 'cca-chip cmd', d?.title || command.cmd || 'No cmd'));
    for (const [k, v] of Object.entries(command)) if (k !== 'cmd') {
      const field = d?.fields?.find(f => f.name === k), label = field?.label || k.replace(/_/g, ' ');
      const shown = /_pct$/.test(k) && typeof v === 'number' ? v + '%' : (k === 'device_id' || k === 'target_id') && Number.isSafeInteger(v) ? `${v} (${asHex(v)})` : display(v);
      wrap.append(node('span', 'cca-chip', `${label} ${shown}`));
    }
    return wrap;
  }
  function usesOf(presetId) {
    const uses = [];
    for (const t of config?.targets || []) for (const [key, id] of Object.entries(t.buttons || {})) if (id === presetId) {
      const layout = resolveLayout(t).layout, label = PICO[layout]?.buttons.find(b => b[0] === key)?.[1] || 'Button ' + key;
      uses.push(`${t.name || t.id} · ${label}`);
    }
    return uses;
  }
  function picoView(target, layout, refresh) {
    const wrap = node('div', 'pico-wrap'), body = node('div', 'pico'), legend = node('div', 'pico-legend');
    body.title = PICO[layout].name;
    for (const [key, label] of PICO[layout].buttons) {
      const preset = presetById(target.buttons?.[key]), active = selectedButton?.target === target && selectedButton.key === key;
      const kind = key === 'raise' || key === 'lower' ? 'rl' : key === 'fav' ? 'fav' : layout === 'pj2-4b' ? 'scene' : 'main';
      const b = node('button', 'pk ' + kind + (preset ? ' set' : '') + (active ? ' on' : ''), key === 'raise' ? '▲' : key === 'lower' ? '▼' : key === 'fav' ? '' : layout === 'pj2-4b' ? key : label);
      b.type = 'button'; b.title = label + (preset ? ': ' + (preset.name || preset.id) : ': not programmed');
      b.setAttribute?.('aria-label', b.title); b.setAttribute?.('aria-pressed', String(active)); b.picoKey = key;
      const pick = () => {
        const previous = selectedButton?.target; selectedButton = active ? null : {target, key}; refresh();
        if (previous && previous !== target) targetItems.find(item => item.target === previous)?.refresh();
      };
      b.onclick = pick; body.append(b);
      const row = node('button', 'pico-row' + (active ? ' on' : '')); row.type = 'button'; row.onclick = pick; row.picoKey = key;
      row.append(node('strong', '', label), node('span', preset ? '' : 'muted', preset ? preset.name || preset.id : 'Not programmed'));
      legend.append(row);
    }
    wrap.append(body, legend);
    if (selectedButton?.target === target) wrap.append(buttonPanel(target, layout, selectedButton.key, refresh));
    return wrap;
  }
  function buttonPanel(target, layout, key, refresh) {
    const label = PICO[layout].buttons.find(b => b[0] === key)?.[1] || key, panel = node('div', 'pico-panel');
    panel.append(node('h4', '', `${label} on ${target.name || target.id}`));
    const choose = node('label', '', 'Runs preset'), select = node('select');
    selectOptions(select, [option('', 'Nothing (not programmed)'), ...presetDrafts.map(p => option(p.id, p.name || p.id))], target.buttons?.[key] || '');
    select.id = 'cca-button-preset';
    select.addEventListener('change', () => {
      target.buttons = {...(target.buttons || {})};
      if (select.value) target.buttons[key] = select.value; else delete target.buttons[key];
      configDirty = true; refresh(); renderPresetUses(); note('config-note', 'Button programming changed. Save settings to keep it.');
    });
    choose.append(select); panel.append(choose);
    const preset = presetById(target.buttons?.[key]);
    if (!preset) { panel.append(node('p', 'small muted', presetDrafts.length ? 'Choose a preset for this button, or add one below.' : 'Add a preset below, then assign it here.')); return panel; }
    const command = presetCommand(preset);
    panel.append(commandChips(command), node('pre', 'cca-json', command ? JSON.stringify(command, null, 2) : preset.text));
    const actions = node('div', 'actions'), send = node('button', '', 'Send now'), load = node('button', 'secondary', 'Open in command form'), edit = node('button', 'plain', 'Edit preset');
    send.disabled = !command || sending;
    send.onclick = () => sendCommand(command, 'button-note');
    load.onclick = () => { byId('preset-select').value = preset.id; loadPreset(); byId('command-select').scrollIntoView?.({behavior:'smooth', block:'center'}); };
    edit.onclick = () => { const card = presetCards.get(preset.id); if (card) { card.details.open = true; card.item.scrollIntoView?.({behavior:'smooth', block:'center'}); } };
    actions.append(send, load, edit);
    const out = node('p', 'small muted'); out.id = 'cca-button-note'; out.setAttribute?.('role', 'status');
    panel.append(actions, node('p', 'small muted', 'Sends the preset exactly as saved. A physical press of this Pico is not changed by this programming.'), out);
    return panel;
  }
  function targetCard(target, index) {
    const item = node('div', 'cca-item cca-target'), head = node('div', 'cca-fields'), device = node('div', 'cca-device');
    head.append(inputFor(target.name, 'Name', v => { target.name = v; renderSelectors(); renderPresetUses(); }),
      inputFor(target.id, 'ID', v => { target.id = v; renderSelectors(); }),
      ...addressFields.map(field => inputFor(target[field], field === 'device_id' ? 'Device ID (serial)' : field.replace('_', ' '),
        v => { target[field] = v; if (field !== 'subnet') refresh(); }, 'Decimal or 0x hexadecimal')));
    const layoutLabel = node('label', '', 'Device'), layoutSelect = node('select');
    selectOptions(layoutSelect, [option('', 'Detect from Lutron'), option('none', 'Plain target'), ...Object.entries(PICO).map(([k, v]) => option(k, v.name)), ...Object.entries(KINDS).map(([k, v]) => option(k, v.name))], target.layout || '');
    layoutSelect.addEventListener('change', () => { if (layoutSelect.value) target.layout = layoutSelect.value; else delete target.layout; configDirty = true; refresh(); renderPresetUses(); });
    layoutLabel.append(layoutSelect); head.append(layoutLabel);
    function refresh() {
      const r = resolveLayout(target), parts = [];
      const status = node('p', 'small ' + (r.layout || r.source === 'chosen' ? 'muted' : 'warn-text'));
      status.textContent = r.source === 'lutron' ? `${PICO[r.layout].name} from Lutron: ${deviceLabel(r.device)}` :
        r.source === 'lutron-zone' ? `Shade from Lutron: ${zoneLabel(r.zone)}` :
        r.source === 'chosen' ? (r.layout ? `Shown as ${layoutName(r.layout)} (chosen here)` : 'Shown as a plain target (chosen here).') +
          (r.device && layoutOf(r.device) && layoutOf(r.device) !== r.layout ? ` Lutron reports ${PICO[layoutOf(r.device)].name}.` : '') +
          (r.zone && r.layout !== 'shade' && r.zone.type === 'Shade' ? ' Lutron reports a shade.' : '') :
        r.source === 'not-pico' ? `Lutron device ${deviceLabel(r.device)} is not a Pico.` :
        String(target.device_id ?? '').trim() === '' && String(target.target_id ?? '').trim() === '' ? 'Enter a Pico\'s serial as the device ID or a shade\'s Lutron zone as the target ID, or choose under Device.' :
        lutronKeypads().length ? 'No Lutron Pico or shade matches these IDs. Choose what this target is under Device.' :
        'Home Assistant\'s Lutron data is not available, so the layout cannot be detected. Choose it under Device.';
      parts.push(status);
      if (PICO[r.layout]) parts.push(picoView(target, r.layout, refresh)); else if (KINDS[r.layout]) parts.push(levelView(target, r.layout, refresh));
      device.replaceChildren(...parts);
    }
    const remove = node('button', 'secondary', 'Remove target');
    remove.onclick = () => { if (selectedButton?.target === target) selectedButton = null; config.targets.splice(index, 1); configDirty = true; renderConfig(); };
    refresh(); item.refresh = refresh; item.target = target;
    item.append(head, device, remove); return item;
  }
  const presetCards = new Map();
  function renderPresetUses() {
    for (const [id, card] of presetCards) {
      const uses = usesOf(id);
      card.uses.textContent = uses.length ? 'Programmed on ' + uses.join(', ') : 'Not on any Pico button';
    }
  }
  function presetCard(preset, index) {
    const item = node('div', 'cca-item cca-preset'), fields = node('div', 'cca-fields'), chips = node('div'), uses = node('p', 'small muted');
    const showChips = () => chips.replaceChildren(commandChips(presetCommand(preset)));
    fields.append(inputFor(preset.name, 'Name', v => { preset.name = v; renderSelectors(); refreshTargets(); }),
      inputFor(preset.id, 'ID', v => {
        // Keep button programming pointing at this preset when its ID is edited.
        for (const t of config.targets) for (const [k, id] of Object.entries(t.buttons || {})) if (id === preset.id) t.buttons[k] = v;
        presetCards.delete(preset.id); preset.id = v; presetCards.set(v, card); renderSelectors();
      }));
    const details = node('details'), summary = node('summary', '', 'Command JSON'), input = node('textarea');
    input.value = preset.text; input.setAttribute?.('aria-label', 'Native command JSON');
    input.addEventListener('input', () => { preset.text = input.value; configDirty = true; showChips(); refreshTargets(); });
    details.append(summary, input); details.open = !presetCommand(preset);
    const remove = node('button', 'secondary', 'Remove preset');
    remove.onclick = () => {
      const n = usesOf(preset.id).length;
      for (const t of config.targets) for (const [k, id] of Object.entries(t.buttons || {})) if (id === preset.id) delete t.buttons[k];
      presetDrafts.splice(index, 1); configDirty = true; renderConfig();
      if (n) note('config-note', `Preset removed and cleared from ${n} Pico button${n === 1 ? '' : 's'}. Save settings to keep it.`);
    };
    showChips(); item.append(fields, chips, uses, details, remove);
    const card = {item, details, uses}; presetCards.set(preset.id, card); return item;
  }
  let targetItems = [];
  function refreshTargets() { for (const item of targetItems) item.refresh(); }
  function renderLutronPicos() {
    const select = byId('add-pico'), known = new Set((config?.targets || []).map(t => { try { return parseInteger(t.device_id); } catch { return null; } }));
    const picos = lutronPicos().filter(d => !known.has(d.serial));
    selectOptions(select, [option('', picos.length ? `Add a Pico from Lutron (${picos.length})` : lutronKeypads().length ? 'Every Lutron Pico is added' : 'No Lutron Picos available'),
      ...picos.map(d => option(String(d.serial), deviceLabel(d)))], '');
    select.disabled = !picos.length;
  }
  function renderConfig() {
    if (!config) return;
    byId('expected-mac').value = config.expected_mac || '';
    if (selectedButton && !config.targets.includes(selectedButton.target)) selectedButton = null;
    presetCards.clear();
    const presets = presetDrafts.map(presetCard);
    targetItems = config.targets.map(targetCard);
    byId('targets').replaceChildren(...(targetItems.length ? targetItems : [node('p','small muted','No saved targets yet. Add a Pico from Lutron, or add a target by hand.')]));
    byId('presets').replaceChildren(...(presets.length ? presets : [node('p','small muted','No saved presets yet.')]));
    renderPresetUses(); renderLutronPicos(); renderSelectors(); statusView();
  }
  // Called by the page when Home Assistant data refreshes; redraw only if the Lutron devices changed, so typing is not interrupted.
  function lutronChanged() {
    const sig = JSON.stringify(lutronKeypads().map(d => [d.serial, d.model, d.type, d.name, d.device, d.area]));
    if (sig === lutronSig) return; lutronSig = sig;
    if (config) { refreshTargets(); renderPresetUses(); renderLutronPicos(); }
  }
  function configPayload() {
    const expected = byId('expected-mac').value.trim().toLowerCase();
    if (expected && !/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/.test(expected)) throw Error('Expected MAC must have six colon-separated hexadecimal bytes.');
    const ids = new Set();
    const targets = config.targets.map(t => {
      const id = String(t.id || '').trim(), name = String(t.name || '').trim();
      if (!id || !name) throw Error('Every target needs an ID and name.');
      if (ids.has(id)) throw Error('Target IDs must be unique.'); ids.add(id);
      const value = {...t, id, name};
      if (!value.layout) delete value.layout;
      const layout = value.layout ? PICO[value.layout] : null, buttons = {};
      for (const [k, p] of Object.entries(t.buttons || {})) if (p && (!value.layout || layout?.buttons.some(b => b[0] === k))) buttons[k] = p;
      if (Object.keys(buttons).length) value.buttons = buttons; else delete value.buttons;
      for (const field of addressFields) {
        const raw = t[field];
        if (raw === undefined || raw === null || String(raw).trim() === '') delete value[field];
        else value[field] = parseInteger(raw, 0, field === 'subnet' ? 65535 : 0xffffffff, field);
      }
      return value;
    });
    ids.clear();
    const presets = presetDrafts.map(p => {
      const id = String(p.id || '').trim(), name = String(p.name || '').trim();
      if (!id || !name) throw Error('Every preset needs an ID and name.');
      if (ids.has(id)) throw Error('Preset IDs must be unique.'); ids.add(id);
      let command; try { command = JSON.parse(p.text); } catch { throw Error(`Preset ${name} has invalid JSON.`); }
      if (!command || typeof command !== 'object' || Array.isArray(command) || typeof command.cmd !== 'string' || !command.cmd) throw Error(`Preset ${name} needs a command object with a cmd.`);
      return {id, name, command};
    });
    for (const t of targets) for (const p of Object.values(t.buttons || {}))
      if (!ids.has(p)) throw Error(`${t.name} has a button programmed with a preset that no longer exists.`);
    return {expected_mac:expected, targets, presets};
  }
  function setConfig(value) {
    const v = value && typeof value === 'object' ? value : {};
    config = {expected_mac:v.expected_mac || '', targets:Array.isArray(v.targets) ? clone(v.targets) : [], presets:[]};
    presetDrafts = (Array.isArray(v.presets) ? v.presets : []).map(p => ({id:p.id, name:p.name, text:JSON.stringify(p.command || {},null,2)}));
    configDirty = false; renderConfig();
  }
  async function fetchConfig() {
    const value = await req('cca/config'); setConfig(value); note('config-note','Saved add-on settings loaded.');
  }
  function loadPreset() {
    const preset = presetDrafts.find(p => p.id === byId('preset-select').value);
    if (!preset) { note('config-note','Choose a preset to load.'); return; }
    let command; try { command = JSON.parse(preset.text); } catch { note('config-note','Preset JSON is invalid.'); return; }
    if (!command || !descriptor(command.cmd)) {
      byId('advanced-json').value = preset.text; note('config-note','This command is absent from current metadata. It is available in Advanced JSON for review.'); return;
    }
    byId('command-select').value = command.cmd; renderCommandFields(command);
    note('config-note','Preset loaded for review. Nothing was sent.');
  }
  async function confirmCommand(command) {
    if (command.cmd === 'unpair') return confirmSheet('Unpair this device?', 'This sends an unpair command over RF to the selected target. Review the command JSON and device ID before continuing.', 'Unpair');
    if (command.cmd === 'ota_full_tx') return confirmSheet('Start the RF firmware sequence?', 'This starts the full CCA firmware transfer over RF. Local sequence completion does not verify device installation.', 'Start RF transfer');
    return true;
  }
  async function sendCommand(command, noteId = 'command-note') {
    if (sending || nodeBusy()) return;
    if (!command || typeof command !== 'object' || Array.isArray(command) || typeof command.cmd !== 'string' || !command.cmd) {
      note(noteId,'Enter a command object with a cmd.'); return;
    }
    sending = true; updatePreview();
    try {
      if (!await confirmCommand(command)) return;
      activeReceipt = null; note(noteId,'Sending once; waiting for a gateway receipt.');
      const eventBoot = boot, after = cursor;
      const result = await req('cca/command', jsonOptions(command));
      const peer = status?.status?.c6 || status?.status?.peer || {};
      activeReceipt = {id:result.request_id, cmd:command.cmd, boot:peer.boot_id ?? null, eventBoot, after, stage:'accepted', reason:'', feedback:null, motorFeedback:null, noteId};
      showReceipt();
      for (const e of rows) updateReceipt(e);
      fetchEvents();
    } catch (e) { note(noteId,'Command response unavailable or rejected: ' + e.message + '. The outcome may be unknown; it will not be resent automatically.'); }
    finally { sending = false; updatePreview(); }
  }
  function automation(serial) {
    return `alias: CCA presence\ntriggers:\n  - trigger: state\n    entity_id: binary_sensor.replace_with_your_sensor\nactions:\n  - action: mqtt.publish\n    data:\n      topic: ccx_bridge/cca/presence/set\n      retain: false\n      payload: >-\n        {"serial":${serial},"state":"{{ 'occupied' if trigger.to_state.state == 'on' else 'vacant' if trigger.to_state.state == 'off' else 'unavailable' }}"}`;
  }
  function renderPresence() {
    try { const serial = parseInteger(byId('presence-serial').value,1,0xffffffff,'Sensor serial'); byId('presence-example').textContent = automation(serial); }
    catch { byId('presence-example').textContent = 'Enter a sensor serial to prepare the Home Assistant automation.'; }
  }
  async function presenceSend() {
    if (nodeBusy()) return;
    try {
      const serial = parseInteger(byId('presence-serial').value,1,0xffffffff,'Sensor serial'), state = byId('presence-state').value;
      if (!['occupied','vacant','unavailable'].includes(state)) throw Error('Choose a valid state.');
      const result = await req('cca/state',{...jsonOptions({serial,state}),method:'PUT'});
      note('presence-note', `Presence state accepted${result.revision !== undefined ? ' · revision ' + result.revision : ''}. Native LRF RF output is unsupported.`);
    } catch (e) { note('presence-note','Presence update failed or is uncertain: ' + e.message); }
  }
  function renderOta() {
    const o = ota || {}, upload = o.upload || {}, size = upload.size ?? o.size, hash = upload.sha256 ?? o.sha256;
    const phase = o.state || 'idle', staged = o.staged ?? o.received, expected = o.expected ?? size;
    pill('ota-pill', phase === 'idle' && !size ? 'No file' : phase.replaceAll('_',' '), o.error ? 'bad' : phase === 'staging' ? 'info' : 'wait');
    byId('ota-facts').replaceChildren(facts('Staged file and progress', [['Body size',size === undefined ? null : size + ' bytes'], ['Body SHA-256',hash],
      ['Staged locally',staged === undefined ? null : `${staged} of ${display(expected)} bytes`], ['State',phase], ['Error',o.error], ['Detail',o.message]]));
    note('ota-note', o.error ? 'Upload or staging error: ' + o.error : o.message || (size ? 'File is stored by the add-on. Stage or resume separately; no RF transfer starts automatically.' : 'Upload a file to the add-on before staging.'));
  }
  async function uploadFile() {
    if (otaSending || nodeBusy()) return;
    const file = byId('ota-file').files?.[0], format = byId('ota-format').value;
    if (!file) { note('ota-note','Choose an LDF container or extracted body first.'); return; }
    if (!['ldf','body'].includes(format)) { note('ota-note','Choose the correct file format.'); return; }
    otaSending = true; note('ota-note','Uploading once to the add-on; this does not start staging or RF.');
    try { ota = await req('cca/ota/file?format=' + encodeURIComponent(format),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file}); renderOta(); }
    catch (e) { note('ota-note','File upload failed or its response was lost: ' + e.message + '. Check status before retrying.'); }
    finally { otaSending = false; fetchStatus(); }
  }
  async function otaAction(action) {
    if (otaSending || nodeBusy()) return;
    const payload = {action};
    if (action === 'start') {
      try { payload.device_id = parseInteger(byId('ota-device').value,1,0xffffffff,'Device ID'); payload.subnet = parseInteger(byId('ota-subnet').value,0,65535,'Subnet'); }
      catch(e) { note('ota-note',e.message); return; }
      if (!await confirmSheet('Start the RF firmware transfer?', `Start the local RF sequence for device ${payload.device_id} on subnet ${payload.subnet}. Completion does not verify device installation.`, 'Start RF transfer')) return;
    }
    if (action === 'clear' && !await confirmSheet('Clear staged firmware?', 'This resets the staged device session. A later transfer will require staging again.', 'Clear')) return;
    otaSending = true; note('ota-note',action === 'stop_staging' ? 'Stopping add-on staging; this does not abort RF.' : 'Requesting ' + action + ' once.');
    try { ota = await req('cca/ota/action',jsonOptions(payload)); renderOta(); }
    catch(e) { note('ota-note','Action response unavailable or rejected: ' + e.message + '. Its outcome may be unknown; it will not be resent automatically.'); }
    finally { otaSending = false; fetchStatus(); }
  }
  async function pickNodeFile(file) {
    nodeFile = file || null; nodeFileValid = false;
    note('node-file-name', file ? `${file.name} · ${file.size.toLocaleString()} bytes` : 'Choose a signed CCA node image');
    note('node-file-note', '8,192–1,048,576 bytes, in 4,096-byte blocks'); renderNodeUpdate();
    if (!file) return;
    if (file.size < nodeMin || file.size > nodeMax || file.size % 4096) {
      note('node-file-note', 'Image must be 8,192–1,048,576 bytes in 4,096-byte blocks.'); return;
    }
    try {
      const bytes = new Uint8Array(await file.slice(0,1).arrayBuffer());
      if (nodeFile !== file) return;
      nodeFileValid = bytes[0] === 0xe9;
      note('node-file-note', nodeFileValid ? 'ESP image header found; the CCA node verifies the signature.' : 'Invalid ESP image: expected first byte 0xE9.');
    } catch { if (nodeFile !== file) return; note('node-file-note', 'Could not read the image header.'); }
    renderNodeUpdate();
  }
  async function uploadNode() {
    const file = nodeFile;
    if (!file || !nodeFileValid || !nodeReady()) return;
    if (!await confirmSheet('Update the CCA node?', `${file.name} (${file.size.toLocaleString()} bytes) will be sent once. CCA radio and controls pause; CCX and the event feed continue.`, 'Update')) return;
    if (file !== nodeFile || !nodeFileValid || !nodeReady()) { nodeMessage = 'CCA updater status changed; wait until it permits an update.'; renderNodeUpdate(); return; }
    nodeSending = true; nodeMessage = 'Sending once; waiting for gateway status.'; renderNodeUpdate();
    try {
      await req('cca/node-update', {method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
      nodeMessage = 'Image accepted for transfer. Checking node status.';
    } catch { nodeMessage = 'Upload response unavailable or rejected. Check status before any manual retry; the app will not resend automatically.'; }
    finally { nodeSending = false; await fetchStatus(); renderNodeUpdate(); }
  }
  async function initialize() {
    if (loading) return;
    loading = true; attemptAt = Date.now();
    try {
      const results = await Promise.allSettled([req('cca/capabilities'), req('cca/config')]);
      if (results[0].status === 'fulfilled') { capabilities = results[0].value; capsAt = Date.now(); renderCommandChoices(); }
      else { capabilities = null; renderCommandChoices(); note('command-help','CCA capabilities unavailable: ' + results[0].reason.message); }
      if (results[1].status === 'fulfilled') { setConfig(results[1].value); note('config-note','Saved add-on settings loaded.'); }
      else note('config-note','Settings unavailable: ' + results[1].reason.message);
      loaded = results.every(r => r.status === 'fulfilled');
    } finally { loading = false; }
    await Promise.all([fetchStatus(),fetchEvents()]);
  }
  async function refreshCapabilities() {
    if (fetchingCaps) return;
    fetchingCaps = true; capsAt = Date.now();
    try {
      const next = await req('cca/capabilities');
      if (JSON.stringify(next) !== JSON.stringify(capabilities)) { capabilities = next; renderCommandChoices(); }
    } catch(e) { if (!commands().length) note('command-help','CCA capabilities unavailable: ' + e.message); }
    finally { fetchingCaps = false; }
  }
  function show() { return !loaded ? initialize() : Promise.all([fetchStatus(),fetchEvents()]); }

  byId('command-select').onchange = () => { renderCommandFields(); applyTarget(); };
  byId('target-select').onchange = applyTarget;
  byId('load-preset').onclick = loadPreset;
  byId('command-send').onclick = () => { const c = updatePreview(); if (c) sendCommand(c); };
  byId('save-preset').onclick = () => {
    const command = updatePreview(); if (!command || !config) return;
    const d = descriptor(command.cmd), id = uniqueId(command.cmd, presetDrafts);
    presetDrafts.push({id, name:d?.title || command.cmd, text:JSON.stringify(command,null,2)});
    configDirty = true; renderConfig(); byId('preset-select').value = id; note('config-note','Preset added to the editor. Save settings to keep it.');
  };
  byId('add-target').onclick = () => { if (!config) return; config.targets.push({id:uniqueId('target',config.targets),name:'New target'}); configDirty = true; renderConfig(); };
  byId('add-pico').addEventListener('change', () => {
    const d = lutronPicos().find(x => String(x.serial) === byId('add-pico').value);
    if (!config || !d) return;
    const name = [d.name, d.device].filter(Boolean).join(' ') || 'Pico ' + d.serial;
    const target = {id:uniqueId(name, config.targets), name, device_id:String(d.serial)};
    config.targets.push(target); selectedButton = {target, key:PICO[layoutOf(d)].buttons[0][0]}; configDirty = true; renderConfig();
    note('config-note', `${name} added as a ${PICO[layoutOf(d)].name}. Choose what each button runs, then save settings.`);
  });
  byId('add-preset').onclick = () => { if (!config) return; const id = uniqueId('preset',presetDrafts); presetDrafts.push({id,name:'New preset',text:'{"cmd":"query"}'}); configDirty = true; renderConfig(); };
  byId('expected-mac').addEventListener('input', () => { configDirty = true; if (config) config.expected_mac = byId('expected-mac').value; statusView(); });
  byId('config-save').onclick = async () => {
    if (!config) return;
    try { const payload = configPayload(); const result = await req('cca/config',{...jsonOptions(payload),method:'PUT'}); setConfig(result && Array.isArray(result.targets) ? result : payload); note('config-note','Settings saved in the add-on.'); }
    catch(e) { note('config-note','Settings were not saved: ' + e.message); }
  };
  byId('config-reload').onclick = async () => {
    if (configDirty && !await confirmSheet('Discard CCA setting edits?', 'Unsaved target, preset and expected MAC changes will be lost.', 'Discard')) return;
    try { await fetchConfig(); } catch(e) { note('config-note','Settings unavailable: ' + e.message); }
  };
  byId('presence-serial').addEventListener('input',renderPresence); byId('presence-send').onclick = presenceSend;
  byId('presence-copy').onclick = async () => {
    try { const serial = parseInteger(byId('presence-serial').value,1,0xffffffff,'Sensor serial'); await navigator.clipboard.writeText(automation(serial)); note('presence-note','Automation copied. Replace the example sensor entity in Home Assistant.'); }
    catch(e) { note('presence-note','Could not copy: ' + e.message + '. You can select and copy the example below.'); }
  };
  byId('events-filter').addEventListener('input',renderEvents);
  byId('events-download').onclick = () => {
    const file = new Blob([JSON.stringify({boot_id:boot,cursor,stream,lost,events:rows},null,2)],{type:'application/json'}), url = URL.createObjectURL(file), a = node('a');
    a.href = url; a.download = 'cca-activity.json'; a.click(); URL.revokeObjectURL(url);
  };
  byId('ota-upload').onclick = uploadFile;
  for (const [id, action] of [['stage','stage'],['resume','resume'],['stop','stop_staging'],['clear','clear'],['start','start']]) byId('ota-' + id).onclick = () => otaAction(action);
  byId('node-file').onchange = e => pickNodeFile(e.target.files?.[0] || null);
  byId('node-upload').onclick = uploadNode;
  byId('advanced-send').onclick = () => {
    try { sendCommand(JSON.parse(byId('advanced-json').value),'advanced-note'); }
    catch(e) { note('advanced-note','Invalid JSON: ' + e.message); }
  };
  renderPresence(); renderOta();
  setInterval(() => {
    if (!['panel-cca', 'panel-local'].some(id => document.getElementById(id)?.hidden === false)) return;
    if (!loaded) { if (Date.now() - attemptAt >= 10000) initialize(); return; }
    fetchStatus(); fetchEvents(); if (Date.now() - capsAt >= 30000) refreshCapabilities();
  },2000);
  // For the Local programming tab: the CCA node's mappings, and listing them again.
  const local = {
    mappings: feedbackMappings, canList: () => !!descriptor('feedback_list'), list: listMappings, ready: () => loaded,
    onChange(f) { eventListeners.push(f); },
  };
  return {show, lutronChanged, local, _test:{levelCommand, feedbackMappings, layoutOf, resolveLayout, selectButton(target, key) { selectedButton = target ? {target:config.targets.find(t => t.id === target), key} : null; renderConfig(); }, config:() => config, parseInteger, asHex, parseField, commandFromForm, eventView, packetView, receiptText, configPayload, setConfig, renderCommandChoices, sendCommand, automation, otaAction, pickNodeFile, uploadNode, nodeReady,
    setCapabilities(v) { capabilities = v; renderCommandChoices(); }, setStatus(v) { status = v; statusAt = Date.now(); statusView(); },
    state() { return {boot,cursor,rows:clone(rows),activeReceipt,configDirty}; }}};
})();
