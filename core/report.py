"""Turns a paper-trading run into a shareable report: equity chart, report.html, and a report-card PNG."""
import base64
import html
import io
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

TEMPLATE_DIR = Path(__file__).parent / "templates"
BRAND_DIR = Path(__file__).parent / "brand"

# Chart colors per theme: strategy line, benchmark line, gridlines, muted text.
_THEME_COLORS = {
    "light": {"strategy": "#3f9b1a", "benchmark": "#9aa79e", "grid": "#dfe6da", "muted": "#6b7a70"},
    "dark": {"strategy": "#8bc53f", "benchmark": "#5f6b64", "grid": "#223028", "muted": "#b7c2ba"},
}


def logo_data_uri(variant: str = "on-light") -> str:
    """Base64 data: URI for a CoinGecko API lockup (on-light | on-dark | mono), so templates need no relative paths."""
    svg = (BRAND_DIR / f"coingecko-api-{variant}.svg").read_text()
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def format_usd(value: float | None, *, signed: bool = False) -> str:
    """123.4 -> '$123.40'; -50 -> '-$50.00'; signed=True adds a '+' for positive values."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ("+" if signed else "")
    return f"{sign}${abs(value):,.2f}"


def format_pct(value: float | None, *, decimals: int = 1, signed: bool = False) -> str:
    """Formats a number that's already in percent units: 18.75 -> '18.75%' (or '+18.75%' if signed and positive)."""
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def format_ratio_as_pct(ratio: float | None, *, decimals: int = 0) -> str:
    """Formats a 0-1 ratio as a percent: 0.64 -> '64%'."""
    if ratio is None:
        return "—"
    return f"{ratio * 100:.{decimals}f}%"


def prettify_run_name(run_name: str) -> str:
    """'smart-money-radar-demo' -> 'Smart Money Radar Demo', used when no human title is supplied."""
    return run_name.replace("-", " ").replace("_", " ").strip().title()


def _pnl_class(pnl_usd: float | None, pnl_pct: float | None) -> str:
    """'positive' | 'negative' | '' for coloring the P&L stat, preferring the $ figure."""
    value = pnl_usd if pnl_usd is not None else pnl_pct
    if not value:
        return ""
    return "positive" if value > 0 else "negative"


def equity_chart_png(equity_curve: list[tuple[float, float]], benchmark: list[tuple[float, float]] | None = None) -> bytes:
    """Renders the equity curve, plus an optional benchmark line, to an in-memory PNG."""
    fig, ax = plt.subplots(figsize=(8, 4))
    if equity_curve:
        xs = [datetime.fromtimestamp(ts, tz=timezone.utc) for ts, _ in equity_curve]
        ax.plot(xs, [e for _, e in equity_curve], label="strategy", color="#8bc53f", linewidth=2)
    if benchmark:
        xs = [datetime.fromtimestamp(ts, tz=timezone.utc) for ts, _ in benchmark]
        ax.plot(xs, [e for _, e in benchmark], label="benchmark", color="#999999", linestyle="--")
    ax.set_ylabel("Equity (USD)")
    if equity_curve or benchmark:
        ax.legend()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=144)
    plt.close(fig)
    return buf.getvalue()


def _scale_points(series, x_min, x_max, y_min, y_max, width, height, pad=6, top_pad=6):
    """Maps (ts, value) points into SVG pixel coordinates within width x height."""
    x_span = (x_max - x_min) or 1
    y_span = (y_max - y_min) or 1
    pts = []
    for t, v in series:
        x = pad + (t - x_min) / x_span * (width - 2 * pad)
        y = height - pad - (v - y_min) / y_span * (height - pad - top_pad)
        pts.append((x, y))
    return pts


def _smooth_path(points: list[tuple[float, float]]) -> str:
    """SVG path 'd' string that smooths a polyline using quadratic-bezier midpoints."""
    if len(points) < 3:
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)
    d = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for i in range(1, len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        d.append(f"Q{x0:.1f},{y0:.1f} {mx:.1f},{my:.1f}")
    d.append(f"L{points[-1][0]:.1f},{points[-1][1]:.1f}")
    return " ".join(d)


def equity_svg(
    equity_curve: list[tuple[float, float]],
    benchmark: list[tuple[float, float]] | None = None,
    *,
    width: int = 1000,
    height: int = 200,
    theme: str = "light",
    show_labels: bool = False,
) -> str:
    """Inline SVG hero chart for the report card: a filled strategy line, plus an optional lighter dashed benchmark line.

    `show_labels` adds axis context (min/max equity, start/end time) for a full-page report, where the
    chart needs to read as evidence, not just a decorative sparkline (the report card keeps them off).
    """
    colors = _THEME_COLORS.get(theme, _THEME_COLORS["light"])
    pad_left = 64 if show_labels else 6
    pad_bottom = 24 if show_labels else 6
    if not equity_curve:
        return (
            f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{width / 2}" y="{height / 2}" fill="{colors["muted"]}" font-size="15" '
            f'text-anchor="middle" dominant-baseline="middle">No equity data yet</text></svg>'
        )

    combined = equity_curve + (benchmark or [])
    xs = [t for t, _ in combined]
    ys = [v for _, v in combined]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if y_min == y_max:
        y_min, y_max = y_min - 1, y_max + 1
    plot_w, plot_h = width - pad_left - 6, height - pad_bottom - 6

    def scale(series):
        return _scale_points(
            [(t, v) for t, v in series],
            x_min, x_max, y_min, y_max, plot_w, plot_h - 6, pad=0, top_pad=6,
        )

    def shift(pts):
        return [(x + pad_left, y + 6) for x, y in pts]

    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" font-family="Inter,system-ui,sans-serif">'
        f'<defs><linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{colors["strategy"]}" stop-opacity="0.32"/>'
        f'<stop offset="100%" stop-color="{colors["strategy"]}" stop-opacity="0"/></linearGradient></defs>'
    ]
    grid_fracs = (0.0, 0.25, 0.5, 0.75, 1.0) if show_labels else (0.25, 0.5, 0.75)
    for f in grid_fracs:
        y = 6 + plot_h * f
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - 6}" y2="{y:.1f}" stroke="{colors["grid"]}" stroke-width="1" stroke-dasharray="4 4"/>')
        if show_labels:
            value = y_max - (y_max - y_min) * f
            parts.append(f'<text x="{pad_left - 10}" y="{y:.1f}" fill="{colors["muted"]}" font-size="11" text-anchor="end" dominant-baseline="middle">{format_usd(value)}</text>')

    if benchmark:
        b_pts = shift(scale(benchmark))
        parts.append(
            f'<path d="{_smooth_path(b_pts)}" fill="none" stroke="{colors["benchmark"]}" '
            f'stroke-width="2" stroke-dasharray="7 5" stroke-linecap="round"/>'
        )

    s_pts = shift(scale(equity_curve))
    line_d = _smooth_path(s_pts)
    area_d = f"{line_d} L{s_pts[-1][0]:.1f},{6 + plot_h:.1f} L{s_pts[0][0]:.1f},{6 + plot_h:.1f} Z"
    parts.append(f'<path d="{area_d}" fill="url(#eqFill)" stroke="none"/>')
    parts.append(
        f'<path d="{line_d}" fill="none" stroke="{colors["strategy"]}" '
        f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
    )
    lx, ly = s_pts[-1]
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="8" fill="{colors["strategy"]}" opacity="0.18"/>')
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="{colors["strategy"]}"/>')

    if show_labels:
        start_label = datetime.fromtimestamp(x_min, tz=timezone.utc).strftime("%b %d, %H:%M")
        end_label = datetime.fromtimestamp(x_max, tz=timezone.utc).strftime("%b %d, %H:%M UTC")
        parts.append(f'<text x="{pad_left}" y="{height - 4}" fill="{colors["muted"]}" font-size="11">{start_label}</text>')
        parts.append(f'<text x="{width - 6}" y="{height - 4}" fill="{colors["muted"]}" font-size="11" text-anchor="end">{end_label}</text>')

    parts.append("</svg>")
    return "".join(parts)


def equity_sparkline_svg(equity_curve: list[tuple[float, float]] | None, *, width: int = 380, height: int = 340, theme: str = "dark") -> str:
    """Stylized, decorative equity sparkline for the cover's right-side visual. Falls back to an abstract motif with no data."""
    if not equity_curve or len(equity_curve) < 2:
        return abstract_chart_motif_svg(width=width, height=height, theme=theme)

    colors = _THEME_COLORS.get(theme, _THEME_COLORS["dark"])
    xs = [t for t, _ in equity_curve]
    ys = [v for _, v in equity_curve]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if y_min == y_max:
        y_min, y_max = y_min - 1, y_max + 1

    pts = _scale_points(equity_curve, x_min, x_max, y_min, y_max, width, height, pad=10, top_pad=30)
    line_d = _smooth_path(pts)
    area_d = f"{line_d} L{pts[-1][0]:.1f},{height} L{pts[0][0]:.1f},{height} Z"
    accent = colors["strategy"]
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<defs><linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{accent}" stop-opacity="0.45"/>'
        f'<stop offset="100%" stop-color="{accent}" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area_d}" fill="url(#sparkFill)" stroke="none"/>'
        f'<path d="{line_d}" fill="none" stroke="{accent}" stroke-width="4" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="6" fill="{accent}"/>'
        "</svg>"
    )


def abstract_chart_motif_svg(*, width: int = 380, height: int = 340, theme: str = "dark") -> str:
    """A tasteful decorative ascending-bars motif for a cover with no run data to plot."""
    colors = _THEME_COLORS.get(theme, _THEME_COLORS["dark"])
    accent = colors["strategy"]
    bars = [0.32, 0.5, 0.42, 0.66, 0.58, 0.8, 1.0]
    n = len(bars)
    gap = 16
    bar_w = (width - gap * (n - 1)) / n
    rects = []
    for i, h_frac in enumerate(bars):
        bar_h = height * 0.68 * h_frac
        x = i * (bar_w + gap)
        y = height - bar_h
        opacity = 0.22 + 0.68 * (i / (n - 1))
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'rx="{bar_w / 2:.1f}" fill="{accent}" opacity="{opacity:.2f}"/>'
        )
    return f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">' + "".join(rects) + "</svg>"


def _fill(html: str, context: dict) -> str:
    """Replaces every {{key}} in `html` with str(context[key])."""
    for k, v in context.items():
        html = html.replace("{{" + k + "}}", str(v))
    return html


def _card_context(
    display_title: str,
    subtitle: str | None,
    metrics: dict,
    credits_used: int,
    equity_curve: list | None,
    benchmark: list | None,
    theme: str,
) -> dict:
    pnl_usd = metrics.get("pnl_usd")
    pnl_pct = metrics.get("pnl_pct")
    return {
        "theme": theme,
        "title": display_title,
        "subtitle_html": f'<div class="subtitle">{subtitle}</div>' if subtitle else "",
        "pnl_usd_display": format_usd(pnl_usd, signed=True),
        "pnl_pct_display": format_pct(pnl_pct, signed=True),
        "pnl_class": _pnl_class(pnl_usd, pnl_pct),
        "win_rate_display": format_ratio_as_pct(metrics.get("win_rate")),
        "trades": metrics.get("trades", "—"),
        "max_drawdown_display": format_pct(metrics.get("max_drawdown_pct")),
        "credits_used": credits_used,
        "logo": logo_data_uri("on-dark" if theme == "dark" else "on-light"),
        "equity_svg": equity_svg(equity_curve or [], benchmark, theme=theme),
        "benchmark_legend_html": (
            '<span><span class="swatch benchmark"></span><span class="text">Benchmark</span></span>' if benchmark else ""
        ),
    }


def _matplotlib_card(
    display_title: str,
    subtitle: str | None,
    metrics: dict,
    credits_used: int,
    out_path: Path,
    equity_curve: list | None = None,
    benchmark: list | None = None,
    theme: str = "light",
):
    """Fallback report card when Playwright isn't installed: a stat block plus a plain equity plot, drawn with matplotlib."""
    is_dark = theme == "dark"
    bg = "#0b0f0d" if is_dark else "#f7f9f6"
    fg = "#f4f7f4" if is_dark else "#12271a"
    muted = "#b7c2ba" if is_dark else "#6b7a70"
    accent = "#8bc53f" if is_dark else "#3f9b1a"

    fig = plt.figure(figsize=(12, 6.3), facecolor=bg)

    ax_text = fig.add_axes((0.06, 0.42, 0.88, 0.52))
    ax_text.axis("off")
    ax_text.text(0, 0.92, display_title, fontsize=24, weight="bold", color=fg)
    if subtitle:
        ax_text.text(0, 0.74, subtitle, fontsize=14, color=muted)
    lines = [
        f"P&L: {format_usd(metrics.get('pnl_usd'), signed=True)} ({format_pct(metrics.get('pnl_pct'), signed=True)})",
        f"Win rate: {format_ratio_as_pct(metrics.get('win_rate'))}",
        f"Trades: {metrics.get('trades', '—')}",
        f"Max drawdown: {format_pct(metrics.get('max_drawdown_pct'))}",
        f"Credits used: {credits_used}",
    ]
    for i, line in enumerate(lines):
        ax_text.text(0, 0.52 - i * 0.13, line, fontsize=14, color=fg)

    ax_chart = fig.add_axes((0.06, 0.06, 0.88, 0.30))
    ax_chart.set_facecolor(bg)
    if equity_curve:
        xs = [t for t, _ in equity_curve]
        ax_chart.plot(xs, [v for _, v in equity_curve], color=accent, linewidth=2)
    if benchmark:
        xs = [t for t, _ in benchmark]
        ax_chart.plot(xs, [v for _, v in benchmark], color=muted, linewidth=1.5, linestyle="--")
    ax_chart.set_xticks([])
    for spine in ax_chart.spines.values():
        spine.set_color(muted)
    ax_chart.tick_params(colors=muted)

    fig.savefig(out_path, dpi=100, facecolor=bg)
    plt.close(fig)


def render_report_card(
    run_name: str,
    metrics: dict,
    credits_used: int,
    out_path: str | Path,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    equity_curve: list | None = None,
    benchmark: list | None = None,
    theme: str = "light",
):
    """Renders templates/report-card.html with Playwright if it's installed, else falls back to matplotlib."""
    out_path = Path(out_path)
    display_title = title or prettify_run_name(run_name)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _matplotlib_card(display_title, subtitle, metrics, credits_used, out_path, equity_curve, benchmark, theme)
        return
    html = _fill(
        (TEMPLATE_DIR / "report-card.html").read_text(),
        _card_context(display_title, subtitle, metrics, credits_used, equity_curve, benchmark, theme),
    )
    tmp = out_path.with_suffix(".tmp.html")
    tmp.write_text(html)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1200, "height": 630})
            page.goto(tmp.resolve().as_uri())
            page.screenshot(path=str(out_path))
            browser.close()
    except Exception:
        _matplotlib_card(display_title, subtitle, metrics, credits_used, out_path, equity_curve, benchmark, theme)
    finally:
        tmp.unlink(missing_ok=True)


def _html(run_name: str, metrics: dict, credits_used: int, equity_curve: list | None, benchmark: list | None = None, details: dict | None = None) -> str:
    chart_svg = equity_svg(equity_curve or [], benchmark, width=1100, height=340, theme="dark", show_labels=True)
    details = details or {}
    esc = html.escape
    metric_items = [
        ("P&L", format_usd(metrics.get("pnl_usd"), signed=True), _pnl_class(metrics.get("pnl_usd"), metrics.get("pnl_pct"))),
        ("Return", format_pct(metrics.get("pnl_pct"), signed=True), _pnl_class(metrics.get("pnl_usd"), metrics.get("pnl_pct"))),
        ("Win rate", format_ratio_as_pct(metrics.get("win_rate")), ""),
        ("Trades", metrics.get("trades", "—"), ""),
        ("Max drawdown", format_pct(metrics.get("max_drawdown_pct")), ""),
        ("REST credits", credits_used, ""),
    ]
    metric_html = "".join(f'<div class="metric"><span>{esc(str(k))}</span><b class="{c}">{esc(str(v))}</b></div>' for k, v, c in metric_items)
    token_rows = details.get("token_rows") or []
    def token_identity(row: dict) -> str:
        image = row.get("image")
        visual = f'<img src="{esc(image)}" alt="" class="token-image">' if image else '<span class="token-image fallback">◈</span>'
        label = esc(str(row.get("symbol") or row.get("token") or "—"))
        chain = esc(str(row.get("chain") or ""))
        return f'<td class="token-cell">{visual}<span><strong>{label}</strong><small>{chain}</small></span></td>'
    token_html = "".join(
        f'<tr>{token_identity(r)}'
        f'<td>{r.get("buys", 0)}</td><td>{r.get("sells", 0)}</td><td>{esc(format_usd(r.get("paper_usd"), signed=True))}</td>'
        f'<td class="{("positive" if (r.get("pnl_usd") or 0) > 0 else "negative" if (r.get("pnl_usd") or 0) < 0 else "")}">{esc(format_usd(r.get("pnl_usd"), signed=True))}</td></tr>'
        for r in token_rows
    ) or '<tr><td colspan="5" class="muted">No token-level decisions were recorded for this run.</td></tr>'
    decision_rows = details.get("decision_rows") or []
    decision_html = "".join(
        f'<tr><td>{esc(str(r.get("time") or "—"))}</td><td class="{("positive" if r.get("action") == "buy" else "negative" if r.get("action") == "sell" else "")}">{esc(str(r.get("action") or "signal"))}</td>'
        f'<td>{esc(str(r.get("symbol") or r.get("token") or "—"))}</td><td>{esc(str(r.get("wallet") or "—"))}</td><td>{esc(str(r.get("reason") or ""))}</td></tr>'
        for r in decision_rows[:30]
    ) or '<tr><td colspan="5" class="muted">No decision feed was recorded.</td></tr>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{run_name} report</title>
<style>
* {{ box-sizing:border-box; }} body {{ margin:0; background:#0b0f0d; color:#f2f6f2; font-family:Inter,system-ui,sans-serif; }}
.wrap {{ max-width:1120px; margin:0 auto; padding:42px 28px 70px; }}
.eyebrow {{ color:#8bc53f; font-size:11px; font-weight:800; letter-spacing:.16em; text-transform:uppercase; }}
h1 {{ font-size:42px; margin:8px 0; letter-spacing:-.04em; }} .sub {{ color:#aebbb1; margin:0 0 26px; }}
.metrics {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin:22px 0; }}
.metric,.panel {{ background:#151c18; border:1px solid #29362e; border-radius:14px; }} .metric {{ padding:15px; }} .metric span,small,.muted {{ display:block; color:#8e9c92; font-size:11px; }} .metric b {{ display:block; margin-top:7px; font-size:20px; }}
.positive {{ color:#55dc70!important; }} .negative {{ color:#ff756d!important; }}
.panel {{ padding:20px; margin-top:16px; }} .panel h2 {{ margin:0 0 14px; font-size:18px; }}
.chart-card {{ background:radial-gradient(circle at 15% 0%,rgba(139,197,63,.08),transparent 45%),#0f1511; border-radius:14px; padding:18px 14px 10px; }} .chart-card svg {{ width:100%; height:auto; display:block; }}
.chart-legend {{ display:flex; gap:16px; margin-bottom:8px; font-size:12px; color:#aebbb1; }} .chart-legend span {{ display:inline-flex; align-items:center; gap:6px; }} .swatch {{ width:10px; height:10px; border-radius:50%; display:inline-block; }} .swatch.strategy {{ background:#8bc53f; }} .swatch.benchmark {{ background:#5f6b64; border:1px dashed #aebbb1; }}
table {{ border-collapse:collapse; width:100%; }} th {{ color:#8e9c92; font-size:10px; text-transform:uppercase; letter-spacing:.08em; text-align:left; }} th,td {{ padding:11px 9px; border-bottom:1px solid #29362e; font-size:13px; }} td small {{ margin-top:3px; }}
.footer {{ color:#718076; font-size:12px; margin-top:24px; }} .token-cell{{display:flex;align-items:center;gap:8px}} .token-image{{width:26px;height:26px;border-radius:50%;object-fit:cover;background:#26352a;display:grid;place-items:center;color:#8bc53f;font-weight:800}} .token-image.fallback{{font-size:11px}}
@media(max-width:800px) {{ .metrics {{ grid-template-columns:repeat(2,1fr); }} h1 {{ font-size:31px; }} .wrap {{ padding:25px 16px 60px; }} }}
</style></head>
<body>
<main class="wrap"><div class="eyebrow">CoinGecko API · Smart Money Radar</div><h1>{esc(prettify_run_name(run_name))}</h1>
<p class="sub">Paper-trading evidence with wallet signals, token context, and an auditable decision feed.</p>
<section class="metrics">{metric_html}</section>
<section class="panel"><h2>Equity curve</h2><div class="chart-legend"><span><span class="swatch strategy"></span>Strategy</span>{'<span><span class="swatch benchmark"></span>Benchmark</span>' if benchmark else ''}</div><div class="chart-card">{chart_svg}</div></section>
<section class="panel"><h2>Token breakdown</h2><table><thead><tr><th>Token</th><th>Buys</th><th>Sells</th><th>Paper notional</th><th>P&amp;L</th></tr></thead><tbody>{token_html}</tbody></table></section>
<section class="panel"><h2>Decision feed</h2><table><thead><tr><th>Time</th><th>Action</th><th>Token</th><th>Wallet</th><th>Context</th></tr></thead><tbody>{decision_html}</tbody></table></section>
<p class="footer">Paper trades are simulated. CoinGecko API data is used for observations; this report does not represent financial advice or executed orders.</p></main>
</body></html>"""


def build(
    run_name: str,
    metrics: dict,
    equity_curve: list,
    benchmark: list | None = None,
    credits_used: int = 0,
    out_dir: str | Path = "reports",
    *,
    title: str | None = None,
    subtitle: str | None = None,
    theme: str = "light",
    details: dict | None = None,
) -> dict:
    """Writes {out_dir}/{run_name}/report.html, equity.png, and report-card.png. Returns their paths."""
    out = Path(out_dir) / run_name
    out.mkdir(parents=True, exist_ok=True)
    chart_bytes = equity_chart_png(equity_curve, benchmark)
    (out / "equity.png").write_bytes(chart_bytes)
    card_path = out / "report-card.png"
    render_report_card(
        run_name,
        metrics,
        credits_used,
        card_path,
        title=title,
        subtitle=subtitle,
        equity_curve=equity_curve,
        benchmark=benchmark,
        theme=theme,
    )
    html_path = out / "report.html"
    html_path.write_text(_html(run_name, metrics, credits_used, equity_curve, benchmark, details))
    return {"html": str(html_path), "equity_png": str(out / "equity.png"), "card_png": str(card_path)}
