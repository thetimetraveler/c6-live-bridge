#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Run from this add-on checkout; pass the protocol repository root if needed.
const addonRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceRoot = path.resolve(process.argv[2] || path.join(addonRoot, '..'));
const esbuild = createRequire(path.join(sourceRoot, 'package.json'))('esbuild');
const entry = 'export { identifyPacket, parseFieldValue } from "./protocol/protocol-ui.ts";';
const result = await esbuild.build({
  absWorkingDir: sourceRoot,
  stdin: { contents: entry, resolveDir: sourceRoot, loader: 'ts' },
  bundle: true, platform: 'browser', format: 'iife', globalName: 'CCAProtocol',
  target: 'es2020', minify: true, legalComments: 'none', write: false, metafile: true,
});
const hash = createHash('sha256').update(entry).update('\0');
for (const name of Object.keys(result.metafile.inputs).filter(n => n !== '<stdin>').sort()) {
  hash.update(name).update('\0').update(readFileSync(path.resolve(sourceRoot, name))).update('\0');
}
const digest = hash.digest('hex');
const banner = `// Generated from protocol/protocol-ui.ts and dependencies (SHA-256 ${digest}); esbuild ${esbuild.version}. Do not edit.\n`;
const output = banner + result.outputFiles[0].text;
writeFileSync(path.join(addonRoot, 'c6-live-bridge/cca-decoder.js'), output);
console.log(`CCA decoder: ${Buffer.byteLength(output)} bytes; source SHA-256 ${digest}`);
