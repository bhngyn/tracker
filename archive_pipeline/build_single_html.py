"""Bundle the viewer, its data and the methodology into ONE self-contained .html file.

usage: build_single_html.py [--out FILE]

Run after build_index.py + package.py. Inlines OPEN ME.html + viewer/app.css + viewer/app.js +
viewer/data/*.js + METHODOLOGY.md and adds an Overview tab (coverage, known gaps, top watchlist names,
methodology). Search, places, transcripts, OCR and review tags all work from the file alone. Media files
are not embedded: next to the archive (or Lite package) root, thumbnails and videos load from the relative
paths; on its own, each item links to the original post.

Data goes in <script type="application/json"> blocks (JSON.parse is much faster than a 50 MB JS literal),
with every "<" written as \\u003c so no caption or comment can close the script element early.
Optional project.toml section:  [single_html]  notes = ["...", ...]   (extra lines for "Known gaps")
"""
import argparse, hashlib, html, json, re
from datetime import datetime, timezone
from common import CFG, PACKAGES, ROOT, log

DATA = ROOT / "viewer" / "data"
DATASETS = ("meta", "items", "places", "hits", "search")


def read_dataset(name: str) -> dict:
    """viewer/data/<name>.js -> {key: json_text} (one `window.ARCHIVE.<key>=...;` per file)."""
    s = (DATA / f"{name}.js").read_text(encoding="utf-8")
    out = {}
    marks = list(re.finditer(r"window\.ARCHIVE\.(\w+)=", s))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(s)
        out[m.group(1)] = s[m.end():end].strip().rstrip(";")
    return out


def json_block(key: str, text: str) -> str:
    return f'<script type="application/json" data-key="{key}">{text.replace("<", chr(92) + "u003c")}</script>'


def inline_md(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(https?://[^\s<)`]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', s)
    return s


def md_to_html(md: str) -> str:
    """Just enough Markdown for METHODOLOGY.md: headings, paragraphs, lists (one nesting level), tables, code."""
    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
        elif ln.startswith("#"):
            lvl = min(len(ln) - len(ln.lstrip("#")) + 1, 4)
            out.append(f"<h{lvl}>{inline_md(ln.lstrip('#').strip())}</h{lvl}>"); i += 1
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")]); i += 1
            body = [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            out.append('<div class="wrap"><table class="mdt"><thead><tr>' + "".join(f"<th>{inline_md(c)}</th>" for c in rows[0])
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{inline_md(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table></div>")
        elif ln.startswith("    "):
            code = []
            while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
                code.append(lines[i][4:]); i += 1
            out.append(f"<pre>{html.escape(chr(10).join(code).strip())}</pre>")
        elif re.match(r"\s*- ", ln):
            items = []
            while i < len(lines) and lines[i].strip() and (re.match(r"\s*- ", lines[i]) or lines[i].startswith("  ")):
                l = lines[i]
                if re.match(r"\s{2,}- ", l):
                    items.append(("sub", l.strip()[2:]))
                elif re.match(r"- ", l):
                    items.append(("top", l[2:]))
                elif items:
                    items[-1] = (items[-1][0], items[-1][1] + " " + l.strip())
                i += 1
            h, open_sub = ["<ul>"], False
            for kind, t in items:
                if kind == "sub" and not open_sub: h.append("<ul>"); open_sub = True
                if kind == "top" and open_sub: h.append("</ul>"); open_sub = False
                h.append(f"<li>{inline_md(t)}</li>")
            h.append("</ul>" * (2 if open_sub else 1)); out.append("".join(h))
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "    ")) and not re.match(r"\s*- ", lines[i]):
                para.append(lines[i].strip()); i += 1
            out.append(f"<p>{inline_md(' '.join(para))}</p>")
    return "\n".join(out)


def gaps_from_methodology(md: str) -> list[str]:
    m = re.search(r"^## \d+\. Known gaps.*?$(.*?)^## ", md, re.S | re.M)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        if ln.startswith("- "): out.append(ln[2:].strip())
        elif ln.startswith("  ") and out: out[-1] += " " + ln.strip()
    return out


def overview_html(data: dict, md: str) -> str:
    meta = json.loads(data["meta"])
    items = json.loads(data["items"])
    places = json.loads(data["places"])
    title = CFG.get("viewer", {}).get("title", "Archive")
    purpose = " ".join(CFG.get("methodology", {}).get("purpose", "").split())
    notes = CFG.get("single_html", {}).get("notes", [])

    cov = {}
    for it in items:
        k = (it["p"], it["a"]); d = it.get("d") or ""
        c = cov.setdefault(k, [0, "9999", "0000", 0, 0])
        c[0] += 1
        if d: c[1], c[2] = min(c[1], d), max(c[2], d)
        c[3] += sum(1 for m in it["m"] if m["k"] == "video")
        c[4] += sum(1 for m in it["m"] if m["k"] == "photo")
    cov_rows = "".join(
        f"<tr><td>{html.escape(p)}</td><td>{html.escape(a)}</td><td class=n>{c[0]:,}</td><td class=n>{c[3]:,}</td>"
        f"<td class=n>{c[4]:,}</td><td>{c[1] if c[0] else ''}</td><td>{c[2]}</td></tr>"
        for (p, a), c in sorted(cov.items(), key=lambda kv: (kv[0][0], -kv[1][0])))

    fields = meta.get("by_field", {})
    field_rows = "".join(f'<tr><td><span class="badge {f}">{f}</span></td><td class=n>{n:,}</td><td data-fielddef="{f}"></td></tr>'
                         for f, n in sorted(fields.items(), key=lambda kv: -kv[1]))

    top = sorted((p for p in places if p.get("ni")), key=lambda p: -p["ni"])[:30]
    cats = {}
    for p in places:
        if p.get("ni"): cats[p["category"]] = cats.get(p["category"], 0) + 1
    top_rows = "".join(
        f'<tr><td><a href="#" class="goplace" data-id="{html.escape(p["id"])}" data-name="{html.escape(p["name"])}">{html.escape(p["name"])}</a>'
        f'{" <span class=\"badge weak\" title=\"also a common word / name — check each hit\">ambiguous</span>" if p.get("ambiguous") else ""}</td>'
        f'<td>{html.escape(p["category"])}</td><td>{html.escape(p.get("region") or "")}</td><td class=n>{p["ni"]:,}</td><td class=n>{p["n"]:,}</td></tr>'
        for p in top)

    gaps = gaps_from_methodology(md) + notes
    kpi = lambda label, v: f'<div class="kpi"><b>{v}</b>{label}</div>'
    bp = meta.get("by_platform", {})
    return f"""
  <section id="tab-overview" class="tab on prose ov">
    <div id="mediamode" class="banner"></div>
    <h2>{html.escape(title)} — read me first</h2>
    <p>{inline_md(purpose)}</p>
    <p class="warnbox"><b>A match is a lead, not a finding.</b> Captions, Whisper transcripts and OCR are machine-made
      and contain errors, especially in names, non-English speech and noisy audio. Confirm every hit against the video,
      photo or original post before relying on it, and record your decision in <b>My review</b>.</p>
    <div class="kpis">
      {kpi("items (posts &amp; videos)", f"{len(items):,}")}
      {kpi("searchable text passages", f"{meta.get('segments', 0):,}")}
      {kpi("watchlist matches", f"{meta.get('hits', 0):,}")}
      {kpi("YouTube", f"{bp.get('youtube', 0):,}")}{kpi("Facebook", f"{bp.get('facebook', 0):,}")}{kpi("Instagram", f"{bp.get('instagram', 0):,}")}
    </div>
    <p class="muted">Index built {html.escape(meta.get('built', ''))} · this file generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
      · {sum(cats.values())} watchlist names have at least one match.</p>

    <h2>Quick start</h2>
    <ol>
      <li><b>Search</b> for a place, person or phrase. Results rank by where the text came from. A timestamp opens the item at that moment.</li>
      <li><b>Places</b> lists every watchlist name with the items that mention it, strongest evidence first.</li>
      <li>Open an item to read its full transcript and OCR, then tag it <i>relevant / needs review / not relevant</i> with a note.</li>
      <li><b>My review → Export CSV</b> saves your tags (they are kept in this browser only). <b>Import CSV</b> merges a colleague's file.</li>
    </ol>

    <h2>Coverage</h2>
    <div class="wrap"><table class="mdt"><thead><tr><th>Platform</th><th>Account</th><th class=n>Items</th><th class=n>Videos</th><th class=n>Photos</th><th>Earliest</th><th>Latest</th></tr></thead>
      <tbody>{cov_rows}</tbody></table></div>

    <h2>Where the searchable text comes from</h2>
    <div class="wrap"><table class="mdt"><thead><tr><th>Field</th><th class=n>Passages</th><th>Meaning</th></tr></thead><tbody>{field_rows}</tbody></table></div>

    <h2>Most-mentioned watchlist names</h2>
    <p class="muted">Items = posts/videos with at least one match; mentions = all matches. <i>Ambiguous</i> names are also common words
      or first names, so their counts include false positives. Click a name to open it in Places.</p>
    <div class="wrap"><table class="mdt"><thead><tr><th>Name</th><th>Category</th><th>Region</th><th class=n>Items</th><th class=n>Mentions</th></tr></thead><tbody>{top_rows}</tbody></table></div>

    <h2>Known gaps</h2>
    <ul>{"".join(f"<li>{inline_md(g)}</li>" for g in gaps)}</ul>

    <details class="methodology"><summary><h2>Full methodology</h2></summary>
      {md_to_html(md)}
    </details>
  </section>"""


EXTRA_CSS = """
.ov h2 { margin-top: 28px; }
.ov .kpis { display:flex; flex-wrap:wrap; gap:10px; margin:14px 0; }
.ov .kpi { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 14px; min-width:140px; font-size:13px; color:var(--muted); }
.ov .kpi b { display:block; font-size:22px; color:var(--ink); font-variant-numeric:tabular-nums; }
.wrap { overflow-x:auto; }
table.mdt { border-collapse:collapse; background:var(--panel); border:1px solid var(--line); font-size:13px; margin:6px 0 12px; }
table.mdt th, table.mdt td { text-align:left; padding:5px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
table.mdt th { color:var(--muted); font-weight:600; font-size:12px; }
table.mdt .n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.warnbox { border-left:4px solid var(--rev); background:var(--panel); padding:8px 14px; border-radius:6px; }
.banner { padding:8px 14px; border-radius:8px; background:var(--accent-soft); font-size:13px; }
.banner:empty { display:none; }
details.methodology summary { cursor:pointer; list-style:revert; }
details.methodology summary h2 { display:inline; }
.ov pre { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:8px 10px; overflow-x:auto; }
#loading { position:fixed; inset:0; display:flex; align-items:center; justify-content:center; background:var(--bg); z-index:99; font-size:16px; color:var(--muted); }
#loading[hidden] { display:none; }
@media (max-width: 640px) { main { padding: 12px 16px 60px; } header { padding: 8px 16px; } }
"""

LOADER = r"""
(() => {
  const A = window.ARCHIVE = window.ARCHIVE || {};
  const map = { search: "segs" };
  for (const el of document.querySelectorAll('script[type="application/json"][data-key]')) {
    A[map[el.dataset.key] || el.dataset.key] = JSON.parse(el.textContent);
    el.textContent = "";  // free the source text
  }
})();
"""

AFTER = r"""
(() => {
  const $ = s => document.querySelector(s);
  // field meanings, reusing the About tab's definitions
  document.querySelectorAll("[data-fielddef]").forEach(td => {
    const dd = [...document.querySelectorAll("#fielddefs dt")].find(dt => dt.textContent.trim() === td.dataset.fielddef);
    if (dd && dd.nextElementSibling) td.textContent = dd.nextElementSibling.textContent;
  });
  // Overview name -> Places tab filtered to that name
  document.addEventListener("click", e => {
    const a = e.target.closest(".goplace"); if (!a) return;
    e.preventDefault();
    document.querySelector('nav button[data-tab="places"]').click();
    const f = $("#p-q"); f.value = a.dataset.name; f.dispatchEvent(new Event("input", { bubbles: true }));
    if (a.closest("tr").querySelector(".badge.weak")) { const w = $("#p-weak"); if (!w.checked) w.click(); }
    setTimeout(() => {
      const row = document.querySelector(`#places tbody tr[data-place="${a.dataset.id}"]`);
      if (row) row.click();
    }, 100);
  });
  // Is the archive next to this file? Probe one thumbnail.
  const probe = (window.ARCHIVE.items || []).flatMap(i => i.m).find(m => m.th);
  const mode = $("#mediamode");
  const standalone = "<b>Standalone copy:</b> all text, transcripts, OCR and matches are in this file. Photos and videos are not; each item links to the original post. To see the archived media, put this file in the archive folder (next to <code>youtube/</code>, <code>facebook/</code>, <code>instagram/</code>).";
  if (!probe) { mode.innerHTML = standalone; return; }
  const img = new Image();
  img.onload = () => { mode.innerHTML = "<b>Archive media found</b> next to this file: thumbnails, photos and videos load from the archive folder."; };
  img.onerror = () => { mode.innerHTML = standalone; document.body.classList.add("standalone"); };
  img.src = probe.th;
  // Gallery images have no fallback of their own
  document.addEventListener("error", e => {
    const t = e.target;
    if (t.tagName === "IMG" && t.closest(".gallery")) t.closest("a").classList.add("missing");
  }, true);
})();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="output .html (default: <packages_dir>/<slug>_archive_single_<date>.html)")
    a = ap.parse_args()
    slug = CFG["project"].get("slug", "archive")
    out = a.out or str(PACKAGES / f"{slug}_archive_single_{datetime.now().strftime('%Y%m%d')}.html")

    page = (ROOT / "OPEN ME.html").read_text(encoding="utf-8")
    css = (ROOT / "viewer" / "app.css").read_text(encoding="utf-8") + EXTRA_CSS
    app = (ROOT / "viewer" / "app.js").read_text(encoding="utf-8")
    cfg_js = (ROOT / "viewer" / "config.js").read_text(encoding="utf-8")
    md = (ROOT / "METHODOLOGY.md").read_text(encoding="utf-8")
    for name, s in (("app.js", app), ("config.js", cfg_js), ("app.css", css)):
        assert "</script" not in s.lower() and "</style" not in s.lower(), f"{name} would close its element early"

    data = {}
    for n in DATASETS:
        for k, v in read_dataset(n).items():
            json.loads(v)  # fail loudly on a malformed data file
            data["search" if k == "segs" else k] = v

    # tabs: Overview first and selected
    page = page.replace('<button data-tab="search" class="on">', '<button data-tab="overview" class="on">Overview</button>\n    <button data-tab="search">')
    page = page.replace('<section id="tab-search" class="tab on">', '<section id="tab-search" class="tab">')
    page = page.replace("\n<main>", "\n<main>" + overview_html(data, md), 1)
    page = page.replace("See METHODOLOGY.md for sources", "See <b>Overview → Full methodology</b> for sources")
    page = page.replace('<input id="q" type="search" placeholder="Search everything" autocomplete="off" autofocus>',
                        '<input id="q" type="search" placeholder="Search everything" autocomplete="off">')
    page = page.replace('<link rel="stylesheet" href="viewer/app.css">', f"<style>{css}</style>")
    page = page.replace("<body>", '<body>\n<div id="loading">Loading the archive index…</div>', 1)

    scripts = "\n".join(json_block(k, v) for k, v in data.items())
    tail = (f"{scripts}\n<script>{LOADER}</script>\n<script>{cfg_js}</script>\n<script>{app}</script>\n"
            f"<script>{AFTER}\ndocument.getElementById('loading').hidden = true;</script>\n")
    page = re.sub(r'<script src="viewer/data/meta\.js"></script>.*?<script src="viewer/app\.js"></script>\n',
                  lambda _: tail, page, flags=re.S)
    assert 'src="viewer/' not in page, "a viewer script tag was not inlined"

    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    sha = hashlib.sha256(page.encode("utf-8")).hexdigest()
    log(f"single html = {out}  ({len(page.encode('utf-8')) / 1e6:.1f} MB, sha256 {sha})")


if __name__ == "__main__":
    main()
