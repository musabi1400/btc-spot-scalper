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

// ═══════════════════════════════════════════════
//  Phase 3: Analytics, Equity Curve, Alerts, Signal Score
// ═══════════════════════════════════════════════

// Helper: fetch with graceful 404 handling
async function apiCallSafe(method, path, body = null) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    if (res.status === 404) {
        // Endpoint not yet implemented — return null so callers can show "unavailable"
        return null;
    }
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
}

// ─── Daily / Monthly Performance ───

async function loadPerformanceAnalytics() {
    const panel = $('performancePanel');
    try {
        const data = await apiCallSafe('GET', '/api/analytics/performance');
        if (!data) {
            panel.innerHTML = '<div class="text-center py-6 text-gray-600 text-sm">Performance analytics endpoint not available yet.</div>';
            return;
        }
        renderPerformanceAnalytics(data);
    } catch (e) {
        console.error('loadPerformanceAnalytics error:', e);
        panel.innerHTML = `<div class="text-center py-6 text-scalper-red text-sm">Failed to load: ${escapeHtml(e.message)}</div>`;
    }
}

function renderPerformanceAnalytics(data) {
    const panel = $('performancePanel');
    const daily = data.daily || {};
    const monthly = data.monthly || {};
    const dailyPnl = parseFloat(daily.net_pnl || 0);
    const monthlyPnl = parseFloat(monthly.net_pnl || 0);
    const dailyClass = dailyPnl >= 0 ? 'text-scalper-green' : 'text-scalper-red';
    const monthlyClass = monthlyPnl >= 0 ? 'text-scalper-green' : 'text-scalper-red';

    // Build CSS bar chart of last N days (if provided)
    const dailyBars = data.daily_history || [];
    const maxAbsPnl = Math.max(1, ...dailyBars.map(d => Math.abs(parseFloat(d.net_pnl || 0))));

    const barsHtml = dailyBars.length ? `
        <div class="mt-4">
            <div class="text-xs text-gray-500 mb-2">Recent Daily PnL</div>
            <div class="flex items-end gap-1 h-32">
                ${dailyBars.map(d => {
                    const pnl = parseFloat(d.net_pnl || 0);
                    const h = Math.max(2, (Math.abs(pnl) / maxAbsPnl) * 100);
                    const color = pnl >= 0 ? '#10b981' : '#ef4444';
                    const label = d.date ? d.date.slice(5) : '';
                    return `
                        <div class="flex-1 flex flex-col items-center justify-end" title="${escapeHtml(label)}: ${fmtUSD(pnl)}">
                            <div style="height: ${h}%; width: 100%; background: ${color}; border-radius: 3px 3px 0 0; min-height: 2px;"></div>
                            <div class="text-[9px] text-gray-600 mt-1">${escapeHtml(label)}</div>
                        </div>
                    `;
                }).join('')}
            </div>
        </div>
    ` : '';

    panel.innerHTML = `
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 fade-in">
            <!-- Daily -->
            <div class="bg-scalper-dark rounded-lg p-4">
                <h3 class="text-xs font-bold text-scalper-blue mb-3">Today</h3>
                <div class="grid grid-cols-2 gap-3 text-xs">
                    <div><span class="text-gray-500">Trades:</span> <span class="font-bold text-white">${daily.trades || 0}</span></div>
                    <div><span class="text-gray-500">Wins:</span> <span class="font-bold text-scalper-green">${daily.wins || 0}</span></div>
                    <div><span class="text-gray-500">Losses:</span> <span class="font-bold text-scalper-red">${daily.losses || 0}</span></div>
                    <div><span class="text-gray-500">Net PnL:</span> <span class="font-bold ${dailyClass}">${fmtUSD(dailyPnl)}</span></div>
                </div>
            </div>
            <!-- Monthly -->
            <div class="bg-scalper-dark rounded-lg p-4">
                <h3 class="text-xs font-bold text-scalper-yellow mb-3">This Month</h3>
                <div class="grid grid-cols-2 gap-3 text-xs">
                    <div><span class="text-gray-500">Trades:</span> <span class="font-bold text-white">${monthly.trades || 0}</span></div>
                    <div><span class="text-gray-500">Wins:</span> <span class="font-bold text-scalper-green">${monthly.wins || 0}</span></div>
                    <div><span class="text-gray-500">Losses:</span> <span class="font-bold text-scalper-red">${monthly.losses || 0}</span></div>
                    <div><span class="text-gray-500">Net PnL:</span> <span class="font-bold ${monthlyClass}">${fmtUSD(monthlyPnl)}</span></div>
                </div>
            </div>
        </div>
        ${barsHtml}
    `;
}

// ─── Equity Curve ───

async function loadEquityCurve() {
    const statusEl = $('equityCurveStatus');
    try {
        const data = await apiCallSafe('GET', '/api/analytics/equity-curve');
        if (!data) {
            if (statusEl) statusEl.textContent = 'Equity curve endpoint not available yet.';
            // Clear canvas
            drawEquityCurve('equityCurveCanvas', []);
            return;
        }
        const points = data.points || data.equity || data || [];
        drawEquityCurve('equityCurveCanvas', points);
        if (statusEl) statusEl.style.display = 'none';
    } catch (e) {
        console.error('loadEquityCurve error:', e);
        if (statusEl) statusEl.textContent = 'Failed to load equity curve: ' + e.message;
        drawEquityCurve('equityCurveCanvas', []);
    }
}

function drawEquityCurve(canvasId, data) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    // Handle high-DPI
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || canvas.width;
    const cssH = canvas.clientHeight || canvas.height || 320;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    ctx.scale(dpr, dpr);

    const w = cssW;
    const h = cssH;
    const padL = 50, padR = 16, padT = 20, padB = 28;
    const plotW = w - padL - padR;
    const plotH = h - padT - padB;

    // Clear
    ctx.clearRect(0, 0, w, h);

    // Background grid
    ctx.strokeStyle = '#1f2937';
    ctx.lineWidth = 1;
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillStyle = '#6b7280';

    // Horizontal grid lines + y labels
    const yLines = 4;
    for (let i = 0; i <= yLines; i++) {
        const y = padT + (plotH / yLines) * i;
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(w - padR, y);
        ctx.stroke();
    }

    // No data
    if (!data || !data.length) {
        ctx.fillStyle = '#6b7280';
        ctx.textAlign = 'center';
        ctx.fillText('No equity data yet', w / 2, h / 2);
        return;
    }

    // Normalize: extract cumulative pnl values
    const points = data.map(p => {
        if (typeof p === 'number') return { t: null, v: p };
        return {
            t: p.timestamp || p.time || p.date || null,
            v: parseFloat(p.cumulative_pnl != null ? p.cumulative_pnl : (p.pnl != null ? p.pnl : (p.value != null ? p.value : 0))),
        };
    });
    const values = points.map(p => p.v);
    let minV = Math.min(0, ...values);
    let maxV = Math.max(0, ...values);
    if (minV === maxV) { maxV = minV + 1; }

    // Draw y labels
    ctx.textAlign = 'right';
    for (let i = 0; i <= yLines; i++) {
        const frac = 1 - (i / yLines);
        const val = minV + (maxV - minV) * frac;
        const y = padT + (plotH / yLines) * i;
        ctx.fillText(fmtUSD(val).replace('$', ''), padL - 6, y + 3);
    }

    // Zero line
    if (minV < 0 && maxV > 0) {
        const zeroY = padT + plotH * (maxV / (maxV - minV));
        ctx.strokeStyle = '#374151';
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(padL, zeroY);
        ctx.lineTo(w - padR, zeroY);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    // Map points to pixel coords
    const xStep = points.length > 1 ? plotW / (points.length - 1) : 0;
    const xy = points.map((p, i) => ({
        x: padL + i * xStep,
        y: padT + plotH * (1 - (p.v - minV) / (maxV - minV)),
        v: p.v,
    }));

    // Fill area under curve
    const baseY = padT + plotH;
    ctx.beginPath();
    ctx.moveTo(xy[0].x, baseY);
    xy.forEach(pt => ctx.lineTo(pt.x, pt.y));
    ctx.lineTo(xy[xy.length - 1].x, baseY);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, padT, 0, baseY);
    grad.addColorStop(0, 'rgba(16, 185, 129, 0.25)');
    grad.addColorStop(1, 'rgba(16, 185, 129, 0.02)');
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    const endV = values[values.length - 1];
    const lineColor = endV >= 0 ? '#10b981' : '#ef4444';
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2;
    ctx.beginPath();
    xy.forEach((pt, i) => {
        if (i === 0) ctx.moveTo(pt.x, pt.y);
        else ctx.lineTo(pt.x, pt.y);
    });
    ctx.stroke();

    // End dot
    const last = xy[xy.length - 1];
    ctx.fillStyle = lineColor;
    ctx.beginPath();
    ctx.arc(last.x, last.y, 3, 0, Math.PI * 2);
    ctx.fill();

    // x labels (first / last)
    ctx.fillStyle = '#6b7280';
    ctx.textAlign = 'left';
    if (points[0].t) {
        const t0 = points[0].t;
        ctx.fillText(String(t0).slice(5, 16), padL, h - 8);
    }
    ctx.textAlign = 'right';
    if (points[points.length - 1].t) {
        const tN = points[points.length - 1].t;
        ctx.fillText(String(tN).slice(5, 16), w - padR, h - 8);
    }

    // End value label
    ctx.fillStyle = lineColor;
    ctx.textAlign = 'right';
    ctx.font = 'bold 11px "JetBrains Mono", monospace';
    ctx.fillText(fmtUSD(endV), w - padR, padT + 12);
}

// ─── Trade Distribution ───

async function loadTradeDistribution() {
    try {
        const data = await apiCallSafe('GET', '/api/analytics/distribution');
        if (!data) {
            $('distByHour').innerHTML = '<div class="text-gray-600 text-sm">Distribution endpoint not available yet.</div>';
            $('distByExit').innerHTML = '<div class="text-gray-600 text-sm">—</div>';
            $('distWinLoss').innerHTML = '<div class="text-gray-600 text-sm">—</div>';
            return;
        }
        renderTradeDistribution(data);
    } catch (e) {
        console.error('loadTradeDistribution error:', e);
        $('distByHour').innerHTML = `<div class="text-scalper-red text-sm">Failed: ${escapeHtml(e.message)}</div>`;
    }
}

function renderTradeDistribution(data) {
    // Trades by hour
    const byHour = data.by_hour || data.trades_by_hour || {};
    const hourEl = $('distByHour');
    const hourEntries = Object.entries(byHour);
    const maxHour = Math.max(1, ...hourEntries.map(([_, v]) => v));
    if (!hourEntries.length) {
        hourEl.innerHTML = '<div class="text-gray-600 text-sm">No data</div>';
    } else {
        hourEl.innerHTML = hourEntries.map(([hour, count]) => {
            const pct = (count / maxHour) * 100;
            return `
                <div class="flex items-center gap-2 text-xs">
                    <span class="text-gray-500 w-8 text-right">${String(hour).padStart(2,'0')}h</span>
                    <div class="css-bar-track flex-1">
                        <div class="css-bar-fill" style="width: ${pct}%; background: #3b82f6;"></div>
                    </div>
                    <span class="text-gray-400 w-6 text-right">${count}</span>
                </div>
            `;
        }).join('');
    }

    // Trades by exit reason
    const byExit = data.by_exit_reason || data.trades_by_exit_reason || {};
    const exitEl = $('distByExit');
    const exitColors = {
        take_profit: '#10b981',
        stop_loss: '#ef4444',
        trailing: '#f59e0b',
        emergency: '#7f1d1d',
    };
    const exitEntries = Object.entries(byExit);
    const maxExit = Math.max(1, ...exitEntries.map(([_, v]) => v));
    if (!exitEntries.length) {
        exitEl.innerHTML = '<div class="text-gray-600 text-sm">No data</div>';
    } else {
        exitEl.innerHTML = exitEntries.map(([reason, count]) => {
            const pct = (count / maxExit) * 100;
            const color = exitColors[reason] || '#6b7280';
            return `
                <div class="flex items-center gap-2 text-xs">
                    <span class="text-gray-500 w-24 truncate">${escapeHtml(reason)}</span>
                    <div class="css-bar-track flex-1">
                        <div class="css-bar-fill" style="width: ${pct}%; background: ${color};"></div>
                    </div>
                    <span class="text-gray-400 w-6 text-right">${count}</span>
                </div>
            `;
        }).join('');
    }

    // Win/loss donut
    const wins = data.wins || 0;
    const losses = data.losses || 0;
    const total = wins + losses;
    const wlEl = $('distWinLoss');
    if (total === 0) {
        wlEl.innerHTML = '<div class="text-gray-600 text-sm">No trades</div>';
        return;
    }
    const winPct = (wins / total) * 100;
    const lossPct = (losses / total) * 100;
    // conic-gradient donut
    const donutStyle = `background: conic-gradient(#10b981 0% ${winPct}%, #ef4444 ${winPct}% 100%);`;

    wlEl.innerHTML = `
        <div class="winloss-donut mb-3" style="${donutStyle}"></div>
        <div class="flex gap-4 text-xs">
            <div class="flex items-center gap-1">
                <span class="w-2.5 h-2.5 rounded-full" style="background:#10b981;"></span>
                <span class="text-gray-400">Wins: <span class="font-bold text-scalper-green">${wins}</span> (${winPct.toFixed(1)}%)</span>
            </div>
            <div class="flex items-center gap-1">
                <span class="w-2.5 h-2.5 rounded-full" style="background:#ef4444;"></span>
                <span class="text-gray-400">Losses: <span class="font-bold text-scalper-red">${losses}</span> (${lossPct.toFixed(1)}%)</span>
            </div>
        </div>
    `;
}

// ─── Alerts ───

async function loadAlerts() {
    const panel = $('alertsPanel');
    try {
        const data = await apiCallSafe('GET', '/api/alerts?limit=20');
        if (!data) {
            panel.innerHTML = '<div class="text-gray-600 text-sm">Alerts endpoint not available yet.</div>';
            return;
        }
        const alerts = Array.isArray(data) ? data : (data.alerts || []);
        renderAlerts(alerts);
    } catch (e) {
        console.error('loadAlerts error:', e);
        panel.innerHTML = `<div class="text-scalper-red text-sm">Failed to load alerts: ${escapeHtml(e.message)}</div>`;
    }
}

function renderAlerts(alerts) {
    const panel = $('alertsPanel');
    if (!alerts || alerts.length === 0) {
        panel.innerHTML = '<div class="text-gray-600 text-sm">No alerts</div>';
        return;
    }

    panel.innerHTML = alerts.map(a => {
        const level = (a.level || 'INFO').toUpperCase();
        const time = a.timestamp ? new Date(a.timestamp).toLocaleString() : '--';
        const ackd = a.acknowledged ? 'alert-acknowledged' : '';
        const ackBtn = a.acknowledged ? '' : `
            <button onclick="acknowledgeAlert(${a.id})" class="text-[10px] px-2 py-0.5 rounded bg-scalper-border hover:bg-gray-600 text-gray-300 transition">Acknowledge</button>
        `;
        return `
            <div class="alert-${level} ${ackd} rounded-lg p-3 text-xs fade-in">
                <div class="flex items-start justify-between gap-2">
                    <div class="flex-1">
                        <div class="flex items-center gap-2 mb-1">
                            <span class="font-bold ${level === 'CRITICAL' ? 'text-red-300' : level === 'ERROR' ? 'text-scalper-red' : level === 'WARN' ? 'text-scalper-yellow' : 'text-scalper-blue'}">[${level}]</span>
                            <span class="text-gray-600">${time}</span>
                        </div>
                        <div class="text-gray-300">${escapeHtml(a.message || a.title || '')}</div>
                        ${a.source ? `<div class="text-gray-600 text-[10px] mt-1">Source: ${escapeHtml(a.source)}</div>` : ''}
                    </div>
                    <div class="flex-shrink-0">${ackBtn}</div>
                </div>
            </div>
        `;
    }).join('');
}

async function acknowledgeAlert(id) {
    try {
        await apiCall('POST', `/api/alerts/${id}/acknowledge`);
        // Reload alerts to reflect acknowledgement
        loadAlerts();
    } catch (e) {
        console.error('acknowledgeAlert error:', e);
        // Try safe variant in case 404
        const res = await apiCallSafe('POST', `/api/alerts/${id}/acknowledge`);
        if (res === null) {
            alert('Alerts acknowledge endpoint not available yet.');
        } else {
            alert('Failed to acknowledge: ' + e.message);
        }
    }
}

// ─── Signal Score ───

async function loadSignalScore() {
    const panel = $('signalScorePanel');
    try {
        const data = await apiCallSafe('GET', '/api/analytics/signal-score');
        if (!data) {
            panel.innerHTML = '<div class="text-center py-6 text-gray-600 text-sm">Signal score endpoint not available yet.</div>';
            return;
        }
        renderSignalScore(data);
    } catch (e) {
        console.error('loadSignalScore error:', e);
        panel.innerHTML = `<div class="text-center py-6 text-scalper-red text-sm">Failed: ${escapeHtml(e.message)}</div>`;
    }
}

function renderSignalScore(data) {
    const panel = $('signalScorePanel');
    const score = Math.min(100, Math.max(0, parseFloat(data.total_score || data.score || 0)));
    const recommendation = (data.recommendation || 'WAIT').toUpperCase();
    const regime = data.market_regime || data.regime || 'UNKNOWN';

    // Recommendation color
    const recColors = {
        ENTER: { bg: 'bg-scalper-green', text: 'text-white', label: 'ENTER' },
        WAIT: { bg: 'bg-scalper-yellow', text: 'text-black', label: 'WAIT' },
        SKIP: { bg: 'bg-scalper-red', text: 'text-white', label: 'SKIP' },
    };
    const rec = recColors[recommendation] || recColors.WAIT;

    // Regime color
    const regimeColors = {
        TRENDING_BULL: 'text-scalper-green',
        TRENDING_BEAR: 'text-scalper-red',
        RANGING: 'text-gray-400',
        VOLATILE: 'text-scalper-yellow',
    };
    const regimeColor = regimeColors[regime] || 'text-gray-400';

    // Score bar color
    let barColor = '#ef4444';
    if (score >= 70) barColor = '#10b981';
    else if (score >= 45) barColor = '#f59e0b';

    // Individual conditions
    const conditions = data.conditions || data.condition_scores || [];
    const condHtml = conditions.length ? `
        <div class="mt-4 space-y-2">
            <div class="text-xs text-gray-500 mb-1">Individual Condition Scores</div>
            ${conditions.map(c => {
                const name = c.name || c.condition || '—';
                const val = parseFloat(c.score || 0);
                const weight = parseFloat(c.weight || 0);
                const maxScore = parseFloat(c.max || 100);
                const fillPct = Math.min(100, (val / maxScore) * 100);
                const cColor = val / maxScore >= 0.7 ? '#10b981' : val / maxScore >= 0.4 ? '#f59e0b' : '#ef4444';
                return `
                    <div class="text-xs">
                        <div class="flex justify-between mb-0.5">
                            <span class="text-gray-400">${escapeHtml(name)} <span class="text-gray-600">(w:${weight})</span></span>
                            <span class="font-bold" style="color:${cColor};">${val}</span>
                        </div>
                        <div class="css-bar-track">
                            <div class="css-bar-fill" style="width: ${fillPct}%; background: ${cColor};"></div>
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    ` : '';

    panel.innerHTML = `
        <div class="fade-in">
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
                <!-- Score -->
                <div class="bg-scalper-dark rounded-lg p-4">
                    <div class="text-xs text-gray-500 mb-1">Total Weighted Score</div>
                    <div class="text-3xl font-bold mb-2" style="color:${barColor};">${score.toFixed(0)}<span class="text-sm text-gray-600">/100</span></div>
                    <div class="css-bar-track" style="height:10px;">
                        <div class="css-bar-fill" style="width: ${score}%; background: ${barColor};"></div>
                    </div>
                </div>
                <!-- Recommendation -->
                <div class="bg-scalper-dark rounded-lg p-4 flex flex-col items-center justify-center">
                    <div class="text-xs text-gray-500 mb-1">Recommendation</div>
                    <span class="px-4 py-1.5 rounded-lg ${rec.bg} ${rec.text} text-lg font-bold">${rec.label}</span>
                </div>
                <!-- Market regime -->
                <div class="bg-scalper-dark rounded-lg p-4 flex flex-col items-center justify-center">
                    <div class="text-xs text-gray-500 mb-1">Market Regime</div>
                    <span class="text-lg font-bold ${regimeColor}">${escapeHtml(regime)}</span>
                </div>
            </div>
            ${condHtml}
        </div>
    `;
}

// ─── HTML escape helper ───
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
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

    // Load initial data (existing)
    loadSettings();
    loadTrades();
    loadPerformance();
    loadLogs();

    // Load initial data (Phase 3 — analytics & alerts)
    loadPerformanceAnalytics();
    loadEquityCurve();
    loadTradeDistribution();
    loadSignalScore();
    loadAlerts();

    // Periodic refresh (every 15s for non-WS data)
    setInterval(() => {
        loadTrades();
        loadPerformance();
        loadLogs();
    }, 15000);

    // Phase 3: Analytics refresh every 30s
    setInterval(() => {
        loadPerformanceAnalytics();
        loadEquityCurve();
        loadTradeDistribution();
        loadSignalScore();
    }, 30000);

    // Phase 3: Alerts refresh every 15s
    setInterval(() => {
        loadAlerts();
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

    // ── Phase 3: Alerts refresh button ──
    const refreshAlertsBtn = $('refreshAlertsBtn');
    if (refreshAlertsBtn) refreshAlertsBtn.addEventListener('click', loadAlerts);

    // Redraw equity curve on window resize
    let resizeTimer = null;
    window.addEventListener('resize', () => {
        if (resizeTimer) clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
            // Only redraw if we have data cached — re-fetch is simplest
            loadEquityCurve();
        }, 300);
    });
});