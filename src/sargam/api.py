"""
The review UI's logic, with no transport in it.

Two things serve this: `web.py`, a standard-library server for the local
single-user tool, and `server.py`, a FastAPI app for the hosted one. Neither
owns any behaviour -- they resolve who is asking, build a Ctx, and call in
here. Keeping the handlers framework-agnostic is what stops the local tool
from acquiring a web-framework dependency it has no use for, and stops the
hosted app from drifting into a second implementation of the same product.

A handler takes (ctx, body) and returns a JSON-able dict. It never reads a
global, because in the hosted case there is one store per request and the
credential belongs to whoever is asking.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

from . import ask as A
from . import entities as E
from . import ground as G
from . import publish
from . import render as R
from .timeline import YEAR, fmt


@dataclass
class Ctx:
    """Everything a handler is allowed to touch."""
    store: object                       # store.Store
    manuscript: pathlib.Path
    api_key: str | None = None


def _jsonable(o):
    """Last line of defence for numpy scalars. The bounds come out of a numpy
    matrix, and a stray numpy.float64 reaching json.dumps kills the whole
    request with a 500 rather than degrading one field."""
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def dumps(obj) -> bytes:
    return json.dumps(obj, default=_jsonable).encode()


# --------------------------------------------------------------------- state

def snapshot(ctx: Ctx) -> dict:
    st = ctx.store
    tl = st.tl
    events = []
    for ev in tl.order():
        lo, hi = tl.event_bounds(ev.id)
        events.append({
            "id": ev.id,
            "summary": ev.summary,
            "when": fmt(lo, hi),
            "year": R.coarse_when(tl, ev.id),
            # bool(): numpy comparisons yield numpy.bool_, which json refuses.
            "loose": bool(tl.slack(ev.id) > 2 * YEAR),
            "entities": sorted(ev.entities),
        })

    paras = []
    for row in st.paragraphs():
        vs = G.from_rows(st.groundings(row["id"]))
        derived = json.loads(row["derived_from"])
        paras.append({
            "id": row["id"],
            "chapter": row["chapter"],
            "ordinal": row["ordinal"],
            "frozen": bool(row["frozen"]),
            "dirty": bool(row["dirty"]),
            "derived_from": derived,
            "sentences": G.annotate(row["body"], vs),
            "sources": R.sources_for(st, derived),
        })

    q = None
    eq = A.next_entity(st)
    if eq is not None:
        q = {"kind": "entity", **eq}
    else:
        nxt = A.next_placement(st)
        if nxt is not None:
            _, pq = nxt
            q = {
                "kind": "placement",
                "event_id": pq.event_id,
                "prompt": pq.prompt,
                "rationale": pq.rationale,
                "guess": pq.guess,
                "options": [{"kind": o.kind, "anchor_id": o.anchor_id,
                             "label": o.label} for o in pq.options],
            }

    return {
        "events": events,
        "paragraphs": paras,
        "question": q,
        "pending": A.pending(st),
        "conflicts": [dict(r) for r in st.conflicts()],
        "counts": G.report(st)["counts"],
    }


# ------------------------------------------------------------------- actions

def answer(ctx: Ctx, body: dict) -> dict:
    st = ctx.store
    from . import placement as P
    opts = [P.Option(o["kind"], o["anchor_id"], o["label"])
            for o in body["options"]]
    q = P.Question(event_id=body["event_id"], prompt=body["prompt"],
                   options=opts)
    qid = st.record_question(q.event_id, q.prompt,
                             [{"kind": o.kind, "anchor": o.anchor_id,
                               "label": o.label} for o in opts])
    st.record_answer(qid, body["choice"])
    changed, msg = A.apply_placement(st, q, body["choice"], question_id=qid)
    if changed:
        st.mark_dirty({q.event_id})
    return {"ok": changed, "message": msg}


def bind_entity(ctx: Ctx, body: dict) -> dict:
    E.answer_entity(ctx.store, body["unresolved_id"], body.get("entity_id"),
                    body.get("new_name"))
    return {"ok": True}


def set_frozen(ctx: Ctx, body: dict) -> dict:
    ctx.store.set_frozen(body["paragraph_id"], bool(body["frozen"]))
    return {"ok": True}


def compile_now(ctx: Ctx, body: dict) -> dict:
    st = ctx.store
    book = R.compile_book(st, style=body.get("style", "plain"),
                          api_key=ctx.api_key)
    rep = publish.write(st, book, ctx.manuscript)
    sha = publish.commit(rep["repo"], f"compile: {book['rendered']} rendered")
    st.record_compile(sha, book["rendered"], book["cached"], book["flagged"])
    return {"ok": True, "rendered": book["rendered"], "cached": book["cached"],
            "flagged": book["flagged"], "dropped": rep["dropped"],
            "commit": sha}


ROUTES = {"/api/answer": answer, "/api/entity": bind_entity,
          "/api/freeze": set_frozen, "/api/compile": compile_now}


# ---------------------------------------------------------------------- page

def page(base: str = "") -> str:
    """The UI, told where it lives.

    Behind a reverse proxy the browser's URL keeps a prefix the app never
    sees, so every fetch has to be built from that prefix rather than from
    "/". Injecting it once here is the only place that knowledge belongs --
    a page that hardcodes absolute paths works in development and breaks the
    first time it is proxied, which is the worst order to find out.
    """
    base = (base or "").rstrip("/")
    inject = f'<script>window.__SARGAM_BASE__={json.dumps(base)};</script>'
    return PAGE.replace("<script>", inject + "\n<script>", 1)


# Visual language taken from the site this is proxied from: Inter, a strictly
# neutral palette, 14px radii, a translucent sticky header. It reads as part of
# that site rather than an app bolted onto it.
#
# The theme is stored under the same `theme` key the host site uses. Served
# from the same origin, that means whichever theme someone picked there is the
# one this opens in, and a change here follows them back.
#
# The palette is monochrome by design, which leaves no colour spare for
# meaning -- so the three grounding verdicts get the only hues on the page,
# kept low-chroma so they read as annotation rather than decoration.
PAGE = """<!doctype html>
<html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>sargam</title>
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b0b0c" media="(prefers-color-scheme: dark)">
<script>
  // Before first paint, or the page flashes the wrong theme.
  (function(){try{
    var s=localStorage.getItem('theme');
    var t=s||(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
    document.documentElement.setAttribute('data-theme',t);
  }catch(e){}})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#ffffff; --bg-elev:#ffffff; --bg-soft:#f7f7f8;
  --text:#0e0e10; --text-soft:#3d3d42; --muted:#6a6a70; --faint:#9b9ba2;
  --border:#e7e7ea; --border-strong:#d6d6da;
  --shadow:0 1px 2px rgba(10,10,12,.04), 0 10px 30px rgba(10,10,12,.05);
  --radius:14px; --radius-sm:10px; --radius-pill:999px;
  --header-bg:rgba(255,255,255,.72);
  --maxw:1080px; --t:180ms cubic-bezier(.4,0,.2,1);
  --ok:#2f6f4f; --warn:#8a6d2f; --bad:#9a3b30;
  --warn-soft:rgba(138,109,47,.10); --bad-soft:rgba(154,59,48,.10);
}
[data-theme="dark"]{
  --bg:#0b0b0c; --bg-elev:#141417; --bg-soft:#131316;
  --text:#f3f3f5; --text-soft:#cfcfd4; --muted:#9a9aa1; --faint:#6c6c72;
  --border:#26262b; --border-strong:#36363d;
  --shadow:0 1px 2px rgba(0,0,0,.5), 0 10px 30px rgba(0,0,0,.35);
  --header-bg:rgba(11,11,12,.7);
  --ok:#6fbf95; --warn:#d2ae63; --bad:#e08a7d;
  --warn-soft:rgba(210,174,99,.12); --bad-soft:rgba(224,138,125,.12);
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
 background:var(--bg);color:var(--text);line-height:1.65;font-size:16px;
 -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
 text-rendering:optimizeLegibility;
 padding-bottom:env(safe-area-inset-bottom,0px)}
::selection{background:var(--text);color:var(--bg)}
a{color:inherit}
img,svg{display:block;max-width:100%}
:focus-visible{outline:2px solid var(--text-soft);outline-offset:2px;border-radius:3px}
.sprite{position:absolute;width:0;height:0;overflow:hidden}

.site-header{position:sticky;top:env(safe-area-inset-top,0px);z-index:50;
 background:var(--header-bg);-webkit-backdrop-filter:saturate(170%) blur(12px);
 backdrop-filter:saturate(170%) blur(12px);border-bottom:1px solid var(--border)}
.bar{max-width:var(--maxw);margin:0 auto;padding:.7rem 1.5rem;display:flex;
 align-items:center;gap:.9rem;flex-wrap:wrap}
.wordmark{font-weight:600;font-size:.95rem;letter-spacing:-.01em;
 text-decoration:none;display:inline-flex;align-items:center;gap:.5rem}
.wordmark .icon{width:15px;height:15px;color:var(--muted)}
.stat{color:var(--muted);font-size:.8rem}
.stat b{color:var(--text);font-weight:500;font-variant-numeric:tabular-nums}
.spacer{flex:1}

.icon-btn{display:inline-grid;place-items:center;width:32px;height:32px;
 border:1px solid var(--border);border-radius:var(--radius-sm);
 background:var(--bg-elev);color:var(--muted);cursor:pointer;
 transition:color var(--t),background var(--t),border-color var(--t)}
.icon-btn:hover{color:var(--text);background:var(--bg-soft);border-color:var(--border-strong)}
.icon-btn .icon{width:16px;height:16px}
.theme-toggle .icon-moon{display:block}
.theme-toggle .icon-sun{display:none}
[data-theme="dark"] .theme-toggle .icon-moon{display:none}
[data-theme="dark"] .theme-toggle .icon-sun{display:block}

.btn{display:inline-flex;align-items:center;gap:.5rem;padding:.52rem .95rem;
 font-size:.875rem;font-weight:500;font-family:inherit;line-height:1;
 text-decoration:none;color:var(--text);background:var(--bg-elev);
 border:1px solid var(--border-strong);border-radius:var(--radius-sm);
 cursor:pointer;transition:background var(--t),border-color var(--t),transform var(--t)}
.btn:hover{background:var(--bg-soft);border-color:var(--text-soft);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn .icon{width:15px;height:15px;color:var(--muted)}
.btn-solid{background:var(--text);border-color:var(--text);color:var(--bg)}
.btn-solid .icon{color:var(--bg)}
.btn-solid:hover{background:var(--text-soft);border-color:var(--text-soft);color:var(--bg)}
.btn-sm{padding:.36rem .7rem;font-size:.8rem}

main{max-width:var(--maxw);margin:0 auto;padding:1.5rem;
 display:grid;grid-template-columns:minmax(230px,.85fr) minmax(0,2fr);
 gap:1.25rem;align-items:start}
@media(max-width:860px){main{grid-template-columns:1fr;padding:1.25rem 1rem}}

.card{background:var(--bg-elev);border:1px solid var(--border);
 border-radius:var(--radius);padding:1.15rem;box-shadow:var(--shadow);min-width:0}
.card+.card{margin-top:1.25rem}
.section-head{display:flex;align-items:center;gap:.5rem;margin-bottom:.9rem}
.section-head h2{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;
 color:var(--muted);font-weight:600}
.section-head .icon{width:14px;height:14px;color:var(--faint)}

.ev{display:flex;gap:.7rem;padding:.4rem 0;border-bottom:1px solid var(--border);
 font-size:.875rem;line-height:1.5}
.ev:last-child{border-bottom:0}
.yr{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;
 min-width:5.2rem;font-size:.8rem;padding-top:.08rem}
.ev.loose .yr{color:var(--warn)}
.ev-sum{color:var(--text-soft)}

.para{border:1px solid var(--border);border-radius:var(--radius-sm);
 padding:1rem 1.05rem;margin-bottom:.9rem;background:var(--bg)}
.para:last-child{margin-bottom:0}
.para.frozen{border-color:var(--border-strong)}
.chap{font-size:.7rem;letter-spacing:.08em;text-transform:uppercase;
 color:var(--faint);margin-bottom:.55rem;display:flex;align-items:center;gap:.45rem}
.prose{color:var(--text-soft);line-height:1.75}
.s-inferred{border-bottom:1.5px dotted var(--warn);background:var(--warn-soft)}
.s-unsupported{background:var(--bad-soft);text-decoration:line-through;
 text-decoration-color:var(--bad)}
.s-unchecked{border-bottom:1.5px dotted var(--faint)}
.meta{margin-top:.8rem;padding-top:.7rem;border-top:1px solid var(--border);
 font-size:.78rem;color:var(--muted);display:flex;gap:.7rem;flex-wrap:wrap;
 align-items:center}
.tag{font-size:.68rem;letter-spacing:.04em;padding:.1rem .5rem;
 border-radius:var(--radius-pill);border:1px solid var(--border-strong);
 color:var(--muted);text-transform:uppercase;font-weight:500}
.tag.w{color:var(--warn);border-color:var(--warn)}
details{margin-top:.6rem}
summary{cursor:pointer;font-size:.78rem;color:var(--muted);list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"\\203A";display:inline-block;margin-right:.4rem;
 transition:transform var(--t)}
details[open] summary::before{transform:rotate(90deg)}
.src{margin-top:.55rem;padding:.7rem .8rem;background:var(--bg-soft);
 border-radius:var(--radius-sm);font-size:.82rem;white-space:pre-wrap;
 color:var(--text-soft)}
.src b{color:var(--faint);font-weight:500;font-size:.72rem}

.q{background:var(--bg-elev);border:1px solid var(--border-strong);
 border-radius:var(--radius);padding:1.15rem;margin-bottom:1.25rem;
 box-shadow:var(--shadow)}
.q p.prompt{margin:0 0 .9rem;font-size:1rem;font-weight:500;line-height:1.5}
.opt{display:flex;width:100%;text-align:left;margin-bottom:.45rem;
 justify-content:space-between}
.opt:last-of-type{margin-bottom:0}
.why{margin-top:.7rem;font-size:.78rem;color:var(--muted)}

.keybar{background:var(--bg-elev);border:1px solid var(--border);
 border-radius:var(--radius);padding:.9rem 1.05rem;margin-bottom:1.25rem;
 display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;font-size:.85rem;
 box-shadow:var(--shadow)}
.keybar input{font:inherit;font-size:.85rem;padding:.45rem .7rem;
 border:1px solid var(--border-strong);border-radius:var(--radius-sm);
 background:var(--bg);color:var(--text);min-width:13rem;flex:1}
.keybar input::placeholder{color:var(--faint)}
.keybar .note{color:var(--muted);font-size:.78rem;flex-basis:100%;margin:0;
 line-height:1.55}

.empty{color:var(--muted);font-size:.85rem}
.signin{text-align:center;padding:3rem 1rem;max-width:26rem;margin:0 auto}
.signin h3{font-size:1.15rem;font-weight:600;margin-bottom:.6rem;letter-spacing:-.01em}
.signin p{color:var(--muted);font-size:.9rem;margin-bottom:1.4rem;line-height:1.7}
.err{color:var(--bad);font-size:.82rem;margin-bottom:1rem}
#who{font-size:.8rem;color:var(--muted)}
#toast{position:fixed;left:50%;transform:translateX(-50%) translateY(.5rem);
 bottom:calc(1.25rem + env(safe-area-inset-bottom,0px));background:var(--text);
 color:var(--bg);padding:.5rem 1rem;border-radius:var(--radius-pill);
 font-size:.82rem;opacity:0;transition:opacity var(--t),transform var(--t);
 pointer-events:none;z-index:80;box-shadow:var(--shadow)}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
</style></head><body>
<svg class="sprite" aria-hidden="true">
 <symbol id="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></symbol>
 <symbol id="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></symbol>
 <symbol id="i-arrow-left" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 19-7-7 7-7"/><path d="M19 12H5"/></symbol>
 <symbol id="i-clock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></symbol>
 <symbol id="i-book-open" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/></symbol>
 <symbol id="i-refresh" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></symbol>
</svg>

<header class="site-header">
  <div class="bar">
    <a class="wordmark" href="/projects.html"><svg class="icon"><use href="#i-arrow-left"/></svg>sargam</a>
    <span class="stat" id="stats"></span>
    <span class="spacer"></span>
    <span id="who"></span>
    <button class="btn btn-sm" onclick="compile()">
      <svg class="icon"><use href="#i-refresh"/></svg>Recompile</button>
    <button class="icon-btn theme-toggle" id="theme" aria-label="Switch theme">
      <svg class="icon icon-moon"><use href="#i-moon"/></svg>
      <svg class="icon icon-sun"><use href="#i-sun"/></svg>
    </button>
  </div>
</header>

<main>
  <section class="card">
    <div class="section-head"><svg class="icon"><use href="#i-clock"/></svg><h2>Timeline</h2></div>
    <div id="timeline"></div>
  </section>
  <div style="min-width:0">
    <div id="keybar"></div>
    <div id="question"></div>
    <section class="card">
      <div class="section-head"><svg class="icon"><use href="#i-book-open"/></svg><h2>Manuscript</h2></div>
      <div id="paras"></div>
    </section>
  </div>
</main>
<div id="toast"></div>

<script>
let S=null, ME=null;
const _b=window.__SARGAM_BASE__||'';
const BASE=_b.endsWith('/')?_b.slice(0,-1):_b;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

(function(){
 var root=document.documentElement, t=document.getElementById('theme');
 t.addEventListener('click',function(){
  var n=root.getAttribute('data-theme')==='dark'?'light':'dark';
  root.setAttribute('data-theme',n);
  try{localStorage.setItem('theme',n);}catch(e){}
 });
})();

function toast(m){const t=document.getElementById('toast');t.textContent=m;
 t.classList.add('on');setTimeout(()=>t.classList.remove('on'),1900);}
async function post(path,body){
 const r=await fetch(BASE+path,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify(body||{})});return r.json();}
async function postStatus(path,body){
 const r=await fetch(BASE+path,{method:'POST',
  headers:{'content-type':'application/json'},body:JSON.stringify(body||{})});
 let j={}; try{j=await r.json();}catch(e){}
 return {status:r.status, body:j};}

async function load(){
 const r=await fetch(BASE+'/api/state');
 if(r.status===401){signedOut();return;}
 S=await r.json();
 try{ME=await(await fetch(BASE+'/api/me')).json();}catch(e){ME=null;}
 draw();
}

function signedOut(){
 const p=new URLSearchParams(location.search), err=p.get('error');
 const msg=err==='unverified'
   ? 'That Google account has no verified email address.'
   : err ? 'Sign-in did not complete. Please try again.' : '';
 document.getElementById('stats').textContent='';
 document.getElementById('who').innerHTML='';
 document.getElementById('question').innerHTML='';
 document.getElementById('keybar').innerHTML='';
 document.getElementById('timeline').innerHTML='<p class="empty">\\u2014</p>';
 document.getElementById('paras').innerHTML=
  `<div class="signin"><h3>Your memoir, in order</h3>
   <p>Chat in, chronologically ordered book out. Sign in to keep your material
   between visits. It stays yours, and you can export or delete all of it at
   any time.</p>
   ${msg?`<p class="err">${esc(msg)}</p>`:''}
   <a class="btn btn-solid" href="${BASE}/auth/login">Continue with Google</a></div>`;
}

function keyBar(){
 if(!ME||!ME.signed_in||ME.single_user||!ME.can_store_keys) return '';
 if(ME.has_key) return `<div class="keybar">
   <span>Anthropic key <b>${esc(ME.hint)}</b></span>
   <span class="spacer"></span>
   <button class="btn btn-sm" onclick="clearKey()">Remove</button></div>`;
 return `<div class="keybar">
   <input id="apikey" type="password" autocomplete="off" placeholder="sk-ant-...">
   <button class="btn btn-sm btn-solid" onclick="saveKey()">Save key</button>
   <p class="note">Stored encrypted and used only for your own compiles.
   Without one sargam still solves your timeline and asks placement
   questions; it just writes plainer prose.</p></div>`;
}

function draw(){
 const p=S.pending,c=S.counts;
 document.getElementById('stats').innerHTML=
  `<b>${S.events.length}</b> events &middot; <b>${S.paragraphs.length}</b> paragraphs`
  +(p.placement?` &middot; <b>${p.placement}</b> unplaced`:'')
  +(p.flagged?` &middot; <b>${p.flagged}</b> flagged`:'')
  +(c.unsupported?` &middot; <b style="color:var(--bad)">${c.unsupported}</b> unsupported`:'');
 document.getElementById('who').innerHTML=(ME&&ME.signed_in&&!ME.single_user)
  ?`${esc(ME.email||ME.name||'')} &nbsp;<a href="${BASE}/auth/logout">Sign out</a>`:'';

 document.getElementById('timeline').innerHTML=S.events.length?S.events.map(e=>
  `<div class="ev ${e.loose?'loose':''}"><span class="yr">${esc(e.year)}</span>
   <span class="ev-sum">${esc(e.summary)}</span></div>`).join(''):
  '<p class="empty">Nothing yet. Capture something with <code>sargam add</code>.</p>';

 const q=S.question,qd=document.getElementById('question');
 if(!q){qd.innerHTML='';}
 else if(q.kind==='placement'){
  qd.innerHTML=`<div class="q"><p class="prompt">${esc(q.prompt)}</p>`
   +q.options.map((o,i)=>`<button class="btn opt" onclick="answer(${i})">
     <span>${esc(o.label)}</span>${q.guess===i?'<span class="tag">likely</span>':''}</button>`).join('')
   +(q.rationale?`<div class="why">${esc(q.rationale)}</div>`:'')+`</div>`;
 }else{
  qd.innerHTML=`<div class="q"><p class="prompt">${esc(q.prompt)}</p>`
   +q.options.map((o,i)=>`<button class="btn opt" onclick="bind(${i})">
     <span>${esc(o.label)}</span>${o.n_events?`<span class="tag">${o.n_events}</span>`:''}</button>`).join('')+`</div>`;
 }

 document.getElementById('keybar').innerHTML=keyBar();

 document.getElementById('paras').innerHTML=S.paragraphs.length?S.paragraphs.map(pa=>{
  const body=pa.sentences.map(s=>
    `<span class="s-${s.verdict}" title="${esc(s.verdict)}${s.evidence.length?' \\u2190 '+esc(s.evidence.join(', ')):''}">${esc(s.text)}</span>`).join(' ');
  const srcs=Object.entries(pa.sources).map(([k,v])=>
    `<div class="src"><b>${esc(k)}</b><br>${esc(v)}</div>`).join('');
  return `<div class="para ${pa.frozen?'frozen':''}">
   <div class="chap"><span>${esc(pa.chapter)}</span>
    ${pa.frozen?'<span class="tag">frozen</span>':''}
    ${pa.frozen&&pa.dirty?'<span class="tag w">sources changed</span>':''}</div>
   <div class="prose">${body}</div>
   <div class="meta">
     <button class="btn btn-sm" onclick="freeze('${pa.id}',${!pa.frozen})">${pa.frozen?'Unfreeze':'Freeze'}</button>
     <span>${pa.derived_from.length} event${pa.derived_from.length===1?'':'s'}</span>
   </div>
   <details><summary>Sources</summary>${srcs||'<p class="empty">none</p>'}</details>
  </div>`;}).join(''):
  '<p class="empty">Nothing compiled yet. Press Recompile.</p>';
}

async function answer(i){const q=S.question;
 const r=await post('/api/answer',{event_id:q.event_id,prompt:q.prompt,
  options:q.options,choice:i});toast(r.message||'recorded');await load();}
async function bind(i){const q=S.question,o=q.options[i];
 let name=null;
 if(o.entity_id===null){name=prompt('Name?');if(!name)return;}
 await post('/api/entity',{unresolved_id:q.unresolved_id,entity_id:o.entity_id,
  new_name:name});toast('bound');await load();}
async function freeze(id,f){await post('/api/freeze',{paragraph_id:id,frozen:f});
 toast(f?'frozen':'unfrozen');await load();}
async function saveKey(){
 const el=document.getElementById('apikey'), key=(el.value||'').trim();
 if(!key){toast('paste a key first');return;}
 el.value=''; toast('checking\\u2026');
 const r=await postStatus('/api/key',{api_key:key});
 toast(r.status===200?'key saved':(r.body.detail||'could not save that key'));
 await load();}
async function clearKey(){await postStatus('/api/key/clear',{});
 toast('key removed'); await load();}
async function compile(){toast('compiling\\u2026');const r=await post('/api/compile',{});
 toast(r.commit?`${r.rendered} rendered, ${r.cached} cached`
   :`${r.cached} cached \\u2014 no change`);await load();}
load();
</script></body></html>"""
