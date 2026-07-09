/**
 * dashboard.js
 * ============
 * Frontend logic for the BTC Spot Scalper dashboard.
 * Connects to the FastAPI WebSocket for real-time updates
 * and REST API for configuration / data.
 */

// ──────────────────────────────────────────────
//  State
// ──────────────────────────────────────────────

let ws = null;
let reconnectInterval = null;
let currentMode = 'demo';

// ──────────────────────────────────────────────
//  DOM Helpers
// ──────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const fmtUSD = (v) => v != null ? `$${parseFloat(v).toFixed(2)}` : '$--';
const fmtBTC = (v) => v != null ? `₿${parseFloat(v).toFixed(8)}` : '₿--';
const fmtPct = (v) => v != null ? `${parseFloat(v).toFixed(3)}%` : '--';

// ──────────────────────────────────────────────
//  WebSocket Connection
// ──────────────────────────────────────────────

function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
        console.log('WebSocket connected');
        if (reconnectInterval) {
            clearInterval(reconnectInterval);
            reconnectInterval = null;
        }
        ws.send('status'); // request immediate status
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleMessage(data);
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected — reconnecting...');
        if (!reconnectInterval) {
            reconnectInterval = setInterval(() => connectWS(), 3000);
        }
    };

    ws.onerror = (err) => {
        console.error('WebSocket error', err);
    };
}

// ──────────────────────────────────────────────
//  Message Handler
// ──────────────────────────────────────────────

function handleMessage(data) {
    switch (data.type) {
        case 'status':
            updateStatus(data);
            break;
        case 'pong':
            break;
        default:
            console.log('Unknown message type:', data.type);
    }
}

// ──────────────────────────────────────────────
//  UI Updates
// ──────────────────────────────────────────────

function updateStatus(data) {
    // Bot state
    const stateDot = $('botStateDot');
    const stateText = $('botStateText');

    stateDot.className = 'w-2.5 h-2.5 rounded-full';
    stateText.textContent = data.bot_state;

    switch (data.bot_state) {
        case 'SEARCHING':
            stateDot.classList.add('dot-searching');
            stateText.classList.add('text-scalper-yellow');
            break;
        case 'IN_TRADE':
            stateDot.classList.add('dot-in-trade');
            stateText.classList.add('text-scalper-green');
            break;
        case 'HALTED':
            stateDot.classList.add('dot-halted');
            stateText.classList.add('text-scalper-red');
            break;
        default:
            stateDot.classList.add('dot-stopped');
            stateText.classList.add('text-gray-500');
    }

    // Balance
    if (data.balance) {
        $('usdtBalance').textContent = fmtUSD(data.balance.usdt_total);
        $('usdtFree').textContent = fmtUSD(data.balance.usdt_free);
        $('btcBalance').textContent = fmtBTC(data.balance.btc_total);
        $('btcFree').textContent = fmtBTC(data.balance.btc_free);
    }

    // Indicators
    if (data.latest_snapshot) {
        const s = data.latest_snapshot;
        $('currentPrice').textContent = fmtUSD(s.price);
        $('indVwap').textContent = fmtUSD(s.vwap);
        $('indEma9').textContent = fmtUSD(s.ema9);
        $('indEma21').textContent = fmtUSD(s.ema21);
        $('indEma50').textContent = fmtUSD(s.ema50);
        $('indVolRatio').textContent = `${s.volume_ratio?.toFixed(2)}×`;
        $('indRsi').textContent = s.rsi?.toFixed(1);
        $('indTrend15m').textContent = s.trend_15m?.toUpperCase();

        // Trend colour
        const trendEl = $('indTrend15m');
        trendEl.className = 'text-sm font-bold';
        if (s.trend_15m === 'bullish') trendEl.classList.add('text-scalper-green');
        else if (s.trend_15m === 'bearish') trendEl.classList.add('text-scalper-red');
        else trendEl.classList.add('text-gray-400');
    }

    // Confluence
    if (data.latest_confluence) {
        const c = data.latest_confluence;
        $('confluenceScore').textContent = `${c.score}/5`;

        // Condition indicators
        const conds = c.conditions;
        $('cond1').textContent = conds.c1_bullish_trend ? '✅' : '❌';
        $('cond2').textContent = conds.c2_vwap_position ? '✅' : '❌';
        $('cond3').textContent = conds.c3_volume_spike ? '✅' : '❌';
        $('cond4').textContent = conds.c4_rsi_zone ? '✅' : '❌';
        $('cond5').textContent = conds.c5_bid_wall ? '✅' : '❌';

        $('confluenceReason').textContent = c.reason;
    }

    // Active trade
    if (data.active_trade) {
        renderActiveTrade(data.active_trade);
    } else {
        $('activeTradePanel').innerHTML = '<div class="text-center py-8 text-gray-600 text-sm">No active position</div>';
    }

    // Risk status
    if (data.risk_status) {
        const r = data.risk_status;
        $('dailyLosses').textContent = `${r.consecutive_losses} / ${r.max_daily_losses}`;
        $('tradesToday').textContent = r.trades_today;

        const cbEl = $('circuitBreaker');
        if (r.halted) {
            cbEl.textContent = 'TRIGGERED';
            cbEl.className = 'font-bold text-scalper-red';
            $('haltInfo').classList.remove('hidden');
            $('haltInfo').textContent = `Halted until: ${r.halt_until || 'manual reset'}`;
        } else {
            cbEl.textContent = 'ARMED';
            cbEl.className = 'font-bold text-scalper-green';
            $('haltInfo').classList.add('hidden');
        }
    }
}

function renderActiveTrade(trade) {
    const pnl = trade.net_pnl_usdt || 0;
    const pnlClass = pnl >= 0 ? 'text-scalper-green' : 'text-scalper-red';

    $('activeTradePanel').innerHTML = `
        <div class="grid grid-cols-2 gap-3 fade-in">
            <div class="bg-scalper-dark rounded-lg p-3">
                <div class="text-xs text-gray-600">Entry Price</div>
                <div class="text-sm font-bold text-white">${fmtUSD(trade.entry_price)}</div>
            </div>
            <div class="bg-scalper-dark rounded-lg p-3">
                <div class="text-xs text-gray-600">Quantity</div>
                <div class="text-sm font-bold text-white">${fmtBTC(trade.quantity_btc)}</div>
            </div>
            <div class="bg-scalper-dark rounded-lg p-3">
                <div class="text-xs text-gray-600">Stop Loss</div>
                <div class="text-sm font-bold text-scalper-red">${fmtUSD(trade.stop_loss_price)}</div>
            </div>
            <div class="bg-scalper-dark rounded-lg p-3">
                <div class="text-xs text-gray-600">Take Profit</div>
                <div class="text-sm font-bold text-scalper-green">${fmtUSD(trade.take_profit_price)}</div>
            </div>
            <div class="bg-scalper-dark rounded-lg p-3">
                <div class="text-xs text-gray-600">Trailing SL</div>
                <div class="text-sm font-bold text-scalper-yellow">${fmtUSD(trade.trailing_sl_price)}</div>
            </div>
            <div class="bg-scalper-dark rounded-lg p-3">
                <div class="text-xs text-gray-600">Position Size</div>
                <div class="text-sm font-bold text-white">${fmtUSD(trade.position_size_usdt)}</div>
            </div>
            <div class="bg-scalper-dark rounded-lg p-3 col-span-2">
                <div class="text-xs text-gray-600">Confluence Score at Entry</div>
                <div class="text-sm font-bold text-scalper-blue">${trade.confluence_score}/5</div>
            </div>
        </div>
    `;
}

// ──────────────────────────────────────────────
//  REST API Calls
// ──────────────────────────────────────────────

async function apiCall(method, path, body = null) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
}

// ─── Settings ───

async function loadSettings() {
    try {
        const s = await apiCall('GET', '/api/settings');
        currentMode = s.mode;
        updateModeUI(s.mode);
        $('credStatus').textContent = s.has_api_key ? '✅ Credentials saved' : 'No credentials set';
        $('credStatus').className = s.has_api_key ? 'text-xs text-scalper-green text-center' : 'text-xs text-gray-600 text-center';

        // Auto-trade toggle
        updateAutoTradeUI(s.auto_trade);
    } catch (e) {
        console.error('loadSettings error:', e);
    }
}

async function saveCredentials() {
    const apiKey = $('apiKeyInput').value.trim();
    const apiSecret = $('apiSecretInput').value.trim();

    if (!apiKey || !apiSecret) {
        $('credStatus').textContent = '⚠️ Please enter both key and secret';
        $('credStatus').className = 'text-xs text-scalper-red text-center';
        return;
    }

    $('credStatus').textContent = 'Saving...';
    $('credStatus').className = 'text-xs text-scalper-yellow text-center';

    try {
        const res = await apiCall('POST', '/api/settings/credentials', {
            api_key: apiKey,
            api_secret: apiSecret,
        });
        $('credStatus').textContent = '✅ ' + res.message;
        $('credStatus').className = 'text-xs text-scalper-green text-center';
        $('apiKeyInput').value = '';
        $('apiSecretInput').value = '';
    } catch (e) {
        $('credStatus').textContent = '❌ ' + e.message;
        $('credStatus').className = 'text-xs text-scalper-red text-center';
    }
}

async function updateMode(mode) {
    try {
        await apiCall('POST', '/api/settings/mode', { mode });
        currentMode = mode;
        updateModeUI(mode);
    } catch (e) {
        console.error('updateMode error:', e);
        alert('Failed to switch mode: ' + e.message);
    }
}

async function toggleAutoTrade() {
    const isOn = $('autoTradeToggle').classList.contains('auto-trade-on');
    try {
        await apiCall('POST', '/api/settings/autotrade', { enabled: !isOn });
        updateAutoTradeUI(!isOn);
    } catch (e) {
        console.error('toggleAutoTrade error:', e);
        alert('Failed to toggle auto-trading: ' + e.message);
    }
}

// ─── Emergency Stop ───

async function executeEmergencyStop() {
    try {
        const res = await apiCall('POST', '/api/emergency-stop', { confirm: true });
        alert(res.message);
        $('emergencyModal').classList.add('hidden');
        updateAutoTradeUI(false);
    } catch (e) {
        alert('Emergency stop failed: ' + e.message);
    }
}

// ─── Trades & Performance ───

async function loadTrades() {
    try {
        const trades = await apiCall('GET', '/api/trades?limit=50');
        renderTradesTable(trades);
    } catch (e) {
        console.error('loadTrades error:', e);
    }
}

async function loadPerformance() {
    try {
        const p = await apiCall('GET', '/api/performance');
        $('winRate').textContent = `${p.win_rate}%`;
        $('totalTrades').textContent = p.total_trades;
        $('metricNetProfit').textContent = fmtUSD(p.total_net_profit);
        $('metricNetProfit').className = `text-lg font-bold ${p.total_net_profit >= 0 ? 'text-scalper-green' : 'text-scalper-red'}`;
        $('metricProfitFactor').textContent = p.profit_factor === 999 ? '∞' : p.profit_factor.toFixed(2);
        $('metricAvgWin').textContent = fmtUSD(p.avg_win);
        $('metricAvgLoss').textContent = fmtUSD(p.avg_loss);
        $('metricTotalFees').textContent = fmtUSD(p.total_fees);
    } catch (e) {
        console.error('loadPerformance error:', e);
    }
}

async function loadLogs() {
    try {
        const logs = await apiCall('GET', '/api/logs?limit=50');
        renderLogs(logs);
    } catch (e) {
        console.error('loadLogs error:', e);
    }
}

// ──────────────────────────────────────────────
//  Renderers
// ──────────────────────────────────────────────

function renderTradesTable(trades) {
    const tbody = $('tradesTableBody');
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="13" class="text-center py-6 text-gray-600">No trades yet</td></tr>';
        return;
    }

    tbody.innerHTML = trades.map(t => {
        const pnlClass = t.net_pnl_usdt >= 0 ? 'text-scalper-green' : 'text-scalper-red';
        const rowClass = t.status === 'CLOSED'
            ? (t.net_pnl_usdt >= 0 ? 'trade-row-win' : 'trade-row-loss')
            : 'trade-row-open';

        const statusBadge = t.status === 'CLOSED'
            ? `<span class="px-2 py-0.5 rounded bg-gray-700 text-gray-300">${t.status}</span>`
            : `<span class="px-2 py-0.5 rounded bg-scalper-blue text-white">${t.status}</span>`;

        const time = t.entry_time ? new Date(t.entry_time).toLocaleString() : '--';

        return `
            <tr class="${rowClass} border-b border-scalper-border hover:bg-scalper-dark transition">
                <td class="py-2 px-2 text-gray-500">${t.id}</td>
                <td class="py-2 px-2">${statusBadge}</td>
                <td class="py-2 px-2 text-right">${fmtUSD(t.entry_price)}</td>
                <td class="py-2 px-2 text-right">${t.exit_price ? fmtUSD(t.exit_price) : '--'}</td>
                <td class="py-2 px-2 text-right">${fmtBTC(t.quantity_btc)}</td>
                <td class="py-2 px-2 text-right text-scalper-red">${t.stop_loss_price ? fmtUSD(t.stop_loss_price) : '--'}</td>
                <td class="py-2 px-2 text-right text-scalper-green">${t.take_profit_price ? fmtUSD(t.take_profit_price) : '--'}</td>
                <td class="py-2 px-2 text-right text-scalper-yellow">${fmtUSD(t.fees_total_usdt)}</td>
                <td class="py-2 px-2 text-right ${pnlClass} font-bold">${t.status === 'CLOSED' ? fmtUSD(t.net_pnl_usdt) : '--'}</td>
                <td class="py-2 px-2 text-right ${pnlClass}">${t.status === 'CLOSED' ? fmtPct(t.return_pct) : '--'}</td>
                <td class="py-2 px-2 text-center text-scalper-blue">${t.confluence_score || '--'}/5</td>
                <td class="py-2 px-2 text-gray-500">${t.exit_reason || '--'}</td>
                <td class="py-2 px-2 text-gray-500 text-xs">${time}</td>
            </tr>
        `;
    }).join('');
}

function renderLogs(logs) {
    const feed = $('logFeed');
    if (!logs || logs.length === 0) {
        feed.innerHTML = '<div class="text-gray-600">No logs yet</div>';
        return;
    }

    feed.innerHTML = logs.reverse().map(l => {
        const time = new Date(l.timestamp).toLocaleTimeString();
        return `<div class="log-${l.level} fade-in">
            <span class="text-gray-600">[${time}]</span>
            <span class="font-bold ${l.level === 'TRADE' ? 'text-scalper-green' : l.level === 'ERROR' ? 'text-scalper-red' : l.level === 'WARN' ? 'text-scalper-yellow' : ''}">[${l.level}]</span>
            ${l.message}
        </div>`;
    }).join('');
}

// ──────────────────────────────────────────────
//  UI State Updaters
// ──────────────────────────────────────────────

function updateModeUI(mode) {
    const demoBtn = $('modeDemo');
    const liveBtn = $('modeLive');

    if (mode === 'live') {
        demoBtn.className = 'px-3 py-1.5 text-xs font-bold rounded-md mode-inactive';
        liveBtn.className = 'px-3 py-1.5 text-xs font-bold rounded-md bg-scalper-red text-white';
    } else {
        demoBtn.className = 'px-3 py-1.5 text-xs font-bold rounded-md bg-scalper-blue text-white';
        liveBtn.className = 'px-3 py-1.5 text-xs font-bold rounded-md mode-inactive';
    }
}

function updateAutoTradeUI(enabled) {
    const toggle = $('autoTradeToggle');
    const knob = $('autoTradeKnob');

    if (enabled) {
        toggle.classList.add('auto-trade-on');
        knob.classList.add('knob');
        knob.style.transform = 'translateX(24px)';
        knob.style.background = 'white';
    } else {
        toggle.classList.remove('auto-trade-on');
        knob.style.transform = 'translateX(0)';
        knob.style.background = '#6b7280';
    }
}

// ──────────────────────────────────────────────
//  Event Bindings
// ──────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // WebSocket
    connectWS();

    // Load initial data
    loadSettings();
    loadTrades();
    loadPerformance();
    loadLogs();

    // Periodic refresh (every 15s for non-WS data)
    setInterval(() => {
        loadTrades();
        loadPerformance();
        loadLogs();
    }, 15000);

    // ── Mode toggle ──
    $('modeDemo').addEventListener('click', () => {
        if (currentMode !== 'demo') updateMode('demo');
    });
    $('modeLive').addEventListener('click', () => {
        if (currentMode !== 'live') {
            if (confirm('⚠️ Switching to LIVE mode — real funds will be at risk. Continue?')) {
                updateMode('live');
            }
        }
    });

    // ── Credentials ──
    $('saveCredentialsBtn').addEventListener('click', saveCredentials);

    // ── Auto-trade toggle ──
    $('autoTradeToggle').addEventListener('click', toggleAutoTrade);

    // ── Emergency stop ──
    $('emergencyStopBtn').addEventListener('click', () => {
        $('emergencyModal').classList.remove('hidden');
    });
    $('emergencyCancel').addEventListener('click', () => {
        $('emergencyModal').classList.add('hidden');
    });
    $('emergencyConfirm').addEventListener('click', executeEmergencyStop);

    // Close modal on backdrop click
    $('emergencyModal').addEventListener('click', (e) => {
        if (e.target.id === 'emergencyModal') {
            $('emergencyModal').classList.add('hidden');
        }
    });
});