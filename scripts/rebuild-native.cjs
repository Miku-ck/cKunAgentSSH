#!/usr/bin/env node
'use strict';

// Rebuild native modules (node-pty, cpu-features, ...) against the Electron
// ABI. This replaces the bare `electron-builder install-app-deps` postinstall.
//
// Why this wrapper exists:
//   cpu-features@0.0.10's install script is
//     `node buildcheck.js > buildcheck.gypi && node-gyp rebuild`
//   i.e. buildcheck.gypi is generated at install time and is NOT in the
//   published tarball. `electron-builder install-app-deps` (@electron/rebuild)
//   calls node-gyp directly and skips that npm lifecycle, so buildcheck.gypi is
//   missing and `gyp` aborts at configure:
//     gyp: buildcheck.gypi not found (while reading includes of
//          deps/cpu_features/cpu_features.gyp)
//
//   We make sure buildcheck.gypi exists before handing off to install-app-deps.
//   Prefer the package's own detector (needs a C toolchain + registry access);
//   fall back to a static, x64/win-correct stub when the detector can't run
//   (e.g. sandboxed CI, no MSVC on PATH). On Windows x64 the gyp condition is
//   false, so the stub's defines/sources are never applied and it is equivalent
//   to a real detection result.

const { execSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const cpuFeatures = path.join(root, 'node_modules', 'cpu-features');
const gypi = path.join(cpuFeatures, 'buildcheck.gypi');
const buildcheckJs = path.join(cpuFeatures, 'buildcheck.js');

const STATIC_GYPI = JSON.stringify(
  {
    conditions: [
      [
        'OS!="win" and target_arch not in "ia32 x32 x64"',
        {
          defines: [],
          libraries: [],
          sources: [
            'deps/cpu_features/include/internal/hwcaps.h',
            'deps/cpu_features/src/hwcaps.c',
          ],
        },
      ],
    ],
  },
  null,
  2,
);

if (fs.existsSync(buildcheckJs) && !fs.existsSync(gypi)) {
  let generated = false;
  try {
    console.log('[rebuild-native] generating cpu-features/buildcheck.gypi via buildcheck.js');
    execSync('node buildcheck.js > buildcheck.gypi', { cwd: cpuFeatures, stdio: 'inherit' });
    generated = fs.existsSync(gypi);
  } catch (err) {
    console.warn('[rebuild-native] buildcheck.js failed: ' + (err && err.message ? err.message.split('\n')[0] : err));
  }
  if (!generated) {
    console.warn('[rebuild-native] writing static x64/win-safe buildcheck.gypi fallback');
    fs.writeFileSync(gypi, STATIC_GYPI);
  }
}

console.log('[rebuild-native] electron-builder install-app-deps');
execSync('electron-builder install-app-deps', { cwd: root, stdio: 'inherit' });
