"""Loopback-only UI for Phase 3.7 private triage and reviewer-1 labels."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("INNER_VIEW_TEST_ASSET_ROOT", str(ROOT / "webapp/backend/tests/fixtures/runtime_assets"))

from webapp.backend.services.gl_catalog import load_gl_catalog  # noqa: E402
from webapp.backend.services.private_labeling_workspace import (  # noqa: E402
    LabelValidationError, PrivateLabelingWorkspace, WorkspaceError,
)
from webapp.backend.services.reviewer_1_pilot import Reviewer1Pilot  # noqa: E402


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Private Labeling Workspace</title>
<style>
body{font:14px system-ui;margin:0;background:#10141c;color:#e8edf5}header{padding:12px 18px;background:#192131;display:flex;gap:18px}
main{display:grid;grid-template-columns:320px 1fr 440px;height:calc(100vh - 55px)}section{padding:12px;overflow:auto;border-right:1px solid #344056}
button,select,input,textarea{background:#202b3e;color:#fff;border:1px solid #52617a;border-radius:4px;padding:7px}button{cursor:pointer}
.case{padding:8px;margin:5px 0;background:#1a2231}.active{outline:2px solid #76a9fa}iframe,img{width:100%;height:72vh;object-fit:contain;background:#fff}
textarea{width:96%;height:58vh;font-family:monospace}.error{color:#ff8d8d}.ok{color:#8ee6a8}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.source-evidence{margin:12px 0;padding:12px;background:#192131;border:1px solid #52617a}.candidate{padding:8px 0;border-top:1px solid #344056}.muted{color:#aebbd0}
</style></head><body><header><b>PHASE 3.7 — Blind Reviewer 1</b><span id="status"></span><span>No InnerView output · no reviewer 2</span></header>
<main><section><h3>Pilot 20 queue</h3><div id="queue"></div></section><section><h3 id="caseTitle">Select a document</h3><div id="meta"></div><div id="sourceMetadata" class="source-evidence"></div><div id="preview"></div></section>
<section><h3>Triage decision</h3><div class="grid"><select id="decision"><option>keep_for_labeling</option><option>replace_with_reserve</option><option>exclude_unadjudicable</option><option>wrong_cohort</option><option>duplicate_missed</option><option>pages_belong_to_same_document</option><option>pages_should_be_split</option><option>needs_manual_rotation</option><option>needs_better_source</option></select><input id="reviewer" placeholder="reviewer id"></div><input id="reason" style="width:96%;margin-top:8px" placeholder="required reason"><select id="replacement" style="width:100%;margin-top:8px"><option value="">automatic same-cohort reserve replacement</option></select><button onclick="triage()" style="margin-top:8px">Save triage decision</button><hr>
<h3>Reviewer 1 blind label</h3><input id="dataset" value="selected_120_v1" placeholder="dataset version"><div><button onclick="activity('resume')">Resume timer</button> <button onclick="activity('pause')">Pause timer</button></div><textarea id="label"></textarea><div><button onclick="saveLabel('in_progress')">Autosave draft</button> <button onclick="saveLabel('complete')">Validate & complete</button> <button onclick="abandon()">Abandon with reason</button></div><pre id="messages"></pre></section></main>
<script>
const TOKEN='__TOKEN__'; let current=null; let timer=null;
const headers={'Content-Type':'application/json','X-Workspace-Token':TOKEN};
const unknown={status:'unknown',reason:'not visible'};
function template(){return {document:{document_family:unknown,vendor_name:unknown,vendor_normalization:unknown,invoice_number:unknown,invoice_date:unknown,due_date:unknown,property:unknown,service_address:unknown,bill_or_credit:unknown,total:{status:'unreadable',reason:'not yet labeled'},expected_route:unknown,document_completeness:unknown,reviewer_confidence:0,economic_responsibility:{payment_source:unknown,economic_bearer:unknown,settlement_treatment:unknown,allocation_scope:unknown,allocation_targets:[],evidence:[]}},line_items:[],unresolved_questions:[]}}
async function api(path,opts={}){let r=await fetch(path,{...opts,headers:{...headers,...(opts.headers||{})}});let data=await r.json();if(!r.ok)throw Error(data.detail||JSON.stringify(data));return data}
async function refresh(){let [q,s]=await Promise.all([api('/api/pilot'),api('/api/pilot-status')]);status.textContent=`Pilot ${s.completed}/20 · remaining ${s.remaining}`;queue.innerHTML='';q.forEach(x=>{let d=document.createElement('div');d.className='case'+(current===x.benchmark_id?' active':'');d.textContent=`${x.benchmark_id} · ${x.pilot_difficulty} · ${x.pilot_completion_status}`;d.onclick=()=>openCase(x);queue.appendChild(d)})}
function addText(parent,tag,text,cls=''){let e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;parent.appendChild(e);return e}
function renderSourceMetadata(data){sourceMetadata.replaceChildren();addText(sourceMetadata,'h3',data.evidence_notice);addText(sourceMetadata,'div',`Original filename: ${data.original_filename}`);addText(sourceMetadata,'div',`Filename stem: ${data.filename_stem}`);addText(sourceMetadata,'div',`Relevant parent folders: ${data.relevant_parent_folders.join(' › ')||'none'}`);const groups={amount:'Parsed amount candidates',property_or_entity:'Parsed property/entity candidates',vendor:'Parsed vendor candidates',expense_category:'Parsed purpose/category candidates',person_or_cardholder:'Parsed person/cardholder candidates',date:'Parsed date candidates',corporate_indicator:'Corporate indicators',reimbursement_indicator:'Reimbursement indicators',unit_or_project:'Unit/project candidates'};Object.entries(groups).forEach(([type,title])=>{let rows=data.candidates.filter(c=>c.candidate_type===type);addText(sourceMetadata,'h4',title);if(!rows.length)addText(sourceMetadata,'div','No candidates','muted');rows.forEach(c=>{let box=addText(sourceMetadata,'div',`${c.normalized_value} (${c.source_kind})`,'candidate');let select=document.createElement('select');['','confirmed','rejected','partially_correct','ambiguous','irrelevant'].forEach(v=>{let o=document.createElement('option');o.value=v;o.textContent=v||'unreviewed';o.selected=v===c.disposition;select.appendChild(o)});select.onchange=()=>reviewCandidate(c.candidate_id,select.value);box.appendChild(select)})});addText(sourceMetadata,'h4','Parser warnings');data.parser_warnings.forEach(w=>addText(sourceMetadata,'div',w,'muted'))}
function openCase(x){current=x.benchmark_id;caseTitle.textContent=x.benchmark_id;meta.textContent=JSON.stringify({cohort:x.cohort,difficulty:x.pilot_difficulty,page_count:x.page_count,quality:x.quality_metrics,warnings:x.inventory_warnings,draft_validation_status:x.draft_validation_status,draft_validation_errors:x.draft_validation_errors,preview_rotation_degrees:x.preview_rotation_degrees},null,2);renderSourceMetadata(x.source_metadata_evidence);preview.innerHTML=`<iframe src="${x.preview_url}?token=${TOKEN}" style="transform:rotate(${x.preview_rotation_degrees||0}deg);transform-origin:center"></iframe>`;label.value=JSON.stringify(x.draft||template(),null,2);activity('start')}
async function activity(action,details={}){if(!current||!reviewer.value)return;await api('/api/pilot-activity',{method:'POST',body:JSON.stringify({benchmark_id:current,reviewer_id:reviewer.value,action,details})})}
async function abandon(){let why=prompt('Required abandonment reason');if(!why)return;await api('/api/pilot-abandon',{method:'POST',body:JSON.stringify({benchmark_id:current,reviewer_id:reviewer.value,reason:why})});await refresh()}
async function reviewCandidate(candidate_id,disposition){if(!disposition)return;try{await api('/api/source-metadata-review',{method:'POST',body:JSON.stringify({benchmark_id:current,candidate_id,reviewer_id:reviewer.value,disposition})});messages.className='ok';messages.textContent='Source metadata interpretation saved; raw evidence unchanged'}catch(e){messages.className='error';messages.textContent=e.message}}
async function triage(){try{await api('/api/triage',{method:'POST',body:JSON.stringify({benchmark_id:current,reviewer:reviewer.value,decision:decision.value,reason:reason.value,replacement_benchmark_id:replacement.value||null})});messages.className='ok';messages.textContent='Triage saved';await refresh()}catch(e){messages.className='error';messages.textContent=e.message}}
async function saveLabel(status){try{let payload={benchmark_id:current,reviewer_id:reviewer.value,dataset_version:dataset.value,completion_status:status,label:JSON.parse(label.value)};let out=await api('/api/label',{method:'POST',body:JSON.stringify(payload)});await activity(status==='complete'?'complete':'autosave',{validation_errors:out.validation_errors});messages.className=out.validation_status==='valid'?'ok':'error';messages.textContent=JSON.stringify({saved:true,validation:out.validation_status,errors:out.validation_errors},null,2);await refresh()}catch(e){messages.className='error';messages.textContent=e.message}}
label.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{if(current&&reviewer.value)saveLabel('in_progress')},1500)});refresh();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    workspace: PrivateLabelingWorkspace
    pilot: Reviewer1Pilot
    token: str

    def do_GET(self):
        route = urlparse(self.path)
        if route.path == "/":
            return self._bytes(HTML.replace("__TOKEN__", self.token).encode(), "text/html; charset=utf-8")
        if not self._authorized(route.query): return self._json({"detail": "unauthorized"}, 403)
        try:
            if route.path == "/api/status": return self._json(self.workspace.status())
            if route.path == "/api/tier-d": return self._json(self.workspace.tier_d_queue())
            if route.path == "/api/pilot": return self._json(self.pilot.queue())
            if route.path == "/api/pilot-status": return self._json(self.pilot.metrics())
            parts = route.path.strip("/").split("/")
            if len(parts) == 5 and parts[:3] == ["api", "private-workspace", "document"] and parts[4] == "preview":
                path = self.workspace.private_document_path(parts[3])
                return self._bytes(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            return self._json({"detail": "not found"}, 404)
        except (WorkspaceError, OSError) as exc: return self._json({"detail": str(exc)}, 400)

    def do_POST(self):
        if not self._authorized(""): return self._json({"detail": "unauthorized"}, 403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if self.path == "/api/triage":
                return self._json(self.workspace.record_triage(body["benchmark_id"], reviewer=body["reviewer"],
                    decision=body["decision"], reason=body["reason"], replacement_benchmark_id=body.get("replacement_benchmark_id")))
            if self.path == "/api/label":
                if body["benchmark_id"] not in self.pilot.pilot_ids(): raise WorkspaceError("document is outside pilot_20_v1")
                return self._json(self.workspace.save_label(body["benchmark_id"], body["label"], reviewer_id=body["reviewer_id"],
                    dataset_version=body["dataset_version"], completion_status=body.get("completion_status", "in_progress")))
            if self.path == "/api/pilot-activity":
                return self._json(self.pilot.record_activity(body["benchmark_id"], reviewer_id=body["reviewer_id"],
                    action=body["action"], details=body.get("details")))
            if self.path == "/api/pilot-abandon":
                return self._json(self.pilot.abandon_draft(body["benchmark_id"], reviewer_id=body["reviewer_id"], reason=body["reason"]))
            if self.path == "/api/source-metadata-review":
                return self._json(self.workspace.review_source_metadata_candidate(body["benchmark_id"], body["candidate_id"],
                    reviewer_id=body["reviewer_id"], disposition=body["disposition"], note=body.get("note")))
            if self.path == "/api/freeze": return self._json(self.workspace.freeze_dataset(body.get("version", "v1")))
            return self._json({"detail": "not found"}, 404)
        except LabelValidationError as exc: return self._json({"detail": "label validation failed", "errors": exc.errors}, 422)
        except (WorkspaceError, KeyError, ValueError, json.JSONDecodeError) as exc: return self._json({"detail": str(exc)}, 400)

    def _authorized(self, query: str) -> bool:
        query_token = next((item.split("=",1)[1] for item in query.split("&") if item.startswith("token=")), "")
        return self.client_address[0] in {"127.0.0.1", "::1"} and (self.headers.get("X-Workspace-Token") == self.token or query_token == self.token)

    def _json(self, payload, status=200):
        self._bytes(json.dumps(payload, default=str).encode(), "application/json", status)

    def _bytes(self, payload: bytes, content_type: str, status=200):
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("INNER_VIEW_PRIVATE_BENCHMARK_ROOT", "")))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--safe-status", type=Path)
    args = parser.parse_args()
    if not str(args.root) or not args.root.is_dir(): raise SystemExit("INNER_VIEW_PRIVATE_BENCHMARK_ROOT is missing")
    _, catalog = load_gl_catalog()
    Handler.workspace = PrivateLabelingWorkspace(args.root, catalog)
    Handler.pilot = Reviewer1Pilot(Handler.workspace)
    Handler.pilot.prepare_manifest()
    if args.safe_status:
        args.safe_status.parent.mkdir(parents=True, exist_ok=True)
        args.safe_status.write_text(Handler.workspace.safe_status_markdown(), encoding="utf-8")
    if args.status_only:
        print(json.dumps(Handler.workspace.status(), indent=2, sort_keys=True))
        return 0
    Handler.token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Private labeling workspace: http://127.0.0.1:{args.port}/")
    print("Loopback only. Detailed labels remain under the private root.")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
