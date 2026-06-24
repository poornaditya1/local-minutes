let activeJobId = null;
let pollTimer = null;
let recorderTimer = null;
let lastDeviceInfo = null;

const $ = (id) => document.getElementById(id);
const statusEl = $('status');

function setStatus(message) {
  statusEl.textContent = message;
}

function option(value, text) {
  const el = document.createElement('option');
  el.value = value;
  el.textContent = text;
  return el;
}

function labelForDevice(input) {
  const badges = [];
  if (input.is_default_input) badges.push('default');
  if (input.recommended_system) badges.push('recommended system audio');
  const suffix = badges.length ? `, ${badges.join(', ')}` : '';
  return `${input.name} (${input.kind}${suffix})`;
}

async function refreshDevices() {
  setStatus('Loading audio devices...');
  try {
    const res = await fetch('/api/devices');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not load devices.');
    lastDeviceInfo = data;

    const mic = $('micDevice');
    const sys = $('systemDevice');
    mic.innerHTML = '';
    sys.innerHTML = '';
    mic.appendChild(option('', 'Do not record microphone'));
    sys.appendChild(option('', 'Do not record system audio'));

    for (const input of data.inputs || []) {
      mic.appendChild(option(input.id, labelForDevice(input)));
      sys.appendChild(option(input.id, labelForDevice(input)));
    }

    const inputs = data.inputs || [];
    const defaultMic = inputs.find(i => i.is_default_input && !i.likely_loopback);
    const firstPhysicalMic = inputs.find(i => !i.likely_loopback);
    if (defaultMic || firstPhysicalMic) mic.value = (defaultMic || firstPhysicalMic).id;

    const recommendedSystem = inputs.find(i => i.recommended_system && i.id !== mic.value);
    const likelySystem = inputs.find(i => i.likely_loopback && i.id !== mic.value);
    if (recommendedSystem || likelySystem) sys.value = (recommendedSystem || likelySystem).id;

    renderDeviceHelp(data);
    setStatus(`Devices loaded. Recorder sample rate: ${data.sample_rate || 'unknown'} Hz.`);
  } catch (err) {
    setStatus(err.message);
  }
}

function renderDeviceHelp(data) {
  const macHelp = $('macHelp');
  const warnings = $('deviceWarnings');
  warnings.innerHTML = '';
  for (const warning of data.warnings || []) {
    const div = document.createElement('div');
    div.className = 'warning-pill';
    div.textContent = warning;
    warnings.appendChild(div);
  }
  macHelp.hidden = !data.macos_help;
}

async function refreshModels() {
  setStatus('Connecting to LM Studio...');
  const baseUrl = $('baseUrl').value.trim() || 'http://localhost:1234/v1';
  const input = $('lmModel');
  const list = $('lmModelList');
  const old = input.value;
  list.innerHTML = '';
  try {
    const res = await fetch(`/api/lmstudio-models?base_url=${encodeURIComponent(baseUrl)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not connect to LM Studio. Start the LM Studio Local Server first.');
    const models = data.models || [];
    for (const model of models) list.appendChild(option(model, model));
    if (!old && models.length > 0) input.value = models[0];
    setStatus(models.length ? 'LM Studio models loaded.' : 'No LM Studio models listed. You can type a loaded model ID manually.');
  } catch (err) {
    setStatus(err.message);
  }
}

async function startRecording() {
  const payload = {
    mic_device: $('micDevice').value || null,
    system_device: $('systemDevice').value || null,
    meeting_title: $('meetingTitle').value,
    manual_notes: $('manualNotes').value,
    lmstudio_base_url: $('baseUrl').value,
    lmstudio_model: $('lmModel').value,
    whisper_model: $('whisperModel').value,
    mic_gain: Number($('micGain').value || 1.0),
    system_gain: Number($('systemGain').value || 1.0),
  };

  if (!payload.mic_device && !payload.system_device) {
    setStatus('Select at least one audio source.');
    return;
  }
  if (!payload.lmstudio_model) {
    setStatus('Select an LM Studio model first.');
    return;
  }

  try {
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not start recording.');
    activeJobId = data.job.job_id;
    $('startBtn').disabled = true;
    $('stopBtn').disabled = false;
    $('notesOutput').textContent = 'Recording...';
    $('transcriptOutput').textContent = 'Recording...';
    $('links').innerHTML = '';
    $('recordingDiagnostics').textContent = 'Waiting for audio levels...';
    setStatus('Recording in progress. Watch the live levels to confirm mic and system audio are both active.');
    startRecorderPolling();
  } catch (err) {
    setStatus(err.message);
  }
}

async function stopRecording() {
  try {
    $('stopBtn').disabled = true;
    setStatus('Stopping recorder...');
    stopRecorderPolling();
    const res = await fetch('/api/stop', {method: 'POST'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not stop recording.');
    activeJobId = data.job.job_id;
    setStatus(data.job.message || 'Processing...');
    pollJob();
    pollTimer = setInterval(pollJob, 2500);
  } catch (err) {
    $('stopBtn').disabled = false;
    setStatus(err.message);
  }
}

function startRecorderPolling() {
  stopRecorderPolling();
  pollRecorderStatus();
  recorderTimer = setInterval(pollRecorderStatus, 1000);
}

function stopRecorderPolling() {
  if (recorderTimer) clearInterval(recorderTimer);
  recorderTimer = null;
}

async function pollRecorderStatus() {
  try {
    const res = await fetch('/api/recorder-status');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not read recorder status.');
    renderRecorderStatus(data);
  } catch (err) {
    $('recordingDiagnostics').textContent = err.message;
  }
}

function renderRecorderStatus(data) {
  const streams = data.streams || {};
  const labels = Object.keys(streams);
  if (!labels.length) {
    $('recordingDiagnostics').textContent = 'Recorder is not active.';
    return;
  }
  const lines = [`Sample rate: ${data.sample_rate} Hz`];
  for (const label of labels) {
    const s = streams[label];
    const rms = s.last_rms_dbfs ?? 'n/a';
    const peak = s.last_peak_dbfs ?? 'n/a';
    const quiet = typeof s.last_rms_dbfs === 'number' && s.seconds > 3 && s.last_rms_dbfs < -55;
    const warning = quiet ? '  | quiet or silent' : '';
    const error = s.error ? `  | ERROR: ${s.error}` : '';
    lines.push(`${label}: ${s.seconds}s, RMS ${rms} dBFS, peak ${peak} dBFS${warning}${error}`);
  }
  $('recordingDiagnostics').textContent = lines.join('\n');
}

async function pollJob() {
  if (!activeJobId) return;
  try {
    const res = await fetch(`/api/jobs/${activeJobId}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not read job.');
    const job = data.job;
    setStatus(`${job.status}: ${job.message}`);

    if (job.transcript_md) $('transcriptOutput').textContent = job.transcript_md;
    if (job.notes_md) $('notesOutput').textContent = job.notes_md;
    renderLinks(job.files || {});

    if (job.status === 'done' || job.status === 'failed') {
      clearInterval(pollTimer);
      pollTimer = null;
      $('startBtn').disabled = false;
      $('stopBtn').disabled = true;
      if (job.status === 'failed') {
        $('notesOutput').textContent = `Processing failed:\n${job.error || 'Unknown error.'}`;
      }
    }
  } catch (err) {
    setStatus(err.message);
  }
}

function renderLinks(files) {
  const links = $('links');
  links.innerHTML = '';
  for (const [label, href] of Object.entries(files)) {
    const a = document.createElement('a');
    a.href = href;
    a.textContent = `Download ${label}`;
    links.appendChild(a);
  }
}

$('refreshDevices').addEventListener('click', refreshDevices);
$('refreshModels').addEventListener('click', refreshModels);
$('startBtn').addEventListener('click', startRecording);
$('stopBtn').addEventListener('click', stopRecording);

refreshDevices();
refreshModels();
