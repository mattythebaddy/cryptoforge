/**
 * CryptoForge Dashboard — Live trading dashboard with WebSocket updates.
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let ws = null;
let equityChart = null;
let strategyChart = null;
let reconnectAttempts = 0;
const MAX_RECONNECT = 20;

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function fetchJSON(url) {
    try {
        const res = await fetch(url);
        return await res.json();
    } catch (e) {
        console.warn('Fetch failed:', url, e);
        return null;
    }
}

function formatMoney(val) {
    if (val === null || val === undefined) return '$0.00';
    const sign = val >= 0 ? '+' : '';
    return `${sign}$${Math.abs(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatPct(val) {
    if (val === null || val === undefined) return '0.00%';
    const sign = val >= 0 ? '+' : '';
    return `${sign}${val.toFixed(2)}%`;
}

function pnlClass(val) {
    if (val > 0) return 'pnl-positive';
    if (val < 0) return 'pnl-negative';
    return '';
}

function changeClass(val) {
    if (val > 0) return 'positive';
    if (val < 0) return 'negative';
    return 'neutral';
}

function stratBadge(strat) {
    return `<span class="strategy-badge ${strat}">${strat}</span>`;
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
    });
});

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
function initCharts() {
    const chartDefaults = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { display: false },
        },
        scales: {
            x: {
                grid: { color: 'rgba(48, 54, 61, 0.5)' },
                ticks: { color: '#8b949e', font: { size: 11 } },
            },
            y: {
                grid: { color: 'rgba(48, 54, 61, 0.5)' },
                ticks: { color: '#8b949e', font: { size: 11 } },
            },
        },
    };

    // Equity curve
    const eqCtx = document.getElementById('equity-chart').getContext('2d');
    equityChart = new Chart(eqCtx, {
        type: 'line',
        data: {
            labels: ['Start'],
            datasets: [{
                data: [10000],
                borderColor: '#58a6ff',
                backgroundColor: 'rgba(88, 166, 255, 0.08)',
                fill: true,
                tension: 0.3,
                pointRadius: 2,
                pointHoverRadius: 5,
                borderWidth: 2,
            }],
        },
        options: {
            ...chartDefaults,
            scales: {
                ...chartDefaults.scales,
                y: {
                    ...chartDefaults.scales.y,
                    ticks: {
                        ...chartDefaults.scales.y.ticks,
                        callback: v => '$' + v.toLocaleString(),
                    },
                },
            },
        },
    });

    // Strategy P&L bar chart
    const stCtx = document.getElementById('strategy-chart').getContext('2d');
    strategyChart = new Chart(stCtx, {
        type: 'bar',
        data: {
            labels: [],
            datasets: [{
                data: [],
                backgroundColor: [],
                borderRadius: 6,
                maxBarThickness: 50,
            }],
        },
        options: {
            ...chartDefaults,
            indexAxis: 'y',
            scales: {
                ...chartDefaults.scales,
                x: {
                    ...chartDefaults.scales.x,
                    ticks: {
                        ...chartDefaults.scales.x.ticks,
                        callback: v => '$' + v.toFixed(2),
                    },
                },
            },
        },
    });
}

function updateEquityChart(data) {
    if (!equityChart || !data || !data.data) return;
    const points = data.data;
    equityChart.data.labels = points.map(p => p.trade === 0 ? 'Start' : `#${p.trade}`);
    equityChart.data.datasets[0].data = points.map(p => p.equity);
    equityChart.update('none');
}

function updateStrategyChart(strategies) {
    if (!strategyChart || !strategies) return;
    const labels = strategies.map(s => s.id);
    const data = strategies.map(s => s.total_pnl);
    const colors = data.map(v => v >= 0 ? '#3fb950' : '#f85149');

    strategyChart.data.labels = labels;
    strategyChart.data.datasets[0].data = data;
    strategyChart.data.datasets[0].backgroundColor = colors;
    strategyChart.update('none');
}

// ---------------------------------------------------------------------------
// Render functions
// ---------------------------------------------------------------------------
function updateStatus(d) {
    document.getElementById('equity').textContent =
        `$${(d.equity || 10000).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;

    const totalPnl = d.total_pnl || 0;
    const totalPnlPct = d.total_pnl_pct || 0;
    const el = document.getElementById('total-pnl');
    el.textContent = `${formatMoney(totalPnl)} (${formatPct(totalPnlPct)})`;
    el.className = `stat-change ${changeClass(totalPnl)}`;

    document.getElementById('win-rate').textContent = `${(d.win_rate || 0).toFixed(1)}%`;
    document.getElementById('total-trades').textContent = `${d.total_trades || 0} trades`;

    document.getElementById('open-positions').textContent = d.open_positions || 0;
    document.getElementById('active-strats').textContent =
        `${(d.active_strategies || []).length} strategies active`;

    const dailyPnl = d.daily_pnl || 0;
    const dpEl = document.getElementById('daily-pnl');
    dpEl.textContent = formatMoney(dailyPnl);
    dpEl.className = `stat-value ${pnlClass(dailyPnl)}`;

    const dd = document.getElementById('drawdown');
    dd.textContent = `Drawdown: ${(d.drawdown_pct || 0).toFixed(2)}%`;
    dd.className = `stat-change ${(d.drawdown_pct || 0) > 5 ? 'negative' : 'neutral'}`;

    // Header
    document.getElementById('regime-badge').textContent = (d.regime || 'ranging').replace(/_/g, ' ').toUpperCase();
    document.getElementById('uptime').textContent = `Uptime: ${d.uptime || '--'}`;

    const cb = d.circuit_breaker;
    if (cb) {
        document.getElementById('regime-badge').style.borderColor = 'rgba(248, 81, 73, 0.5)';
        document.getElementById('regime-badge').style.color = 'var(--red)';
    }
}

function renderTrades(data) {
    const tbody = document.getElementById('trades-body');
    if (!data || !data.trades || data.trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state">No trades yet</td></tr>';
        return;
    }
    tbody.innerHTML = data.trades.map(t => `
        <tr>
            <td>${t.id}</td>
            <td><strong>${t.symbol}</strong></td>
            <td class="${t.side === 'buy' ? 'side-buy' : 'side-sell'}">${t.side.toUpperCase()}</td>
            <td>${stratBadge(t.strategy)}</td>
            <td>$${t.entry_price.toLocaleString()}</td>
            <td>$${t.exit_price.toLocaleString()}</td>
            <td class="${pnlClass(t.pnl)}">${formatMoney(t.pnl)}</td>
            <td class="${pnlClass(t.pnl_pct)}">${formatPct(t.pnl_pct)}</td>
            <td><span class="result-${t.result}">${t.result.toUpperCase()}</span></td>
            <td style="max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"
                title="${t.reason}">${t.reason}</td>
        </tr>
    `).join('');
}

function renderPositions(data) {
    const tbody = document.getElementById('positions-body');
    if (!data || !data.positions || data.positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-state">No open positions</td></tr>';
        return;
    }
    tbody.innerHTML = data.positions.map(p => `
        <tr>
            <td><strong>${p.symbol}</strong></td>
            <td class="${p.side === 'buy' ? 'side-buy' : 'side-sell'}">${p.side.toUpperCase()}</td>
            <td>${stratBadge(p.strategy)}</td>
            <td>$${p.entry_price.toLocaleString()}</td>
            <td>${p.amount.toFixed(6)}</td>
            <td>$${p.notional.toLocaleString()}</td>
            <td>${p.stop_loss ? '$' + p.stop_loss.toLocaleString() : '-'}</td>
            <td>${p.take_profit ? '$' + p.take_profit.toLocaleString() : '-'}</td>
        </tr>
    `).join('');
}

function renderStrategies(data) {
    const tbody = document.getElementById('strategies-body');
    if (!data || !data.strategies || data.strategies.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No strategies loaded</td></tr>';
        return;
    }
    tbody.innerHTML = data.strategies.map(s => `
        <tr>
            <td>${stratBadge(s.id)}</td>
            <td><span class="badge ${s.active ? 'badge-paper' : ''}"
                style="${!s.active ? 'background:var(--red-bg);color:var(--red);border:1px solid rgba(248,81,73,0.3)' : ''}"
                >${s.active ? 'ACTIVE' : 'INACTIVE'}</span></td>
            <td>${s.trades}</td>
            <td>${s.win_rate.toFixed(1)}%</td>
            <td class="${pnlClass(s.total_pnl)}">${formatMoney(s.total_pnl)}</td>
            <td>${s.capital_multiplier}x</td>
        </tr>
    `).join('');
}

function renderOptimizer(data) {
    if (!data) return;

    // Stats
    document.getElementById('optimizer-stats').innerHTML = `
        <div class="optimizer-stat">
            <span class="optimizer-stat-label">Status</span>
            <span class="optimizer-stat-value">${data.status}</span>
        </div>
        <div class="optimizer-stat">
            <span class="optimizer-stat-label">Optimization Cycles</span>
            <span class="optimizer-stat-value">${data.cycle_count}</span>
        </div>
        <div class="optimizer-stat">
            <span class="optimizer-stat-label">Trades Since Last Cycle</span>
            <span class="optimizer-stat-value">${data.trade_counter} / ${data.trigger_every}</span>
        </div>
        <div class="optimizer-stat">
            <span class="optimizer-stat-label">Journal Size</span>
            <span class="optimizer-stat-value">${data.journal_size}</span>
        </div>
        <div class="optimizer-stat">
            <span class="optimizer-stat-label">Consecutive Losses</span>
            <span class="optimizer-stat-value" style="color: ${data.consecutive_losses >= 3 ? 'var(--red)' : 'var(--text)'}">${data.consecutive_losses}</span>
        </div>
    `;

    // Multipliers
    const mults = data.capital_multipliers || {};
    document.getElementById('optimizer-multipliers').innerHTML = Object.entries(mults).map(([sid, mult]) => {
        const pct = ((mult - 0.25) / 1.75) * 100; // 0.25 -> 0%, 2.0 -> 100%
        const color = mult >= 1.0 ? 'var(--green)' : mult >= 0.5 ? 'var(--orange)' : 'var(--red)';
        return `
            <div style="margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; font-size: 13px;">
                    ${stratBadge(sid)}
                    <span style="font-weight: 600; color: ${color}">${mult}x</span>
                </div>
                <div class="multiplier-bar">
                    <div class="multiplier-track">
                        <div class="multiplier-fill" style="width: ${pct}%; background: ${color}"></div>
                    </div>
                </div>
            </div>
        `;
    }).join('') || '<div class="empty-state">No allocations yet</div>';

    // Journal entries
    const jBody = document.getElementById('optimizer-journal');
    if (data.recent_trades && data.recent_trades.length > 0) {
        jBody.innerHTML = data.recent_trades.map(t => `
            <tr>
                <td><strong>${t.symbol}</strong></td>
                <td>${stratBadge(t.strategy)}</td>
                <td class="${t.side === 'buy' ? 'side-buy' : 'side-sell'}">${t.side.toUpperCase()}</td>
                <td class="${pnlClass(t.pnl)}">${formatMoney(t.pnl)}</td>
                <td><span class="result-${t.result}">${t.result.toUpperCase()}</span></td>
                <td>${t.hold_candles} candles</td>
                <td>${t.entry_regime}</td>
            </tr>
        `).join('');
    } else {
        jBody.innerHTML = '<tr><td colspan="7" class="empty-state">No journal entries yet</td></tr>';
    }
}

function renderRisk(data) {
    if (!data) return;
    document.getElementById('risk-content').innerHTML = `
        <div>
            <h3 style="margin-bottom: 12px; font-size: 14px; color: var(--text-secondary);">Circuit Breaker</h3>
            <div class="optimizer-stat">
                <span class="optimizer-stat-label">Status</span>
                <span class="optimizer-stat-value" style="color: ${data.circuit_breaker_active ? 'var(--red)' : 'var(--green)'}">
                    ${data.circuit_breaker_active ? 'TRIGGERED' : 'OK'}
                </span>
            </div>
            <div class="optimizer-stat">
                <span class="optimizer-stat-label">Daily Loss</span>
                <span class="optimizer-stat-value">${(data.daily_loss_pct || 0).toFixed(2)}% / ${data.max_daily_loss_pct || 0}%</span>
            </div>
            <div class="optimizer-stat">
                <span class="optimizer-stat-label">Drawdown</span>
                <span class="optimizer-stat-value">${(data.drawdown_pct || 0).toFixed(2)}% / ${data.max_drawdown_pct || 0}%</span>
            </div>
            <div class="optimizer-stat">
                <span class="optimizer-stat-label">Consecutive Losses</span>
                <span class="optimizer-stat-value">${data.consecutive_losses || 0} / ${data.max_consecutive_losses || 0}</span>
            </div>
        </div>
        <div>
            <h3 style="margin-bottom: 12px; font-size: 14px; color: var(--text-secondary);">Portfolio</h3>
            <div class="optimizer-stat">
                <span class="optimizer-stat-label">Equity</span>
                <span class="optimizer-stat-value">$${(data.equity || 0).toLocaleString()}</span>
            </div>
            <div class="optimizer-stat">
                <span class="optimizer-stat-label">Peak Equity</span>
                <span class="optimizer-stat-value">$${(data.peak_equity || 0).toLocaleString()}</span>
            </div>
            <div class="optimizer-stat">
                <span class="optimizer-stat-label">Portfolio Exposure</span>
                <span class="optimizer-stat-value">${(data.portfolio_exposure_pct || 0).toFixed(2)}%</span>
            </div>
            <div class="optimizer-stat">
                <span class="optimizer-stat-label">Open Positions</span>
                <span class="optimizer-stat-value">${data.open_positions_count || 0}</span>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------
async function loadAll() {
    const [status, trades, positions, strategies, optimizer, risk, equity] = await Promise.all([
        fetchJSON('/api/status'),
        fetchJSON('/api/trades'),
        fetchJSON('/api/positions'),
        fetchJSON('/api/strategies'),
        fetchJSON('/api/optimizer'),
        fetchJSON('/api/risk'),
        fetchJSON('/api/equity-history'),
    ]);

    if (status) updateStatus(status);
    if (trades) renderTrades(trades);
    if (positions) renderPositions(positions);
    if (strategies) {
        renderStrategies(strategies);
        updateStrategyChart(strategies.strategies);
    }
    if (optimizer) renderOptimizer(optimizer);
    if (risk) renderRisk(risk);
    if (equity) updateEquityChart(equity);
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws/live`;

    try {
        ws = new WebSocket(url);
    } catch (e) {
        console.warn('WebSocket creation failed:', e);
        scheduleReconnect();
        return;
    }

    ws.onopen = () => {
        console.log('WebSocket connected');
        reconnectAttempts = 0;
        document.getElementById('ws-dot').classList.add('connected');
    };

    ws.onmessage = (evt) => {
        try {
            const data = JSON.parse(evt.data);
            handleWSMessage(data);
        } catch (e) {
            console.warn('WS message parse error:', e);
        }
    };

    ws.onclose = () => {
        document.getElementById('ws-dot').classList.remove('connected');
        scheduleReconnect();
    };

    ws.onerror = () => {
        document.getElementById('ws-dot').classList.remove('connected');
    };
}

function scheduleReconnect() {
    if (reconnectAttempts >= MAX_RECONNECT) return;
    reconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(1.5, reconnectAttempts), 30000);
    console.log(`Reconnecting in ${(delay/1000).toFixed(1)}s (attempt ${reconnectAttempts})`);
    setTimeout(connectWS, delay);
}

function handleWSMessage(data) {
    const type = data.type;

    if (type === 'status' || type === 'heartbeat') {
        updateStatus(data);
    }

    if (type === 'trade_entry' || type === 'trade_exit') {
        // Refresh all data on trade events
        loadAll();
    }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    loadAll();
    connectWS();

    // Periodic full refresh every 30s as backup
    setInterval(loadAll, 30000);
});
