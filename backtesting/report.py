"""
backtesting/report.py
=====================
Human-readable report generation for backtest results.

``BacktestReport`` produces a plain-text summary or a self-contained HTML
page (with an inline SVG equity-curve chart — no external dependencies).
"""
from __future__ import annotations

from typing import Sequence


class BacktestReport:
    """Generate formatted text and HTML reports from backtest metrics."""

    # ── text ────────────────────────────────────────────────────

    @staticmethod
    def generate_text_report(metrics: dict, trades: Sequence[dict]) -> str:
        """Return a formatted multi-line ASCII report."""
        lines = [
            "=" * 60,
            "          BTC SCALPER — BACKTEST REPORT",
            "=" * 60,
            "",
            f"  Initial Capital  : ${metrics.get('initial_capital', 0):,.2f}",
            f"  Final Equity      : ${metrics.get('final_equity', 0):,.2f}",
            f"  Total Return      : {metrics.get('return_pct', 0):.2f}%",
            "",
            "-" * 60,
            "  TRADE STATISTICS",
            "-" * 60,
            f"  Total Trades      : {metrics.get('total_trades', 0)}",
            f"  Wins              : {metrics.get('wins', 0)}",
            f"  Losses            : {metrics.get('losses', 0)}",
            f"  Win Rate          : {metrics.get('win_rate', 0) * 100:.2f}%",
            "",
            "-" * 60,
            "  PROFITABILITY",
            "-" * 60,
            f"  Net PnL           : ${metrics.get('total_pnl', 0):,.2f}",
            f"  Total Fees        : ${metrics.get('total_fees', 0):,.2f}",
            f"  Avg Win           : ${metrics.get('avg_win', 0):,.2f}",
            f"  Avg Loss          : ${metrics.get('avg_loss', 0):,.2f}",
            f"  Profit Factor     : {metrics.get('profit_factor', 0):.2f}",
            "",
            "-" * 60,
            "  RISK METRICS",
            "-" * 60,
            f"  Max Drawdown      : {metrics.get('max_drawdown_pct', 0):.2f}%",
            f"  Sharpe Ratio      : {metrics.get('sharpe_ratio', 0):.4f}",
            "",
            "=" * 60,
        ]

        # Append a small trade table if there are trades
        if trades:
            lines.append("")
            lines.append("  RECENT TRADES (last 10)")
            lines.append("-" * 60)
            header = f"  {'#':>3}  {'Entry':>12}  {'Exit':>12}  {'PnL':>10}  {'Reason':>12}"
            lines.append(header)
            lines.append("-" * 60)
            for i, t in enumerate(trades[-10:], start=1):
                entry = t.get("entry_price", 0)
                exit_p = t.get("exit_price", 0)
                pnl = t.get("net_pnl_usdt", 0)
                reason = t.get("exit_reason", "")
                lines.append(
                    f"  {i:>3}  {entry:>12.2f}  {exit_p:>12.2f}  {pnl:>10.2f}  {reason:>12}"
                )
            lines.append("=" * 60)

        return "\n".join(lines)

    # ── HTML ────────────────────────────────────────────────────

    @staticmethod
    def generate_html_report(
        metrics: dict,
        trades: Sequence[dict],
        equity_curve: Sequence[tuple],
    ) -> str:
        """Return a self-contained HTML document with an inline SVG chart."""
        # Build SVG polyline for equity curve
        svg_points, svg_path = BacktestReport._build_svg(equity_curve)

        rows_html = ""
        for i, t in enumerate(trades, start=1):
            rows_html += (
                "<tr>"
                f"<td>{i}</td>"
                f"<td>{t.get('entry_price', 0):.2f}</td>"
                f"<td>{t.get('exit_price', 0):.2f}</td>"
                f"<td>{t.get('net_pnl_usdt', 0):.2f}</td>"
                f"<td>{t.get('exit_reason', '')}</td>"
                f"<td>{t.get('fees_total_usdt', 0):.4f}</td>"
                "</tr>\n"
            )

        pf = metrics.get("profit_factor", 0)
        pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BTC Scalper — Backtest Report</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 2rem; background: #1a1a2e; color: #e0e0e0; }}
  h1 {{ color: #00d4ff; border-bottom: 2px solid #333; padding-bottom: .5rem; }}
  h2 {{ color: #0abde3; margin-top: 2rem; }}
  .metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin: 1rem 0; }}
  .card {{ background: #16213e; border-radius: 8px; padding: 1rem; text-align: center; }}
  .card .label {{ font-size: .8rem; color: #8d99ae; text-transform: uppercase; }}
  .card .value {{ font-size: 1.5rem; font-weight: bold; margin-top: .3rem; }}
  .positive {{ color: #2ed573; }}
  .negative {{ color: #ff4757; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
  th, td {{ padding: .5rem .75rem; text-align: right; border-bottom: 1px solid #333; }}
  th {{ color: #00d4ff; text-align: center; }}
  td:first-child, th:first-child {{ text-align: center; }}
  .chart-container {{ background: #16213e; border-radius: 8px; padding: 1rem; margin: 1rem 0; }}
</style>
</head>
<body>
  <h1>BTC Scalper — Backtest Report</h1>

  <div class="metrics">
    <div class="card"><div class="label">Initial Capital</div><div class="value">${metrics.get('initial_capital', 0):,.2f}</div></div>
    <div class="card"><div class="label">Final Equity</div><div class="value">${metrics.get('final_equity', 0):,.2f}</div></div>
    <div class="card"><div class="label">Total Return</div><div class="value {'positive' if metrics.get('return_pct',0)>=0 else 'negative'}">{metrics.get('return_pct', 0):.2f}%</div></div>
    <div class="card"><div class="label">Total Trades</div><div class="value">{metrics.get('total_trades', 0)}</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value">{metrics.get('win_rate', 0) * 100:.1f}%</div></div>
    <div class="card"><div class="label">Profit Factor</div><div class="value">{pf_str}</div></div>
    <div class="card"><div class="label">Net PnL</div><div class="value {'positive' if metrics.get('total_pnl',0)>=0 else 'negative'}">${metrics.get('total_pnl', 0):,.2f}</div></div>
    <div class="card"><div class="label">Max Drawdown</div><div class="value negative">{metrics.get('max_drawdown_pct', 0):.2f}%</div></div>
    <div class="card"><div class="label">Sharpe Ratio</div><div class="value">{metrics.get('sharpe_ratio', 0):.4f}</div></div>
  </div>

  <h2>Equity Curve</h2>
  <div class="chart-container">
    {svg_path}
  </div>

  <h2>Trades ({len(trades)})</h2>
  <table>
    <thead>
      <tr><th>#</th><th>Entry</th><th>Exit</th><th>PnL (USDT)</th><th>Reason</th><th>Fees</th></tr>
    </thead>
    <tbody>
      {rows_html if rows_html else '<tr><td colspan="6" style="text-align:center">No trades</td></tr>'}
    </tbody>
  </table>
</body>
</html>"""

    # ── private ─────────────────────────────────────────────────

    @staticmethod
    def _build_svg(equity_curve: Sequence[tuple]) -> tuple[str, str]:
        """Build an inline SVG equity-curve chart.

        Returns ``(points_debug, svg_html)``.
        """
        if not equity_curve or len(equity_curve) < 2:
            return "", "<p style='text-align:center;color:#8d99ae'>No equity data</p>"

        equities = [e for _, e in equity_curve]
        n = len(equities)

        width, height = 800, 300
        padding = 40
        plot_w = width - 2 * padding
        plot_h = height - 2 * padding

        min_eq = min(equities)
        max_eq = max(equities)
        eq_range = max_eq - min_eq if max_eq != min_eq else 1

        def x(i: int) -> float:
            return padding + (i / (n - 1)) * plot_w if n > 1 else padding

        def y(eq: float) -> float:
            return padding + (1 - (eq - min_eq) / eq_range) * plot_h

        points = " ".join(f"{x(i):.1f},{y(eq):.1f}" for i, eq in enumerate(equities))

        # Determine colour: green if final >= initial, red otherwise
        colour = "#2ed573" if equities[-1] >= equities[0] else "#ff4757"

        svg = f"""<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" style="display:block;margin:auto">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#16213e" rx="8"/>
  <!-- grid lines -->
  <line x1="{padding}" y1="{padding}" x2="{padding}" y2="{height-padding}" stroke="#333" stroke-width="1"/>
  <line x1="{padding}" y1="{height-padding}" x2="{width-padding}" y2="{height-padding}" stroke="#333" stroke-width="1"/>
  <!-- equity curve -->
  <polyline points="{points}" fill="none" stroke="{colour}" stroke-width="2"/>
  <!-- labels -->
  <text x="5" y="{padding+5}" fill="#8d99ae" font-size="11">${max_eq:,.0f}</text>
  <text x="5" y="{height-padding}" fill="#8d99ae" font-size="11">${min_eq:,.0f}</text>
  <text x="{width-padding}" y="{height-padding+15}" fill="#8d99ae" font-size="11" text-anchor="end">{n} pts</text>
</svg>"""
        return points, svg