"""Render the bundled README into a readable HTML manual.

The Help button turns README.md into a styled, self-contained HTML page and
hands it to the user's browser. Doing the conversion at the moment Help is
clicked, rather than shipping a pre-built document, is deliberate: the
manual is then generated from the very README that shipped with the build,
so it cannot drift from it, and it works identically from source and from
a frozen executable. A pre-built PDF would need a converter toolchain in
CI, would be a second artifact to keep in step, and would silently go stale
the first time someone edited the README without rebuilding it.

This is a MARKDOWN SUBSET, not a general implementation. It covers exactly
what README.md uses -- headings, tables, fenced code, bullet and numbered
lists with indented continuation lines, and inline code/bold/italic/links.
A construct not in that list will come out as literal text rather than
silently disappearing, and _selftest checks the real README for leftovers.
Python has no markdown module in its standard library and this is not worth
a dependency that PyInstaller would then have to bundle.
"""

import html
import re

__all__ = ["render_manual", "slugify", "MANUAL_CSS"]


def slugify(text: str) -> str:
    """GitHub's heading-anchor rules, so the README's own #links resolve.

    Lowercase, drop anything that is not a letter, digit, space or hyphen,
    then spaces to hyphens. The README links to its own sections, and those
    anchors were written against GitHub's scheme.
    """
    s = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s]+", "-", s.strip())


# ------------------------------------------------------------------ inline
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    """Inline markup for one run of text, code spans taken out first.

    Code spans are split off before anything else so that a `*` or a `**`
    inside one is shown, not interpreted -- the README has code spans
    holding things like `dF/dt` and format strings, and treating those as
    emphasis would corrupt exactly the passages a reader is checking most
    carefully.
    """
    out = []
    pos = 0
    for m in _CODE_SPAN.finditer(text):
        out.append(_inline_plain(text[pos:m.start()]))
        out.append(f"<code>{html.escape(m.group(1))}</code>")
        pos = m.end()
    out.append(_inline_plain(text[pos:]))
    return "".join(out)


def _inline_plain(text: str) -> str:
    text = html.escape(text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    # Links last: by now the link text carries its own markup, and no
    # emphasis pattern can straddle the tags this emits.
    return _LINK.sub(r'<a href="\2">\1</a>', text)


# ------------------------------------------------------------------- blocks
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|?\s*$")
_HR = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")


class _Renderer:
    """One pass over the lines, emitting HTML and collecting the contents."""

    def __init__(self, lines):
        self.lines = lines
        self.i = 0
        self.out = []
        self.toc = []          # (level, slug, text) for h2/h3
        self._seen_slugs = {}

    # -- helpers ---------------------------------------------------------
    def _peek(self, offset=0):
        j = self.i + offset
        return self.lines[j] if j < len(self.lines) else None

    def _slug(self, text):
        """Unique anchor per heading; a repeat gets -1, -2 as GitHub does."""
        base = slugify(text)
        n = self._seen_slugs.get(base, 0)
        self._seen_slugs[base] = n + 1
        return base if n == 0 else f"{base}-{n}"

    # -- block dispatch --------------------------------------------------
    def run(self):
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip():
                self.i += 1
            elif line.startswith("```"):
                self._code()
            elif _HEADING.match(line):
                self._heading()
            elif line.startswith("|"):
                self._table()
            elif _HR.match(line):
                self.out.append("<hr>")
                self.i += 1
            elif _BULLET.match(line) or _NUMBERED.match(line):
                self._list()
            else:
                self._paragraph()
        return self

    def _heading(self):
        m = _HEADING.match(self.lines[self.i])
        level, text = len(m.group(1)), m.group(2).strip()
        slug = self._slug(text)
        if level in (2, 3):
            self.toc.append((level, slug, text))
        # The anchor is on the heading itself so the README's own in-page
        # links land on it, and so a reader can copy a section's URL.
        self.out.append(
            f'<h{level} id="{slug}">'
            f'<a class="anchor" href="#{slug}">{_inline(text)}</a>'
            f"</h{level}>")
        self.i += 1

    def _code(self):
        fence = self.lines[self.i].strip()
        lang = fence[3:].strip()
        self.i += 1
        body = []
        while self.i < len(self.lines) and not self.lines[self.i].startswith("```"):
            body.append(self.lines[self.i])
            self.i += 1
        self.i += 1                      # closing fence
        cls = f' class="lang-{html.escape(lang)}"' if lang else ""
        self.out.append(f"<pre{cls}><code>{html.escape(chr(10).join(body))}"
                         "</code></pre>")

    def _split_row(self, line):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    def _table(self):
        rows = []
        while self.i < len(self.lines) and self.lines[self.i].startswith("|"):
            rows.append(self.lines[self.i])
            self.i += 1
        header, body = None, rows
        if len(rows) >= 2 and _TABLE_SEP.match(rows[1]):
            header, body = rows[0], rows[2:]
        # Wrapped so a wide table scrolls inside itself instead of forcing
        # the whole page sideways.
        parts = ['<div class="table-wrap"><table>']
        if header is not None:
            cells = "".join(f"<th>{_inline(c)}</th>" for c in self._split_row(header))
            parts.append(f"<thead><tr>{cells}</tr></thead>")
        parts.append("<tbody>")
        for r in body:
            cells = "".join(f"<td>{_inline(c)}</td>" for c in self._split_row(r))
            parts.append(f"<tr>{cells}</tr>")
        parts.append("</tbody></table></div>")
        self.out.append("".join(parts))

    def _list_item_lines(self):
        """Collect one item's text: its own line plus indented continuations.

        The README wraps long numbered steps across several indented lines,
        and treats a blank line followed by more indented text as the same
        item continuing. Getting this wrong is what turns one nine-step
        workflow into nine one-line steps and a wall of loose paragraphs.
        """
        parts = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip():
                nxt = self._peek(1)
                if nxt is None or not nxt.startswith((" ", "\t")):
                    break
                self.i += 1
                continue
            if parts and not line.startswith((" ", "\t")):
                break
            if parts and (_BULLET.match(line) or _NUMBERED.match(line)):
                break                     # a nested item; the caller takes it
            parts.append(line.strip())
            self.i += 1
        return " ".join(parts)

    def _list(self):
        """One list, nesting by indent. Only the depths the README uses."""
        base_indent = len(self.lines[self.i]) - len(self.lines[self.i].lstrip())
        ordered = bool(_NUMBERED.match(self.lines[self.i]))
        tag = "ol" if ordered else "ul"
        self.out.append(f"<{tag}>")
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip():
                nxt = self._peek(1)
                if nxt is None or not (nxt.startswith((" ", "\t"))
                                        or _BULLET.match(nxt)
                                        or _NUMBERED.match(nxt)):
                    break
                self.i += 1
                continue
            m = _NUMBERED.match(line) or _BULLET.match(line)
            if not m:
                break
            indent = len(m.group(1))
            if indent < base_indent:
                break
            if indent > base_indent:
                # Nested list: it belongs inside the item just emitted.
                if self.out and self.out[-1].endswith("</li>"):
                    self.out[-1] = self.out[-1][:-len("</li>")]
                    self._list()
                    self.out.append("</li>")
                else:
                    self._list()
                continue
            # Strip the marker, then gather this item's continuation lines.
            self.lines[self.i] = m.group(len(m.groups()))
            text = self._list_item_lines()
            self.out.append(f"<li>{_inline(text)}</li>")
        self.out.append(f"</{tag}>")

    def _paragraph(self):
        """Consecutive non-blank lines are one hard-wrapped paragraph."""
        parts = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if (not line.strip() or line.startswith(("|", "```"))
                    or _HEADING.match(line) or _HR.match(line)
                    or _BULLET.match(line) or _NUMBERED.match(line)):
                break
            parts.append(line.strip())
            self.i += 1
        if parts:
            self.out.append(f"<p>{_inline(' '.join(parts))}</p>")


MANUAL_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1c1e; --muted: #5b6169; --rule: #dfe3e8;
  --code-bg: #f4f6f8; --accent: #14607a; --th-bg: #eef2f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16191c; --fg: #e6e8ea; --muted: #9aa2ab; --rule: #2c3238;
    --code-bg: #1e2429; --accent: #6fc2dc; --th-bg: #222a30;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.65 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.layout { display: flex; align-items: flex-start; gap: 2.5rem;
          max-width: 1180px; margin: 0 auto; padding: 2rem 1.5rem 5rem; }
nav { position: sticky; top: 2rem; flex: 0 0 15rem; max-height: 85vh;
      overflow-y: auto; font-size: 0.86rem; border-left: 2px solid var(--rule);
      padding-left: 0.9rem; }
nav h2 { font-size: 0.74rem; letter-spacing: 0.09em; text-transform: uppercase;
         color: var(--muted); margin: 0 0 0.6rem; border: 0; padding: 0; }
nav a { display: block; padding: 0.16rem 0; color: var(--muted);
        text-decoration: none; }
nav a:hover { color: var(--accent); }
nav a.sub { padding-left: 0.9rem; font-size: 0.95em; }
nav code { background: none; padding: 0; font-size: 1em; }
main { flex: 1 1 auto; min-width: 0; }
header.doc { border-bottom: 2px solid var(--rule); margin-bottom: 1.5rem; }
header.doc .sub { color: var(--muted); font-size: 0.92rem; margin: 0 0 1.2rem; }
h1, h2, h3, h4 { line-height: 1.25; margin: 2.2rem 0 0.8rem; }
h1 { font-size: 1.95rem; margin-top: 0.6rem; }
h2 { font-size: 1.42rem; padding-bottom: 0.3rem; border-bottom: 1px solid var(--rule); }
h3 { font-size: 1.13rem; }
h4 { font-size: 1rem; color: var(--muted); }
a { color: var(--accent); }
a.anchor { color: inherit; text-decoration: none; }
a.anchor:hover::after { content: " #"; color: var(--muted); font-weight: normal; }
p, li { max-width: 46rem; }
code { background: var(--code-bg); border-radius: 3px; padding: 0.1em 0.35em;
       font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
       font-size: 0.88em; }
pre { background: var(--code-bg); border: 1px solid var(--rule); border-radius: 6px;
      padding: 0.9rem 1.1rem; overflow-x: auto; }
pre code { background: none; padding: 0; font-size: 0.85rem; line-height: 1.5; }
.table-wrap { overflow-x: auto; margin: 1.1rem 0; }
table { border-collapse: collapse; font-size: 0.92rem; }
th, td { border: 1px solid var(--rule); padding: 0.45rem 0.7rem;
         text-align: left; vertical-align: top; }
th { background: var(--th-bg); font-weight: 600; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2.2rem 0; }
ol, ul { padding-left: 1.4rem; }
li { margin: 0.35rem 0; }
li > ul, li > ol { margin-top: 0.35rem; }
@media (max-width: 860px) {
  .layout { display: block; padding: 1.2rem 1rem 3rem; }
  nav { position: static; max-height: none; margin-bottom: 2rem;
        border-left: 0; border-top: 1px solid var(--rule); padding: 1rem 0 0; }
}
@media print {
  /* Print -> Save as PDF is the supported route to a paper manual, so the
     printed form is designed rather than left to chance: no navigation,
     black on white, and no page break stranding a heading at the foot. */
  nav { display: none; }
  .layout { display: block; max-width: none; padding: 0; }
  body { font-size: 10.5pt; background: #fff; color: #000; }
  h1, h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
  pre, table, .table-wrap { break-inside: avoid; page-break-inside: avoid; }
  a { color: #000; text-decoration: none; }
  pre, code, th { background: #f2f2f2 !important; }
}
"""


def render_manual(md_text: str, title: str = "Manual",
                   subtitle: str = "") -> str:
    """One self-contained HTML document. No external files, no network.

    Self-contained matters more than it looks: the page is written to a
    temporary directory and opened from there, so anything it referenced by
    relative path would simply be missing, and a frozen app has no server to
    serve assets from.
    """
    r = _Renderer(md_text.replace("\r\n", "\n").split("\n")).run()
    nav = []
    for level, slug, text in r.toc:
        cls = ' class="sub"' if level == 3 else ""
        # _inline, not html.escape: several headings carry inline code
        # ("Reading `pk`"), and escaping would print the backticks.
        nav.append(f'<a{cls} href="#{slug}">{_inline(text)}</a>')
    sub = f'<p class="sub">{html.escape(subtitle)}</p>' if subtitle else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{MANUAL_CSS}</style>\n"
        "</head>\n<body>\n"
        '<div class="layout">\n'
        f'<nav><h2>Contents</h2>{"".join(nav)}</nav>\n'
        f'<main><header class="doc">{sub}</header>\n'
        + "\n".join(r.out) +
        "\n</main>\n</div>\n</body>\n</html>\n"
    )
