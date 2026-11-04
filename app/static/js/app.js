/**
 * Capture-TS — Client-side logic
 * WebSocket real-time updates, theme toggle, UI helpers
 */

// ═══════════════ WebSocket ═══════════════

let ws = null;
let wsReconnectTimer = null;
const WS_RECONNECT_DELAY = 3000;

function connectWebSocket() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
        updateConnectionStatus(true);
        addLog('[SYSTEM] WebSocket connected', 'success');
        if (wsReconnectTimer) {
            clearInterval(wsReconnectTimer);
            wsReconnectTimer = null;
        }
    };

    ws.onclose = () => {
        updateConnectionStatus(false);
        scheduleReconnect();
    };

    ws.onerror = () => {
        updateConnectionStatus(false);
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleWSMessage(data);
        } catch (e) {
            console.error('WS parse error:', e);
        }
    };
}

function scheduleReconnect() {
    if (wsReconnectTimer) return;
    wsReconnectTimer = setInterval(() => {
        if (!ws || ws.readyState === WebSocket.CLOSED) {
            connectWebSocket();
        }
    }, WS_RECONNECT_DELAY);
}

function updateConnectionStatus(connected) {
    const el = document.getElementById('ws-status');
    if (!el) return;
    const dot = el.querySelector('.flux-status-dot');
    const label = el.querySelector('.flux-status-label');
    if (connected) {
        dot.className = 'flux-status-dot flux-status-dot--connected';
        label.textContent = 'Connected';
    } else {
        dot.className = 'flux-status-dot flux-status-dot--disconnected';
        label.textContent = 'Disconnected';
    }
}

function handleWSMessage(data) {
    if (data.type === 'log') {
        addLog(data.message);
    } else if (data.type === 'state') {
        updateTelemetry(data);
    }
}

// ═══════════════ Telemetry Updates ═══════════════

let lastFileSize = 0;
let lastUpdateTime = Date.now();
let lastKnownState = 'IDLE';

// The recorder panel is only redrawn when a capture starts or stops, so the
// "recording…" line is kept honest here: it switches from "starting" to the
// live counter on the first locked state and then follows the telemetry.
function updateRecorderLive(data) {
    const live = document.querySelector('.flux-record-summary--live');
    if (!live) return;

    const title = live.querySelector('#live-title');
    const detail = live.querySelector('#live-detail');
    if (!title || !detail) return;

    const state = (data.state || 'idle').toLowerCase();
    if (state !== 'recording' && state !== 'tuning') return;

    if (state === 'tuning') {
        title.textContent = 'Starting recording…';
        detail.textContent = 'Waiting for the first data…';
        return;
    }

    const target = live.dataset.target ? Number(live.dataset.target) : 0;
    const captured = Math.floor(Number(data.duration || 0));
    title.textContent = 'Recording';
    detail.textContent = target
        ? `${target}s target, ${captured}s captured`
        : `${captured}s captured, no limit`;
}

function updateTelemetry(data) {
    const stateEl = document.getElementById('metric-state');
    const sizeEl = document.getElementById('metric-filesize');
    const durationEl = document.getElementById('metric-duration');
    const bitrateEl = document.getElementById('metric-bitrate');
    const progressEl = document.getElementById('record-progress');

    if (stateEl) {
        const state = (data.state || 'idle').toUpperCase();

        if ((lastKnownState === 'RECORDING' || lastKnownState === 'TUNING') &&
            (state === 'IDLE' || state === 'ERROR')) {
            // Recording ended (completed, cancelled or failed): refresh the panel
            // and the recordings table so the outcome and error are visible.
            if (typeof htmx !== 'undefined') {
                htmx.ajax('GET', '/api/record/panel', {target: '#recorder-panel', swap: 'innerHTML'});
                htmx.ajax('GET', '/api/recordings', {target: '#recordings-table', swap: 'innerHTML'});
            }
        }
        lastKnownState = state;

        stateEl.textContent = state;
        stateEl.className = 'flux-metric__value';
        if (state === 'RECORDING') {
            stateEl.style.color = 'var(--flux-error)';
        } else if (state === 'TUNING') {
            stateEl.style.color = 'var(--flux-warning)';
        } else {
            stateEl.style.color = '';
        }
    }

    updateRecorderLive(data);

    if (sizeEl && data.file_size_display) {
        sizeEl.textContent = data.file_size_display;
    }

    if (durationEl && data.duration_display) {
        durationEl.textContent = data.duration_display;
    }

    // Calculate instantaneous bitrate
    if (bitrateEl && data.file_size !== undefined) {
        const now = Date.now();
        const elapsed = (now - lastUpdateTime) / 1000;
        if (elapsed > 0 && lastFileSize > 0) {
            const bytesPerSec = (data.file_size - lastFileSize) / elapsed;
            const mbps = (bytesPerSec * 8) / 1_000_000;
            if (mbps > 0) {
                bitrateEl.textContent = mbps.toFixed(1) + ' Mbps';
            }
        }
        lastFileSize = data.file_size;
        lastUpdateTime = now;
    }

    // Progress indicator
    if (progressEl) {
        if (data.state === 'recording') {
            progressEl.style.display = 'block';
            const fill = progressEl.querySelector('#progress-fill');
            if (fill) fill.style.width = '100%';
            const label = progressEl.querySelector('#progress-label');
            if (label) label.textContent = `Recording: ${data.file_size_display || '0 B'} — ${data.duration_display || '00:00'}`;
        } else {
            progressEl.style.display = 'none';
        }
    }

    // Update lock status
    const lockEl = document.getElementById('lock-status');
    if (lockEl) {
        if (data.state === 'recording' || data.state === 'tuning') {
            lockEl.innerHTML = '<i data-lucide="lock" class="flux-icon--sm"></i><span>LOCKED</span>';
            lockEl.className = 'flux-badge flux-badge--success';
        } else {
            lockEl.innerHTML = '<i data-lucide="lock-open" class="flux-icon--sm"></i><span>UNLOCKED</span>';
            lockEl.className = 'flux-badge flux-badge--outline';
        }
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }
}

// ═══════════════ Log Console ═══════════════

function addLog(message, level) {
    const console_ = document.getElementById('log-console');
    if (!console_) return;

    // Detect level from message content if not provided
    if (!level) {
        if (message.includes('[ERROR]')) level = 'error';
        else if (message.includes('[WARNING]')) level = 'warning';
        else if (message.includes('[SUCCESS]')) level = 'success';
        else if (message.includes('[TSDUCK]')) level = 'tsduck';
        else level = 'info';
    }

    const entry = document.createElement('div');
    entry.className = `flux-log__entry flux-log__entry--${level}`;
    const text = document.createElement('span');
    text.textContent = message;
    entry.appendChild(text);
    console_.appendChild(entry);

    // Auto-scroll to bottom
    console_.scrollTop = console_.scrollHeight;

    // Limit log entries
    while (console_.children.length > 200) {
        console_.removeChild(console_.firstChild);
    }
}

function clearLog() {
    const console_ = document.getElementById('log-console');
    if (console_) {
        console_.innerHTML = '<div class="flux-log__entry flux-log__entry--info"><span>[SYSTEM] Log cleared.</span></div>';
    }
}

// ═══════════════ Theme Toggle ═══════════════

function loadTheme() {
    const saved = localStorage.getItem('flux-theme') || 'dark';
    const isDark = saved !== 'light';

    if (isDark) {
        document.documentElement.classList.add('dark');
        document.documentElement.classList.remove('light');
    } else {
        document.documentElement.classList.remove('dark');
        document.documentElement.classList.add('light');
    }
    
    // Update ui-header
    const header = document.querySelector('ui-header');
    if (header) {
        header.setAttribute('theme', isDark ? 'dark' : 'light');
    }
}

document.addEventListener('theme-change', (e) => {
    const theme = e.detail.theme; // 'light' or 'dark'
    if (theme === 'dark') {
        document.documentElement.classList.add('dark');
        document.documentElement.classList.remove('light');
    } else {
        document.documentElement.classList.remove('dark');
        document.documentElement.classList.add('light');
    }
    localStorage.setItem('flux-theme', theme);
});

// ═══════════════ UI Helpers ═══════════════

function toggleAllServices(checkbox) {
    const checkboxes = document.querySelectorAll('.service-checkbox');
    checkboxes.forEach(cb => cb.checked = checkbox.checked);
}

function toggleServiceSelection(radio) {
    // Visual hint — could disable/enable checkboxes
    const isSelected = radio.value === 'selected';
    const checkboxes = document.querySelectorAll('.service-checkbox');
    checkboxes.forEach(cb => {
        cb.disabled = !isSelected;
        if (!isSelected) cb.checked = true;
    });
}

// Frequency display helper
function updateFreqHint() {
    const input = document.getElementById('frequency');
    const hint = document.getElementById('freq-mhz');
    if (input && hint) {
        const hz = parseInt(input.value) || 0;
        hint.textContent = (hz / 1_000_000).toFixed(1) + ' MHz';
    }
}

// Keep the SYSTEM select coherent with the chosen receiver. A hybrid stick
// exposes its DVB-T and DVB-C tuners as two devices, and asking one of them for
// the other's delivery system is a hard tsp failure, so the options are limited
// to what the selected device reports.
function selectSupportedSystems(deviceSelect) {
    const systemSelect = document.querySelector('#delivery_system');
    if (!deviceSelect || !systemSelect) return;

    const device = deviceSelect.options[deviceSelect.selectedIndex];
    const systems = (device && device.dataset.systems ? device.dataset.systems : '')
        .split(',')
        .map(s => s.trim())
        .filter(Boolean);

    let firstSupported = null;
    for (const option of systemSelect.options) {
        const supported = systems.includes(option.value);
        option.disabled = !supported;
        option.hidden = !supported;
        if (supported && firstSupported === null) firstSupported = option;
    }

    const current = systemSelect.options[systemSelect.selectedIndex];
    if (firstSupported && (!current || current.disabled)) {
        firstSupported.selected = true;
    }
}

// ═══════════════ Init ═══════════════

document.addEventListener('DOMContentLoaded', () => {
    loadTheme();
    connectWebSocket();

    // Frequency hint updater
    const freqInput = document.getElementById('frequency');
    if (freqInput) {
        freqInput.addEventListener('input', updateFreqHint);
    }

    // The device list is rendered by the server; the SYSTEM options must match
    // it as soon as the page is usable, not only after the first change.
    selectSupportedSystems(document.querySelector('#adapter'));

    // Keep-alive ping
    setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
        }
    }, 30000);
});
