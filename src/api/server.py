"""CryptoForge Dashboard API — FastAPI backend for the trading dashboard."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Global references (set by create_api_app)
# ---------------------------------------------------------------------------
_components: dict[str, Any] = {}
_ws_clients: set[WebSocket] = set()
_start_time: float = time.time()


async def broadcast_update(data: dict[str, Any]) -> None:
    """Push a JSON message to all connected WebSocket clients."""
    if not _ws_clients:
        return
    payload = json.dumps(data, default=str)
    stale: list[WebSocket] = []
    for ws in _ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            stale.append(ws)
    for ws in stale:
        _ws_clients.discard(ws)


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def create_api_app(components: dict[str, Any]) -> FastAPI:
    """Create and configure the FastAPI dashboard app."""
    global _components, _start_time
    _components = components
    _start_time = time.time()

    app = FastAPI(title="CryptoForge Dashboard", version="1.0.0")

    # Serve static files (HTML/CSS/JS dashboard)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # -----------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        """Serve the main dashboard page."""
        index_path = static_dir / "index.html"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return "<h1>CryptoForge Dashboard</h1><p>Static files not found.</p>"

    @app.get("/api/status")
    async def get_status():
        """Bot status overview."""
        paper = _components.get("paper_tracker")
        regime_det = _components.get("regime_detector")
        strategy_mgr = _components.get("strategy_manager")
        cb = _components.get("circuit_breaker")
        optimizer = _components.get("optimizer")

        uptime_s = time.time() - _start_time
        hours = int(uptime_s // 3600)
        minutes = int((uptime_s % 3600) // 60)

        equity = paper.equity if paper else 0
        starting = paper._starting_equity if paper else 10000
        daily_pnl = paper.daily_pnl if paper else 0
        total_trades = paper.trade_count if paper else 0
        drawdown_pct = 0.0
        if paper and paper._starting_equity > 0:
            peak = max(starting, equity)
            drawdown_pct = max(0, (peak - equity) / peak * 100)

        # Win rate from recent trades
        win_rate = 0.0
        if paper and paper._trades:
            recent = paper._trades[-50:]
            wins = sum(1 for t in recent if t["result"] == "win")
            win_rate = wins / len(recent) * 100

        active_strats = []
        if strategy_mgr:
            active_strats = [s.strategy_id for s in strategy_mgr.get_active_strategies()]

        return {
            "equity": round(equity, 2),
            "starting_equity": round(starting, 2),
            "daily_pnl": round(daily_pnl, 4),
            "total_pnl": round(equity - starting, 4),
            "total_pnl_pct": round((equity - starting) / starting * 100, 2) if starting > 0 else 0,
            "drawdown_pct": round(drawdown_pct, 2),
            "total_trades": total_trades,
            "win_rate": round(win_rate, 1),
            "consecutive_losses": paper._consecutive_losses if paper else 0,
            "open_positions": len(paper.get_open_positions()) if paper else 0,
            "regime": str(regime_det.current_regime) if regime_det else "unknown",
            "active_strategies": active_strats,
            "uptime": f"{hours}h {minutes}m",
            "uptime_seconds": int(uptime_s),
            "circuit_breaker": cb.check().any_triggered if cb else False,
            "optimizer_cycles": optimizer._cycle_count if optimizer else 0,
            "mode": "paper",
        }

    @app.get("/api/positions")
    async def get_positions():
        """Open positions with details."""
        paper = _components.get("paper_tracker")
        if not paper:
            return {"positions": []}

        positions = []
        for pos in paper.get_open_positions():
            positions.append({
                "symbol": pos.get("symbol", ""),
                "strategy": pos.get("strategy", ""),
                "side": pos.get("side", ""),
                "entry_price": round(pos.get("entry_price", 0), 2),
                "amount": round(pos.get("amount", 0), 8),
                "stop_loss": round(pos.get("stop_loss", 0), 2) if pos.get("stop_loss") else None,
                "take_profit": round(pos.get("take_profit", 0), 2) if pos.get("take_profit") else None,
                "notional": round(pos.get("amount", 0) * pos.get("entry_price", 0), 2),
            })
        return {"positions": positions}

    @app.get("/api/trades")
    async def get_trades(limit: int = 100):
        """Trade history."""
        paper = _components.get("paper_tracker")
        if not paper:
            return {"trades": [], "total": 0}

        trades = paper._trades[-limit:][::-1]  # most recent first
        formatted = []
        for i, t in enumerate(trades):
            formatted.append({
                "id": len(paper._trades) - i,
                "symbol": t.get("symbol", ""),
                "strategy": t.get("strategy", ""),
                "side": t.get("side", ""),
                "entry_price": round(t.get("entry_price", 0), 2),
                "exit_price": round(t.get("exit_price", 0), 2),
                "amount": round(t.get("amount", 0), 8),
                "pnl": round(t.get("pnl", 0), 4),
                "pnl_pct": round(t.get("pnl_pct", 0), 2),
                "result": t.get("result", ""),
                "reason": t.get("reason", ""),
            })
        return {"trades": formatted, "total": len(paper._trades)}

    @app.get("/api/strategies")
    async def get_strategies():
        """Strategy information and performance."""
        strategy_mgr = _components.get("strategy_manager")
        optimizer = _components.get("optimizer")
        if not strategy_mgr:
            return {"strategies": []}

        strategies = []
        for sid, strat in strategy_mgr.strategies.items():
            active = strat in strategy_mgr.get_active_strategies()

            # Get capital multiplier from optimizer
            cap_mult = 1.0
            if optimizer and optimizer._cap_alloc:
                cap_mult = optimizer._cap_alloc.get_multiplier(sid)

            # Count trades per strategy
            paper = _components.get("paper_tracker")
            strat_trades = [t for t in (paper._trades if paper else []) if t.get("strategy") == sid]
            wins = sum(1 for t in strat_trades if t["result"] == "win")
            total = len(strat_trades)
            total_pnl = sum(t.get("pnl", 0) for t in strat_trades)
            wr = (wins / total * 100) if total > 0 else 0

            strategies.append({
                "id": sid,
                "active": active,
                "config": {k: v for k, v in strat.config.items() if not isinstance(v, (dict, list))},
                "trades": total,
                "wins": wins,
                "win_rate": round(wr, 1),
                "total_pnl": round(total_pnl, 4),
                "capital_multiplier": round(cap_mult, 2),
            })
        return {"strategies": strategies}

    @app.get("/api/optimizer")
    async def get_optimizer():
        """Optimizer status and history."""
        optimizer = _components.get("optimizer")
        if not optimizer:
            return {"status": "not_running"}

        journal = optimizer._journal
        cap_alloc = optimizer._cap_alloc

        # Get multipliers
        multipliers = {}
        strategy_mgr = _components.get("strategy_manager")
        if strategy_mgr and cap_alloc:
            for sid in strategy_mgr.strategies:
                multipliers[sid] = round(cap_alloc.get_multiplier(sid), 2)

        # Recent journal entries
        recent_entries = []
        entries = journal._entries[-20:] if hasattr(journal, "_entries") else []
        for e in reversed(entries):
            recent_entries.append({
                "symbol": e.symbol,
                "strategy": e.strategy_id,
                "side": e.side,
                "pnl": round(e.pnl, 4),
                "pnl_pct": round(e.pnl_pct, 2),
                "result": e.result,
                "hold_candles": e.hold_duration_candles,
                "entry_regime": e.entry_regime,
                "exit_regime": e.exit_regime,
            })

        return {
            "status": "active",
            "cycle_count": optimizer._cycle_count,
            "trade_counter": optimizer._trade_counter,
            "trigger_every": optimizer._trigger_n,
            "consecutive_losses": optimizer._consecutive_losses,
            "journal_size": journal.total_count,
            "capital_multipliers": multipliers,
            "recent_trades": recent_entries,
        }

    @app.get("/api/risk")
    async def get_risk():
        """Risk engine and circuit breaker status."""
        risk_engine = _components.get("risk_engine")
        cb = _components.get("circuit_breaker")
        portfolio = _components.get("portfolio")

        result: dict[str, Any] = {"status": "unknown"}
        if cb:
            state = cb.get_state()
            status = cb.check()
            result = {
                "circuit_breaker_active": status.any_triggered,
                "daily_loss_pct": round(state.get("daily_loss_pct", 0), 2),
                "max_daily_loss_pct": state.get("max_daily_loss_pct", 0),
                "drawdown_pct": round(state.get("drawdown_pct", 0), 2),
                "max_drawdown_pct": state.get("max_drawdown_pct", 0),
                "consecutive_losses": state.get("consecutive_losses", 0),
                "max_consecutive_losses": state.get("max_consecutive_losses", 0),
            }

        if risk_engine:
            result["equity"] = round(risk_engine._equity, 2)
            result["peak_equity"] = round(risk_engine._peak_equity, 2)

        if portfolio:
            equity = risk_engine._equity if risk_engine else 10000
            result["portfolio_exposure_pct"] = round(portfolio.exposure_pct(equity), 2)
            result["open_positions_count"] = portfolio.open_count

        return result

    @app.get("/api/equity-history")
    async def get_equity_history():
        """Equity curve data from trade history."""
        paper = _components.get("paper_tracker")
        if not paper:
            return {"data": []}

        starting = paper._starting_equity
        points = [{"trade": 0, "equity": starting, "pnl": 0}]
        running_equity = starting

        for i, t in enumerate(paper._trades):
            running_equity += t.get("pnl", 0)
            points.append({
                "trade": i + 1,
                "equity": round(running_equity, 2),
                "pnl": round(t.get("pnl", 0), 4),
                "strategy": t.get("strategy", ""),
                "result": t.get("result", ""),
            })

        return {"data": points, "starting_equity": starting}

    # -----------------------------------------------------------------------
    # WebSocket
    # -----------------------------------------------------------------------

    @app.websocket("/ws/live")
    async def websocket_live(ws: WebSocket):
        await ws.accept()
        _ws_clients.add(ws)
        log.info("ws.client_connected", total=len(_ws_clients))
        try:
            # Send initial state on connect
            status = await get_status()
            await ws.send_text(json.dumps({"type": "status", **status}, default=str))

            # Keep alive — ping every 30s
            while True:
                try:
                    await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Send heartbeat
                    try:
                        status = await get_status()
                        await ws.send_text(json.dumps({"type": "heartbeat", **status}, default=str))
                    except Exception:
                        break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            _ws_clients.discard(ws)
            log.info("ws.client_disconnected", total=len(_ws_clients))

    return app


# ---------------------------------------------------------------------------
# Server runner (runs in background asyncio task)
# ---------------------------------------------------------------------------

async def start_api_server(app: FastAPI, port: int = 8050) -> None:
    """Run uvicorn inside the existing asyncio event loop."""
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()
