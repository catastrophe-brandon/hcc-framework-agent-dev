#!/usr/bin/env node
import { access, mkdtemp, readFile, rm } from 'node:fs/promises';
import { constants } from 'node:fs';
import { once } from 'node:events';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const alias = process.argv[2] || process.env.UI_HARNESS_PROFILE || 'viewer';
const baseUrl = 'http://127.0.0.1:8765';
const chromePath = process.env.CHROME_BIN;
if (!chromePath) {
  throw new Error('Set CHROME_BIN to a local Chromium executable.');
}
await access(chromePath, constants.X_OK);

const profiles = JSON.parse(await readFile(path.join(scriptDir, 'fake-auth', 'profiles.json'), 'utf8'));
const profile = profiles[alias];
if (!profile) {
  throw new Error(`Unknown local test profile: ${alias}`);
}
const configuredUsername = process.env.UI_HARNESS_USERNAME;
const configuredPassword = process.env.UI_HARNESS_PASSWORD;
if (Boolean(configuredUsername) !== Boolean(configuredPassword)) {
  throw new Error('Set both UI_HARNESS_USERNAME and UI_HARNESS_PASSWORD.');
}
if (configuredUsername && alias === (process.env.UI_HARNESS_PROFILE || 'viewer')) {
  profile.username = configuredUsername;
  profile.password = configuredPassword;
}

const browserDir = await mkdtemp(path.join(os.tmpdir(), 'ui-test-agent-chrome-'));
let chrome;
let socket;
let requestId = 0;
const pending = new Map();

function connectCdp(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    ws.addEventListener('open', () => resolve(ws), { once: true });
    ws.addEventListener('error', () => reject(new Error('Could not connect to local Chromium CDP.')), { once: true });
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) return;
      const waiter = pending.get(message.id);
      if (!waiter) return;
      pending.delete(message.id);
      if (message.error) waiter.reject(new Error(`CDP command failed: ${message.error.message}`));
      else waiter.resolve(message.result || {});
    });
  });
}

function cdp(method, params = {}) {
  const id = ++requestId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await cdp('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  if (result.exceptionDetails) throw new Error('Local browser fixture interaction failed.');
  return result.result?.value;
}

async function waitFor(expression, expected, timeoutMs = 8000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (await evaluate(expression) === expected) return;
    await delay(100);
  }
  throw new Error('Timed out waiting for the local fixture state.');
}

async function setInput(selector, value) {
  const valueJson = JSON.stringify(value);
  const selectorJson = JSON.stringify(selector);
  const succeeded = await evaluate(`(() => {
    const field = document.querySelector(${selectorJson});
    if (!field) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(field, ${valueJson});
    field.dispatchEvent(new Event('input', {bubbles: true}));
    field.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  })()`);
  if (!succeeded) throw new Error('Expected fixture input was not found.');
}

try {
  const health = await fetch(`${baseUrl}/login`);
  if (!health.ok) throw new Error('Start the fake HCC server before running this check.');

  chrome = spawn(chromePath, [
    '--headless=new',
    '--no-sandbox',
    '--disable-gpu',
    '--no-first-run',
    '--disable-sync',
    '--disable-extensions',
    '--proxy-server=direct://',
    '--proxy-bypass-list=*',
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=0',
    `--user-data-dir=${browserDir}`,
    'about:blank',
  ], {
    stdio: 'ignore',
    env: {
      PATH: process.env.PATH || '',
      HOME: browserDir,
      TMPDIR: os.tmpdir(),
      LANG: 'C',
    },
  });
  let launchError;
  chrome.on('error', (error) => { launchError = error; });

  let target;
  const deadline = Date.now() + 10000;
  while (!target && Date.now() < deadline) {
    if (launchError || chrome.exitCode !== null) break;
    try {
      const ports = (await readFile(path.join(browserDir, 'DevToolsActivePort'), 'utf8')).trim().split('\n');
      const targets = await fetch(`http://127.0.0.1:${ports[0]}/json/list`);
      const pages = await targets.json();
      target = pages.find((page) => page.type === 'page');
    } catch {
      await delay(100);
    }
  }
  if (!target?.webSocketDebuggerUrl) {
    throw new Error(launchError ? 'Could not launch local Chromium.' : 'Local Chromium did not start its debugging endpoint.');
  }

  socket = await connectCdp(target.webSocketDebuggerUrl);
  await cdp('Page.enable');
  await cdp('Runtime.enable');
  await cdp('Page.navigate', { url: `${baseUrl}/login` });
  await waitFor('document.readyState', 'complete');

  await setInput('#username', profile.username);
  const passwordStepShown = await evaluate(`(() => {
    document.querySelector('#continue').click();
    return !document.querySelector('#password-step').hidden;
  })()`);
  if (!passwordStepShown) throw new Error('The username step did not advance to password entry.');

  await setInput('#password', 'intentionally-wrong-fixture-value');
  await evaluate("document.querySelector('#login-form').requestSubmit()");
  await waitFor("document.querySelector('#status').textContent", 'Sign-in failed');

  await setInput('#password', profile.password);
  await evaluate("document.querySelector('#login-form').requestSubmit()");
  await waitFor("document.querySelector('#status').textContent", 'Signed in');
  const observed = await evaluate(`({
    identity: document.querySelector('#identity').textContent,
    passwordCleared: document.querySelector('#password').value === '',
    consoleVisible: !document.querySelector('#console').hidden,
    adminToolsVisible: !document.querySelector('#organization-settings').hidden,
  })`);
  const shouldShowAdminTools = profile.auth_context === 'Organization administrator';
  if (
    !observed.consoleVisible ||
    !observed.passwordCleared ||
    !observed.identity.includes(profile.org_display) ||
    observed.adminToolsVisible !== shouldShowAdminTools
  ) {
    throw new Error('The local fixture did not reach the expected authenticated state.');
  }

  await evaluate("document.querySelector('#subscriptions-link').click()");
  await waitFor("!document.querySelector('#subscriptions').hidden", true);
  const subscriptionsVisible = await evaluate(
    "document.querySelector('#subscriptions').textContent.includes('Two subscriptions are available.')",
  );
  if (!subscriptionsVisible) throw new Error('The local subscriptions flow did not open.');

  process.stdout.write(`${JSON.stringify({
    status: 'pass',
    alias,
    passwordCleared: true,
    subscriptionsVisible,
    adminToolsVisible: observed.adminToolsVisible,
  })}\n`);
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) {
    const exited = once(chrome, 'exit').catch(() => {});
    chrome.kill('SIGTERM');
    await Promise.race([exited, delay(2000)]);
  }
  await rm(browserDir, { recursive: true, force: true });
}
