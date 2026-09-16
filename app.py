import os
import json
import signal
import subprocess
import shutil
import zipfile
import hashlib
import psutil
import threading
import time
import urllib.request
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, abort
import io

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
SERVERS_DIR = BASE_DIR / "servers"
SERVERS_DIR.mkdir(exist_ok=True)

NORMAL_PASSWORD = os.environ.get("NORMAL_PASSWORD", "KIU BE")

RUNNING_PROCESSES = {}

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {
        "servers": {},
        "users": {},
        "settings": {
            "maintenance": False,
            "maintenance_msg": "System under maintenance.",
            "theme_color": "#00ff41",
            "normal_password": NORMAL_PASSWORD,
            "site_name": "TAHMID CODEX",
            "auto_restart_interval": 300
        }
    }

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_theme_color():
    data = load_data()
    return data.get("settings", {}).get("theme_color", "#00ff41")

@app.context_processor
def inject_theme():
    return {"theme_color": get_theme_color()}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        data = load_data()
        settings = data.get("settings", {})
        if settings.get("maintenance"):
            return render_template("maintenance.html", message=settings.get("maintenance_msg", "Under maintenance"), site_name=settings.get("site_name", "TAHMID CODEX"), theme_color=get_theme_color())
        return f(*args, **kwargs)
    return decorated

def is_process_alive(pid):
    try:
        if not pid:
            return False
        p = psutil.Process(pid)
        return p.is_running() and p.status() not in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

def kill_process(pid):
    try:
        p = psutil.Process(pid)
        children = p.children(recursive=True)
        p.terminate()
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except psutil.TimeoutExpired:
            p.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

def get_run_command(runtime, main_file):
    ext = Path(main_file).suffix.lower()
    if runtime == "node" or ext in (".js", ".ts", ".mjs"):
        return ["node", main_file]
    elif runtime == "static":
        return ["python", "-m", "http.server", "8080"]
    else:
        return ["python", "-u", main_file]

def _sync_process_status():
    data = load_data()
    changed = False
    for name, cfg in data["servers"].items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            changed = True
    if changed:
        save_data(data)

_sync_process_status()

# ==================== RENDER RESTART PREVENTION ====================
# Render 15 মিনিট inactivity তে restart করে, তাই প্রতি 10 মিনিটে ping দিই
def render_keep_alive():
    """Render এ restart যাতে না হয় সেজন্য প্রতি 10 মিনিটে ping"""
    while True:
        try:
            time.sleep(600)  # 10 মিনিট
            
            # নিজের api/ping এ কল করি
            port = os.environ.get("PORT", 5000)
            url = f"http://127.0.0.1:{port}/api/ping"
            
            req = urllib.request.Request(url, headers={'User-Agent': 'Render-KeepAlive/1.0'})
            urllib.request.urlopen(req, timeout=10)
            
            # Render এর external URL থাকলে সেটাও ping করি
            external_url = os.environ.get("RENDER_EXTERNAL_URL")
            if external_url:
                ping_url = f"{external_url}/api/ping"
                req2 = urllib.request.Request(ping_url, headers={'User-Agent': 'Render-KeepAlive/1.0'})
                urllib.request.urlopen(req2, timeout=10)
                
        except Exception:
            pass

# Keep-Alive থ্রেড চালু করি
threading.Thread(target=render_keep_alive, daemon=True).start()

@app.route("/api/ping")
def ping():
    """Render Keep-Alive এর জন্য ping endpoint"""
    return "pong", 200

def keep_alive():
    """পুরনো keep-alive (পূর্বের মতো)"""
    while True:
        time.sleep(240)
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url:
                ping_url = f"{url}/api/ping"
            else:
                port = os.environ.get("PORT", 5000)
                ping_url = f"http://127.0.0.1:{port}/api/ping"
            req = urllib.request.Request(ping_url, headers={'User-Agent': 'KeepAlive-Bot/1.0'})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

threading.Thread(target=keep_alive, daemon=True).start()

# ==================== AUTO RESTART SYSTEM ====================
def auto_restart_server(name):
    try:
        data = load_data()
        cfg = data["servers"].get(name)
        if not cfg:
            return
        
        pid = cfg.get("pid")
        if pid and is_process_alive(pid):
            kill_process(pid)
            if name in RUNNING_PROCESSES:
                try:
                    RUNNING_PROCESSES[name]["proc"].terminate()
                    RUNNING_PROCESSES[name]["log_file"].close()
                except Exception:
                    pass
                del RUNNING_PROCESSES[name]
        
        main_file = cfg.get("main_file") or "main.py"
        main_cmd = cfg.get("main_command") or ""
        extract_dir = SERVERS_DIR / name / "extracted"
        main_path = extract_dir / main_file
        if not main_path.exists():
            return
        
        log_path = SERVERS_DIR / name / "logs.txt"
        if main_cmd:
            cmd = main_cmd.split()
        else:
            cmd = get_run_command(cfg.get("runtime", "python"), main_file)
        env = os.environ.copy()
        env["PORT"] = str(cfg.get("port", 8080))
        
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] AUTO-RESTART triggered\n{'='*50}\n")
        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdin=subprocess.PIPE, stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid, text=True, bufsize=1)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        data["servers"][name] = cfg
        save_data(data)
    except Exception as e:
        print(f"Auto-restart error for {name}: {e}")

def auto_restart_monitor():
    while True:
        try:
            data = load_data()
            settings = data.get("settings", {})
            interval = settings.get("auto_restart_interval", 300)
            
            for name, cfg in data["servers"].items():
                pid = cfg.get("pid")
                if pid and not is_process_alive(pid):
                    cfg["status"] = "stopped"
                    cfg["pid"] = None
                    save_data(data)
                
                if cfg.get("status") == "stopped":
                    main_file = cfg.get("main_file") or "main.py"
                    extract_dir = SERVERS_DIR / name / "extracted"
                    if (extract_dir / main_file).exists():
                        threading.Thread(target=auto_restart_server, args=[name], daemon=True).start()
            
            time.sleep(interval)
        except Exception:
            time.sleep(30)

threading.Thread(target=auto_restart_monitor, daemon=True).start()

# ==================== LOGIN ====================
@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    
    if request.method == "POST":
        password = request.form.get("password", "").strip()
        data = load_data()
        settings = data.get("settings", {})
        normal_pass = settings.get("normal_password", NORMAL_PASSWORD)
        
        if password != normal_pass:
            return render_template("login.html", error="Wrong password", theme_color=get_theme_color(), site_name=settings.get("site_name", "TAHMID CODEX"))
        
        username = "admin"
        user = data["users"].get(username)
        if not user:
            data["users"][username] = {
                "joined": datetime.now().isoformat(),
                "password_hash": hash_password(password)
            }
            save_data(data)
        else:
            if user.get("password_hash") != hash_password(password):
                data["users"][username]["password_hash"] = hash_password(password)
                save_data(data)
        
        session["username"] = username
        return redirect(url_for("dashboard"))
    
    data = load_data()
    settings = data.get("settings", {})
    return render_template("login.html", error=None, theme_color=get_theme_color(), site_name=settings.get("site_name", "TAHMID CODEX"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ==================== DASHBOARD ====================
@app.route("/dashboard")
@login_required
def dashboard():
    username = session["username"]
    data = load_data()
    settings = data.get("settings", {})
    site_name = settings.get("site_name", "TAHMID CODEX")
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    changed = False
    for name, cfg in user_servers.items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            data["servers"][name] = cfg
            changed = True
    if changed:
        save_data(data)
    running = sum(1 for v in user_servers.values() if v.get("status") == "running")
    return render_template("dashboard.html", servers=user_servers, running=running, total=len(user_servers), username=username, site_name=site_name, theme_color=get_theme_color())

@app.route("/api/stats")
@login_required
def system_stats():
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    return jsonify({"cpu": cpu, "ram": ram, "disk": disk})

# ==================== SERVER CRUD ====================
@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    runtime = request.form.get("runtime", "python")
    if not name:
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    cfg = {
        "name": name,
        "owner": session["username"],
        "runtime": runtime,
        "status": "stopped",
        "main_file": "",
        "main_command": "",
        "port": 8080,
        "pid": None,
        "created": datetime.now().isoformat()
    }
    data["servers"][name] = cfg
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("server_detail", name=name))

@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if cfg and cfg.get("owner") == session["username"]:
        pid = cfg.get("pid")
        if pid:
            kill_process(pid)
        if name in RUNNING_PROCESSES:
            try:
                RUNNING_PROCESSES[name]["proc"].terminate()
                RUNNING_PROCESSES[name]["log_file"].close()
            except Exception:
                pass
            del RUNNING_PROCESSES[name]
        del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard"))

@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return "Server not found", 404
    if cfg.get("owner") != session["username"]:
        return "Access denied", 403
    pid = cfg.get("pid")
    if pid and not is_process_alive(pid):
        cfg["status"] = "stopped"
        cfg["pid"] = None
        data["servers"][name] = cfg
        save_data(data)
    if "main_command" not in cfg:
        cfg["main_command"] = ""
    extract_dir = SERVERS_DIR / name / "extracted"
    files = list_files(extract_dir)
    return render_template("server.html", server_name=name, config=cfg, files=files, theme_color=get_theme_color())

def list_files(directory, base=""):
    result = []
    if not directory.exists():
        return result
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                result.extend(list_files(entry, rel))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except Exception:
        pass
    return result

@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "error": "Empty filename"}), 400
    
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    
    upload_path = SERVERS_DIR / name / f"upload_{f.filename}"
    f.save(upload_path)
    
    extracted_files = []
    
    if f.filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(upload_path, "r") as z:
                for member in z.infolist():
                    if member.filename.startswith(("/", "\\", "..", "../")):
                        upload_path.unlink(missing_ok=True)
                        return jsonify({"success": False, "error": "Invalid zip path"})
                
                z.extractall(extract_dir)
                for member in z.infolist():
                    if not member.is_dir():
                        extracted_files.append(member.filename)
            upload_path.unlink(missing_ok=True)
        except Exception as e:
            upload_path.unlink(missing_ok=True)
            return jsonify({"success": False, "error": f"Zip extraction failed: {str(e)}"}), 500
    else:
        dest = extract_dir / f.filename
        shutil.move(str(upload_path), str(dest))
        extracted_files = [f.filename]
        if not cfg.get("main_file") and f.filename.endswith((".py", ".js", ".ts")):
            cfg["main_file"] = f.filename
            data["servers"][name] = cfg
            save_data(data)
    
    return jsonify({"success": True, "files": extracted_files, "count": len(extracted_files)})

@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    payload = request.get_json()
    cfg["main_file"] = payload.get("main_file", cfg.get("main_file", ""))
    cfg["main_command"] = payload.get("main_command", cfg.get("main_command", ""))
    cfg["port"] = payload.get("port", cfg.get("port", 8080))
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    pid = cfg.get("pid")
    if pid and is_process_alive(pid):
        return jsonify({"success": False, "error": "Already running"})
    
    main_file = cfg.get("main_file") or "main.py"
    main_cmd = cfg.get("main_command") or ""
    extract_dir = SERVERS_DIR / name / "extracted"
    main_path = extract_dir / main_file
    
    if not main_path.exists():
        return jsonify({"success": False, "error": f"{main_file} not found. Upload your files first."})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    if main_cmd:
        cmd = main_cmd.split()
    else:
        cmd = get_run_command(cfg.get("runtime", "python"), main_file)
    
    env = os.environ.copy()
    env["PORT"] = str(cfg.get("port", 8080))
    
    try:
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] Starting: {' '.join(cmd)}\n{'='*50}\n")
        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdin=subprocess.PIPE, stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid, text=True, bufsize=1)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        data["servers"][name] = cfg
        save_data(data)
        return jsonify({"success": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False}), 403
    
    pid = cfg.get("pid")
    stopped = False
    
    if name in RUNNING_PROCESSES:
        entry = RUNNING_PROCESSES[name]
        proc = entry["proc"]
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            entry["log_file"].close()
        except Exception:
            pass
        del RUNNING_PROCESSES[name]
        stopped = True
    
    if pid and not stopped:
        kill_process(pid)
    
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        with open(log_path, "a") as lf:
            lf.write(f"[{datetime.now().isoformat()}] Server stopped\n")
    except Exception:
        pass
    
    cfg["status"] = "stopped"
    cfg["pid"] = None
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})


# ==================== FILE EDITOR ====================
EDITABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".mjs", ".cjs", ".html", ".htm", ".css",
    ".json", ".txt", ".md", ".sh", ".env", ".yaml", ".yml", ".xml",
    ".ini", ".cfg", ".conf", ".toml", ".sql", ".java", ".c", ".cpp",
    ".h", ".hpp", ".go", ".rs", ".php", ".rb", ".swift", ".kt",
    ".jsx", ".tsx", ".vue", ".svelte"
}
MAX_EDIT_SIZE = 100 * 1024 * 1024  # 100 MB text editor limit

def get_server_for_user(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return None, data
    if cfg.get("owner") != session.get("username"):
        return False, data
    return cfg, data

def safe_server_file(name, relative_path):
    """Resolve a project file while preventing path traversal."""
    extract_dir = (SERVERS_DIR / name / "extracted").resolve()
    raw = str(relative_path or "").replace("\\", "/").strip("/")
    if not raw:
        return None
    candidate = (extract_dir / raw).resolve()
    try:
        candidate.relative_to(extract_dir)
    except ValueError:
        return None
    return candidate


@app.route("/server/<name>/delete-file", methods=["POST"])
@login_required
def delete_server_file(name):
    cfg, data = get_server_for_user(name)
    if cfg is None:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg is False:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json(silent=True) or {}
    relative_path = payload.get("path", "")
    path = safe_server_file(name, relative_path)

    if path is None or not path.exists() or not path.is_file():
        return jsonify({"success": False, "error": "File not found"}), 404

    extract_dir = (SERVERS_DIR / name / "extracted").resolve()
    try:
        path.relative_to(extract_dir)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid file path"}), 400

    try:
        # If the deleted file is configured as the main file, clear that setting.
        rel = path.relative_to(extract_dir).as_posix()
        path.unlink()

        backup = path.with_name(path.name + ".bak")
        if backup.exists() and backup.is_file():
            backup.unlink()

        if cfg.get("main_file") == rel:
            cfg["main_file"] = ""
            data["servers"][name] = cfg
            save_data(data)

        return jsonify({"success": True, "message": "File deleted successfully", "path": rel})
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not delete file: {e}"}), 500

@app.route("/server/<name>/download", methods=["GET"])
@login_required
def download_server_file(name):
    cfg, data = get_server_for_user(name)
    if cfg is None:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg is False:
        return jsonify({"success": False, "error": "Access denied"}), 403

    path = safe_server_file(name, request.args.get("path", ""))
    if path is None or not path.exists() or not path.is_file():
        return jsonify({"success": False, "error": "File not found"}), 404

    extract_dir = (SERVERS_DIR / name / "extracted").resolve()
    try:
        path.relative_to(extract_dir)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid file path"}), 400

    return send_file(path, as_attachment=True, download_name=path.name)

@app.route("/server/<name>/download-zip", methods=["POST"])
@login_required
def download_server_zip(name):
    cfg, data = get_server_for_user(name)
    if cfg is None:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg is False:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json(silent=True) or {}
    selected = payload.get("files", [])
    if not isinstance(selected, list) or not selected:
        return jsonify({"success": False, "error": "Select at least one file"}), 400
    if len(selected) > 500:
        return jsonify({"success": False, "error": "Too many files selected (max 500)"}), 400

    extract_dir = (SERVERS_DIR / name / "extracted").resolve()
    safe_files = []
    seen = set()
    total_size = 0

    for raw in selected:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = safe_server_file(name, raw)
        if path is None or not path.exists() or not path.is_file():
            return jsonify({"success": False, "error": f"Invalid file: {raw}"}), 400
        try:
            rel = path.relative_to(extract_dir).as_posix()
        except ValueError:
            return jsonify({"success": False, "error": "Invalid file path"}), 400
        if rel in seen:
            continue
        seen.add(rel)
        total_size += path.stat().st_size
        if total_size > 250 * 1024 * 1024:
            return jsonify({"success": False, "error": "Selected files are too large (max 250 MB)"}), 413
        safe_files.append((path, rel))

    if not safe_files:
        return jsonify({"success": False, "error": "Select at least one valid file"}), 400

    memory = io.BytesIO()
    try:
        with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, rel in safe_files:
                archive.write(path, arcname=rel)
        memory.seek(0)
        zip_name = f"{name}-selected-files-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        return send_file(memory, mimetype="application/zip", as_attachment=True, download_name=zip_name)
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not create ZIP: {e}"}), 500

def file_is_editable(path):
    return path.name.lower().endswith(".env") or path.suffix.lower() in EDITABLE_EXTENSIONS

@app.route("/server/<name>/file", methods=["GET"])
@login_required
def read_server_file(name):
    cfg, data = get_server_for_user(name)
    if cfg is None:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg is False:
        return jsonify({"success": False, "error": "Access denied"}), 403

    path = safe_server_file(name, request.args.get("path", ""))
    if path is None or not path.exists() or not path.is_file():
        return jsonify({"success": False, "error": "File not found"}), 404
    if not file_is_editable(path):
        return jsonify({"success": False, "error": "This file type is not editable in the browser"}), 415
    if path.stat().st_size > MAX_EDIT_SIZE:
        return jsonify({"success": False, "error": "File is too large for the web editor (max 100 MB)"}), 413

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return jsonify({"success": False, "error": "File is not valid UTF-8 text"}), 415
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not read file: {e}"}), 500

    rel = path.relative_to((SERVERS_DIR / name / "extracted").resolve()).as_posix()
    return jsonify({"success": True, "path": rel, "content": content, "size": path.stat().st_size})

@app.route("/server/<name>/file", methods=["POST"])
@login_required
def save_server_file(name):
    cfg, data = get_server_for_user(name)
    if cfg is None:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg is False:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json(silent=True) or {}
    relative_path = payload.get("path", "")
    content = payload.get("content")
    if not isinstance(content, str):
        return jsonify({"success": False, "error": "File content is required"}), 400
    if len(content.encode("utf-8")) > MAX_EDIT_SIZE:
        return jsonify({"success": False, "error": "File is too large (max 100 MB)"}), 413

    path = safe_server_file(name, relative_path)
    if path is None:
        return jsonify({"success": False, "error": "Invalid file path"}), 400
    if not path.exists() or not path.is_file():
        return jsonify({"success": False, "error": "File not found"}), 404
    if not file_is_editable(path):
        return jsonify({"success": False, "error": "This file type is not editable in the browser"}), 415

    try:
        # Keep a small backup so an accidental save can be recovered.
        backup = path.with_name(path.name + ".bak")
        if path.stat().st_size <= MAX_EDIT_SIZE:
            shutil.copy2(path, backup)

        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        return jsonify({"success": True, "message": "File saved successfully", "path": relative_path})
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"success": False, "error": f"Could not save file: {e}"}), 500

@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"logs": "Server not found"})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    if not log_path.exists():
        return jsonify({"logs": "No logs yet. Start the server to see output."})
    
    try:
        if log_path.stat().st_size > 1024 * 1024:
            with open(log_path, 'r', errors='replace') as f:
                f.seek(-50000, 2)
                content = f.read()
            content = "... (showing last 50KB) ...\n" + content
        else:
            content = log_path.read_text(errors="replace")
        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[-200:]
            content = "... (showing last 200 lines) ...\n" + "\n".join(lines)
        return jsonify({"logs": content or "No output yet."})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {e}"})
     

@app.route("/server/<name>/input", methods=["POST"])
@login_required
def send_server_input(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    entry = RUNNING_PROCESSES.get(name)
    if not entry:
        return jsonify({"success": False, "error": "Server is not running or its process is not attached"}), 400

    proc = entry.get("proc")
    if not proc or proc.poll() is not None:
        return jsonify({"success": False, "error": "Server process has stopped"}), 400

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("input", ""))
    # A terminal line is sent exactly once, followed by Enter.
    if command == "":
        command = ""
    try:
        if proc.stdin is None:
            return jsonify({"success": False, "error": "This process does not accept terminal input"}), 400
        proc.stdin.write(command + "\n")
        proc.stdin.flush()

        log_path = SERVERS_DIR / name / "logs.txt"
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"[{datetime.now().isoformat()}] > {command}\n")
        return jsonify({"success": True})
    except (BrokenPipeError, OSError, ValueError) as e:
        return jsonify({"success": False, "error": f"Terminal input failed: {e}"}), 400

@app.route("/server/<name>/logs/clear", methods=["POST"])
@login_required
def clear_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        log_path.write_text("")
    except Exception:
        pass
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5300))
    app.run(host="0.0.0.0", port=port, debug=False)