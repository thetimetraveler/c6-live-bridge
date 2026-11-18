#!/usr/bin/env node
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./cca-decoder.js', import.meta.url), 'utf8');
const context = vm.createContext({});
vm.runInContext(source, context);
const decoder = context.CCAProtocol;
assert.deepEqual(Object.keys(decoder).sort(), ['describeShortAck', 'identifyPacket', 'parseFieldValue']);

// Synthetic/redacted sensor frame with a valid CRC-16/CA0F trailer.
const frame = Buffer.from('8800deadbeef210e0400022600deadbeef004707ff005574', 'hex');
let crc = 0;
for (const byte of frame.subarray(0, -2)) {
  let table = crc & 0xff00;
  for (let i = 0; i < 8; i++) table = (table & 0x8000 ? (table << 1) ^ 0xca0f : table << 1) & 0xffff;
  crc = ((crc << 8) & 0xff00) ^ byte ^ table;
}
assert.equal(crc, frame.readUInt16BE(frame.length - 2));
const identified = decoder.identifyPacket(frame);
assert.equal(identified.typeName, 'SENSOR_EXTENDED');
assert.equal(identified.category, 'SENSOR');
const flags = identified.fields.find(field => field.name === 'event_flags');
assert.equal(flags.offset, 11);
const bytes = [...frame].map(byte => byte.toString(16).padStart(2, '0').toUpperCase());
const parsed = decoder.parseFieldValue(bytes, flags.offset, flags.size, flags.format, flags.name);
assert.equal(parsed.raw, '26');
assert.equal(parsed.decoded, null);
assert.equal(decoder.identifyPacket([0x07, 0x05, 0xcb, 0x2d, 0xf7]).typeName, 'ACK_07');
assert.equal(decoder.identifyPacket([0x07, 0x05, 0xcb, 0x2d, 0xf6]).typeName, '0x07');
assert.equal(decoder.identifyPacket(Buffer.from('072831000d617c614c3924fa', 'hex')).typeName, 'ACK_07');
const poll = Buffer.from('8101a1123400210a00ffffffffff09060000cccccccc59e8', 'hex');
assert.equal(decoder.describeShortAck(Buffer.from('17023d8498','hex'), poll), 'level=24% entry=5');
assert.equal(decoder.describeShortAck(Buffer.from('17023d8499','hex'), poll), undefined);
console.log('CCA decoder bundle tests passed');
