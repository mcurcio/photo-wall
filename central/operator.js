'use strict';

const $ = id => document.getElementById(id);
let token = '', state = {frames: [], players: [], outputs: []};
let refreshing = false;
const errors = {
  unauthorized: 'The token was not accepted. Reconnect with your operator token.',
  persistent_storage_required: 'This Player needs working persistent storage before binding.',
  source_revision_immutable: 'Use a new source name or revision to change this query.',
  binding_generation_conflict: 'This Frame changed. Refresh and review its binding.',
  calibration_revision_conflict: 'Calibration changed. Refresh and review the saved values.',
  invalid_command: 'Check the values and references in this command.',
  invalid_request: 'Check the required fields and their values.',
};

function message(text, error = false) {
  $('message').textContent = text;
  $('message').classList.toggle('error', error);
}

async function api(path, method = 'GET', body) {
  const response = await fetch(path, {
    method, signal: AbortSignal.timeout(15000),
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(errors[data.error] || data.error || 'Request failed');
  return data;
}

function table(headers, rows) {
  const result = document.createElement('table');
  const heading = result.createTHead().insertRow();
  for (const label of headers) {
    const cell = document.createElement('th');
    cell.textContent = label;
    heading.append(cell);
  }
  const body = result.createTBody();
  for (const row of rows) {
    const line = body.insertRow();
    for (const value of row) line.insertCell().textContent = value;
  }
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = headers.length;
    cell.textContent = 'None configured';
  }
  return result;
}

function options(id, items) {
  const selected = new Set(Array.from($(id).selectedOptions, item => item.value));
  $(id).replaceChildren(...items.map(([value, label]) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    option.selected = selected.has(value);
    return option;
  }));
}

function required(id) {
  const value = $(id).value.trim();
  if (!value) throw Error('Complete or select ' + document.querySelector(`label[for="${id}"]`).textContent + '.');
  return value;
}
const endpoint = value => encodeURIComponent(value);
const date = instant => new Date(instant * 1000).toLocaleString();
function instant(id, optional = false) {
  if (!$(id).value && optional) return null;
  const value = new Date(required(id)).getTime() / 1000;
  if (!Number.isFinite(value)) throw Error('Enter a valid date and time.');
  return value;
}

async function refresh() {
  state = await api('/v1/operator/inventory');
  $('players').replaceChildren(table(['Player', 'Outputs', 'State'], state.players.map(player => [
    player.id, state.outputs.filter(output => output.player_id === player.id)
      .map(output => output.output_id + (output.observation.connected ? '' : ' (disconnected)')).join(', '),
    player.retired_at !== null ? 'Retired' : player.health.storage_fault ? 'Storage fault · unbound only' : 'Registered',
  ])));
  $('frames').replaceChildren(table(['Frame', 'Output', 'Generation', 'Calibration'], state.frames.map(frame => [
    frame.id, frame.player_id ? frame.player_id + ' / ' + frame.output_id : 'Unbound', frame.generation,
    frame.calibration_valid ? 'Committed r' + frame.calibration.revision : 'Review required',
  ])));
  const frames = state.frames.map(frame => [frame.id, frame.id]);
  for (const id of ['bind-frame', 'cal-frame', 'scene-frames']) options(id, frames);
  const players = state.players.filter(player => player.retired_at === null);
  options('retire-player', players.map(player => [player.id, player.id]));
  options('bind-output', state.outputs.filter(output => players.some(player =>
    player.id === output.player_id && !player.health.storage_fault)).map(output => [
    JSON.stringify([output.player_id, output.output_id]), output.player_id + ' / ' + output.output_id,
  ]));
  loadCalibration();
}

function loadCalibration() {
  const frame = state.frames.find(item => item.id === $('cal-frame').value);
  if (!frame) return;
  const calibration = frame.preview || frame.calibration;
  $('rotation').value = calibration.rotation;
  $('gain').value = calibration.gain;
  $('corners').value = JSON.stringify(calibration.corners);
  $('crop').value = JSON.stringify(calibration.crop);
}

async function refreshContent() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [media, runtime] = await Promise.all([api('/v1/operator/media'), api('/v1/operator/runtime')]);
    const health = media.health;
    $('media-health').textContent = 'Stored/reserved: ' + (health.accounted_bytes / 1024 ** 2).toFixed(1) +
      ' MiB · Quota: ' + (health.max_bytes / 1024 ** 2).toFixed(0) + ' MiB · Worker: ' +
      (health.worker_seen ? date(health.worker_seen) : 'Awaiting first heartbeat') +
      (health.worker_error ? ' · ' + health.worker_error : '');
    $('sources').replaceChildren(table(['Source', 'Status', 'Valid assets', 'Last success', 'Details'], media.sources.map(source => [
      source.source_ref, source.status, source.counts.valid || 0,
      source.last_success ? date(source.last_success) : 'Awaiting refresh',
      source.diagnostics.map(item => item.code).join(', '),
    ])));
    options('scene-source', media.sources.map(source => [source.source_ref, source.source_ref]));
    const scenes = Object.values(runtime.definitions), programs = Object.values(runtime.programs);
    $('scenes').replaceChildren(table(['Scene', 'Revision', 'Cycle', 'Playback'], scenes.map(scene => [
      scene.scene_id, scene.revision, scene.cycle_seconds + ' seconds', scene.loop ? 'Repeating' : 'One cycle',
    ])));
    $('programs').replaceChildren(table(['Program', 'Scene', 'Start', 'Finish', 'Priority'], programs.map(program => [
      program.program_id, program.scene_id, date(program.starts_at), date(program.ends_at), program.priority,
    ])));
    $('runs').replaceChildren(table(['Run', 'Scene', 'Phase', 'Participants'], runtime.current.runs.slice(-100).map(run => [
      run.run_id, run.scene_id, run.phase, run.participants.join(', '),
    ])));
    for (const id of ['program-scene', 'activate-scene']) options(id, scenes.map(scene => [scene.scene_id, scene.scene_id]));
    options('remove-program', programs.map(program => [program.program_id, program.program_id]));
    options('control-run', runtime.current.runs.filter(run => ['body', 'outro'].includes(run.phase))
      .map(run => [run.run_id, run.scene_id + ' / ' + run.run_id]));
  } finally { refreshing = false; }
}

function action(id, command, saved = 'Saved.') {
  $(id).onclick = async () => {
    $(id).disabled = true;
    try { await command(); message(saved); }
    catch (error) { message(error.message, true); }
    finally { $(id).disabled = false; }
  };
}

action('connect', async () => {
  token = $('token').value;
  await refresh();
  await refreshContent();
  $('token').value = '';
  $('login').hidden = true;
  $('controls').hidden = false;
}, 'Connected.');
action('refresh', refresh, 'Inventory refreshed.');
action('refresh-content', refreshContent, 'Content and health refreshed.');
action('create-frame', async () => {
  await api('/v1/operator/frames', 'POST', {id: required('frame-id'),
    width_mm: +$('width-mm').value, height_mm: +$('height-mm').value,
    profile: {width_px: +$('width-px').value, height_px: +$('height-px').value, diagonal_inches: +$('diagonal').value}});
  await refresh();
});
action('bind', async () => {
  const [player_id, output_id] = JSON.parse(required('bind-output'));
  const frame = state.frames.find(item => item.id === required('bind-frame'));
  await api('/v1/operator/frames/' + endpoint(frame.id) + '/binding', 'PUT',
    {player_id, output_id, expected_generation: frame.generation});
  await refresh();
});
action('retire', async () => {
  await api('/v1/operator/players/' + endpoint(required('retire-player')) + '/retire', 'POST');
  await refresh();
});
$('cal-frame').onchange = loadCalibration;
for (const button of document.querySelectorAll('[data-cal]')) button.onclick = async () => {
  button.disabled = true;
  try {
    const frame = state.frames.find(item => item.id === required('cal-frame'));
    await api('/v1/operator/frames/' + endpoint(frame.id) + '/calibration', 'POST', {
      operation: button.dataset.cal, expected_revision: frame.calibration.revision,
      expected_generation: frame.generation, calibration: {rotation: +$('rotation').value,
        gain: +$('gain').value, corners: JSON.parse($('corners').value), crop: JSON.parse($('crop').value)},
    });
    await refresh();
    message(button.dataset.cal === 'preview' ? 'Preview active for 30 seconds.' : 'Calibration saved.');
  } catch (error) { message(error.message, true); }
  finally { button.disabled = false; }
};
action('create-source', async () => {
  const source_ref = required('source-id'), kind = $('source-type').value;
  await api('/v1/operator/sources/' + endpoint(source_ref), 'PUT', {schema: 1, source_ref,
    connection_ref: required('connection-id'), favorites: $('favorites').value === 'any' ? null : $('favorites').value === 'true',
    captured_from: instant('captured-from', true), captured_until: instant('captured-until', true),
    media_types: kind === 'both' ? ['image', 'video'] : [kind]});
  await refreshContent();
});
action('create-scene', async () => {
  const frames = Array.from($('scene-frames').selectedOptions, item => item.value);
  if (!frames.length) throw Error('Select at least one participating Frame.');
  const scene_id = required('scene-id'), source = required('scene-source');
  await api('/v1/operator/scenes/' + endpoint(scene_id), 'PUT', {scene_id,
    revision: +$('scene-revision').value, cycle_seconds: +$('cycle-seconds').value,
    loop: $('scene-loop').value === 'true', contributions: frames.map(frame => ({
      target: 'frame:' + frame, role: frame, source_refs: [source], kind: 'media', retain_on_expiry: true,
    }))});
  await refreshContent();
});
action('create-program', async () => {
  const program_id = required('program-id');
  await api('/v1/operator/programs/' + endpoint(program_id), 'PUT', {program_id,
    scene_id: required('program-scene'), starts_at: instant('program-start'),
    ends_at: instant('program-end'), priority: +$('program-priority').value});
  await refreshContent();
});
action('activate', async () => {
  const admission = await api('/v1/operator/activations', 'POST', {
    scene_id: required('activate-scene'), activation_id: 'operator:' + crypto.randomUUID()});
  if (admission.status !== 'admitted') throw Error('Scene ' + admission.status + (admission.reason ? ': ' + admission.reason : ''));
  await refreshContent();
}, 'Scene started.');
for (const operation of ['finish', 'cancel']) action(operation + '-run', async () => {
  await api('/v1/operator/runs/' + endpoint(required('control-run')) + '/' + operation, 'POST');
  await refreshContent();
});
action('delete-program', async () => {
  await api('/v1/operator/programs/' + endpoint(required('remove-program')), 'DELETE');
  await refreshContent();
});
$('timezone').textContent = 'Dates use ' + Intl.DateTimeFormat().resolvedOptions().timeZone + '.';
for (const [id, offset] of [['program-start', 60000], ['program-end', 3660000]]) {
  const time = new Date(Date.now() + offset);
  time.setMinutes(time.getMinutes() - time.getTimezoneOffset());
  $(id).value = time.toISOString().slice(0, 16);
}
async function health() {
  try {
    const response = await fetch('/healthz', {signal: AbortSignal.timeout(5000)});
    const value = await response.json();
    $('health').textContent = value.status === 'ok' ? 'Central healthy' : 'Central unavailable';
  } catch { $('health').textContent = 'Central unavailable'; }
}
health();
setInterval(health, 10000);
