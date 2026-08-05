#!/usr/bin/env python3
"""Serve the sidecar Phase 0 golden review as a phone-friendly web app.

The Phase 0 gate needs 200 hand-checked GERS-to-QID decisions with zero false
accepts.  The frozen review set, the append-only verdict file and the validator
all exist; what did not exist was a way to actually make the 200 decisions.  The
generated review sheet is a 275 KB markdown file, which is the one shape you
cannot work through on a phone.

This serves the same frozen review set as one decision per screen, records
verdicts straight into the real verdict file, and decides nothing itself.

Contract, enforced here so the validator never sees a malformed row:

  * verdict is one of accept / reject / needs_more_evidence
  * reviewer is non-empty
  * reviewed_at is ISO 8601 WITH a UTC offset (generated server-side)
  * a note is mandatory for reject and needs_more_evidence
  * one row per decision_id -- the validator rejects repeats, so changing your
    mind REPLACES the row rather than appending a second one
  * `meta` is never touched: it binds the file to the golden-set sha256, and the
    validator refuses any verdict file bound to different inputs

Writes are atomic (temp file + rename), so a tap on a flaky phone connection
cannot truncate the verdict file.

Usage:

    scripts/serve_sidecar_phase0_review.py             # loopback only
    scripts/serve_sidecar_phase0_review.py --port 8765

Loopback by default and unauthenticated. To reach it from another device,
forward it deliberately (`ssh -L`, or `tailscale serve`) rather than binding a
routable address.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SET = ROOT / "benchmarks/2026-08-04-sidecar-phase0-golden-review-set-v1.json"
VERDICTS = ROOT / "benchmarks/2026-08-04-sidecar-phase0-golden-verdicts-v1.json"
VERDICT_VALUES = ("accept", "reject", "needs_more_evidence")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json_atomic(path: Path, value: dict) -> None:
    """Temp file in the same directory, then rename -- never a partial file."""
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(value, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def name_tokens(value: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", value or "").lower()
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return {w for w in re.sub(r"[^\w]+", " ", folded, flags=re.UNICODE).split() if w}


SCRIPT_MARKERS = ("THAI", "CJK", "HIRAGANA", "KATAKANA", "HANGUL", "CYRILLIC",
                  "ARABIC", "GREEK", "HEBREW", "DEVANAGARI")


def script_of(value: str) -> str:
    """Coarse script label, from the first alphabetic character."""
    for ch in value or "":
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            for marker in SCRIPT_MARKERS:
                if marker in name:
                    return marker
            return "LATIN"
    return "NONE"


def name_relation(overture: dict, wikidata: dict) -> dict:
    """Token-level name agreement, because the frozen flag is whole-string.

    `comparison.has_normalized_name_overlap` is true only when the two sides
    share an entire normalized name.  That reports `Universite Laval` against
    `Laval University`, and `State Game Lodge (Custer State Park)` against
    `State Game Lodge`, as having NO overlap -- 77 of the 134 rows it flags
    actually share tokens, and 44 of those are one name wholly containing the
    other.  Reviewing all 134 at the same suspicion level wastes attention on
    the corroborated ones and dilutes it on the 57 that genuinely share nothing.
    """
    left: set[str] = set()
    for name in overture.get("normalized_names") or overture.get("names") or []:
        left |= name_tokens(name)
    right: set[str] = set()
    for name in wikidata.get("normalized_labels") or wikidata.get("labels") or []:
        right |= name_tokens(name)
    shared = left & right
    if not left or not right:
        relation = "EMPTY_SIDE"
    elif not shared:
        relation = "ZERO"
    elif left <= right or right <= left:
        relation = "SUBSET"
    else:
        relation = "PARTIAL"
    # Script matters more than token overlap here. 46 of the 57 zero-overlap
    # rows are cross-script -- Overture holds the Thai/Greek/CJK name and
    # Wikidata the romanization, so token overlap CANNOT fire and its absence
    # carries no information. Only the 11 same-script rows are genuine risk.
    left_script = script_of((overture.get("names") or [""])[0])
    right_script = script_of((wikidata.get("labels") or [""])[0])
    cross_script = (left_script != right_script
                    and "NONE" not in (left_script, right_script))
    # A snapshot with no label at all cannot be judged on name by anyone.
    qid = wikidata.get("wikidata_qid")
    labels = wikidata.get("labels") or []
    label_missing = not labels or all(lab == qid for lab in labels)
    if relation == "ZERO" and (cross_script or label_missing):
        tier = "UNJUDGEABLE_BY_NAME"
    elif relation == "ZERO":
        tier = "REVIEW"
    else:
        tier = "NAME_CORROBORATED"
    return {
        "relation": relation,
        "risk_tier": tier,
        "shared": sorted(shared),
        "overture_only": sorted(left - right),
        "wikidata_only": sorted(right - left),
        "shared_count": len(shared),
        "overture_count": len(left),
        "wikidata_count": len(right),
        "overture_script": left_script,
        "wikidata_script": right_script,
        "cross_script": cross_script,
        "label_missing": label_missing,
    }


def card_payload(decision: dict, verdict: dict | None) -> dict:
    """Only what the screen needs -- the full set is 750 KB and mostly prose."""
    overture = decision.get("overture") or {}
    wikidata = decision.get("wikidata") or {}
    comparison = decision.get("comparison") or {}
    provisional = decision.get("provisional") or {}
    return {
        "decision_id": decision["decision_id"],
        "review_order": decision.get("review_order"),
        "risk_class": decision.get("risk_class"),
        "risk_flags": [
            {"flag": f.get("flag"), "explanation": f.get("explanation")}
            for f in (decision.get("risk_flags") or [])
        ],
        "review_guidance": decision.get("review_guidance"),
        "review_urls": decision.get("review_urls") or [],
        "overture": {
            "names": overture.get("names") or [],
            "country": overture.get("country"),
            "gers_id": overture.get("gers_id"),
            "release": overture.get("release"),
            "coordinate": overture.get("coordinate"),
            "sources": overture.get("sources") or [],
            "categories": overture.get("categories"),
        },
        "wikidata": {
            "qid": wikidata.get("wikidata_qid"),
            "labels": wikidata.get("labels") or [],
            "description": wikidata.get("description"),
            "coordinate": wikidata.get("coordinate"),
            "p1968_claims": wikidata.get("p1968_claims") or [],
        },
        "comparison": {
            "distance_km": comparison.get("distance_km"),
            "distance_gate_km": comparison.get("distance_gate_km"),
            "distance_over_gate": comparison.get("distance_over_gate"),
            "distance_null_reason": comparison.get("distance_null_reason"),
            "has_normalized_name_overlap":
                comparison.get("has_normalized_name_overlap"),
            "shared_normalized_names":
                comparison.get("shared_normalized_names") or [],
            "matched_source_identifiers":
                comparison.get("matched_source_identifiers") or [],
        },
        "name_relation": name_relation(overture, wikidata),
        "provisional": {
            "decision": provisional.get("decision"),
            "rule_id": provisional.get("rule_id"),
            "rule_statement": provisional.get("rule_statement"),
            "match_method": provisional.get("match_method"),
            "automatic_acceptance": provisional.get("automatic_acceptance"),
        },
        "verdict": verdict,
    }


PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><title>Sidecar Phase 0 review</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<style>
:root{--bg:#fbfbfa;--fg:#1b1b1a;--dim:#6a6a68;--line:#e2e2df;--card:#fff;
--ok:#0f7b4f;--no:#b0322a;--maybe:#8a5a08;--accent:#2f5fd0}
@media(prefers-color-scheme:dark){:root{--bg:#131314;--fg:#e9e9e7;--dim:#9a9a97;
--line:#2c2c2e;--card:#1c1c1e;--ok:#41c98a;--no:#ff7a70;--maybe:#e0a83c;--accent:#7aa2f7}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding-bottom:env(safe-area-inset-bottom)}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
padding:10px 14px;z-index:5}
.bar{height:5px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;background:var(--ok);width:0%}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between}
.muted{color:var(--dim);font-size:13px}
main{padding:14px;max-width:760px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:14px;margin-bottom:12px}
h2{font-size:19px;margin:0 0 2px}
h3{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);
margin:0 0 6px}
.pill{display:inline-block;font-size:12px;padding:2px 9px;border-radius:999px;
border:1px solid var(--line);color:var(--dim);margin:0 6px 6px 0}
.pill.warn{color:var(--maybe);border-color:var(--maybe)}
.pill.acc{color:var(--ok);border-color:var(--ok)}
.grid{display:grid;grid-template-columns:1fr;gap:12px}
@media(min-width:620px){.grid{grid-template-columns:1fr 1fr}}
.kv{display:flex;gap:8px;font-size:14px;padding:3px 0;border-bottom:1px dotted var(--line)}
.kv b{font-weight:600;color:var(--dim);min-width:92px;font-size:13px}
.kv span{word-break:break-word}
a{color:var(--accent)}
.acts{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px}
button{font:inherit;font-weight:600;padding:14px 6px;border-radius:12px;
border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.ok{border-color:var(--ok);color:var(--ok)}
button.no{border-color:var(--no);color:var(--no)}
button.maybe{border-color:var(--maybe);color:var(--maybe)}
button:active{transform:scale(.97)}
button[disabled]{opacity:.4}
.nav{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
textarea,input{font:inherit;width:100%;padding:10px;border-radius:10px;
border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.done{border-color:var(--ok)}
.warnbox{border-left:3px solid var(--maybe);padding-left:10px;font-size:14px;
color:var(--dim);margin-top:8px}
.gate{border-left:3px solid var(--no);padding-left:10px;font-size:14px;
color:var(--no);margin-top:8px}
</style></head><body>
<header>
  <div class="row"><b>Sidecar Phase 0</b><span class="muted" id="prog">…</span></div>
  <div class="bar"><i id="barfill"></i></div>
  <div class="row" style="margin-top:8px">
    <input id="who" placeholder="your name (reviewer)" style="max-width:60%">
    <button id="jump" style="padding:8px 12px;font-size:14px">Next undecided</button>
  </div>
  <div class="row" style="margin-top:6px">
    <label class="muted"><input type="checkbox" id="onlyzero" style="width:auto">
      only rows needing real judgement</label>
    <span class="muted" id="filtn"></span>
  </div>
</header>
<main id="app">loading…</main>
<script>
let D=[],V=[],i=0,who=localStorage.getItem('reviewer')||'';
const $=s=>document.querySelector(s);
$('#who').value=who;
$('#who').oninput=e=>{who=e.target.value;localStorage.setItem('reviewer',who)};
$('#onlyzero').checked=localStorage.getItem('onlyzero')==='1';
$('#onlyzero').onchange=e=>{localStorage.setItem('onlyzero',e.target.checked?'1':'0');
  const at=V[i]&&V[i].decision_id;refilter();
  const k=V.findIndex(d=>d.decision_id===at);i=k>=0?k:0;
  const u=V.findIndex(d=>!d.verdict);if(k<0&&u>=0)i=u;draw()};
// The queue is risk-ordered, not risk-filtered. Hiding the corroborated rows
// concentrates attention on the 57 that share no token with their QID -- but
// the gate still needs a verdict on ALL 200, so hidden is not decided.
function refilter(){
  V=$('#onlyzero').checked
    ? D.filter(d=>d.name_relation.risk_tier==='REVIEW') : D;
  $('#filtn').textContent=`${V.length} shown`;
}
$('#jump').onclick=()=>{const n=V.findIndex(d=>!d.verdict);if(n>=0){i=n;draw()}};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const kv=(k,v)=>v==null||v===''||(Array.isArray(v)&&!v.length)?'':
  `<div class="kv"><b>${esc(k)}</b><span>${esc(Array.isArray(v)?v.join(', '):v)}</span></div>`;
// The frozen flag is whole-string equality; this is the token truth.
function nameBadge(r){
  if(!r) return '';
  if(r.relation==='SUBSET') return `<span class="pill acc">names nest · ${r.shared_count} shared</span>`;
  if(r.relation==='PARTIAL') return `<span class="pill acc">${r.shared_count} of ${Math.max(r.overture_count,r.wikidata_count)} tokens shared</span>`;
  if(r.relation==='ZERO'&&r.label_missing) return '<span class="pill warn">Wikidata has NO label</span>';
  if(r.relation==='ZERO'&&r.cross_script) return `<span class="pill">cross-script · ${esc(r.overture_script)}→${esc(r.wikidata_script)}</span>`;
  if(r.relation==='ZERO') return '<span class="pill warn">NO shared token, same script</span>';
  return '<span class="pill warn">a name side is empty</span>';
}

async function load(){
  const r=await fetch('./api/decisions');const j=await r.json();
  D=j.decisions;refilter();const n=V.findIndex(d=>!d.verdict);i=n>=0?n:0;draw();
}
function draw(){
  if(!V.length){$('#app').innerHTML='<div class="card">Nothing matches the filter.</div>';return}
  i=Math.min(i,V.length-1);
  const d=V[i],done=D.filter(x=>x.verdict).length,vdone=V.filter(x=>x.verdict).length;
  $('#prog').textContent=`${done}/${D.length} decided · ${vdone}/${V.length} here · #${d.review_order}`;
  $('#barfill').style.width=(100*done/D.length)+'%';
  const o=d.overture,w=d.wikidata,c=d.comparison,p=d.provisional;
  const dist=c.distance_km==null?`— (${esc(c.distance_null_reason||'no distance')})`
    :`${c.distance_km.toFixed(3)} km${c.distance_over_gate?' ⚠ over gate':''}`;
  const gate=p.decision==='accepted'?`<div class="gate"><b>Provisionally ACCEPTED.</b>
    Rejecting this contradicts the matcher and fails the Phase 0 gate — which is
    exactly the finding, if it is true. Do not soften it.</div>`:'';
  $('#app').innerHTML=`
  <div class="card">
    <h2>${esc(o.names[0]||'(no Overture name)')} <span class="muted">↔</span>
        ${esc(w.labels[0]||'(no Wikidata label)')}</h2>
    <div class="muted">${esc(d.decision_id)}</div>
    <div style="margin-top:8px">
      <span class="pill ${p.decision==='accepted'?'acc':'warn'}">${esc(p.decision)}</span>
      <span class="pill">${esc(d.risk_class)}</span>
      ${nameBadge(d.name_relation)}
    </div>
    ${gate}
  </div>

  <div class="grid">
    <div class="card"><h3>Overture</h3>
      ${kv('names',o.names)}${kv('category',o.categories)}${kv('country',o.country)}
      ${kv('coord',o.coordinate?`${o.coordinate.latitude}, ${o.coordinate.longitude}`:null)}
      ${kv('GERS',o.gers_id)}${kv('release',o.release)}
      ${kv('sources',o.sources.map(s=>`${s.dataset}:${s.record_id}`))}
    </div>
    <div class="card"><h3>Wikidata</h3>
      ${kv('labels',w.labels)}${kv('QID',w.qid)}${kv('description',w.description)}
      ${kv('coord',w.coordinate?`${w.coordinate.latitude}, ${w.coordinate.longitude}`:null)}
      ${kv('P1968',w.p1968_claims.map(x=>`${x.value}${x.matches_overture_source_record?' ✓ matches':''}`))}
    </div>
  </div>

  <div class="card"><h3>Comparison</h3>
    ${kv('distance',dist)}
    ${kv('shared words',d.name_relation.shared)}
    ${d.name_relation.cross_script?kv('scripts',d.name_relation.overture_script+' vs '+d.name_relation.wikidata_script+' — token overlap cannot fire'):''}
    ${d.name_relation.label_missing?kv('note','the frozen Wikidata snapshot carries NO label for this QID'):''}
    ${kv('Overture only',d.name_relation.overture_only)}
    ${kv('Wikidata only',d.name_relation.wikidata_only)}
    ${kv('shared names',c.shared_normalized_names)}
    ${kv('matched ids',c.matched_source_identifiers.map(s=>`${s.dataset}:${s.record_id}`))}
    ${kv('rule',p.rule_id)}
    <div class="warnbox">${esc(p.rule_statement)}</div>
    ${d.review_guidance?`<div class="warnbox"><b>Guidance.</b> ${esc(d.review_guidance)}</div>`:''}
    ${d.risk_flags.map(f=>`<div class="warnbox"><b>${esc(f.flag)}.</b> ${esc(f.explanation)}</div>`).join('')}
  </div>

  <div class="card"><h3>Open</h3>
    ${d.review_urls.map(u=>`<div style="padding:6px 0"><a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a></div>`).join('')}
  </div>

  <div class="card ${d.verdict?'done':''}">
    <h3>${d.verdict?'Recorded — '+esc(d.verdict.verdict):'Your call'}</h3>
    <textarea id="note" rows="2" placeholder="note (required to reject or ask for more evidence)">${esc(d.verdict?d.verdict.note:'')}</textarea>
    <div class="acts">
      <button class="ok" onclick="send('accept')">Accept</button>
      <button class="no" onclick="send('reject')">Reject</button>
      <button class="maybe" onclick="send('needs_more_evidence')">Need more</button>
    </div>
    <div class="nav">
      <button onclick="go(-1)" ${i===0?'disabled':''}>← Prev</button>
      <button onclick="go(1)" ${i===V.length-1?'disabled':''}>Next →</button>
    </div>
  </div>`;
  window.scrollTo(0,0);
}
function go(n){i=Math.min(V.length-1,Math.max(0,i+n));draw()}
async function send(v){
  if(!who.trim()){alert('Put your name in the reviewer box first.');return}
  const note=$('#note').value.trim();
  if(v!=='accept'&&!note){alert('A note is required to '+v.replace(/_/g,' ')+'.');return}
  const r=await fetch('./api/verdict',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({decision_id:V[i].decision_id,verdict:v,reviewer:who.trim(),note})});
  const j=await r.json();
  if(!r.ok){alert(j.error||'failed');return}
  V[i].verdict=j.verdict;
  const n=V.findIndex(d=>!d.verdict);
  if(n>=0&&n!==i){i=n}else if(i<V.length-1){i++}
  draw();
}
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    review: dict = {}
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # quieter than the default
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value: dict) -> None:
        self._send(code, json.dumps(value).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/decisions":
            verdicts = {r["decision_id"]: r
                        for r in load_json(VERDICTS).get("verdicts", [])}
            self._json(200, {"decisions": [
                card_payload(d, verdicts.get(d["decision_id"]))
                for d in self.review["decisions"]]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/") != "/api/verdict":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "bad JSON"})
            return

        identifier = body.get("decision_id")
        verdict = body.get("verdict")
        reviewer = str(body.get("reviewer") or "").strip()
        note = str(body.get("note") or "").strip()
        known = {d["decision_id"] for d in self.review["decisions"]}
        if identifier not in known:
            self._json(400, {"error": f"unknown decision {identifier!r}"})
            return
        if verdict not in VERDICT_VALUES:
            self._json(400, {"error": f"invalid verdict {verdict!r}"})
            return
        if not reviewer:
            self._json(400, {"error": "reviewer is required"})
            return
        if verdict != "accept" and not note:
            self._json(400, {"error": f"{verdict} requires a note"})
            return

        row = {
            "decision_id": identifier,
            "verdict": verdict,
            "reviewer": reviewer,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "note": note,
        }
        # The validator rejects a repeated decision_id, so a changed mind
        # REPLACES the row in place rather than appending a second one. `meta`
        # is passed through untouched: it binds the file to the golden-set hash.
        current = load_json(VERDICTS)
        rows = [r for r in current.get("verdicts", [])
                if r.get("decision_id") != identifier]
        rows.append(row)
        current["verdicts"] = rows
        write_json_atomic(VERDICTS, current)
        self._json(200, {"verdict": row, "decided": len(rows)})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1",
                        help="default: loopback only. Pass an explicit address "
                             "to expose it; nothing here is authenticated.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    review = load_json(REVIEW_SET)
    Handler.review = review
    verdicts = load_json(VERDICTS)
    decided = len(verdicts.get("verdicts", []))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"review set : {REVIEW_SET.name} ({len(review['decisions'])} decisions)")
    print(f"verdicts   : {VERDICTS.name} ({decided} already recorded)")
    print(f"serving    : http://{args.host}:{args.port}/")
    print("stop with TaskStop or Ctrl-C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
