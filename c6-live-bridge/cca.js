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
  let capabilities = null, config = null, status = null, statusAt = 0, ota = null;
  let boot = null, cursor = 0, rows = [], keys = new Set(), stream = {}, lost = 0;
  // Rows the user has expanded, kept across re-renders so new packets do not collapse them.
  const openEvents = new Set();
  let activeReceipt = null, formInputs = new Map(), presetDrafts = [];
  let loaded = false, loading = false, fetchingEvents = false, fetchingStatus = false, fetchingCaps = false;
  let sending = false, otaSending = false, configDirty = false, capsAt = 0, attemptAt = 0;

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
  }
  // Re-render without disturbing an open row the user is reading. New packets land above it, so scroll the list back by however
  // far it was pushed. Rows are rebuilt, so carry over the scroll position of the JSON box inside each open row.
  function holdOpenRow(box, render) {
    const open = [...box.children].filter(d => d.open && d.rowKey !== undefined && d.getBoundingClientRect);
    if (!open.length) return render();
    const inner = new Map(open.map(d => [d.rowKey, [...d.querySelectorAll('pre')].map(x => [x.scrollTop, x.scrollLeft])]));
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const anchor = open.find(d => { const r = d.getBoundingClientRect(); return r.bottom > 0 && r.top < vh; }) || open[0];
    const before = anchor.getBoundingClientRect().top;
    render();
    const after = [...box.children];
    for (const d of after) {
      const saved = inner.get(d.rowKey);
      if (saved && d.open) d.querySelectorAll('pre').forEach((x, i) => { if (saved[i]) { x.scrollTop = saved[i][0]; x.scrollLeft = saved[i][1]; } });
    }
    const now = after.find(d => d.rowKey === anchor.rowKey);
    if (!now) return;
    const delta = now.getBoundingClientRect().top - before;
    if (!delta) return;
    for (let s = box; s && s !== document.body && s !== document.documentElement; s = s.parentElement)
      if (s.scrollHeight > s.clientHeight && /auto|scroll/.test(getComputedStyle(s).overflowY)) { s.scrollTop += delta; return; }
    window.scrollBy(0, delta);
  }
  function renderEvents() {
    const filter = byId('events-filter').value.trim().toLowerCase();
    const matching = rows.filter(e => !filter || JSON.stringify(e).toLowerCase().includes(filter));
    const rendered = matching.slice().reverse().map(e => {
      const d = node('details'), summary = node('summary');
      const parts = [e.kind || 'CCA event', e.cmd, e.request_id !== undefined ? 'request ' + e.request_id : '',
        e.stage, Number.isFinite(e.at_ms) ? 'node uptime ' + e.at_ms + ' ms' : '',
        e.rssi !== undefined ? 'RSSI ' + e.rssi : '', e.crc_valid === true ? 'CRC valid' : e.crc_valid === false ? 'CRC invalid' : ''];
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
      d.append(summary, node('pre', 'cca-json', JSON.stringify(e, null, 2)));
      const key = String(boot) + ':' + e.id;
      d.rowKey = key; d.open = openEvents.has(key);
      d.addEventListener('toggle', () => { if (d.open) openEvents.add(key); else openEvents.delete(key); });
      return d;
    });
    const box = byId('events');
    holdOpenRow(box, () => box.replaceChildren(...(rendered.length ? rendered : [node('p', 'empty', rows.length ? 'No activity matches this filter.' : 'Waiting for CCA activity…')])));
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
    } catch (e) { status = {error:e.message}; statusAt = Date.now(); statusView(); }
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
      byId('command-send').disabled = sending;
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
  function renderConfig() {
    if (!config) return;
    byId('expected-mac').value = config.expected_mac || '';
    const targets = config.targets.map((target, index) => {
      const item = node('div', 'cca-item'), fields = node('div', 'cca-fields');
      fields.append(inputFor(target.id, 'ID', v => { target.id = v; renderSelectors(); }),
        inputFor(target.name, 'Name', v => { target.name = v; renderSelectors(); }),
        ...addressFields.map(field => inputFor(target[field], field.replace('_', ' '), v => { target[field] = v; }, 'Decimal or 0x hexadecimal')));
      const remove = node('button', 'secondary', 'Remove target'); remove.onclick = () => { config.targets.splice(index, 1); configDirty = true; renderConfig(); };
      item.append(fields, remove); return item;
    });
    byId('targets').replaceChildren(...(targets.length ? targets : [node('p','small muted','No saved targets yet.')]));
    const presets = presetDrafts.map((preset, index) => {
      const item = node('div','cca-item'), fields = node('div','cca-fields');
      fields.append(inputFor(preset.id, 'ID', v => { preset.id = v; renderSelectors(); }), inputFor(preset.name, 'Name', v => { preset.name = v; renderSelectors(); }));
      const label = node('label','','Native command JSON'), input = node('textarea'); input.value = preset.text;
      input.addEventListener('input', () => { preset.text = input.value; configDirty = true; }); label.append(input);
      const remove = node('button','secondary','Remove preset'); remove.onclick = () => { presetDrafts.splice(index,1); configDirty = true; renderConfig(); };
      item.append(fields,label,remove); return item;
    });
    byId('presets').replaceChildren(...(presets.length ? presets : [node('p','small muted','No saved presets yet.')]));
    renderSelectors(); statusView();
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
    if (sending) return;
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
    if (otaSending) return;
    const file = byId('ota-file').files?.[0], format = byId('ota-format').value;
    if (!file) { note('ota-note','Choose an LDF container or extracted body first.'); return; }
    if (!['ldf','body'].includes(format)) { note('ota-note','Choose the correct file format.'); return; }
    otaSending = true; note('ota-note','Uploading once to the add-on; this does not start staging or RF.');
    try { ota = await req('cca/ota/file?format=' + encodeURIComponent(format),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file}); renderOta(); }
    catch (e) { note('ota-note','File upload failed or its response was lost: ' + e.message + '. Check status before retrying.'); }
    finally { otaSending = false; fetchStatus(); }
  }
  async function otaAction(action) {
    if (otaSending) return;
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
  byId('advanced-send').onclick = () => {
    try { sendCommand(JSON.parse(byId('advanced-json').value),'advanced-note'); }
    catch(e) { note('advanced-note','Invalid JSON: ' + e.message); }
  };
  renderPresence(); renderOta();
  setInterval(() => {
    if (document.getElementById('panel-cca')?.hidden !== false) return;
    if (!loaded) { if (Date.now() - attemptAt >= 10000) initialize(); return; }
    fetchStatus(); fetchEvents(); if (Date.now() - capsAt >= 30000) refreshCapabilities();
  },2000);
  return {show, _test:{parseInteger, asHex, parseField, commandFromForm, eventView, receiptText, configPayload, setConfig, renderCommandChoices, sendCommand, automation, otaAction,
    setCapabilities(v) { capabilities = v; renderCommandChoices(); }, setStatus(v) { status = v; statusAt = Date.now(); statusView(); },
    state() { return {boot,cursor,rows:clone(rows),activeReceipt,configDirty}; }}};
})();
