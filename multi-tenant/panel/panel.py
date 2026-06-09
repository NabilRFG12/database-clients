#!/usr/bin/env python3
"""Supabase multi-tenant admin panel — a thin UI over tenantctl.

Python stdlib only: no pip, no npm, no build step. Binds to 127.0.0.1 by
default; reach it through an SSH tunnel:

    ssh -L 8800:127.0.0.1:8800 root@<vps>   ->   http://localhost:8800

Auth: HTTP Basic, user "admin", password from PANEL_PASSWORD in
multi-tenant/config.env (panel/install.sh generates it).
"""

import hmac
import json
import re
import os
import subprocess
import sys
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TENANTCTL = str(ROOT / "tenantctl")
TENANTS_DIR = ROOT / "tenants"
CONFIG_FILE = ROOT / "config.env"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,18}[a-z0-9]$")
RAM_RE = re.compile(r"^[0-9]+(m|g)$")
CPUS_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
SERVICES = {"realtime", "storage"}


def read_env_file(path):
    data = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                data[key] = value
    except FileNotFoundError:
        pass
    return data


CONFIG = read_env_file(CONFIG_FILE)
PASSWORD = os.environ.get("PANEL_PASSWORD") or CONFIG.get("PANEL_PASSWORD") or ""
BASE_DOMAIN = CONFIG.get("BASE_DOMAIN", "?")
BIND_HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("PANEL_PORT", "8800"))

if not PASSWORD:
    sys.exit("error: PANEL_PASSWORD not set — run panel/install.sh or set it in config.env")


def sh(args, timeout=600):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def ctl(*args):
    return sh([TENANTCTL, *args])


def running_containers():
    code, out = sh(["docker", "ps", "--format", "{{.Names}}"], timeout=20)
    return set(out.splitlines()) if code == 0 else set()


def mem_mib(raw):
    # "199.4MiB / 512MiB" -> 199.4 ; "1.2GiB / ..." -> 1228.8
    value = raw.split("/")[0].strip()
    match = re.match(r"([0-9.]+)\s*([KMG])iB", value)
    if not match:
        return 0.0
    number, unit = float(match.group(1)), match.group(2)
    return number * {"K": 1 / 1024, "M": 1, "G": 1024}[unit]


def docker_stats():
    code, out = sh(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"], timeout=30)
    stats = {}
    if code == 0:
        for line in out.splitlines():
            name, _, usage = line.partition("\t")
            stats[name] = mem_mib(usage)
    return stats


def tenant_containers(name, container_names):
    prefix = name + "-"
    rt = "realtime-dev." + name + "-realtime"
    return [c for c in container_names if c.startswith(prefix) or c == rt]


def list_tenants():
    running = running_containers()
    stats = docker_stats()
    tenants = []
    if TENANTS_DIR.is_dir():
        for entry in sorted(TENANTS_DIR.iterdir()):
            if not (entry / ".env").is_file():
                continue
            env = read_env_file(entry / ".env")
            name = entry.name
            up = tenant_containers(name, running)
            profiles = [p for p in env.get("COMPOSE_PROFILES", "").split(",") if p]
            tenants.append({
                "name": name,
                "domain": env.get("TENANT_DOMAIN", ""),
                "studio_domain": env.get("STUDIO_DOMAIN", ""),
                "services": ["auth", "rest"] + profiles,
                "studio": "studio" in profiles,
                "state": "running" if up else "suspended",
                "containers": len(up),
                "ram_limit": env.get("TENANT_DB_MEM_LIMIT", ""),
                "mem_mib": round(sum(stats.get(c, 0.0) for c in tenant_containers(name, stats)), 1),
            })
    return tenants


def tenant_creds(name):
    env = read_env_file(TENANTS_DIR / name / ".env")
    creds = {
        "name": name,
        "url": "https://" + env.get("TENANT_DOMAIN", ""),
        "anon_key": env.get("ANON_KEY", ""),
        "service_role_key": env.get("SERVICE_ROLE_KEY", ""),
        "studio": "studio" in env.get("COMPOSE_PROFILES", ""),
    }
    if creds["studio"]:
        creds["studio_url"] = "https://" + env.get("STUDIO_DOMAIN", "")
        creds["studio_user"] = env.get("DASHBOARD_USERNAME", "")
        creds["studio_password"] = env.get("DASHBOARD_PASSWORD", "")
    return creds


class Handler(BaseHTTPRequestHandler):
    server_version = "supabase-mt-panel"

    def log_message(self, fmt, *args):  # quieter logs, no creds in URLs anyway
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- plumbing -------------------------------------------------------
    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, password = b64decode(header[6:]).decode().partition(":")
            except Exception:
                return False
            return user == "admin" and hmac.compare_digest(password, PASSWORD)
        return False

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="supabase-mt-panel"')
        self.end_headers()

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._json({"error": message}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except Exception:
            return {}

    def _tenant_name(self, segment):
        if not NAME_RE.match(segment or ""):
            return None
        if not (TENANTS_DIR / segment / ".env").is_file():
            return None
        return segment

    # ---- routes ---------------------------------------------------------
    def do_GET(self):
        if not self._authorized():
            return self._deny()
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            tenants = list_tenants()
            self._json({
                "base_domain": BASE_DOMAIN,
                "tenants": tenants,
                "running": sum(1 for t in tenants if t["state"] == "running"),
                "total_mem_mib": round(sum(t["mem_mib"] for t in tenants), 1),
            })
        elif self.path.startswith("/api/tenants/"):
            name = self._tenant_name(self.path.split("/")[3])
            if not name:
                return self._error("unknown tenant", 404)
            self._json(tenant_creds(name))
        else:
            self._error("not found", 404)

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        parts = [p for p in self.path.split("/") if p]

        if self.path == "/api/tenants":
            return self._create(self._body())

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "tenants":
            name = self._tenant_name(parts[2])
            if not name:
                return self._error("unknown tenant", 404)
            return self._action(name, self._body())

        self._error("not found", 404)

    # ---- handlers -------------------------------------------------------
    def _create(self, body):
        name = (body.get("name") or "").strip().lower()
        ram = body.get("ram") or "1g"
        cpus = str(body.get("cpus") or "1")
        services = [s for s in (body.get("services") or []) if s in SERVICES]
        if not NAME_RE.match(name):
            return self._error("invalid name: lowercase letters, digits, hyphens (3-20 chars)")
        if (TENANTS_DIR / name).exists():
            return self._error("tenant '%s' already exists" % name)
        if not RAM_RE.match(ram) or not CPUS_RE.match(cpus):
            return self._error("invalid ram/cpus value")

        args = ["create", name, "--ram", ram, "--cpus", cpus]
        if services:
            args += ["--services", ",".join(services)]
        if body.get("studio"):
            args.append("--studio")
        code, out = ctl(*args)
        if code != 0:
            return self._error("create failed:\n" + out[-2000:], 500)
        self._json(tenant_creds(name))

    def _action(self, name, body):
        action = body.get("action", "")
        if action in ("suspend", "resume", "backup"):
            code, out = ctl(action, name)
        elif action in ("studio_on", "studio_off"):
            code, out = ctl("studio", name, action.split("_")[1])
        elif action == "delete":
            if body.get("confirm") != name:
                return self._error("confirmation text does not match tenant name")
            code, out = ctl("delete", name, "--yes")
        else:
            return self._error("unknown action")
        if code != 0:
            return self._error("%s failed:\n%s" % (action, out[-2000:]), 500)
        self._json({"ok": True, "output": out[-2000:]})


PAGE = r"""<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Supabase Fleet</title>
<style>
  :root{
    --bg:#09090b; --card:#101012; --card2:#141417; --border:#26262b;
    --text:#fafafa; --muted:#a1a1aa; --muted2:#71717a;
    --accent:#3ecf8e; --accent-dim:rgba(62,207,142,.12);
    --danger:#f87171; --danger-dim:rgba(248,113,113,.12);
    --amber:#fbbf24; --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Inter,Roboto,sans-serif;
    -webkit-font-smoothing:antialiased}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}

  header{display:flex;align-items:center;gap:12px;padding:14px 28px;
    border-bottom:1px solid var(--border);position:sticky;top:0;background:rgba(9,9,11,.85);
    backdrop-filter:blur(8px);z-index:5}
  .logo{width:26px;height:26px;border-radius:7px;background:var(--accent-dim);
    display:grid;place-items:center;color:var(--accent);font-weight:700}
  .brand{font-weight:600;letter-spacing:-.01em}
  .chip{font-size:12px;color:var(--muted);border:1px solid var(--border);
    border-radius:999px;padding:2px 10px;background:var(--card)}
  .spacer{flex:1}

  main{max-width:1060px;margin:0 auto;padding:28px}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:22px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px}
  .stat .k{font-size:12px;color:var(--muted)}
  .stat .v{font-size:24px;font-weight:600;letter-spacing:-.02em;margin-top:2px}

  .card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius)}
  .card-head{display:flex;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border)}
  .card-head h2{margin:0;font-size:15px;font-weight:600}

  table{width:100%;border-collapse:collapse}
  th{font-size:12px;text-align:left;color:var(--muted2);font-weight:500;padding:10px 18px;border-bottom:1px solid var(--border)}
  td{padding:12px 18px;border-bottom:1px solid var(--border);vertical-align:middle}
  tr:last-child td{border-bottom:0}
  tr:hover td{background:var(--card2)}
  .tname{font-weight:600}
  .sub{font-size:12px;color:var(--muted2)}

  .badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;
    border:1px solid var(--border);border-radius:999px;padding:1px 9px;color:var(--muted)}
  .dot{width:7px;height:7px;border-radius:50%}
  .ok .dot{background:var(--accent);box-shadow:0 0 6px var(--accent)}
  .off .dot{background:var(--muted2)}
  .svc{font-size:11px;border-radius:5px;padding:1px 7px;background:var(--card2);
    border:1px solid var(--border);color:var(--muted);margin-right:4px}

  button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--border);
    background:var(--card2);color:var(--text);padding:6px 12px;transition:all .12s}
  button:hover{border-color:#3f3f46;background:#1b1b1f}
  button:disabled{opacity:.5;cursor:wait}
  .btn-primary{background:var(--accent);border-color:var(--accent);color:#052e1c;font-weight:600}
  .btn-primary:hover{background:#34d399;border-color:#34d399}
  .btn-ghost{background:transparent;border-color:transparent;color:var(--muted);padding:5px 9px}
  .btn-ghost:hover{background:var(--card2);border-color:transparent;color:var(--text)}
  .btn-danger{color:var(--danger)}
  .btn-danger:hover{background:var(--danger-dim);border-color:transparent}
  .row-actions{display:flex;gap:2px;justify-content:flex-end}

  dialog{background:var(--card);color:var(--text);border:1px solid var(--border);
    border-radius:14px;padding:0;width:min(520px,92vw);box-shadow:0 24px 64px rgba(0,0,0,.5)}
  dialog::backdrop{background:rgba(0,0,0,.6);backdrop-filter:blur(2px)}
  .dlg-head{padding:18px 22px 0}
  .dlg-head h3{margin:0;font-size:16px}
  .dlg-head p{margin:6px 0 0;color:var(--muted);font-size:13px}
  .dlg-body{padding:18px 22px}
  .dlg-foot{display:flex;justify-content:flex-end;gap:8px;padding:0 22px 20px}

  label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px}
  input[type=text]{width:100%;font:inherit;background:var(--bg);color:var(--text);
    border:1px solid var(--border);border-radius:8px;padding:8px 12px;outline:none}
  input[type=text]:focus{border-color:var(--accent)}
  select{font:inherit;background:var(--bg);color:var(--text);border:1px solid var(--border);
    border-radius:8px;padding:8px 12px;width:100%}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .check{display:flex;align-items:center;gap:10px;padding:10px 12px;margin-top:8px;
    border:1px solid var(--border);border-radius:8px;cursor:pointer}
  .check:hover{background:var(--card2)}
  .check input{accent-color:var(--accent);width:15px;height:15px}
  .check .t{font-size:13px}
  .check .d{font-size:11px;color:var(--muted2)}

  .cred{margin-top:12px}
  .cred .k{font-size:11px;color:var(--muted2);text-transform:uppercase;letter-spacing:.04em}
  .cred .vrow{display:flex;gap:6px;margin-top:4px}
  .cred code{flex:1;display:block;background:var(--bg);border:1px solid var(--border);
    border-radius:8px;padding:8px 10px;font:12px ui-monospace,Menlo,monospace;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  #toasts{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:8px;z-index:50}
  .toast{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--accent);
    border-radius:10px;padding:10px 14px;font-size:13px;box-shadow:0 12px 32px rgba(0,0,0,.4);
    animation:slidein .18s ease-out}
  .toast.err{border-left-color:var(--danger);white-space:pre-wrap}
  @keyframes slidein{from{transform:translateY(8px);opacity:0}to{transform:none;opacity:1}}

  .empty{padding:48px;text-align:center;color:var(--muted2)}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid rgba(5,46,28,.4);
    border-top-color:#052e1c;border-radius:50%;animation:rot .7s linear infinite;vertical-align:-2px;margin-right:7px}
  @keyframes rot{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<header>
  <div class="logo">⚡</div>
  <span class="brand">Supabase Fleet</span>
  <span class="chip" id="domainChip">…</span>
  <div class="spacer"></div>
  <button class="btn-ghost" onclick="refresh()" title="Refresh">⟳ Refresh</button>
  <button class="btn-primary" onclick="openCreate()">＋ New tenant</button>
</header>

<main>
  <div class="stats">
    <div class="stat"><div class="k">Tenants</div><div class="v" id="stTotal">–</div></div>
    <div class="stat"><div class="k">Running</div><div class="v" id="stRun">–</div></div>
    <div class="stat"><div class="k">Fleet RAM (live)</div><div class="v" id="stMem">–</div></div>
  </div>

  <div class="card">
    <div class="card-head"><h2>Tenants</h2></div>
    <table>
      <thead><tr>
        <th>Tenant</th><th>Services</th><th>State</th><th style="text-align:right">RAM</th><th></th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
    </table>
  </div>
</main>

<dialog id="dlg"></dialog>
<div id="toasts"></div>

<script>
let S = null;

async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  const data = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function toast(msg, err){
  const el = document.createElement('div');
  el.className = 'toast' + (err ? ' err' : '');
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), err ? 9000 : 3500);
}

async function refresh(){
  try { S = await api('/api/state'); } catch(e){ toast(e.message, true); return; }
  document.getElementById('domainChip').textContent = '*.' + S.base_domain;
  document.getElementById('stTotal').textContent = S.tenants.length;
  document.getElementById('stRun').textContent = S.running;
  document.getElementById('stMem').textContent = S.total_mem_mib >= 1024
    ? (S.total_mem_mib/1024).toFixed(1) + ' GiB' : S.total_mem_mib + ' MiB';
  const rows = document.getElementById('rows');
  if(!S.tenants.length){
    rows.innerHTML = '<tr><td colspan="5" class="empty">No tenants yet — create your first client.</td></tr>';
    return;
  }
  rows.innerHTML = S.tenants.map(t => {
    const run = t.state === 'running';
    return '<tr>' +
      '<td><div class="tname">' + esc(t.name) + '</div>' +
        '<div class="sub"><a href="https://' + esc(t.domain) + '/auth/v1/health" target="_blank">' + esc(t.domain) + '</a></div></td>' +
      '<td>' + t.services.map(s => '<span class="svc">' + esc(s) + '</span>').join('') + '</td>' +
      '<td><span class="badge ' + (run ? 'ok' : 'off') + '"><span class="dot"></span>' + (run ? 'running' : 'suspended') + '</span></td>' +
      '<td style="text-align:right">' + (run ? t.mem_mib + ' MiB' : '—') + ' <span class="sub">/ ' + esc(t.ram_limit) + '</span></td>' +
      '<td><div class="row-actions">' +
        '<button class="btn-ghost" onclick="showInfo(\'' + t.name + '\')">Keys</button>' +
        (run
          ? '<button class="btn-ghost" onclick="act(\'' + t.name + '\',\'suspend\')">Suspend</button>'
          : '<button class="btn-ghost" onclick="act(\'' + t.name + '\',\'resume\')">Resume</button>') +
        '<button class="btn-ghost" onclick="act(\'' + t.name + '\',\'backup\')">Backup</button>' +
        '<button class="btn-ghost" onclick="act(\'' + t.name + '\',\'' + (t.studio ? 'studio_off' : 'studio_on') + '\')">' + (t.studio ? 'Studio ✓' : 'Studio') + '</button>' +
        '<button class="btn-ghost btn-danger" onclick="openDelete(\'' + t.name + '\')">Delete</button>' +
      '</div></td></tr>';
  }).join('');
}

async function act(name, action){
  const verbs = {suspend:'Suspending', resume:'Resuming', backup:'Backing up', studio_on:'Enabling Studio for', studio_off:'Disabling Studio for'};
  toast((verbs[action] || action) + ' ' + name + '…');
  try {
    const r = await api('/api/tenants/' + name, {method:'POST', body:JSON.stringify({action})});
    toast(action + ' done' + (action === 'backup' && r.output ? ': ' + r.output.split('\n').pop() : ''));
    if(action === 'studio_on') showInfo(name);
  } catch(e){ toast(e.message, true); }
  refresh();
}

const dlg = document.getElementById('dlg');
function openDlg(html){ dlg.innerHTML = html; dlg.showModal(); }
function closeDlg(){ dlg.close(); }

let CREDS = {};
function credRow(label, field){
  const v = CREDS[field] || '';
  return '<div class="cred"><div class="k">' + esc(label) + '</div><div class="vrow">' +
    '<code title="' + esc(v) + '">' + esc(v) + '</code>' +
    '<button onclick="copyV(this, \'' + field + '\')">Copy</button>' +
  '</div></div>';
}
function copyV(btn, field){
  navigator.clipboard.writeText(CREDS[field] || '').then(() => {
    btn.textContent = '✓'; setTimeout(() => btn.textContent = 'Copy', 1200);
  });
}

async function showInfo(name){
  try { CREDS = await api('/api/tenants/' + name); } catch(e){ return toast(e.message, true); }
  let html = '<div class="dlg-head"><h3>' + esc(name) + ' — credentials</h3>' +
    '<p>Paste these into the client app\'s configuration. The service key bypasses RLS — server-side only.</p></div>' +
    '<div class="dlg-body">' +
    credRow('SUPABASE_URL', 'url') +
    credRow('SUPABASE_ANON_KEY', 'anon_key') +
    credRow('SUPABASE_SERVICE_ROLE_KEY', 'service_role_key');
  if(CREDS.studio){
    html += credRow('Studio URL', 'studio_url') + credRow('Studio user', 'studio_user') + credRow('Studio password', 'studio_password');
  }
  html += '</div><div class="dlg-foot"><button onclick="closeDlg()">Close</button></div>';
  openDlg(html);
}

function openCreate(){
  openDlg(
    '<div class="dlg-head"><h3>New tenant</h3><p>Creates an isolated Supabase backend with its own database, auth and keys.</p></div>' +
    '<div class="dlg-body">' +
    '<label>Client name</label><input type="text" id="fName" placeholder="acmecorp" autocomplete="off">' +
    '<div class="grid2"><div><label>RAM cap (database)</label><select id="fRam">' +
      '<option value="512m">512 MB</option><option value="1g" selected>1 GB</option><option value="2g">2 GB</option><option value="4g">4 GB</option></select></div>' +
    '<div><label>CPU cap</label><select id="fCpus">' +
      '<option value="0.5">0.5</option><option value="1" selected>1</option><option value="2">2</option></select></div></div>' +
    '<label>Optional services</label>' +
    '<label class="check"><input type="checkbox" id="fStorage"><span><div class="t">Storage</div><div class="d">file uploads / buckets</div></span></label>' +
    '<label class="check"><input type="checkbox" id="fRealtime"><span><div class="t">Realtime</div><div class="d">live subscriptions / websockets</div></span></label>' +
    '<label class="check"><input type="checkbox" id="fStudio"><span><div class="t">Studio access</div><div class="d">client-facing dashboard with login (full admin of their backend)</div></span></label>' +
    '</div><div class="dlg-foot">' +
    '<button onclick="closeDlg()">Cancel</button>' +
    '<button class="btn-primary" id="fGo" onclick="createTenant()">Create tenant</button>' +
    '</div>');
  document.getElementById('fName').focus();
}

async function createTenant(){
  const name = document.getElementById('fName').value.trim().toLowerCase();
  if(!/^[a-z][a-z0-9-]{1,18}[a-z0-9]$/.test(name)) return toast('Invalid name: lowercase letters, digits, hyphens (3–20 chars)', true);
  const services = [];
  if(document.getElementById('fStorage').checked) services.push('storage');
  if(document.getElementById('fRealtime').checked) services.push('realtime');
  const body = {name, ram:document.getElementById('fRam').value, cpus:document.getElementById('fCpus').value,
    services, studio:document.getElementById('fStudio').checked};
  const go = document.getElementById('fGo');
  go.disabled = true; go.innerHTML = '<span class="spin"></span>Creating… ~1 min';
  try {
    await api('/api/tenants', {method:'POST', body:JSON.stringify(body)});
    closeDlg(); toast('Tenant ' + name + ' created'); await refresh(); showInfo(name);
  } catch(e){
    go.disabled = false; go.textContent = 'Create tenant'; toast(e.message, true);
  }
}

function openDelete(name){
  openDlg(
    '<div class="dlg-head"><h3 style="color:var(--danger)">Delete ' + esc(name) + '</h3>' +
    '<p>This permanently destroys the tenant <b>including its database and files</b>. A backup is taken automatically every night, but consider clicking Backup first.</p></div>' +
    '<div class="dlg-body"><label>Type the tenant name to confirm</label>' +
    '<input type="text" id="delConfirm" placeholder="' + esc(name) + '" autocomplete="off"></div>' +
    '<div class="dlg-foot"><button onclick="closeDlg()">Cancel</button>' +
    '<button class="btn-danger" style="border:1px solid var(--danger)" onclick="doDelete(\'' + name + '\')">Delete forever</button></div>');
}
async function doDelete(name){
  const confirm = document.getElementById('delConfirm').value.trim();
  try {
    await api('/api/tenants/' + name, {method:'POST', body:JSON.stringify({action:'delete', confirm})});
    closeDlg(); toast('Tenant ' + name + ' deleted');
  } catch(e){ toast(e.message, true); }
  refresh();
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


def main():
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    print("supabase-mt-panel listening on http://%s:%d (user: admin)" % (BIND_HOST, BIND_PORT))
    server.serve_forever()


if __name__ == "__main__":
    main()
