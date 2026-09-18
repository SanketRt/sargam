"""
The review UI: http://localhost:7000

The CLI is for capture and compile; this is for the two things that genuinely
want a screen. Answering a placement question is easier when you can see the
timeline it is being placed into, and reviewing a paragraph is only meaningful
next to the fragments it came from, with the model's inferences marked.

Standard library only -- no framework, no build step, no dependencies. One
process, one sqlite connection guarded by a lock, because it is a single-user
tool on localhost.
"""

from __future__ import annotations

import json
import pathlib
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ask as A
import entities as E
import ground as G
import publish
import render as R
import store as S
from timeline import YEAR, fmt

_lock = threading.Lock()
_st: S.Store | None = None
_manuscript: pathlib.Path | None = None


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

def snapshot() -> dict:
    st = _st
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


# -------------------------------------------------------------------- actions

def do_answer(body: dict) -> dict:
    st = _st
    import placement as P
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


def do_entity(body: dict) -> dict:
    E.answer_entity(_st, body["unresolved_id"], body.get("entity_id"),
                    body.get("new_name"))
    return {"ok": True}


def do_freeze(body: dict) -> dict:
    _st.set_frozen(body["paragraph_id"], bool(body["frozen"]))
    return {"ok": True}


def do_compile(body: dict) -> dict:
    st = _st
    book = R.compile_book(st, style=body.get("style", "plain"))
    rep = publish.write(st, book, _manuscript)
    sha = publish.commit(rep["repo"], f"compile: {book['rendered']} rendered")
    st.record_compile(sha, book["rendered"], book["cached"], book["flagged"])
    return {"ok": True, "rendered": book["rendered"], "cached": book["cached"],
            "flagged": book["flagged"], "dropped": rep["dropped"],
            "commit": sha}


ROUTES = {"/api/answer": do_answer, "/api/entity": do_entity,
          "/api/freeze": do_freeze, "/api/compile": do_compile}


# ----------------------------------------------------------------------- page

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
  <button onclick="compile()">Recompile</button>
</header>
<main>
  <section>
    <h2>Timeline</h2>
    <div id="timeline"></div>
  </section>
  <div style="min-width:0">
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
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m){const t=document.getElementById('toast');t.textContent=m;
 t.classList.add('on');setTimeout(()=>t.classList.remove('on'),1800);}
async function post(path,body){
 const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify(body)});return r.json();}
async function load(){S=await(await fetch('/api/state')).json();draw();}

function draw(){
 const p=S.pending,c=S.counts;
 document.getElementById('stats').innerHTML=
  `<b>${S.events.length}</b> events &middot; <b>${S.paragraphs.length}</b> paragraphs `
  +`&middot; <b>${p.placement}</b> unplaced &middot; <b>${p.flagged}</b> flagged`
  +(c.unsupported?` &middot; <b style="color:var(--bad)">${c.unsupported}</b> unsupported`:'');

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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            with _lock:
                data = snapshot()
            self._send(200, dumps(data), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        fn = ROUTES.get(self.path)
        if fn is None:
            self._send(404, b"not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            with _lock:
                out = fn(body)
        except Exception as exc:
            out = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        self._send(200, dumps(out), "application/json")


def serve(db_path, manuscript_dir, port: int = 7000,
          open_browser: bool = True) -> None:
    global _st, _manuscript
    _st = S.Store(db_path)
    _manuscript = pathlib.Path(manuscript_dir)
    url = f"http://localhost:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"sargam review UI on {url}   (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
        _st.close()
