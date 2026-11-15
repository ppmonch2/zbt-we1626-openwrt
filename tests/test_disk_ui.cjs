// UI behavior with a simulated local API; no browser or real network required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value: '', textContent: '', disabled: false,
    addEventListener(event, handler) { this[event] = handler; }});
  return elements.get(id);
}
const buttons = ['check', 'start', 'stop', 'use_cloud', 'refresh'].map(element);
let state = {ok: true, configured: false, login: '', folder: 'heatbridge-v1',
  enabled: false, running: false, status: 'unknown', message: ''};
let fail = false;
const calls = [];
const context = vm.createContext({document: {getElementById: element,
  querySelectorAll: () => buttons}, fetch: async (url, options) => {
  assert.equal(url, 'cgi-bin/disk-api');
  if (fail) throw Error('simulated connection failure');
  const request = options.method === 'POST' ? JSON.parse(options.body) : {action: 'status'};
  calls.push(request);
  if (request.action === 'save') state = {...state, login: request.login, folder: request.folder,
    configured: true, message: 'saved_not_started'};
  if (request.action === 'check') state.message = 'write_read_delete_ok';
  if (request.action === 'start') state = {...state, enabled: true, running: true,
    status: 'disk_connected_waiting', message: 'disk_started'};
  return {ok: true, json: async () => ({...state})};
}});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../entware/www/disk.js'), 'utf8'), context);
const settle = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await settle();
  assert.equal(element('folder').value, 'heatbridge-v1');
  element('login').value = ' fixture-user ';
  element('password').value = 'fixture-password';
  element('settings').submit({preventDefault() {}});
  await settle();
  assert.equal(element('password').value, '');
  assert.equal(calls.at(-1).password, 'fixture-password');
  assert.equal(calls.at(-1).login, 'fixture-user');
  assert.match(element('notice').textContent, /сохранены/);
  await element('check').onclick();
  assert.match(element('notice').textContent, /записан, прочитан и удалён/);
  await element('start').onclick();
  assert.match(element('connection').textContent, /Яндекс Диск/);
  assert.match(element('connection').textContent, /Диск отвечает/);
  assert.ok(![...elements.values()].some(e => e.textContent.includes('fixture-password')));
  fail = true;
  await element('refresh').onclick();
  assert.match(element('notice').textContent, /Нет ответа/);
  assert.ok(buttons.every(button => !button.disabled));
  console.log('Disk UI save/check/start/error flows passed with simulated API.');
})().catch(error => { console.error(error); process.exitCode = 1; });
