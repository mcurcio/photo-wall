"""Behavioral checks for the operator's authored Scene flow."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_authored_scene_flow_in_a_small_browserless_dom():
    script = r'''
const vm = require('vm');
const fs = require('fs');
const source = fs.readFileSync('central/operator.js', 'utf8');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {};
    this.value = ''; this.checked = false; this.disabled = false; this.hidden = false;
    this.textContent = ''; this.selected = false;
    this.classList = {toggle() {}};
  }
  append(...children) { this.children.push(...children.filter(Boolean)); }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this.children = children.filter(Boolean); }
  createTHead() { return new RowContainer('thead'); }
  createTBody() { return new RowContainer('tbody'); }
  querySelectorAll(selector) {
    const result = [];
    const visit = node => {
      for (const child of node.children || []) {
        if (selector === 'select[data-frame]' && child.tagName === 'SELECT' && child.dataset.frame) result.push(child);
        visit(child);
      }
    };
    visit(this); return result;
  }
  get selectedOptions() {
    return this.children.filter(option => option.selected || option.value === this.value);
  }
  set value(value) {
    this._value = String(value ?? '');
    if (this.children) for (const child of this.children) child.selected = child.value === this._value;
  }
  get value() { return this._value ?? ''; }
}
class RowContainer extends Element {
  insertRow() { const row = new RowContainer('tr'); this.append(row); return row; }
  insertCell() { const cell = new Element('td'); this.append(cell); return cell; }
}
const ids = ['health','login','token','connect','message','controls','players','refresh','frames',
  'frame-id','width-mm','height-mm','width-px','height-px','diagonal','create-frame','bind-frame',
  'bind-output','bind','retire-player','retire','cal-frame','rotation','gain','corners','crop',
  'media-health','sources','source-id','connection-id','favorites','source-type','captured-from',
  'captured-until','create-source','scene-id','scene-revision','scene-source','scene-frames',
  'scene-authored','authored-status','authored-choosers','cycle-seconds','scene-loop','create-scene',
  'program-id','program-scene','program-start','program-end','program-priority','timezone','create-program',
  'scenes','programs','runs','activate-scene','activate','control-run','finish-run','cancel-run',
  'remove-program','delete-program','refresh-content','activate-priority','activate-repeat',
  'activate-force','activate-expires'];
const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
elements['scene-frames'].tagName = 'SELECT'; elements['scene-source'].tagName = 'SELECT';
elements['activate-priority'].value = '0'; elements['activate-repeat'].value = 'ignore';
const control = {calls: [], deferredCandidates: [], failPost: false, deferCandidates: false, activationStatus: 'admitted'};
const sourceData = [
  {source_ref:'source:a', status:'ok', counts:{valid:2}, last_success:1000, diagnostics:[]},
  {source_ref:'source:b', status:'ok', counts:{valid:2}, last_success:1000, diagnostics:[]},
];
const candidate = (id, captured = 1000) => ({asset_id:id, kind:'image', original_width:100,
  original_height:100, captured_at:captured, variant:{path:'opaque'}, preparation_failure:null});
const media = {health:{accounted_bytes:1, max_bytes:2, worker_seen:1000, worker_error:null}, sources:sourceData};
const runtime = {definitions:{}, programs:{}, current:{runs:[]}};
const response = (data, ok = true) => ({ok, async json() {return data;}});
async function fetchStub(path, init = {}) {
  if (path === '/healthz') return response({status:'ok'});
  if (path === '/v1/operator/media') return response(media);
  if (path === '/v1/operator/runtime') return response(runtime);
  if (path.includes('/candidates')) {
    if (control.deferCandidates) return new Promise(resolve => control.deferredCandidates.push({path, resolve}));
    return response({status:'ok', candidates:path.includes('frame_id=frame-b') ?
      [candidate('asset-bbbbbbbb')] : [candidate('asset-aaaaaaaa'), candidate('asset-bbbbbbbb')]});
  }
  if (path.endsWith('/authored')) {
    control.calls.push({kind:'AUTHORED', body:JSON.parse(init.body)});
    if (control.failPost) return response({error:'authored_asset_not_found'}, false);
    return response({status:'ok'});
  }
  if (path === '/v1/operator/activations') {
    control.calls.push({kind:'ACTIVATE', body:JSON.parse(init.body)}); return response({status:control.activationStatus});
  }
  if (path.includes('/scenes/')) {
    control.calls.push({kind:'PUT', body:JSON.parse(init.body)}); return response({status:'ok'});
  }
  return response({});
}
const context = {
  document: {
    getElementById(id) { return elements[id]; },
    createElement(tag) { return new Element(tag); },
    querySelector() { return {textContent:'field'}; },
    querySelectorAll() { return []; },
  },
  fetch: fetchStub, AbortSignal:{timeout() {return {}; }}, crypto:{randomUUID(){return 'uuid';}},
  Intl, Date, Error, Map, Set, Array, Number, Boolean, String, JSON, Math, Promise,
  console, setInterval() {}, clearInterval() {}, control, candidate, response,
};
vm.createContext(context);
const test = `(async () => {
  ${source}
  const assert = (condition, message) => { if (!condition) throw Error(message); };
  await refreshContent();
  assert(mediaState.sources.length === 2, 'refreshContent did not hydrate media state');
  $('activate-scene').value = 'scene-1'; control.calls.length = 0; await $('activate').onclick();
  assert(control.calls[0].kind === 'ACTIVATE' && control.calls[0].body.priority === 0 &&
    control.calls[0].body.repeat === 'ignore' && control.calls[0].body.force === false &&
    control.calls[0].body.expires_at === null, 'activation defaults were not sent');
  $('activate-scene').value = 'scene-1'; $('activate-priority').value = '7';
  $('activate-repeat').value = 'queue'; $('activate-repeat').onchange(); $('activate-force').checked = true;
  $('activate-expires').value = '2030-01-01T00:00'; control.activationStatus = 'queued'; control.calls.length = 0; await $('activate').onclick();
  assert(control.calls[0].body.priority === 7 && control.calls[0].body.repeat === 'queue' &&
    control.calls[0].body.force === true && typeof control.calls[0].body.expires_at === 'number',
    'activation options were not sent');
  assert($('message').textContent === 'Scene queued.', 'queued admission was reported as started');
  control.activationStatus = 'ignored'; $('activate-repeat').value = 'ignore'; $('activate-repeat').onchange();
  $('activate-scene').value = 'scene-1'; control.calls.length = 0; await $('activate').onclick();
  assert($('message').textContent === 'Scene already active; request ignored.', 'ignored admission was misreported');
  control.activationStatus = 'rejected'; $('activate-scene').value = 'scene-1'; control.calls.length = 0; await $('activate').onclick();
  assert($('message').textContent.startsWith('Scene rejected'), 'rejected admission was misreported');
  control.activationStatus = 'admitted';
  $('activate-scene').value = 'scene-1'; $('activate-repeat').value = 'queue'; $('activate-repeat').onchange(); $('activate-expires').value = '';
  control.calls.length = 0; await $('activate').onclick();
  assert(control.calls.length === 0 && $('message').textContent.includes('Queue repeat requires an expiry'),
    'queue activation without expiry was sent');
  $('activate-repeat').value = 'ignore'; $('activate-repeat').onchange();
  const frameOption = (value) => { const option = document.createElement('option'); option.value = value; option.textContent = value; option.selected = true; return option; };
  $('scene-frames').replaceChildren(frameOption('frame-a'), frameOption('frame-b'));
  $('scene-source').value = 'source:a'; updateAuthoredAvailability();
  assert(!$('scene-authored').disabled, 'a successfully refreshed source did not enable authored mode');
  $('scene-authored').checked = true;
  await loadAuthoredCandidates();
  const choosers = $('authored-choosers').querySelectorAll('select[data-frame]');
  assert(choosers.length === 2, 'did not render one chooser per Frame');
  assert(choosers[0].children.some(option => option.textContent.includes('Ref …aaaaaaaa')),
    'candidate labels do not distinguish opaque asset references');
  assert(!choosers[1].children.some(option => option.value === 'asset-aaaaaaaa'), 'incompatible option offered on second Frame');
  choosers[0].value = 'asset-aaaaaaaa'; choosers[1].value = 'asset-bbbbbbbb';
  $('scene-id').value = 'scene-1'; $('scene-revision').value = '1'; $('cycle-seconds').value = '10'; $('scene-loop').value = 'true';
  control.calls.length = 0; await $('create-scene').onclick();
  assert(control.calls.length === 1 && control.calls[0].kind === 'AUTHORED', 'authored save must be one atomic request');
  assert(control.calls[0].body.source_ref === 'source:a' && control.calls[0].body.asset_ids.length === 2, 'authored request lacks source or refs');
  assert(control.calls[0].body.scene.contributions.every(item => item.asset_refs && !item.source_refs), 'authored Scene did not use asset refs');
  control.failPost = true; control.calls.length = 0; await $('create-scene').onclick();
  assert(control.calls.length === 1 && control.calls[0].kind === 'AUTHORED' && $('message').textContent.includes('no longer available'),
    'failed atomic save issued a second request or was not reported');
  control.failPost = false;
  $('scene-authored').checked = false; await loadAuthoredCandidates();
  control.calls.length = 0; await $('create-scene').onclick();
  assert(control.calls.length === 1 && control.calls[0].kind === 'PUT' &&
    control.calls[0].body.contributions.every(item => item.source_refs && !item.asset_refs),
    'live Scene flow no longer uses source refs');
  $('scene-authored').checked = true; await loadAuthoredCandidates();
  control.deferredCandidates.length = 0;
  control.deferCandidates = true;
  $('scene-source').value = 'source:a'; const pendingA = loadAuthoredCandidates();
  $('scene-source').value = 'source:b'; const pendingB = loadAuthoredCandidates();
  assert(control.deferredCandidates.length === 4, 'per-Frame candidate requests were not issued independently');
  for (const item of control.deferredCandidates.slice(2)) item.resolve(response({status:'ok', candidates:[candidate('asset-bbbbbbbb', 2000)]}));
  await pendingB;
  for (const item of control.deferredCandidates.slice(0, 2)) item.resolve(response({status:'ok', candidates:[candidate('asset-aaaaaaaa', 3000)]}));
  await pendingA;
  const finalText = $('authored-choosers').querySelectorAll('select[data-frame]')[0].children.map(option => option.textContent).join('|');
  assert(finalText.includes('Ref …bbbbbbbb') && !finalText.includes('Ref …aaaaaaaa'), 'late source response replaced current choices');
  mediaState.sources.find(item => item.source_ref === 'source:b').status = 'permission';
  await loadAuthoredCandidates();
  assert($('create-scene').disabled && authored.status === 'unavailable', 'failed source still allows authored save');
  assert(!$('scene-authored').disabled, 'operator cannot explicitly return to live mode');
})()`;
vm.runInContext(test, context).then(() => console.log('operator-authored:passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, check=False, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "operator-authored:passed" in result.stdout, "asynchronous assertions did not complete"
