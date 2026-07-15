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
import shutil
import subprocess
import sys
import time
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
                "cpus": env.get("TENANT_DB_CPUS", ""),
                "mem_mib": round(sum(stats.get(c, 0.0) for c in tenant_containers(name, stats)), 1),
            })
    return tenants


def _cpu_sample():
    fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    nums = [int(n) for n in fields]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
    return idle, sum(nums)


def host_stats():
    idle1, total1 = _cpu_sample()
    time.sleep(0.3)
    idle2, total2 = _cpu_sample()
    delta = total2 - total1
    cpu_pct = round(100 * (1 - (idle2 - idle1) / delta), 1) if delta > 0 else 0.0

    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        meminfo[key] = int(value.strip().split()[0])  # kB
    mem_total = meminfo.get("MemTotal", 0) / 1024
    mem_avail = meminfo.get("MemAvailable", 0) / 1024
    mem_used = mem_total - mem_avail

    disk = shutil.disk_usage("/")
    return {
        "cores": os.cpu_count() or 1,
        "load1": round(os.getloadavg()[0], 2),
        "cpu_pct": cpu_pct,
        "mem_total_mib": round(mem_total),
        "mem_used_mib": round(mem_used),
        "mem_pct": round(100 * mem_used / mem_total, 1) if mem_total else 0,
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "disk_used_gb": round(disk.used / 1024**3, 1),
        "disk_pct": round(100 * disk.used / disk.total, 1) if disk.total else 0,
        # rough headroom: how many more light tenants (~400 MiB working set)
        # fit while keeping 20% of RAM free for the host
        "tenant_headroom": max(0, int((mem_avail - 0.2 * mem_total) // 400)),
    }


def tenant_creds(name):
    env = read_env_file(TENANTS_DIR / name / ".env")
    creds = {
        "name": name,
        "url": "https://" + env.get("TENANT_DOMAIN", ""),
        "anon_key": env.get("ANON_KEY", ""),
        "service_role_key": env.get("SERVICE_ROLE_KEY", ""),
        "db_password": env.get("POSTGRES_PASSWORD", ""),
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
                "host": host_stats(),
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

    def _edit(self, name, body):
        args = ["set", name]
        ram = (body.get("ram") or "").strip()
        cpus = str(body.get("cpus") or "").strip()
        if ram:
            if not RAM_RE.match(ram):
                return self._error("invalid ram value")
            args += ["--ram", ram]
        if cpus:
            if not CPUS_RE.match(cpus):
                return self._error("invalid cpus value")
            args += ["--cpus", cpus]
        if isinstance(body.get("services"), list):
            services = [s for s in body["services"] if s in SERVICES]
            args += ["--services", ",".join(services)]
        if len(args) == 2:
            return self._error("nothing to change")
        code, out = ctl(*args)
        if code != 0:
            return self._error("edit failed:\n" + out[-2000:], 500)
        self._json({"ok": True, "output": out[-2000:]})

    def _action(self, name, body):
        action = body.get("action", "")
        if action in ("suspend", "resume", "backup"):
            code, out = ctl(action, name)
        elif action in ("studio_on", "studio_off"):
            code, out = ctl("studio", name, action.split("_")[1])
        elif action == "edit":
            return self._edit(name, body)
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
    --bg:#09090b; --bg2:#0c0c0f; --card:#101013; --card2:#16161a; --border:#232329;
    --border2:#2e2e36; --text:#fafafa; --muted:#a1a1aa; --muted2:#6b6b76;
    --accent:#3ecf8e; --accent-dim:rgba(62,207,142,.12); --accent-line:rgba(62,207,142,.35);
    --danger:#f87171; --danger-dim:rgba(248,113,113,.12);
    --amber:#fbbf24; --radius:12px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:
      radial-gradient(1200px 400px at 50% -220px, rgba(62,207,142,.07), transparent 60%),
      var(--bg);
    color:var(--text);
    font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Inter,Roboto,sans-serif;
    -webkit-font-smoothing:antialiased}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  svg{display:block}
  .ic{display:inline-flex;vertical-align:-2px}

  header{display:flex;align-items:center;gap:12px;padding:14px 28px;
    border-bottom:1px solid var(--border);position:sticky;top:0;background:rgba(9,9,11,.82);
    backdrop-filter:blur(10px);z-index:5}
  .logo{width:28px;height:28px;border-radius:8px;background:var(--accent-dim);
    border:1px solid var(--accent-line);display:grid;place-items:center;color:var(--accent)}
  .brand{font-weight:600;letter-spacing:-.01em}
  .chip{font-size:12px;color:var(--muted);border:1px solid var(--border);
    border-radius:999px;padding:2px 10px;background:var(--card);font-family:ui-monospace,Menlo,monospace}
  .spacer{flex:1}

  main{max-width:1100px;margin:0 auto;padding:26px 28px 60px}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:16px}
  .stat{background:linear-gradient(180deg,var(--card) 0%,var(--bg2) 100%);
    border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px}
  .stat .k{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--muted)}
  .stat .k .ic{color:var(--muted2)}
  .stat .v{font-size:26px;font-weight:600;letter-spacing:-.02em;margin-top:6px;
    font-variant-numeric:tabular-nums}
  .stat .v small{font-size:13px;font-weight:400;color:var(--muted2)}
  .stat .hint{font-size:11.5px;color:var(--muted2);margin-top:5px}
  .bar{height:6px;border-radius:999px;background:var(--card2);border:1px solid var(--border);
    margin-top:12px;overflow:hidden}
  .bar i{display:block;height:100%;border-radius:999px;background:var(--accent);
    transition:width .4s ease, background .4s ease}
  .bar.warn i{background:var(--amber)}
  .bar.crit i{background:var(--danger)}
  .bar.mini{height:4px;margin-top:5px;border:0;background:#1d1d22}
  .pressure{display:flex;gap:10px;align-items:flex-start;margin-top:14px;padding:10px 12px;
    border:1px solid rgba(251,191,36,.35);background:rgba(251,191,36,.08);
    border-radius:8px;font-size:12.5px;color:var(--amber)}

  .card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
    overflow:hidden}
  .card-head{display:flex;align-items:center;gap:12px;padding:13px 18px;
    border-bottom:1px solid var(--border);flex-wrap:wrap}
  .card-head h2{margin:0;font-size:15px;font-weight:600}
  .summary{font-size:12.5px;color:var(--muted2)}
  .summary b{color:var(--muted);font-weight:500}
  .search{position:relative;margin-left:auto}
  .search .ic{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--muted2);
    pointer-events:none}
  .search input[type=text]{width:210px;font-size:13px;
    padding:6px 10px 6px 32px;transition:border-color .12s}

  .tablewrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse;min-width:760px}
  th{font-size:11.5px;text-align:left;color:var(--muted2);font-weight:500;
    text-transform:uppercase;letter-spacing:.05em;padding:10px 18px;border-bottom:1px solid var(--border)}
  td{padding:13px 18px;border-bottom:1px solid var(--border);vertical-align:middle}
  tr:last-child td{border-bottom:0}
  tbody tr{transition:background .1s}
  tbody tr:hover td{background:var(--card2)}
  tr.dim .tname,tr.dim .svc{opacity:.55}
  .tname{font-weight:600;letter-spacing:-.01em}
  .sub{font-size:12px;color:var(--muted2)}
  .ram{font-variant-numeric:tabular-nums;white-space:nowrap}

  .badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;
    border:1px solid var(--border);border-radius:999px;padding:2px 10px;color:var(--muted)}
  .badge.ok{border-color:var(--accent-line);background:var(--accent-dim);color:var(--accent)}
  .dot{width:7px;height:7px;border-radius:50%}
  .ok .dot{background:var(--accent);box-shadow:0 0 8px var(--accent)}
  .off .dot{background:var(--muted2)}
  .svc{display:inline-block;font-size:11px;border-radius:5px;padding:1px 7px;background:var(--card2);
    border:1px solid var(--border);color:var(--muted);margin:1px 4px 1px 0}
  .svc.core{color:var(--muted2);border-style:dashed}

  button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--border);
    background:var(--card2);color:var(--text);padding:6px 12px;transition:all .12s;
    display:inline-flex;align-items:center;gap:6px}
  button:hover{border-color:var(--border2);background:#1c1c21}
  button:disabled{opacity:.5;cursor:wait}
  .btn-primary{background:var(--accent);border-color:var(--accent);color:#052e1c;font-weight:600}
  .btn-primary:hover{background:#34d399;border-color:#34d399}
  .btn-ghost{background:transparent;border-color:transparent;color:var(--muted);padding:5px 10px}
  .btn-ghost:hover{background:var(--card2);border-color:transparent;color:var(--text)}
  .btn-icon{padding:5px 7px}
  .btn-danger{color:var(--danger)}
  .btn-danger:hover{background:var(--danger-dim);border-color:transparent}
  .row-actions{display:flex;gap:2px;justify-content:flex-end}

  #menu{position:fixed;z-index:40;min-width:185px;background:var(--card);
    border:1px solid var(--border2);border-radius:10px;padding:5px;
    box-shadow:0 16px 48px rgba(0,0,0,.55)}
  #menu[hidden]{display:none}
  .mi{display:flex;align-items:center;gap:9px;width:100%;padding:7px 10px;border:0;
    background:transparent;border-radius:7px;color:var(--text);font-size:13px;text-align:left}
  .mi .ic{color:var(--muted2)}
  .mi:hover{background:var(--card2);border:0}
  .mi.danger{color:var(--danger)}
  .mi.danger .ic{color:var(--danger)}
  .msep{height:1px;background:var(--border);margin:5px 6px}

  dialog{background:var(--card);color:var(--text);border:1px solid var(--border2);
    border-radius:14px;padding:0;width:min(520px,92vw);box-shadow:0 24px 64px rgba(0,0,0,.5)}
  dialog::backdrop{background:rgba(0,0,0,.6);backdrop-filter:blur(2px)}
  .dlg-head{padding:18px 22px 0}
  .dlg-head h3{margin:0;font-size:16px}
  .dlg-head p{margin:6px 0 0;color:var(--muted);font-size:13px}
  .dlg-body{padding:18px 22px}
  .dlg-foot{display:flex;justify-content:flex-end;gap:8px;padding:0 22px 20px}
  .note{margin-top:14px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;
    background:var(--bg2);font-size:12px;color:var(--muted2)}

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
  .check.off{opacity:.55;cursor:not-allowed}
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
    animation:slidein .18s ease-out;max-width:380px}
  .toast.err{border-left-color:var(--danger);white-space:pre-wrap}
  @keyframes slidein{from{transform:translateY(8px);opacity:0}to{transform:none;opacity:1}}

  .empty{padding:56px 20px;text-align:center;color:var(--muted2)}
  .empty .big{font-size:15px;color:var(--muted);margin-bottom:6px}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid rgba(5,46,28,.4);
    border-top-color:#052e1c;border-radius:50%;animation:rot .7s linear infinite}
  @keyframes rot{to{transform:rotate(360deg)}}

  @media (max-width:720px){
    header{padding:12px 16px}
    main{padding:18px 16px 48px}
    .stats{grid-template-columns:1fr}
    .search{margin-left:0;width:100%}
    .search input{width:100%}
  }
</style>
</head>
<body>
<header>
  <div class="logo" id="logoIc"></div>
  <span class="brand">Supabase Fleet</span>
  <span class="chip" id="domainChip">…</span>
  <div class="spacer"></div>
  <button class="btn-ghost" id="refreshBtn" onclick="refresh()" title="Refresh"></button>
  <button class="btn-primary" id="newBtn" onclick="openCreate()"></button>
</header>

<main>
  <div class="stats">
    <div class="stat">
      <div class="k" id="kCpu">VPS CPU</div>
      <div class="v" id="hCpu">–</div>
      <div class="bar" id="hCpuBar"><i style="width:0%"></i></div>
      <div class="hint" id="hCpuHint"></div>
    </div>
    <div class="stat">
      <div class="k" id="kMem">VPS RAM</div>
      <div class="v" id="hMem">–</div>
      <div class="bar" id="hMemBar"><i style="width:0%"></i></div>
      <div class="hint" id="hMemHint"></div>
    </div>
    <div class="stat">
      <div class="k" id="kDisk">VPS Disk</div>
      <div class="v" id="hDisk">–</div>
      <div class="bar" id="hDiskBar"><i style="width:0%"></i></div>
      <div class="hint" id="hDiskHint"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-head">
      <h2>Tenants</h2>
      <span class="summary" id="summary"></span>
      <div class="search"><span class="ic" id="searchIc"></span>
        <input type="text" id="q" placeholder="Filter tenants…" autocomplete="off" oninput="renderRows()">
      </div>
    </div>
    <div class="tablewrap">
    <table>
      <thead><tr>
        <th>Tenant</th><th>Services</th><th>State</th><th style="text-align:right">RAM / cap</th><th></th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
    </table>
    </div>
  </div>
</main>

<div id="menu" hidden></div>
<dialog id="dlg"></dialog>
<div id="toasts"></div>

<script>
let S = null;

// ---- inline icons (lucide-style, stroke = currentColor) -------------------
const SV = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">';
const I = {
  zap: SV+'<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  search: SV+'<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>',
  key: SV+'<path d="m21 2-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>',
  pencil: SV+'<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>',
  dots: SV+'<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></svg>',
  pause: SV+'<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>',
  play: SV+'<polygon points="6 3 20 12 6 21 6 3"/></svg>',
  backup: SV+'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  monitor: SV+'<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
  external: SV+'<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>',
  trash: SV+'<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>',
  refresh: SV+'<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>',
  plus: SV+'<path d="M5 12h14"/><path d="M12 5v14"/></svg>',
  cpu: SV+'<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/></svg>',
  server: SV+'<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>',
  disk: SV+'<line x1="22" y1="12" x2="2" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="10.01" y2="16"/></svg>',
};
function ic(n){ return '<span class="ic">' + I[n] + '</span>'; }

document.getElementById('logoIc').innerHTML = I.zap;
document.getElementById('searchIc').innerHTML = I.search;
document.getElementById('refreshBtn').innerHTML = ic('refresh') + 'Refresh';
document.getElementById('newBtn').innerHTML = ic('plus') + 'New tenant';
document.getElementById('kCpu').innerHTML = ic('cpu') + 'VPS CPU';
document.getElementById('kMem').innerHTML = ic('server') + 'VPS RAM';
document.getElementById('kDisk').innerHTML = ic('disk') + 'VPS Disk';

// ---- helpers ---------------------------------------------------------------
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
function tenant(name){ return (S && S.tenants || []).find(t => t.name === name); }
function capMiB(cap){
  const m = /^([0-9]+)(m|g)$/.exec(cap || '');
  return m ? parseInt(m[1], 10) * (m[2] === 'g' ? 1024 : 1) : 0;
}

// ---- host stats ------------------------------------------------------------
function setBar(id, pct){
  const bar = document.getElementById(id);
  bar.className = 'bar' + (pct >= 85 ? ' crit' : pct >= 70 ? ' warn' : '');
  bar.firstElementChild.style.width = Math.min(100, pct) + '%';
}
function renderHost(h){
  document.getElementById('hCpu').innerHTML = h.cpu_pct + '<small>%</small>';
  setBar('hCpuBar', h.cpu_pct);
  document.getElementById('hCpuHint').textContent = h.cores + ' cores · load ' + h.load1;

  const gib = m => (m/1024).toFixed(1);
  document.getElementById('hMem').innerHTML = gib(h.mem_used_mib) + '<small> / ' + gib(h.mem_total_mib) + ' GiB</small>';
  setBar('hMemBar', h.mem_pct);
  document.getElementById('hMemHint').textContent = h.tenant_headroom > 0
    ? '≈ room for ' + h.tenant_headroom + ' more light tenant' + (h.tenant_headroom === 1 ? '' : 's')
    : 'no headroom — scale the VPS before adding tenants';

  document.getElementById('hDisk').innerHTML = h.disk_used_gb + '<small> / ' + h.disk_total_gb + ' GB</small>';
  setBar('hDiskBar', h.disk_pct);
  document.getElementById('hDiskHint').textContent = (h.disk_total_gb - h.disk_used_gb).toFixed(1) + ' GB free';
}

// ---- tenant table ----------------------------------------------------------
function renderRows(){
  const rows = document.getElementById('rows');
  if(!S) return;
  const q = document.getElementById('q').value.trim().toLowerCase();
  const list = S.tenants.filter(t => !q || t.name.includes(q) || t.domain.includes(q));
  const suspended = S.tenants.length - S.running;
  document.getElementById('summary').innerHTML =
    '<b>' + S.running + '</b> running' +
    (suspended ? ' · <b>' + suspended + '</b> suspended' : '') +
    ' · <b>' + (S.total_mem_mib >= 1024 ? (S.total_mem_mib/1024).toFixed(1) + ' GiB' : S.total_mem_mib + ' MiB') + '</b> live RAM';

  if(!S.tenants.length){
    rows.innerHTML = '<tr><td colspan="5"><div class="empty"><div class="big">No tenants yet</div>' +
      'Create your first isolated client backend.<br><br>' +
      '<button class="btn-primary" onclick="openCreate()">' + ic('plus') + 'New tenant</button></div></td></tr>';
    return;
  }
  if(!list.length){
    rows.innerHTML = '<tr><td colspan="5" class="empty">No tenants match “' + esc(q) + '”.</td></tr>';
    return;
  }
  rows.innerHTML = list.map(t => {
    const run = t.state === 'running';
    const cap = capMiB(t.ram_limit);
    const pct = run && cap ? Math.min(100, 100 * t.mem_mib / cap) : 0;
    return '<tr class="' + (run ? '' : 'dim') + '">' +
      '<td><div class="tname">' + esc(t.name) + '</div>' +
        '<div class="sub"><a href="https://' + esc(t.domain) + '/auth/v1/health" target="_blank">' + esc(t.domain) + '</a></div></td>' +
      '<td>' + t.services.map(s =>
        '<span class="svc' + (s === 'auth' || s === 'rest' ? ' core' : '') + '">' + esc(s) + '</span>').join('') + '</td>' +
      '<td><span class="badge ' + (run ? 'ok' : 'off') + '"><span class="dot"></span>' + (run ? 'running' : 'suspended') + '</span></td>' +
      '<td style="text-align:right;min-width:150px"><div class="ram">' +
        (run ? t.mem_mib + ' MiB' : '—') + ' <span class="sub">/ ' + esc(t.ram_limit) + ' · ' + esc(t.cpus || '?') + ' cpu</span></div>' +
        (run ? '<div class="bar mini' + (pct >= 85 ? ' crit' : pct >= 70 ? ' warn' : '') + '"><i style="width:' + pct + '%"></i></div>' : '') +
      '</td>' +
      '<td><div class="row-actions">' +
        '<button class="btn-ghost" onclick="showInfo(\'' + t.name + '\')" title="Connection keys">' + ic('key') + 'Keys</button>' +
        '<button class="btn-ghost" onclick="openEdit(\'' + t.name + '\')" title="Edit caps &amp; services">' + ic('pencil') + 'Edit</button>' +
        '<button class="btn-ghost btn-icon" onclick="toggleMenu(event, \'' + t.name + '\')" title="More actions">' + ic('dots') + '</button>' +
      '</div></td></tr>';
  }).join('');
}

async function refresh(){
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true;
  try { S = await api('/api/state'); }
  catch(e){ toast(e.message, true); btn.disabled = false; return; }
  btn.disabled = false;
  document.getElementById('domainChip').textContent = '*.' + S.base_domain;
  renderHost(S.host);
  renderRows();
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

// ---- row overflow menu -------------------------------------------------------
const menu = document.getElementById('menu');
let MENU_ITEMS = [];
function toggleMenu(ev, name){
  ev.stopPropagation();
  const t = tenant(name);
  if(!t || !menu.hidden && menu.dataset.name === name){ hideMenu(); return; }
  const run = t.state === 'running';
  MENU_ITEMS = [
    run ? {l:'Suspend', i:'pause', f:() => act(name, 'suspend')}
        : {l:'Resume', i:'play', f:() => act(name, 'resume')},
    {l:'Backup now', i:'backup', f:() => act(name, 'backup')},
    t.studio ? {l:'Disable Studio', i:'monitor', f:() => act(name, 'studio_off')}
             : {l:'Enable Studio', i:'monitor', f:() => act(name, 'studio_on')},
  ];
  if(t.studio && t.studio_domain)
    MENU_ITEMS.push({l:'Open Studio', i:'external', f:() => window.open('https://' + t.studio_domain)});
  MENU_ITEMS.push({sep:true}, {l:'Delete…', i:'trash', danger:true, f:() => openDelete(name)});

  menu.dataset.name = name;
  menu.innerHTML = MENU_ITEMS.map((m, idx) => m.sep
    ? '<div class="msep"></div>'
    : '<button class="mi' + (m.danger ? ' danger' : '') + '" data-i="' + idx + '">' + ic(m.i) + esc(m.l) + '</button>'
  ).join('');
  menu.hidden = false;
  const r = ev.currentTarget.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + 'px';
  menu.style.top = (r.bottom + h + 12 > innerHeight ? r.top - h - 6 : r.bottom + 6) + 'px';
}
function hideMenu(){ menu.hidden = true; menu.dataset.name = ''; }
menu.addEventListener('click', ev => {
  const b = ev.target.closest('.mi');
  if(!b) return;
  hideMenu();
  MENU_ITEMS[+b.dataset.i].f();
});
document.addEventListener('click', hideMenu);
addEventListener('resize', hideMenu);
addEventListener('scroll', hideMenu, true);

// ---- dialogs -----------------------------------------------------------------
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
    credRow('SUPABASE_SERVICE_ROLE_KEY', 'service_role_key') +
    credRow('POSTGRES_PASSWORD (db superuser)', 'db_password') +
    '<div class="note">The tenant database is not exposed to the internet — it lives on an internal Docker network. ' +
    'Use this password over SSH (<code>docker exec -it ' + esc(CREDS.name) + '-db psql -U postgres</code>) or via Studio.</div>';
  if(CREDS.studio){
    html += credRow('Studio URL', 'studio_url') + credRow('Studio user', 'studio_user') + credRow('Studio password', 'studio_password');
  }
  html += '</div><div class="dlg-foot"><button onclick="closeDlg()">Close</button></div>';
  openDlg(html);
}

function svcCheck(id, title, desc, checked, disabled){
  return '<label class="check' + (disabled ? ' off' : '') + '">' +
    '<input type="checkbox" id="' + id + '"' + (checked ? ' checked' : '') + (disabled ? ' disabled' : '') + '>' +
    '<span><div class="t">' + title + '</div><div class="d">' + desc + '</div></span></label>';
}

function openCreate(){
  const h = S && S.host;
  let warn = '';
  if(h && (h.mem_pct >= 80 || h.disk_pct >= 85)){
    warn = '<div class="pressure">⚠ <span>The VPS is under ' +
      (h.mem_pct >= 80 ? 'memory' : 'disk') + ' pressure (' +
      (h.mem_pct >= 80 ? 'RAM ' + h.mem_pct : 'disk ' + h.disk_pct) + '%). ' +
      'Consider scaling the server before adding this tenant.</span></div>';
  }
  openDlg(
    '<div class="dlg-head"><h3>New tenant</h3><p>Creates an isolated Supabase backend with its own database, auth and keys.</p>' + warn + '</div>' +
    '<div class="dlg-body">' +
    '<label>Client name</label><input type="text" id="fName" placeholder="acmecorp" autocomplete="off">' +
    '<div class="grid2"><div><label>RAM cap (database)</label><select id="fRam">' +
      '<option value="512m">512 MB</option><option value="1g" selected>1 GB</option><option value="2g">2 GB</option><option value="4g">4 GB</option></select></div>' +
    '<div><label>CPU cap</label><select id="fCpus">' +
      '<option value="0.5">0.5</option><option value="1" selected>1</option><option value="2">2</option></select></div></div>' +
    '<label>Optional services</label>' +
    svcCheck('fStorage', 'Storage', 'file uploads / buckets', false, false) +
    svcCheck('fRealtime', 'Realtime', 'live subscriptions / websockets', false, false) +
    svcCheck('fStudio', 'Studio access', 'client-facing dashboard with login (full admin of their backend)', false, false) +
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

function selectOpts(values, labels, current){
  return values.map((v, i) =>
    '<option value="' + v + '"' + (v === current ? ' selected' : '') + '>' + (labels[i] || v) + '</option>').join('');
}

function openEdit(name){
  const t = tenant(name);
  if(!t) return;
  const run = t.state === 'running';
  const rams = ['512m', '1g', '2g', '4g'];
  if(t.ram_limit && !rams.includes(t.ram_limit)) rams.push(t.ram_limit);
  const ramLabels = {'512m':'512 MB', '1g':'1 GB', '2g':'2 GB', '4g':'4 GB'};
  const cpus = ['0.5', '1', '2'];
  const cur = String(t.cpus || '1');
  if(!cpus.includes(cur)) cpus.push(cur);
  openDlg(
    '<div class="dlg-head"><h3>Edit ' + esc(name) + '</h3>' +
    '<p>Change the database resource caps and toggle optional services. Keys, domain and data are untouched.</p></div>' +
    '<div class="dlg-body">' +
    '<div class="grid2"><div><label>RAM cap (database)</label><select id="eRam">' +
      selectOpts(rams, rams.map(r => ramLabels[r] || r), t.ram_limit) + '</select></div>' +
    '<div><label>CPU cap</label><select id="eCpus">' + selectOpts(cpus, cpus, cur) + '</select></div></div>' +
    '<label>Optional services</label>' +
    svcCheck('eStorage', 'Storage', 'file uploads / buckets — unchecking stops the container, files are kept', t.services.includes('storage'), false) +
    svcCheck('eRealtime', 'Realtime', 'live subscriptions / websockets', t.services.includes('realtime'), false) +
    svcCheck('eStudio', 'Studio access', run ? 'client-facing dashboard with login (full admin of their backend)'
      : 'resume the tenant to change Studio access', t.studio, !run) +
    '<div class="note">' + (run
      ? 'Changing the RAM/CPU cap recreates the database container — expect a few seconds of downtime for this tenant.'
      : 'This tenant is suspended — changes are saved now and applied on resume.') + '</div>' +
    '</div><div class="dlg-foot">' +
    '<button onclick="closeDlg()">Cancel</button>' +
    '<button class="btn-primary" id="eGo" onclick="saveEdit(\'' + name + '\')">Save changes</button>' +
    '</div>');
}

async function saveEdit(name){
  const t = tenant(name);
  if(!t) return closeDlg();
  const services = [];
  if(document.getElementById('eStorage').checked) services.push('storage');
  if(document.getElementById('eRealtime').checked) services.push('realtime');
  const body = {action:'edit', ram:document.getElementById('eRam').value,
    cpus:document.getElementById('eCpus').value, services};
  const wantStudio = document.getElementById('eStudio').checked;
  const go = document.getElementById('eGo');
  go.disabled = true; go.innerHTML = '<span class="spin"></span>Applying…';
  try {
    await api('/api/tenants/' + name, {method:'POST', body:JSON.stringify(body)});
    if(t.state === 'running' && wantStudio !== t.studio){
      await api('/api/tenants/' + name, {method:'POST', body:JSON.stringify({action: wantStudio ? 'studio_on' : 'studio_off'})});
    }
    closeDlg(); toast('Tenant ' + name + ' updated'); await refresh();
    if(wantStudio && !t.studio) showInfo(name);
  } catch(e){
    go.disabled = false; go.textContent = 'Save changes'; toast(e.message, true);
    refresh();
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
// Auto-refresh, but never yank an open dialog/menu out from under the user.
setInterval(() => { if(!dlg.open && menu.hidden) refresh(); }, 15000);
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
