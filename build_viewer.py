#!/usr/bin/env python3
"""
Build the unified HTML viewer for tooling-deep-dives.

Reads the 8 Markdown deep-dives and the README, converts them to styled HTML
with YAML syntax highlighting, and assembles them into a single self-contained
HTML viewer with sidebar navigation.
"""
import html
import re
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "index.html"

PAGES = [
    ("README.md", "Overview", "00"),
    ("01-cert-manager.md", "cert-manager", "01"),
    ("02-external-secrets.md", "External Secrets", "02"),
    ("03-kyverno.md", "Kyverno", "03"),
    ("04-argo-cd.md", "Argo CD", "04"),
    ("05-argo-rollouts.md", "Argo Rollouts", "05"),
    ("06-velero.md", "Velero", "06"),
    ("07-falco.md", "Falco", "07"),
    ("08-prometheus-stack.md", "kube-prometheus-stack", "08"),
]

PALETTE = {
    "bg": "#0d1117", "bg_alt": "#161b22", "bg_dark": "#010409",
    "border": "#30363d", "border_dim": "#21262d",
    "text": "#c9d1d9", "text_dim": "#8b949e", "text_faint": "#6e7681",
    "head": "#f0f6fc",
    "blue": "#58a6ff", "purple": "#d2a8ff", "green": "#3fb950",
    "orange": "#d29922", "red": "#f85149",
}

# ─── YAML syntax highlighting ──────────────────────────────────────────

def _looks_like_yaml(text: str) -> bool:
    """Heuristic: does this block contain YAML-like content?

    Looks for indentation-based key: value patterns, list items, or YAML
    document separators. Used to highlight mixed-content blocks (e.g. shell
    heredocs that contain YAML).
    """
    # YAML doc separator
    if re.search(r'^\s*---\s*$', text, re.MULTILINE):
        return True
    # indented key: value
    if re.search(r'^\s{2,}[A-Za-z_][\w\-]*\s*:\s', text, re.MULTILINE):
        return True
    return False


def highlight_yaml(text: str) -> str:
    """Light YAML highlighting — keys, strings, numbers, comments, booleans."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        # Comments to end of line
        if c == '#':
            end = text.find('\n', i)
            if end == -1:
                end = n
            out.append(f'<span class="c">{html.escape(text[i:end])}</span>')
            i = end
            continue
        # Strings (simple and double-quoted, no escape handling beyond basics)
        if c in ('"', "'"):
            quote = c
            j = i + 1
            while j < n and text[j] != quote:
                if text[j] == '\\' and j + 1 < n:
                    j += 2
                else:
                    j += 1
            j = min(j + 1, n)
            out.append(f'<span class="s">{html.escape(text[i:j])}</span>')
            i = j
            continue
        # key: value — key gets special color, then optional colon, then value
        m = re.match(r'([A-Za-z_][\w\-\.]*?)(\s*:)', text[i:])
        if m and (i == 0 or text[i-1] in ' \t\n'):
            key, colon = m.group(1), m.group(2)
            out.append(f'<span class="k">{html.escape(key)}</span><span class="c">{html.escape(colon)}</span>')
            i += len(m.group(0))
            continue
        # Booleans and null
        m = re.match(r'\b(true|false|null|True|False|Null|yes|no|on|off)\b', text[i:])
        if m:
            out.append(f'<span class="b">{html.escape(m.group(0))}</span>')
            i += len(m.group(0))
            continue
        # Numbers
        m = re.match(r'-?\d+(\.\d+)?', text[i:])
        if m and (i == 0 or not text[i-1].isalnum()):
            out.append(f'<span class="n">{html.escape(m.group(0))}</span>')
            i += len(m.group(0))
            continue
        # YAML doc separator
        if text[i:i+3] == '---':
            out.append(f'<span class="y">{html.escape(text[i:i+3])}</span>')
            i += 3
            continue
        out.append(html.escape(c))
        i += 1
    return ''.join(out)


# ─── Markdown → HTML (lightweight, sufficient for our docs) ────────────

def md_to_html(md: str) -> str:
    """Convert the deep-dive Markdown to HTML.

    Handles: headings, fenced code (YAML/bash), inline code, bold, italic,
    links, simple lists, tables, blockquotes, hr.
    """
    lines = md.split('\n')
    out = []
    in_code = False
    code_lang = ''
    code_buf: list[str] = []
    in_list = False
    in_table = False
    table_header_done = False

    def flush_list():
        nonlocal in_list
        if in_list:
            out.append('</ul>')
            in_list = False

    def flush_table():
        nonlocal in_table, table_header_done
        if in_table:
            out.append('</tbody></table>')
            in_table = False
            table_header_done = False

    def flush_code():
        nonlocal in_code, code_buf, code_lang
        if code_buf:
            body = '\n'.join(code_buf)
            # Apply YAML highlighting if tagged yaml OR if the block looks
            # like it contains YAML (heredocs, mixed-content shell, etc.)
            if code_lang in ('yaml', '') or _looks_like_yaml(body):
                highlighted = highlight_yaml(body)
            else:
                highlighted = html.escape(body)
            cls = f' class="lang-{code_lang}"' if code_lang else ''
            out.append(f'<pre><code{cls}>{highlighted}</code></pre>')
            code_buf = []
            code_lang = ''
        in_code = False

    i = 0
    while i < len(lines):
        line = lines[i]

        # Fenced code
        fence = re.match(r'^```(\w*)\s*$', line)
        if fence:
            if in_code:
                flush_code()
                out.append('</code></pre>')
            else:
                flush_list(); flush_table()
                in_code = True
                code_lang = fence.group(1)
            i += 1
            continue

        if in_code:
            code_buf.append(line)
            i += 1
            continue

        # Headings
        m = re.match(r'^(#{1,6})\s+(.+)$', line)
        if m:
            flush_list(); flush_table()
            level = len(m.group(1))
            content = inline_md(m.group(2))
            out.append(f'<h{level}>{content}</h{level}>')
            i += 1
            continue

        # Horizontal rule
        if re.match(r'^-{3,}\s*$', line):
            flush_list(); flush_table()
            out.append('<hr>')
            i += 1
            continue

        # Table
        if '|' in line and i + 1 < len(lines) and re.match(r'^\s*\|?[\s\-:|]+\|[\s\-:|]+\s*\|?\s*$', lines[i+1] or ''):
            flush_list(); flush_table()
            in_table = True
            header_cells = [c.strip() for c in line.strip().strip('|').split('|')]
            out.append('<table><thead><tr>')
            for c in header_cells:
                out.append(f'<th>{inline_md(c)}</th>')
            out.append('</tr></thead><tbody>')
            i += 2  # skip separator
            table_header_done = True
            continue

        if in_table:
            if '|' not in line or not line.strip():
                flush_table()
                continue
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            out.append('<tr>')
            for c in cells:
                out.append(f'<td>{inline_md(c)}</td>')
            out.append('</tr>')
            i += 1
            continue

        # Blockquote
        m = re.match(r'^>\s*(.*)$', line)
        if m:
            flush_list(); flush_table()
            out.append(f'<blockquote><p>{inline_md(m.group(1))}</p></blockquote>')
            i += 1
            continue

        # Bullet list
        m = re.match(r'^(\s*)[-*]\s+(.*)$', line)
        if m:
            flush_table()
            if not in_list:
                out.append('<ul>')
                in_list = True
            indent = m.group(1)
            content = inline_md(m.group(2))
            out.append(f'<li>{content}</li>')
            i += 1
            continue

        # Numbered list
        m = re.match(r'^\s*\d+\.\s+(.*)$', line)
        if m:
            flush_table()
            content = inline_md(m.group(1))
            if not in_list:
                out.append('<ol>')
                in_list = True
            out.append(f'<li>{content}</li>')
            i += 1
            continue

        # Empty line
        if not line.strip():
            flush_list(); flush_table()
            i += 1
            continue

        # Paragraph
        flush_list(); flush_table()
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r'^(#{1,6}\s|```|>|\s*[-*]\s|\s*\d+\.\s|-{3,}\s*$)', lines[i]):
            para.append(lines[i])
            i += 1
        content = inline_md(' '.join(para))
        out.append(f'<p>{content}</p>')

    flush_list(); flush_table()
    if in_code:
        flush_code()
    return '\n'.join(out)


def inline_md(text: str) -> str:
    """Inline Markdown: code, bold, italic, links, escape."""
    # Escape first, then re-introduce patterns
    # Code spans
    text = re.sub(
        r'`([^`]+)`',
        lambda m: f'<code>{html.escape(m.group(1))}</code>',
        text,
    )
    # Bold
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    # Italic
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', text)
    # Links [text](url)
    text = re.sub(
        r'\[([^\]]+)\]\(([^)]+)\)',
        lambda m: f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>',
        text,
    )
    return text


# ─── Build the viewer ──────────────────────────────────────────────────

def main():
    sections = []
    for fname, title, num in PAGES:
        path = ROOT / fname
        if not path.exists():
            print(f"WARN: {path} missing")
            continue
        md = path.read_text(encoding='utf-8')
        body = md_to_html(md)
        section_id = fname.replace('.md', '').replace(' ', '-').lower()
        sections.append({
            'id': section_id,
            'num': num,
            'title': title,
            'body': body,
        })

    sidebar_items = '\n'.join(
        f'<li class="nav-section"><a class="nav-link" href="#{s["id"]}"><span class="nav-num">{s["num"]}</span><span class="nav-label">{html.escape(s["title"])}</span></a></li>'
        for s in sections
    )

    sections_html = '\n'.join(
        f'<article id="{s["id"]}">\n{s["body"]}\n</article>'
        for s in sections
    )

    p = PALETTE
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kubernetes Tooling Deep-Dives</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{ scroll-behavior: smooth; scroll-padding-top: 80px; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", Roboto, sans-serif;
    background: {p['bg']}; color: {p['text']};
    line-height: 1.65; font-size: 16px;
  }}
  .layout {{ display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; }}
  @media (max-width: 900px) {{
    .layout {{ grid-template-columns: 1fr; }}
    .sidebar {{ position: static !important; height: auto !important; border-right: none !important; border-bottom: 1px solid {p['border_dim']}; }}
  }}

  .sidebar {{
    background: {p['bg_dark']};
    border-right: 1px solid {p['border_dim']};
    padding: 24px 0;
    position: sticky; top: 0; height: 100vh; overflow-y: auto;
  }}
  .brand {{ padding: 0 24px 24px; border-bottom: 1px solid {p['border_dim']}; margin-bottom: 16px; }}
  .brand-title {{ font-size: 18px; font-weight: 700; color: {p['head']}; margin-bottom: 4px; }}
  .brand-subtitle {{ font-size: 12px; color: {p['text_dim']}; line-height: 1.4; }}
  .brand-kbd {{
    display: inline-block; background: {p['bg']};
    border: 1px solid {p['border']}; border-radius: 4px;
    padding: 1px 6px; font-size: 11px; color: {p['text']};
    font-family: ui-monospace, "SF Mono", monospace;
  }}
  .search-box {{ margin: 0 16px 16px; position: relative; }}
  .search-input {{
    width: 100%; background: {p['bg']};
    border: 1px solid {p['border']}; border-radius: 6px;
    padding: 8px 12px 8px 32px; color: {p['text']}; font-size: 13px;
    font-family: inherit;
  }}
  .search-input:focus {{ outline: none; border-color: {p['blue']}; box-shadow: 0 0 0 3px rgba(88,166,255,0.15); }}
  .search-icon {{
    position: absolute; left: 10px; top: 50%;
    transform: translateY(-50%); color: {p['text_dim']};
    pointer-events: none; font-size: 13px;
  }}
  .nav-list {{ list-style: none; }}
  .nav-link {{
    display: flex; align-items: center;
    padding: 7px 24px; color: {p['text_dim']};
    text-decoration: none; font-size: 13px;
    border-left: 2px solid transparent; transition: all 0.15s;
  }}
  .nav-link:hover {{ background: {p['bg_alt']}; color: {p['text']}; }}
  .nav-link.active {{ background: {p['bg_alt']}; color: {p['blue']}; border-left-color: {p['blue']}; }}
  .nav-num {{
    display: inline-block; min-width: 32px;
    color: {p['text_faint']}; font-family: ui-monospace, "SF Mono", monospace;
    font-size: 11px;
  }}
  .nav-link.active .nav-num {{ color: {p['blue']}; }}

  .main {{ max-width: 980px; margin: 0 auto; padding: 48px 56px 96px; }}
  @media (max-width: 600px) {{ .main {{ padding: 24px 20px 64px; }} }}

  h1, h2, h3, h4 {{ color: {p['head']}; line-height: 1.25; }}
  h1 {{ font-size: 36px; font-weight: 800; margin-bottom: 12px; letter-spacing: -0.5px; }}
  h2 {{
    font-size: 26px; font-weight: 700; margin: 48px 0 16px;
    padding-bottom: 8px; border-bottom: 1px solid {p['border_dim']};
  }}
  h3 {{ font-size: 20px; font-weight: 600; margin: 32px 0 12px; color: #e6edf3; }}
  h4 {{ font-size: 16px; font-weight: 600; margin: 24px 0 8px; color: #e6edf3; }}
  p {{ margin: 12px 0; color: {p['text']}; }}
  a {{ color: {p['blue']}; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  strong {{ color: {p['head']}; }}
  em {{ color: {p['text']}; }}
  ul, ol {{ padding-left: 24px; margin: 12px 0; }}
  li {{ margin: 4px 0; }}
  hr {{ border: none; border-top: 1px solid {p['border_dim']}; margin: 32px 0; }}
  blockquote {{
    border-left: 3px solid {p['blue']}; background: {p['bg_alt']};
    padding: 12px 18px; margin: 16px 0;
    color: {p['text']}; border-radius: 0 6px 6px 0;
  }}
  blockquote p {{ margin: 4px 0; }}

  table {{
    width: 100%; border-collapse: collapse; margin: 16px 0;
    background: {p['bg']}; border: 1px solid {p['border']};
    border-radius: 6px; overflow: hidden; font-size: 14px;
  }}
  thead {{ background: {p['bg_alt']}; }}
  th {{ text-align: left; padding: 10px 14px; color: {p['head']}; font-weight: 600; border-bottom: 1px solid {p['border']}; }}
  td {{ padding: 10px 14px; border-bottom: 1px solid {p['border_dim']}; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: {p['bg_alt']}; }}

  code {{
    font-family: ui-monospace, "SF Mono", "Cascadia Code", monospace;
    font-size: 0.9em; background: {p['bg_alt']}; color: #ff7b72;
    padding: 2px 6px; border-radius: 4px; border: 1px solid {p['border_dim']};
  }}
  pre {{
    background: {p['bg_dark']}; border: 1px solid {p['border']};
    border-radius: 8px; padding: 16px 20px; overflow-x: auto;
    margin: 16px 0; line-height: 1.55; font-size: 13px;
  }}
  pre code {{ background: transparent; border: none; padding: 0; color: {p['text']}; font-size: inherit; }}
  pre .k {{ color: #ff7b72; }}
  pre .s {{ color: #a5d6ff; }}
  pre .n {{ color: {p['blue']}; }}
  pre .c {{ color: {p['text_dim']}; font-style: italic; }}
  pre .b {{ color: #d2a8ff; }}
  pre .y {{ color: #ffa657; }}

  .doc-footer {{
    margin-top: 64px; padding-top: 24px;
    border-top: 1px solid {p['border_dim']};
    font-size: 13px; color: {p['text_faint']};
    display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px;
  }}
  .back-to-top {{
    background: {p['bg_alt']}; border: 1px solid {p['border']};
    color: {p['text']}; padding: 4px 12px; border-radius: 6px;
    cursor: pointer; font-size: 12px;
  }}
  .back-to-top:hover {{ background: #21262d; }}

  @media print {{
    .sidebar {{ display: none; }}
    .layout {{ grid-template-columns: 1fr; }}
    .main {{ max-width: none; padding: 24px; }}
    body {{ background: white; color: black; }}
    h1, h2, h3, h4 {{ color: black; }}
    pre {{ background: #f6f8fa; color: black; border-color: #d0d7de; }}
    code {{ background: #f6f8fa; color: black; }}
    table {{ background: white; }}
  }}
</style>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-title">Tooling Deep-Dives</div>
      <div class="brand-subtitle">Production Kubernetes<br><span class="brand-kbd">/</span> search <span class="brand-kbd">Ctrl K</span></div>
    </div>
    <div class="search-box">
      <span class="search-icon">⌕</span>
      <input type="text" id="search" class="search-input" placeholder="Filter sections…">
    </div>
    <ul class="nav-list" id="nav">
{sidebar_items}
    </ul>
  </aside>

  <main class="main">
{sections_html}

    <div class="doc-footer">
      <span>Tooling Deep-Dives · part of the K8s in Production reference</span>
      <button class="back-to-top" onclick="window.scrollTo({{top: 0, behavior: 'smooth'}})">↑ Back to top</button>
    </div>
  </main>
</div>

<script>
  // Scroll-spy
  const sections = document.querySelectorAll('article[id]');
  const navLinks = document.querySelectorAll('.nav-link');
  const spy = () => {{
    let current = '';
    const scrollPos = window.scrollY + 120;
    sections.forEach(s => {{
      if (s.offsetTop <= scrollPos) current = s.id;
    }});
    navLinks.forEach(l => {{
      l.classList.remove('active');
      if (l.getAttribute('href') === '#' + current) l.classList.add('active');
    }});
  }};
  window.addEventListener('scroll', spy, {{ passive: true }});
  spy();

  // Search filter
  const search = document.getElementById('search');
  const nav = document.getElementById('nav');
  search.addEventListener('input', e => {{
    const q = e.target.value.toLowerCase().trim();
    nav.querySelectorAll('.nav-link').forEach(link => {{
      const txt = link.textContent.toLowerCase();
      link.parentElement.style.display = (!q || txt.includes(q)) ? '' : 'none';
    }});
  }});

  // Keyboard shortcut
  document.addEventListener('keydown', e => {{
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {{
      e.preventDefault();
      search.focus();
      search.select();
    }}
    if (e.key === 'Escape' && document.activeElement === search) {{
      search.value = '';
      search.dispatchEvent(new Event('input'));
      search.blur();
    }}
  }});
</script>
</body>
</html>
"""

    OUT.write_text(html_doc, encoding='utf-8')
    print(f"Wrote {OUT} ({len(html_doc):,} bytes)")


if __name__ == "__main__":
    main()
