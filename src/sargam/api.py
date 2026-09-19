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


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
 content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>sargam</title><style>
:root{color-scheme:light dark;--bg:#fbfaf8;--fg:#1a1a18;--dim:#6b6a66;
 --line:#e3e0da;--card:#fff;--accent:#7a5c3e;--warn:#9a6b1e;--bad:#a33b2c;
 --ok:#3d6b45;--pad:16px}
@media(prefers-color-scheme:dark){:root{--bg:#16151a;--fg:#e8e6e1;--dim:#95928c;
 --line:#2e2c33;--card:#1e1d23;--accent:#c9a87c;--warn:#d4a24c;--bad:#e0705c;
 --ok:#7fb089}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 ui-sans-serif,
 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:12px var(--pad);border-bottom:1px solid var(--line);
 display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;
 position:sticky;top:env(safe-area-inset-top,0px);background:var(--bg);z-index:5}
h1{font-size:15px;margin:0;letter-spacing:.14em;text-transform:uppercase;
 color:var(--accent);font-weight:600}
.stat{color:var(--dim);font-size:12px}
.stat b{color:var(--fg);font-weight:600}
button{font:inherit;padding:5px 12px;border:1px solid var(--line);
 border-radius:7px;background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--bg);border-color:var(--accent)}
main{display:grid;grid-template-columns:minmax(240px,1fr) minmax(0,2fr);
 gap:var(--pad);padding:var(--pad);align-items:start}
@media(max-width:820px){main{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:var(--pad);min-width:0}
h2{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);
 margin:0 0 12px;font-weight:600}
.ev{display:flex;gap:10px;padding:5px 0;border-bottom:1px solid var(--line);
 font-size:13px}
.ev:last-child{border:0}
.yr{color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap;
 min-width:76px}
.loose .yr{color:var(--warn)}
.para{border:1px solid var(--line);border-radius:9px;padding:13px;
 margin-bottom:13px}
.para.frozen{border-color:var(--accent)}
.chap{font-size:11px;color:var(--dim);letter-spacing:.1em;margin-bottom:7px;
 text-transform:uppercase}
.s-supported{}
.s-inferred{border-bottom:1.5px dotted var(--warn)}
.s-unsupported{background:color-mix(in oklab,var(--bad) 18%,transparent);
 text-decoration:line-through}
.s-unchecked{border-bottom:1.5px dotted var(--dim)}
.meta{margin-top:10px;font-size:12px;color:var(--dim);display:flex;gap:12px;
 flex-wrap:wrap;align-items:center}
details{margin-top:8px}
summary{cursor:pointer;font-size:12px;color:var(--dim)}
.src{margin-top:7px;padding:9px;background:var(--bg);border-radius:7px;
 font-size:12.5px;white-space:pre-wrap}
.q{background:var(--card);border:1px solid var(--accent);border-radius:10px;
 padding:var(--pad);margin-bottom:var(--pad)}
.q p{margin:0 0 12px;font-size:15px}
.opt{display:block;width:100%;text-align:left;margin-bottom:6px}
.opt.guess{border-color:var(--accent)}
.tag{font-size:10.5px;padding:1px 7px;border-radius:99px;border:1px solid var(--line)}
.tag.w{color:var(--warn);border-color:var(--warn)}
.tag.o{color:var(--ok);border-color:var(--ok)}
.empty{color:var(--dim);font-size:13px}
.signin{text-align:center;padding:40px 16px;max-width:30rem;margin:0 auto}
.signin h3{margin:0 0 10px;font-size:19px}
.signin .empty{margin:0 auto 20px}
.err{color:var(--bad);font-size:13px;margin:0 0 16px}
.btn{display:inline-block;padding:9px 18px;border-radius:8px;
 background:var(--accent);color:var(--bg);text-decoration:none;font-weight:600}
.keybar{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:12px var(--pad);margin-bottom:var(--pad);display:flex;gap:10px;
 align-items:center;flex-wrap:wrap;font-size:13px}
.keybar input{font:inherit;padding:6px 10px;border:1px solid var(--line);
 border-radius:7px;background:var(--bg);color:var(--fg);min-width:14rem;flex:1}
.keybar .note{color:var(--dim);font-size:12px;flex-basis:100%;margin:0}
#who a{color:var(--accent)}
#toast{position:fixed;left:50%;transform:translateX(-50%);
 bottom:calc(18px + env(safe-area-inset-bottom,0px));background:var(--fg);
 color:var(--bg);padding:8px 16px;border-radius:99px;font-size:13px;opacity:0;
 transition:opacity .2s;pointer-events:none;z-index:20}
#toast.on{opacity:1}
</style></head><body>
<header>
  <h1>sargam</h1>
  <span class="stat" id="stats"></span>
  <span style="flex:1"></span>
  <span class="stat" id="who"></span>
  <button onclick="compile()">Recompile</button>
</header>
<main>
  <section>
    <h2>Timeline</h2>
    <div id="timeline"></div>
  </section>
  <div style="min-width:0">
    <div id="keybar"></div>
    <div id="question"></div>
    <section>
      <h2>Manuscript</h2>
      <div id="paras"></div>
    </section>
  </div>
</main>
<div id="toast"></div>
<script>
let S=null;
// Injected by the server. Empty locally; "/projects/sargam" behind the proxy.
const _b=window.__SARGAM_BASE__||'';
const BASE=_b.endsWith('/')?_b.slice(0,-1):_b;
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m){const t=document.getElementById('toast');t.textContent=m;
 t.classList.add('on');setTimeout(()=>t.classList.remove('on'),1800);}
async function post(path,body){
 const r=await fetch(BASE+path,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify(body)});return r.json();}
let ME=null;
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
 document.getElementById('question').innerHTML='';
 document.getElementById('timeline').innerHTML='';
 document.getElementById('paras').innerHTML=
  `<div class="signin"><h3>Your memoir, in order</h3>
   <p class="empty">Chat in, chronologically ordered book out. Sign in to
   keep your material between visits \u2014 it stays yours, and you can export
   or delete all of it at any time.</p>
   ${msg?`<p class="err">${esc(msg)}</p>`:''}
   <a class="btn" href="${BASE}/auth/login">Continue with Google</a></div>`;
}

function draw(){
 const p=S.pending,c=S.counts;
 document.getElementById('stats').innerHTML=
  `<b>${S.events.length}</b> events &middot; <b>${S.paragraphs.length}</b> paragraphs `
  +`&middot; <b>${p.placement}</b> unplaced &middot; <b>${p.flagged}</b> flagged`
  +(c.unsupported?` &middot; <b style="color:var(--bad)">${c.unsupported}</b> unsupported`:'');
 const who=document.getElementById('who');
 who.innerHTML=(ME&&ME.signed_in&&!ME.single_user)
  ?`${esc(ME.email||ME.name||'')} &nbsp;<a href="${BASE}/auth/logout">Sign out</a>`:'';

 document.getElementById('timeline').innerHTML=S.events.length?S.events.map(e=>
  `<div class="ev ${e.loose?'loose':''}"><span class="yr">${esc(e.year)}</span>
   <span>${esc(e.summary)}</span></div>`).join(''):
  '<p class="empty">No events yet. Capture something with <code>sargam add</code>.</p>';

 const q=S.question,qd=document.getElementById('question');
 if(!q){qd.innerHTML='';}
 else if(q.kind==='placement'){
  qd.innerHTML=`<div class="q"><p>${esc(q.prompt)}</p>`
   +q.options.map((o,i)=>`<button class="opt ${q.guess===i?'guess':''}"
     onclick="answer(${i})">${esc(o.label)}${q.guess===i?' &nbsp;<span class="tag">guess</span>':''}</button>`).join('')
   +(q.rationale?`<div class="meta">${esc(q.rationale)}</div>`:'')+`</div>`;
 }else{
  qd.innerHTML=`<div class="q"><p>${esc(q.prompt)}</p>`
   +q.options.map((o,i)=>`<button class="opt" onclick="bind(${i})">${esc(o.label)}
     ${o.n_events?`<span class="tag">${o.n_events}</span>`:''}</button>`).join('')+`</div>`;
 }

 document.getElementById('keybar').innerHTML=keyBar();

 document.getElementById('paras').innerHTML=S.paragraphs.length?S.paragraphs.map(pa=>{
  const body=pa.sentences.map(s=>
    `<span class="s-${s.verdict}" title="${esc(s.verdict)}${s.evidence.length?' ← '+esc(s.evidence.join(', ')):''}">${esc(s.text)}</span>`).join(' ');
  const srcs=Object.entries(pa.sources).map(([k,v])=>
    `<div class="src"><b>${esc(k)}</b><br>${esc(v)}</div>`).join('');
  return `<div class="para ${pa.frozen?'frozen':''}">
   <div class="chap">${esc(pa.chapter)}${pa.frozen?' &middot; frozen':''}
    ${pa.frozen&&pa.dirty?' &middot; <span class="tag w">sources changed</span>':''}</div>
   <div>${body}</div>
   <div class="meta">
     <button onclick="freeze('${pa.id}',${!pa.frozen})">${pa.frozen?'Unfreeze':'Freeze'}</button>
     <span>${pa.derived_from.length} event(s)</span>
   </div>
   <details><summary>Sources</summary>${srcs||'<p class="empty">none</p>'}</details>
  </div>`;}).join(''):
  '<p class="empty">Nothing compiled yet. Press Recompile.</p>';
}

function keyBar(){
 if(!ME||!ME.signed_in||ME.single_user||!ME.can_store_keys) return '';
 if(ME.has_key) return `<div class="keybar">
   <span>Anthropic key <b>${esc(ME.hint||'')}</b> \u2014 used only for your own
   compiles.</span><span style="flex:1"></span>
   <button onclick="clearKey()">Remove</button></div>`;
 return `<div class="keybar">
   <input id="apikey" type="password" autocomplete="off" placeholder="sk-ant-...">
   <button class="primary" onclick="saveKey()">Save key</button>
   <p class="note">Stored encrypted and used only for your compiles. Without
   one, sargam still solves your timeline and asks placement questions; it
   just writes plainer prose.</p></div>`;
}
async function postStatus(path,body){
 const r=await fetch(BASE+path,{method:'POST',
  headers:{'content-type':'application/json'},body:JSON.stringify(body||{})});
 let j={}; try{j=await r.json();}catch(e){}
 return {status:r.status, body:j};
}
async function saveKey(){
 const el=document.getElementById('apikey'), key=(el.value||'').trim();
 if(!key){toast('paste a key first');return;}
 el.value=''; toast('checking\u2026');
 const r=await postStatus('/api/key',{api_key:key});
 toast(r.status===200?'key saved':(r.body.detail||'could not save that key'));
 await load();
}
async function clearKey(){
 await postStatus('/api/key/clear',{});
 toast('key removed'); await load();
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
async function compile(){toast('compiling…');const r=await post('/api/compile',{});
 toast(r.commit?`${r.rendered} rendered, ${r.cached} cached → ${r.commit}`
   :`${r.cached} cached — no change`);await load();}
load();
</script></body></html>"""
