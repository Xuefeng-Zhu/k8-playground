"""
Report renderers. Two outputs:
  - render_terminal: concise CLI summary
  - render_html: self-contained HTML report
"""
import datetime
import html
import io
from typing import Iterable

from .runner import AuditResult, CheckResult, Severity, Category

# Same dark palette as the K8s reference doc so reports feel like part of the set.
PALETTE = {
    "bg": "#0d1117", "bg_alt": "#161b22", "bg_dark": "#010409",
    "border": "#30363d", "border_dim": "#21262d",
    "text": "#c9d1d9", "text_dim": "#8b949e", "text_faint": "#6e7681",
    "head": "#f0f6fc",
    "pass": "#3fb950", "warn": "#d29922", "fail": "#f85149",
    "blue": "#58a6ff", "purple": "#d2a8ff",
}


# ──────────────────────────────────────────────────────────────────────
# Terminal output
# ──────────────────────────────────────────────────────────────────────

def render_terminal(result: AuditResult, color: bool = True) -> str:
    out = io.StringIO()
    counts = result.counts
    score = result.score
    bar_w = 30
    filled = round(bar_w * score / 100)
    bar = "█" * filled + "░" * (bar_w - filled)

    score_color_hex = (
        PALETTE["pass"] if score >= 80
        else PALETTE["warn"] if score >= 50
        else PALETTE["fail"]
    )
    c_score = _ansi(score, score_color_hex) if color else ""
    c_reset = "\033[0m" if color else ""
    c_dim = "\033[90m" if color else ""
    c_bold = "\033[1m" if color else ""

    out.write(f"\n{c_bold}kube-prod-audit{c_reset}  {c_dim}·{c_reset}  cluster: {result.cluster}  {c_dim}·{c_reset}  mode: {result.mode}\n")
    out.write(f"{c_dim}{datetime.datetime.utcnow().isoformat(timespec='seconds')}Z{c_reset}\n\n")

    c_score_text = _ansi(f"{score}/100", score_color_hex) if color else f"{score}/100"
    c_bar = _ansi(bar, score_color_hex) if color else bar

    out.write(f"Score  {c_score_text}  [{c_bar}]\n")
    out.write(f"  {c_bold}PASS{c_reset} {counts['pass']:>2}   "
              f"{c_bold}WARN{c_reset} {counts['warn']:>2}   "
              f"{c_bold}FAIL{c_reset} {counts['fail']:>2}\n\n")

    # Per-category summary
    out.write(f"{c_bold}By category{c_reset}\n")
    for cat, counts_c in result.by_category.items():
        p, w, f = counts_c.get("pass", 0), counts_c.get("warn", 0), counts_c.get("fail", 0)
        mark = "✗" if f else "!" if w else "✓"
        col = PALETTE["fail"] if f else PALETTE["warn"] if w else PALETTE["pass"]
        if color:
            out.write(f"  {_ansi(mark, col)} {cat:<16} pass {p:>2}   warn {w:>2}   fail {f:>2}\n")
        else:
            out.write(f"  {mark} {cat:<16} pass {p:>2}   warn {w:>2}   fail {f:>2}\n")
    out.write("\n")

    # Findings: fails first, then warns
    findings = (
        result.by_severity(Severity.FAIL)
        + result.by_severity(Severity.WARN)
    )
    if not findings:
        out.write(f"{c_bold}All checks passed.{c_reset}\n\n")
    else:
        out.write(f"{c_bold}Findings{c_reset}\n")
        for c in findings:
            mark = "✗" if c.severity == Severity.FAIL else "!"
            col = PALETTE["fail"] if c.severity == Severity.FAIL else PALETTE["warn"]
            tag = "FAIL" if c.severity == Severity.FAIL else "WARN"
            if color:
                out.write(f"  {_ansi(mark, col)} {_ansi(tag, col)} {c_bold}{c.id}{c_reset}  {c.title}\n")
            else:
                out.write(f"  {mark} {tag} {c.id}  {c.title}\n")
            out.write(f"      {c.summary}\n")
            for a in c.affected[:5]:
                out.write(f"        {c_dim}·{c_reset} {a}\n")
            if len(c.affected) > 5:
                out.write(f"        {c_dim}… and {len(c.affected) - 5} more{c_reset}\n")
            if c.fix:
                out.write(f"      {c_dim}fix:{c_reset} {c.fix}\n")
        out.write("\n")

    return out.getvalue()


def _ansi(text, color_hex: str) -> str:
    r, g, b = int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16)
    return f"\033[38;2;{r};{g};{b}m{text}\033[0m"


# ──────────────────────────────────────────────────────────────────────
# HTML output
# ──────────────────────────────────────────────────────────────────────

def render_html(result: AuditResult, reference_root: str = "..") -> str:
    counts = result.counts
    score = result.score
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    p = PALETTE

    # Per-category stats for the breakdown grid
    by_cat = result.by_category

    # Pre-render check rows grouped by category
    sections_html = []
    for cat in Category:
        items = [c for c in result.checks if c.category == cat]
        if not items:
            continue
        rows = "\n".join(_render_check_row(c, reference_root) for c in items)
        n_fail = sum(1 for c in items if c.severity == Severity.FAIL)
        n_warn = sum(1 for c in items if c.severity == Severity.WARN)
        n_pass = sum(1 for c in items if c.severity == Severity.PASS)
        cat_color = p["fail"] if n_fail else p["warn"] if n_warn else p["pass"]
        sections_html.append(f"""
        <section class="cat">
          <h2 id="{cat.value.lower()}">
            <span class="cat-name">{html.escape(cat.value)}</span>
            <span class="cat-pills">
              <span class="pill pass">{n_pass} pass</span>
              <span class="pill warn">{n_warn} warn</span>
              <span class="pill fail">{n_fail} fail</span>
            </span>
          </h2>
          <table>
            <thead>
              <tr><th>ID</th><th>Check</th><th>Status</th><th>Findings</th></tr>
            </thead>
            <tbody>
              {rows}
            </tbody>
          </table>
        </section>""")

    # Per-category score grid
    cat_cards = []
    for cat in Category:
        c = by_cat.get(cat.value, {})
        n_fail = c.get("fail", 0); n_warn = c.get("warn", 0); n_pass = c.get("pass", 0)
        total = n_pass + n_warn + n_fail
        pct = 0 if not total else round(100 * n_pass / total)
        cat_color = p["fail"] if n_fail else p["warn"] if n_warn else p["pass"]
        cat_cards.append(f"""
        <a class="cat-card" href="#{cat.value.lower()}">
          <div class="cat-card-name">{html.escape(cat.value)}</div>
          <div class="cat-card-score" style="color: {cat_color}">{pct}%</div>
          <div class="cat-card-counts">
            <span class="pass">{n_pass}</span> ·
            <span class="warn">{n_warn}</span> ·
            <span class="fail">{n_fail}</span>
          </div>
        </a>""")

    # Score arc
    score_color = p["pass"] if score >= 80 else p["warn"] if score >= 50 else p["fail"]
    score_label = (
        "Production-ready" if score >= 80 else
        "Operational with gaps" if score >= 50 else
        "Needs work"
    )

    findings_count = counts["fail"] + counts["warn"]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>kube-prod-audit · {html.escape(result.cluster)}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", Roboto, sans-serif;
    background: {p['bg']}; color: {p['text']};
    line-height: 1.6; font-size: 15px;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 48px 32px 96px; }}
  @media (max-width: 700px) {{ .container {{ padding: 24px 16px 64px; }} }}

  h1 {{ color: {p['head']}; font-size: 32px; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 4px; }}
  h2 {{ color: {p['head']}; font-size: 22px; font-weight: 700; margin: 48px 0 16px; padding-bottom: 8px; border-bottom: 1px solid {p['border']}; display: flex; align-items: center; justify-content: space-between; }}
  h3 {{ color: {p['head']}; font-size: 17px; font-weight: 600; margin: 16px 0 8px; }}
  p {{ margin: 8px 0; }}
  a {{ color: {p['blue']}; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ font-family: ui-monospace, "SF Mono", monospace; background: {p['bg_alt']}; color: #ff7b72; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; border: 1px solid {p['border_dim']}; }}
  hr {{ border: none; border-top: 1px solid {p['border_dim']}; margin: 32px 0; }}

  /* Header / score panel */
  .meta {{ color: {p['text_dim']}; font-size: 13px; margin-bottom: 24px; display: flex; gap: 12px; flex-wrap: wrap; }}
  .meta span {{ background: {p['bg_alt']}; padding: 3px 10px; border-radius: 999px; border: 1px solid {p['border']}; }}

  .score-panel {{
    background: linear-gradient(135deg, {p['bg_alt']}, {p['bg']});
    border: 1px solid {p['border']};
    border-radius: 12px;
    padding: 32px;
    margin: 24px 0;
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 32px;
    align-items: center;
  }}
  @media (max-width: 700px) {{ .score-panel {{ grid-template-columns: 1fr; text-align: center; }} }}

  .score-num {{ font-size: 72px; font-weight: 800; line-height: 1; color: {score_color}; }}
  .score-of {{ color: {p['text_dim']}; font-size: 18px; }}
  .score-label {{ font-size: 22px; font-weight: 700; color: {p['head']}; }}
  .score-sub {{ color: {p['text_dim']}; font-size: 14px; margin-top: 4px; }}

  .score-stats {{ display: flex; gap: 24px; margin-top: 12px; flex-wrap: wrap; }}
  .score-stat {{ display: flex; align-items: baseline; gap: 6px; }}
  .score-stat .num {{ font-size: 28px; font-weight: 700; }}
  .score-stat .lbl {{ color: {p['text_dim']}; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .score-stat.pass .num {{ color: {p['pass']}; }}
  .score-stat.warn .num {{ color: {p['warn']}; }}
  .score-stat.fail .num {{ color: {p['fail']}; }}

  /* Category grid */
  .cat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 24px 0; }}
  .cat-card {{
    background: {p['bg_alt']}; border: 1px solid {p['border']};
    border-radius: 8px; padding: 16px;
    transition: transform 0.15s, border-color 0.15s;
  }}
  .cat-card:hover {{ transform: translateY(-2px); border-color: {p['blue']}; text-decoration: none; }}
  .cat-card-name {{ color: {p['text_dim']}; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .cat-card-score {{ font-size: 28px; font-weight: 800; margin: 4px 0; }}
  .cat-card-counts {{ font-size: 12px; color: {p['text_dim']}; }}
  .cat-card-counts .pass {{ color: {p['pass']}; }}
  .cat-card-counts .warn {{ color: {p['warn']}; }}
  .cat-card-counts .fail {{ color: {p['fail']}; }}

  /* Section headings */
  .cat-name {{ font-size: 22px; }}
  .cat-pills {{ display: flex; gap: 6px; }}
  .pill {{ font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; background: {p['bg']}; border: 1px solid {p['border']}; }}
  .pill.pass {{ color: {p['pass']}; border-color: {p['pass']}55; }}
  .pill.warn {{ color: {p['warn']}; border-color: {p['warn']}55; }}
  .pill.fail {{ color: {p['fail']}; border-color: {p['fail']}55; }}

  /* Tables */
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0 16px; background: {p['bg']}; border: 1px solid {p['border']}; border-radius: 8px; overflow: hidden; font-size: 14px; }}
  thead {{ background: {p['bg_alt']}; }}
  th {{ text-align: left; padding: 10px 14px; color: {p['head']}; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid {p['border']}; }}
  td {{ padding: 12px 14px; border-bottom: 1px solid {p['border_dim']}; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tbody tr {{ transition: background 0.1s; }}
  tbody tr:hover {{ background: {p['bg_alt']}; }}

  .row-id {{ font-family: ui-monospace, "SF Mono", monospace; color: {p['text_dim']}; font-size: 12px; white-space: nowrap; }}
  .row-title {{ font-weight: 600; color: {p['head']}; }}
  .row-detail {{ color: {p['text_dim']}; font-size: 13px; margin-top: 4px; }}
  .row-affected {{ margin-top: 6px; font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; color: {p['text']}; }}
  .row-affected .item {{ display: inline-block; background: {p['bg_dark']}; border: 1px solid {p['border']}; border-radius: 4px; padding: 1px 6px; margin: 2px 4px 2px 0; }}
  .row-fix {{ margin-top: 6px; color: {p['text_dim']}; font-size: 13px; }}
  .row-fix strong {{ color: {p['blue']}; }}
  .row-ref {{ font-size: 12px; color: {p['text_dim']}; }}

  .status {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; border: 1px solid; white-space: nowrap; }}
  .status.fail {{ color: {p['fail']}; background: {p['fail']}11; border-color: {p['fail']}55; }}
  .status.warn {{ color: {p['warn']}; background: {p['warn']}11; border-color: {p['warn']}55; }}
  .status.pass {{ color: {p['pass']}; background: {p['pass']}11; border-color: {p['pass']}55; }}

  /* Footer */
  .footer {{ margin-top: 64px; padding-top: 24px; border-top: 1px solid {p['border_dim']}; font-size: 13px; color: {p['text_faint']}; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px; }}

  @media print {{
    body {{ background: white; color: black; }}
    .score-panel, .cat-card, table {{ background: white !important; border-color: #d0d7de !important; }}
    h1, h2, h3, .row-title, th {{ color: black !important; }}
    a {{ color: #0550ae !important; }}
  }}
</style>
</head>
<body>
<div class="container">

  <header>
    <h1>kube-prod-audit</h1>
    <div class="meta">
      <span>cluster: <strong style="color: {p['head']}">{html.escape(result.cluster)}</strong></span>
      <span>mode: {html.escape(result.mode)}</span>
      <span>generated: {now}</span>
    </div>

    <div class="score-panel">
      <div>
        <div class="score-num">{score}<span class="score-of">/100</span></div>
      </div>
      <div>
        <div class="score-label">{score_label}</div>
        <div class="score-sub">Based on {len(result.checks)} production-readiness checks</div>
        <div class="score-stats">
          <div class="score-stat pass"><span class="num">{counts['pass']}</span><span class="lbl">pass</span></div>
          <div class="score-stat warn"><span class="num">{counts['warn']}</span><span class="lbl">warn</span></div>
          <div class="score-stat fail"><span class="num">{counts['fail']}</span><span class="lbl">fail</span></div>
        </div>
      </div>
    </div>
  </header>

  <h2 style="border-bottom: none; margin-top: 16px;">By category</h2>
  <div class="cat-grid">
    {"".join(cat_cards)}
  </div>

  <hr>

  {"".join(sections_html)}

  <div class="footer">
    <span>kube-prod-audit · part of the K8s in Production reference</span>
    <span>Run: <code>python -m kube_prod_audit --mode {'live' if result.mode == 'live' else 'demo'}</code></span>
  </div>

</div>
</body>
</html>
"""


def _render_check_row(c: CheckResult, reference_root: str) -> str:
    p = PALETTE
    affected_html = ""
    if c.affected:
        items = "".join(f'<span class="item">{html.escape(a)}</span>' for a in c.affected[:8])
        more = f'<span class="row-detail">… and {len(c.affected) - 8} more</span>' if len(c.affected) > 8 else ""
        affected_html = f'<div class="row-affected">{items}{more}</div>'

    fix_html = ""
    if c.fix:
        fix_html = f'<div class="row-fix"><strong>fix:</strong> {html.escape(c.fix)}</div>'

    ref_html = ""
    if c.reference:
        ref_url = f"{reference_root}/k8s-prod-reference/{c.reference}"
        # We treat the reference as a relative link; if a user runs against
        # a path that doesn't exist, it will be a 404 — the report is still useful.
        ref_html = f'<div class="row-ref">→ <a href="{html.escape(ref_url)}">reference</a></div>'

    return f"""
            <tr>
              <td class="row-id">{html.escape(c.id)}</td>
              <td>
                <div class="row-title">{html.escape(c.title)}</div>
                <div class="row-detail">{html.escape(c.detail or c.summary)}</div>
                {affected_html}
                {fix_html}
                {ref_html}
              </td>
              <td><span class="status {c.severity.value}">{c.severity.value}</span></td>
              <td>{c.summary}</td>
            </tr>"""
