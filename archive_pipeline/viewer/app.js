/* Archive viewer — runs from file://, data arrives via <script> tags as window.ARCHIVE (project wording in viewer/config.js). */
(() => {
  const A = window.ARCHIVE || {};
  const items = A.items || [], segs = A.segs || [], places = A.places || [], hits = A.hits || [];
  const CFG = Object.assign({ lite: false, title: "Archive", placeholder: "Search everything", slug: "archive",
                              watchlistLabel: "Places" }, A.config || {});
  const FIELDS = {
    post: "Post text, video title, description, hashtags",
    caption: "YouTube captions (uploaded or YouTube's automatic ones)",
    speech: "Speech transcribed by Whisper",
    onscreen: "Text read off video frames (OCR)",
    photo: "Text read off photos and thumbnails (OCR)",
    alt: "Platform-generated image description (Facebook/Instagram)",
    location: "Instagram location tag",
    comment: "Comments by other users",
  };
  const $ = (s, el = document) => el.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const norm = s => String(s || "").normalize("NFD").replace(/[\u0300-\u036f\u0591-\u05BD\u05BF-\u05C7\u200e\u200f]/g, "").toLowerCase();
  const hms = t => { if (t == null) return ""; t = Math.floor(t); const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0"); };
  const placeById = Object.fromEntries(places.map(p => [p.id, p]));

  // ---------------------------------------------------------------- storage (tags)
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };
  const KEY = CFG.slug + ".";
  let tags = store.get(KEY + "tags", {});
  const saveTags = () => { store.set(KEY + "tags", tags); $("#tagcount").textContent = Object.keys(tags).length; };

  // ---------------------------------------------------------------- setup
  const M = A.meta || {};
  document.title = CFG.title;
  $("#title").textContent = CFG.title;
  $("#q").placeholder = CFG.placeholder;
  $("#places-tab").textContent = CFG.watchlistLabel;
  document.querySelectorAll(".wl-label").forEach(el => { el.textContent = CFG.watchlistLabel; });
  $("#meta").textContent = `${items.length.toLocaleString()} items · ${segs.length.toLocaleString()} text passages · built ${(M.built || "").slice(0, 10)}${CFG.lite ? " · Lite (no local video)" : ""}`;
  const accounts = [...new Set(items.map(i => `${i.p}: ${i.a}`))].sort();
  $("#f-account").innerHTML += accounts.map(a => `<option>${esc(a)}</option>`).join("");
  $("#f-fields").innerHTML = Object.keys(FIELDS).map(f =>
    `<label title="${esc(FIELDS[f])}"><input type="checkbox" value="${f}" checked> <span class="badge ${f}">${f}</span></label>`).join("");
  $("#fielddefs").innerHTML = Object.entries(FIELDS).map(([f, d]) => `<dt><span class="badge ${f}">${f}</span></dt><dd>${esc(d)}</dd>`).join("");
  $("#p-cat").innerHTML += [...new Set(places.map(p => p.category))].sort().map(c => `<option>${esc(c)}</option>`).join("");
  $("#reviewer").value = store.get(KEY + "reviewer", "");
  $("#reviewer").addEventListener("change", e => store.set(KEY + "reviewer", e.target.value.trim()));
  saveTags();

  document.querySelectorAll("nav button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
  function showTab(t) {
    document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
    document.querySelectorAll(".tab").forEach(s => s.classList.toggle("on", s.id === "tab-" + t));
    if (t === "places") renderPlaces();
    if (t === "tags") renderTags();
  }

  // hits per item (strong only) for the "only items with place hits" filter and card badges
  const itemHits = new Map();
  for (const h of hits) {
    if (h[7]) continue;
    const s = itemHits.get(h[1]) || new Set(); s.add(h[0]); itemHits.set(h[1], s);
  }

  // ---------------------------------------------------------------- search
  let normSegs = null;

  // ---------------------------------------------------------------- query → groups of alternatives
  // A query becomes a list of groups; a passage matches when every group has at least one
  // alternative in it. With "spelling variants & near matches" on:
  //   * a known watchlist name (any recorded spelling, one or more words) expands to all its spellings;
  //   * any other word of 4+ letters also matches archive words that differ only by common
  //     transliteration swaps (kh/ch/h, tz/ts/z, ph/f, y/i, doubled letters, apostrophes) or by 1–2 typos.
  // "Quoted phrases" are always matched exactly.
  const wordRe = /[\p{L}\p{N}]+/gu;
  const known = new Map();  // normalised spelling -> Set of all spellings of that entry
  for (const p of places) {
    const forms = [...new Set([p.name, ...(p.variants || []), ...(p.native || [])].map(norm).filter(Boolean))];
    for (const f of forms) { const s = known.get(f) || new Set(); forms.forEach(x => s.add(x)); known.set(f, s); }
  }
  const skel = w => w.replace(/['’`-]/g, "").replace(/kh|ch/g, "h").replace(/tz|ts/g, "z").replace(/ph/g, "f")
    .replace(/y/g, "i").replace(/(.)\1+/g, "$1").replace(/h$/, "");
  function editDist(a, b, max) {  // Levenshtein with an early exit once every cell exceeds max
    if (Math.abs(a.length - b.length) > max) return max + 1;
    let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i]; let best = i;
      for (let j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
        if (cur[j] < best) best = cur[j];
      }
      if (best > max) return max + 1;
      prev = cur;
    }
    return prev[b.length];
  }
  let vocab = null;  // word -> {n: count, k: skeleton}, built on the first fuzzy search
  function nearWords(term) {
    if (term.length < 4 || !/^[\p{L}\p{N}'’-]+$/u.test(term)) return [];
    if (!vocab) {
      vocab = new Map();
      for (const n of normSegs) for (const w of n.match(wordRe) || []) {
        const v = vocab.get(w); v ? v.n++ : vocab.set(w, { n: 1, k: null });
      }
    }
    // A word the archive already uses often is probably spelled right: only accept transliteration-equal
    // spellings, same-stem inflections, and rare 1-edit variants (typos / transcription errors).
    // A word the archive barely uses is probably misspelled: accept anything within 1–2 edits.
    const t = skel(term), max = term.length < 5 ? 0 : term.length < 8 ? 1 : 2, out = [];
    const tn = vocab.get(term)?.n || 0, real = tn >= 3;
    const stem = (a, b) => { let i = 0; while (i < a.length && a[i] === b[i]) i++; return i; };
    const ENDINGS = new Set(["", "s", "es", "ed", "d", "ing", "er", "ers", "e", "ies", "ied", "ment", "ments"]);
    for (const [w, v] of vocab) {
      if (w === term || Math.abs(w.length - term.length) > max + 2) continue;
      const k = v.k ??= skel(w);
      if (k === t) { out.push([w, 0, v.n]); continue; }
      if (k[0] !== t[0]) continue;
      if (real) {
        const st = stem(w, term);
        const inflect = st >= 4 && ENDINGS.has(w.slice(st)) && ENDINGS.has(term.slice(st));  // harvest ~ harvests/harvesting
        const rareTypo = v.n <= 2 && v.n * 10 <= tn && editDist(t, k, 1) <= 1;
        if (inflect || rareTypo) out.push([w, inflect ? 0.5 : 1, v.n]);
      } else {
        const d = editDist(t, k, max);
        if (d <= max) out.push([w, d, v.n]);
      }
    }
    return out.sort((a, b) => a[1] - b[1] || b[2] - a[2]).slice(0, 20).map(x => x[0]);
  }
  function parseQuery(q, fuzzy = $("#f-fuzzy").checked) {
    const groups = [], loose = [];
    const flush = () => {  // unquoted words: find known names (longest first), the rest word by word
      for (let i = 0; i < loose.length;) {
        let took = 0;
        if (fuzzy) for (let len = Math.min(6, loose.length - i); len >= 1; len--) {
          const phrase = loose.slice(i, i + len).join(" ");
          if (known.has(phrase)) {
            groups.push({ label: phrase, alts: [phrase, ...[...known.get(phrase)].filter(x => x !== phrase)], known: true });
            took = len; break;
          }
        }
        if (!took) {
          const w = loose[i];
          groups.push({ label: w, alts: [w, ...(fuzzy && normSegs ? nearWords(w) : [])], near: true });
          took = 1;
        }
        i += took;
      }
      loose.length = 0;
    };
    q.replace(/"([^"]+)"|(\S+)/g, (_, quoted, word) => {
      if (quoted) { flush(); const t = norm(quoted).trim(); if (t) groups.push({ label: `"${t}"`, alts: [t], exact: true }); }
      else { const t = norm(word); if (t) loose.push(t); }
    });
    flush();
    for (const g of groups) {  // matchers: the typed text as a substring; expansions as whole words
      g.test = g.alts.map((a, i) => {
        if (i === 0 && !g.known) return n => n.includes(a);
        const re = new RegExp(`(?<![\\p{L}\\p{N}])${a.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\p{L}\\p{N}])`, "u");
        return n => n.includes(a) && re.test(n);
      });
      g.match = n => g.test.some(f => f(n));
    }
    return groups;
  }
  const allAlts = groups => groups.flatMap(g => g.alts);
  function itemPasses(it, ii) {
    const pf = $("#f-platform").value, af = $("#f-account").value, from = $("#f-from").value, to = $("#f-to").value;
    if (pf && it.p !== pf) return false;
    if (af && `${it.p}: ${it.a}` !== af) return false;
    if (from && (it.d || "") < from) return false;
    if (to && (it.d || "9999") > to) return false;
    if ($("#f-hits").checked && !itemHits.has(ii)) return false;
    return true;
  }
  function highlight(text, terms) {
    const alts = terms.filter(Boolean).sort((a, b) => b.length - a.length);
    if (!alts.length) return esc(text);
    const re = new RegExp(alts.map(t => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "giu");
    return esc(text).replace(re, m => `<mark>${m}</mark>`);
  }
  function snippet(text, terms, len = 260) {
    const n = norm(text);
    const hits = terms.map(t => n.indexOf(t)).filter(i => i >= 0);
    const pos = hits.length ? Math.min(...hits) : 0;
    const a = Math.max(0, pos - 90), b = Math.min(text.length, a + len);
    return (a ? "…" : "") + text.slice(a, b) + (b < text.length ? "…" : "");
  }
  function thumbOf(it) {
    const m = (it.m || []).find(m => m.th) || {};
    return m.th ? `<img loading="lazy" src="${esc(m.th)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumbph'}))">` : `<div class="thumbph"></div>`;
  }
  function tagDot(it) { const t = tags[it.i]; return t ? `<span class="tagdot ${t.tag.replace(" ", "-")}" title="${esc(t.tag)}"></span>` : ""; }
  const ctxList = [];  // match context per rendered card: {segs: [passage idx], terms: [...]}
  function card(ii, snips, extra = "", ctx = null) {
    const it = items[ii], ph = itemHits.get(ii);
    const k = ctx ? ctxList.push(ctx) - 1 : "";
    return `<div class="card" data-item="${ii}" data-ctx="${k}">${thumbOf(it)}<div>
      <h3 dir="auto">${esc(it.ti || "(untitled)")}${tagDot(it)}</h3>
      <div class="sub">${esc(it.d || "")} · ${esc(it.p)} · ${esc(it.a || "")}${it.loc ? " · 📍 " + esc(it.loc) : ""}${ph ? " · places: " + [...ph].slice(0, 5).map(p => esc(placeById[p]?.name)).join(", ") + (ph.size > 5 ? "…" : "") : ""}${extra}</div>
      ${snips}</div></div>`;
  }
  function snipHtml(s, terms, ii) {
    return `<div class="snip" dir="auto"><span class="badge ${s[2]}">${s[2]}</span> ${s[3] != null ? `<a class="t" data-item="${ii}" data-media="${s[1] ?? ""}" data-t="${s[3]}">${hms(s[3])}</a>` : ""}${highlight(snippet(s[4], terms), terms)}</div>`;
  }

  let lastResults = [], shown = 0, lastTerms = [];
  function runSearch() {
    const q = $("#q").value.trim();
    const fields = new Set([...document.querySelectorAll("#f-fields input:checked")].map(i => i.value));
    const t0 = performance.now();
    if (q) normSegs ??= segs.map(s => norm(s[4]));
    const groups = parseQuery(q);
    const byItem = new Map();
    if (groups.length) {
      for (let i = 0; i < segs.length; i++) {
        const s = segs[i];
        if (!fields.has(s[2])) continue;
        const n = normSegs[i];
        if (!groups.every(g => g.match(n))) continue;
        if (!byItem.has(s[0])) { if (!itemPasses(items[s[0]], s[0])) continue; byItem.set(s[0], []); }
        byItem.get(s[0]).push(i);
      }
    } else {
      items.forEach((it, ii) => { if (itemPasses(it, ii)) byItem.set(ii, []); });
    }
    lastResults = [...byItem.entries()].sort((a, b) => (b[1].length - a[1].length) || String(items[b[0]].d).localeCompare(String(items[a[0]].d)));
    if (!groups.length) lastResults.sort((a, b) => String(items[b[0]].d).localeCompare(String(items[a[0]].d)));
    const n = lastResults.reduce((a, r) => a + r[1].length, 0);
    const also = groups.filter(g => g.alts.length > 1).map(g =>
      `<b>${esc(g.label)}</b> → ${g.alts.slice(1, 11).map(esc).join(", ")}${g.alts.length > 11 ? ` +${g.alts.length - 11} more` : ""}${g.known ? " <span class='badge weak'>known spellings</span>" : ""}`);
    $("#status").innerHTML = groups.length
      ? `${lastResults.length.toLocaleString()} items, ${n.toLocaleString()} passages (${Math.round(performance.now() - t0)} ms)` +
        (also.length ? `<div class="also">Also searched: ${also.join(" · ")}</div>` : "")
      : `${lastResults.length.toLocaleString()} items — type a search, or browse newest first`;
    lastTerms = allAlts(groups);
    $("#results").innerHTML = ""; shown = 0; more(lastTerms);
  }
  function more(terms = lastTerms) {
    const chunk = lastResults.slice(shown, shown + 50);
    shown += chunk.length;
    $("#results").insertAdjacentHTML("beforeend", chunk.map(([ii, idx]) =>
      card(ii, idx.slice(0, 4).map(i => snipHtml(segs[i], terms, ii)).join("") + (idx.length > 4 ? `<div class="muted">+${idx.length - 4} more passages</div>` : ""),
           "", idx.length ? { segs: idx, terms } : null)).join(""));
    $("#results .more")?.remove();
    if (shown < lastResults.length) $("#results").insertAdjacentHTML("beforeend", `<button class="more">Show more (${(lastResults.length - shown).toLocaleString()} left)</button>`);
  }
  $("#f-fuzzy").checked = store.get(KEY + "fuzzy", true);
  $("#f-fuzzy").addEventListener("change", e => store.set(KEY + "fuzzy", e.target.checked));
  $("#qform").addEventListener("submit", e => { e.preventDefault(); runSearch(); });
  document.querySelectorAll(".filters input, .filters select").forEach(el => {
    if (el.closest("#tab-search")) el.addEventListener("change", runSearch);
  });

  // ---------------------------------------------------------------- places
  let curPlace = null;
  function renderPlaces() {
    const weak = $("#p-weak").checked, cat = $("#p-cat").value, q = norm($("#p-q").value);
    const cnt = new Map();
    for (const h of hits) {
      if (h[7] && !weak) continue;
      const c = cnt.get(h[0]) || { n: 0, items: new Set() }; c.n++; c.items.add(h[1]); cnt.set(h[0], c);
    }
    const rows = places.filter(p => (!cat || p.category === cat) && (!q || norm(p.name + " " + p.variants.join(" ") + " " + p.native.join(" ")).includes(q)))
      .map(p => [p, cnt.get(p.id) || { n: 0, items: new Set() }])
      .filter(([p, c]) => c.n || q)
      .sort((a, b) => b[1].items.size - a[1].items.size || a[0].name.localeCompare(b[0].name));
    $("#places tbody").innerHTML = rows.map(([p, c]) =>
      `<tr data-place="${p.id}" class="${p.id === curPlace ? "on" : ""}"><td>${esc(p.name)}${p.ambiguous ? ' <span class="badge weak" title="also a common word or name">ambiguous</span>' : ""}${p.native.length ? ` <span class="muted" dir="rtl">${esc(p.native[0])}</span>` : ""}</td>
       <td>${esc(p.category)}</td><td>${esc(p.region)}</td><td class="n">${c.items.size}</td><td class="n">${c.n}</td></tr>`).join("")
      || `<tr><td colspan="5" class="muted">No mentions found${weak ? "" : " (try including ambiguous matches)"}.</td></tr>`;
  }
  function renderPlaceHits(pid) {
    curPlace = pid;
    const p = placeById[pid], weak = $("#p-weak").checked;
    const hs = hits.filter(h => h[0] === pid && (weak || !h[7]));
    const byItem = new Map();
    for (const h of hs) { const a = byItem.get(h[1]) || []; a.push(h); byItem.set(h[1], a); }
    const ranked = [...byItem.entries()].sort((a, b) => Math.max(...b[1].map(h => h[6])) - Math.max(...a[1].map(h => h[6])) || String(items[b[0]].d).localeCompare(String(items[a[0]].d)));
    const terms = [];
    $("#placehits").innerHTML = `<h2>${esc(p.name)} <span class="muted">${esc(p.category)} · ${esc(p.region)}${p.note ? " · " + esc(p.note) : ""}</span></h2>
      <p class="muted">Matched names: ${esc([...p.variants, ...p.native].join(", "))}</p>
      <p class="muted">${ranked.length} items · ${hs.length} mentions</p>` +
      ranked.map(([ii, hh]) => card(ii, hh.slice(0, 5).map(h => {
        const s = segs[h[5]];
        return snipHtml(s, [norm(matchedIn(s[4], p))], ii).replace('class="snip"', `class="snip"${h[7] ? ' title="ambiguous match"' : ""}`) ;
      }).join("") + (hh.length > 5 ? `<div class="muted">+${hh.length - 5} more</div>` : ""), "",
        { segs: [...new Set(hh.map(h => h[5]))], terms: [...p.variants, ...p.native, p.name].map(norm) })).join("");
    renderPlaces();
  }
  function matchedIn(text, p) {
    const n = norm(text);
    return [...p.variants, ...p.native].map(norm).sort((a, b) => b.length - a.length).find(v => n.includes(v)) || "";
  }
  $("#places").addEventListener("click", e => { const tr = e.target.closest("tr[data-place]"); if (tr) renderPlaceHits(tr.dataset.place); });
  ["#p-weak", "#p-cat"].forEach(s => $(s).addEventListener("change", () => { renderPlaces(); if (curPlace) renderPlaceHits(curPlace); }));
  $("#p-q").addEventListener("input", renderPlaces);

  // ---------------------------------------------------------------- item drawer
  const segsByItem = new Map();
  segs.forEach((s, i) => { const a = segsByItem.get(s[0]) || []; a.push(i); segsByItem.set(s[0], a); });

  let openCtx = null;  // match context of the open item (kept when the drawer re-renders, e.g. after tagging)
  function openItem(ii, media = null, t = null, ctx = undefined) {
    if (ctx !== undefined || +$("#item").dataset.item !== ii) openCtx = ctx || null;
    const it = items[ii], c = openCtx;
    const mine = segsByItem.get(ii) || [];
    const matched = c ? c.segs.filter(i => segs[i] && segs[i][0] === ii) : [];
    const mset = new Set(matched), terms = c ? c.terms : [];
    const matchedMedia = new Set(matched.map(i => segs[i][1]).filter(x => x != null));
    const vids = it.m.map((m, mi) => [m, mi]).filter(([m]) => m.k === "video");
    const photos = it.m.map((m, mi) => [m, mi]).filter(([m]) => m.k !== "video" && (m.f || m.th));
    // first matched moment in a video: cue the player there (without autoplay)
    const cue = matched.map(i => segs[i]).find(s => s[3] != null && (s[1] == null || it.m[s[1]]?.k === "video"));
    const mi0 = media != null && media !== "" ? +media : cue && cue[1] != null ? cue[1] : (vids[0]?.[1] ?? null);
    const tg = tags[it.i] || {};
    const byField = {};
    mine.forEach(i => (byField[segs[i][2]] ||= []).push(i));
    const noMedia = !it.m.length ? "This post has no media files in the archive" + (it.err ? ` (the platform returned “${esc(it.err)}”)` : "") + " — open the original post."
      : !it.m.some(m => m.f || m.th) ? "This post's media isn't in the archive (not downloaded or unavailable) — open the original post." : "";
    const why = matched.length ? `<div class="why"><b>Why this matched</b> <span class="muted">(${matched.length} passage${matched.length > 1 ? "s" : ""})</span>
        ${matched.slice(0, 8).map(i => {
          const s = segs[i], m = s[1] != null ? it.m[s[1]] : null;
          const thumb = m && m.k !== "video" && (m.th || m.f) ? `<img class="whythumb" src="${esc(m.th || m.f)}" alt="">` : "";
          return `<div class="snip" dir="auto">${thumb}<span class="badge ${s[2]}">${s[2]}</span> ${s[3] != null ? `<a class="t" data-item="${ii}" data-media="${s[1] ?? ""}" data-t="${s[3]}">${hms(s[3])}</a>` : ""}${highlight(snippet(s[4], terms, 320), terms)}</div>`;
        }).join("")}${matched.length > 8 ? `<div class="muted">+${matched.length - 8} more, highlighted below</div>` : ""}</div>` : "";
    $("#item-body").innerHTML = `
      <div class="itemhead"><h2 dir="auto">${esc(it.ti || "(untitled)")}</h2>
        <div class="muted">${esc(it.d || "")} · ${esc(it.p)} · ${esc(it.a || "")}${it.loc ? " · 📍 " + esc(it.loc) : ""} ·
          <a href="${esc(it.u)}" target="_blank" rel="noopener">original post ↗</a> · id ${esc(it.i)}${it.err ? " · ⚠ platform returned: " + esc(it.err) : ""}</div></div>
      <div class="tagbar">
        ${["relevant", "needs review", "not relevant"].map(x => `<button data-tag="${x}" class="${x.replace(" ", "-")} ${tg.tag === x ? "on" : ""}">${x}</button>`).join("")}
        <input id="tagnote" placeholder="note (what's shown, where, when…)" value="${esc(tg.note || "")}">
        ${tg.tag ? `<button data-tag="">clear</button>` : ""}
      </div>
      ${why}
      ${noMedia ? `<div class="nomedia">${noMedia} <a href="${esc(it.u)}" target="_blank" rel="noopener">original post ↗</a></div>` : ""}
      ${vids.length ? vids.map(([m, mi]) => playerHtml(it, m, mi)).join("") : ""}
      ${photos.length ? `<div class="gallery">${photos.map(([m, mi]) => `<a href="${esc(m.f || m.th)}" target="_blank" class="${matchedMedia.has(mi) ? "matched" : ""}" title="${matchedMedia.has(mi) ? "text in this image matched" : ""}"><img loading="lazy" src="${esc(m.th || m.f)}" alt=""></a>`).join("")}</div>` : ""}
      ${it.x ? `<h3>Text</h3><div class="posttext" dir="auto">${terms.length ? highlight(it.x, terms) : esc(it.x)}</div>` : ""}
      ${Object.entries(byField).filter(([f]) => f !== "post").map(([f, ids]) => `<h3><span class="badge ${f}">${f}</span> <span class="muted">${esc(FIELDS[f])}</span></h3>
        <div class="segs">${ids.map(i => { const s = segs[i]; return `<div class="snip${mset.has(i) ? " hl" : ""}" dir="auto" data-t="${s[3] ?? ""}" data-media="${s[1] ?? ""}">${s[3] != null ? `<a class="t" data-item="${ii}" data-media="${s[1] ?? ""}" data-t="${s[3]}">${hms(s[3])}</a>` : ""}${mset.has(i) ? highlight(s[4], terms) : esc(s[4])}</div>`; }).join("")}</div>`).join("")}`;
    $("#item").hidden = false;
    $("#item").dataset.item = ii;
    document.querySelectorAll("#item .segs").forEach(box => {  // bring the first matched line into view in each list
      const el = box.querySelector(".snip.hl");
      if (el) box.scrollTop = el.offsetTop - box.offsetTop - 24;
    });
    if (t != null) seek(mi0, t);
    else if (cue) seek(mi0, cue[3], false);
  }
  function playerHtml(it, m, mi) {
    const tlink = it.p === "youtube" ? (t => `${it.u}&t=${Math.floor(t)}s`) : (() => m.r || it.u);
    const fallback = `<a href="${esc(tlink(0))}" target="_blank" rel="noopener">watch on ${esc(it.p)} ↗</a>`;
    if (CFG.lite || !m.f) return `<div class="player muted" data-media="${mi}">Video not included in this copy — ${fallback}</div>`;
    return `<div class="player" data-media="${mi}"><video controls preload="metadata" src="${esc(m.f)}" ${m.th ? `poster="${esc(m.th)}"` : ""}
      onerror="this.parentNode.innerHTML='Local video missing — ${esc(fallback).replace(/'/g, "\\'")}'"></video></div>`;
  }
  function seek(mi, t, play = true) {
    const it = items[+$("#item").dataset.item];
    const v = document.querySelector(`.player[data-media="${mi}"] video`) || document.querySelector(".player video");
    if (!v) {
      if (it.p === "youtube") window.open(`${it.u}&t=${Math.floor(t)}s`, "_blank", "noopener");
      return;
    }
    const go = () => { v.currentTime = +t; if (play) { v.play().catch(() => {}); v.scrollIntoView({ block: "nearest", behavior: "smooth" }); } };
    v.readyState >= 1 ? go() : v.addEventListener("loadedmetadata", go, { once: true });
    if (play) document.querySelectorAll(".segs .snip").forEach(el => el.classList.toggle("now", el.dataset.t === String(t)));
  }
  const ctxOf = el => { const k = el.closest(".card")?.dataset.ctx; return k !== undefined && k !== "" ? ctxList[+k] : null; };
  document.addEventListener("click", e => {
    const tl = e.target.closest("a.t");
    if (tl) {
      e.preventDefault(); e.stopPropagation();
      const ii = +tl.dataset.item;
      if ($("#item").hidden || +$("#item").dataset.item !== ii) openItem(ii, tl.dataset.media, +tl.dataset.t, ctxOf(tl));
      else seek(tl.dataset.media, +tl.dataset.t);
      return;
    }
    const c = e.target.closest(".card");
    if (c && !e.target.closest("a")) return openItem(+c.dataset.item, null, null, ctxOf(c));
    if (e.target.matches("button.more")) return more();
    const tb = e.target.closest(".tagbar button");
    if (tb) {
      const it = items[+$("#item").dataset.item];
      if (tb.dataset.tag) tags[it.i] = { tag: tb.dataset.tag, note: $("#tagnote").value, reviewer: $("#reviewer").value.trim(), at: new Date().toISOString() };
      else delete tags[it.i];
      saveTags(); openItem(+$("#item").dataset.item);
    }
  });
  document.addEventListener("change", e => {
    if (e.target.id === "tagnote") {
      const it = items[+$("#item").dataset.item];
      const cur = tags[it.i] || { tag: "needs review" };
      tags[it.i] = { ...cur, note: e.target.value, reviewer: $("#reviewer").value.trim(), at: new Date().toISOString() };
      saveTags();
    }
  });
  $("#close-item").addEventListener("click", closeItem);
  $("#item").addEventListener("click", e => { if (e.target.id === "item") closeItem(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#item").hidden) closeItem(); });
  function closeItem() { document.querySelectorAll("#item video").forEach(v => v.pause()); $("#item").hidden = true; if ($("#tab-tags").classList.contains("on")) renderTags(); }

  // ---------------------------------------------------------------- tags tab
  const idx = Object.fromEntries(items.map((it, i) => [it.i, i]));
  function renderTags() {
    const f = $("#t-filter").value;
    const rows = Object.entries(tags).filter(([id, t]) => idx[id] != null && (!f || t.tag === f))
      .sort((a, b) => String(b[1].at).localeCompare(String(a[1].at)));
    $("#taglist").innerHTML = rows.length ? rows.map(([id, t]) => card(idx[id],
      `<div class="snip"><b>${esc(t.tag)}</b>${t.note ? " — " + esc(t.note) : ""} <span class="muted">${esc(t.reviewer || "")} ${esc((t.at || "").slice(0, 16).replace("T", " "))}</span></div>`)).join("")
      : `<p class="muted">Nothing tagged yet. Open an item and choose relevant / needs review / not relevant.</p>`;
  }
  $("#t-filter").addEventListener("change", renderTags);
  const csvCell = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
  $("#export-tags").addEventListener("click", () => {
    const head = ["item_id", "platform", "date", "title", "url", "tag", "note", "reviewer", "tagged_at"];
    const lines = [head.join(",")].concat(Object.entries(tags).filter(([id]) => idx[id] != null).map(([id, t]) => {
      const it = items[idx[id]];
      return [id, it.p, it.d, it.ti, it.u, t.tag, t.note, t.reviewer, t.at].map(csvCell).join(",");
    }));
    const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob),
      download: `${CFG.slug}-review-${($("#reviewer").value.trim() || "tags").replace(/\W+/g, "_")}-${new Date().toISOString().slice(0, 10)}.csv` });
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  $("#import-tags").addEventListener("change", async e => {
    const f = e.target.files[0]; if (!f) return;
    const rows = parseCsv((await f.text()).replace(/^\ufeff/, ""));
    const h = rows.shift() || []; const col = n => h.indexOf(n);
    let added = 0, kept = 0;
    for (const r of rows) {
      const id = r[col("item_id")]; if (!id || idx[id] == null) continue;
      const inc = { tag: r[col("tag")], note: r[col("note")], reviewer: r[col("reviewer")], at: r[col("tagged_at")] };
      const cur = tags[id];
      if (!cur || String(inc.at) > String(cur.at)) { tags[id] = inc; added++; } else kept++;  // newest tag wins
    }
    saveTags(); renderTags(); e.target.value = "";
    alert(`Imported ${added} tags (${kept} older duplicates ignored).`);
  });
  function parseCsv(text) {
    const rows = []; let row = [], cell = "", q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) { if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') q = false; else cell += c; }
      else if (c === '"') q = true;
      else if (c === ",") { row.push(cell); cell = ""; }
      else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
      else cell += c;
    }
    if (cell || row.length) { row.push(cell); rows.push(row); }
    return rows;
  }

  runSearch();
})();
