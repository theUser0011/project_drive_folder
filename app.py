from flask import Flask, jsonify, request, render_template, send_file
from flask_cors import CORS
import json, requests, tempfile, subprocess, os, threading, time, re, shutil
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path

# ==========================
# 1️⃣ Flask app + CORS
# ==========================
app = Flask(__name__)
CORS(app)

# ==========================
# 2️⃣ Load Google Drive keys
# ==========================
with open("keys.json", "r") as f:
    keys = json.load(f)

FOLDER_ID = keys["folder_id"]
API_KEY = keys["api_key"]
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"

# ==========================
# 3️⃣ Thread pool & cache
# ==========================
MAX_WORKERS = 4
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
CACHE_DIR = Path(tempfile.gettempdir()) / "awb_mp3_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
ongoing: dict[str, Future] = {}
ongoing_lock = threading.Lock()

# ==========================
# 4️⃣ Google Drive helpers
# ==========================
def drive_file_metadata(file_id: str):
    params = {"key": API_KEY, "fields": "id,name,size,createdTime,modifiedTime,mimeType"}
    url = f"{GOOGLE_DRIVE_FILES_URL}/{file_id}"
    r = requests.get(url, params=params)
    r.raise_for_status()
    return r.json()

def drive_list_all_files(filters):
    query = f"'{FOLDER_ID}' in parents and trashed = false"
    if filters:
        if "type" in filters:
            query += f" and mimeType contains '{filters['type']}'"
        if "name" in filters:
            query += f" and name contains '{filters['name']}'"
    params = {
        "q": query,
        "key": API_KEY,
        "fields": "files(id,name,mimeType,size,createdTime,modifiedTime),nextPageToken",
        "orderBy": "createdTime desc",
        "pageSize": 1000
    }
    files = []
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(GOOGLE_DRIVE_FILES_URL, params=params)
        r.raise_for_status()
        data = r.json()
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return files

# ==========================
# 5️⃣ API routes
# ==========================
@app.route("/api/files", methods=["GET"])
def api_files():
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("pageSize", 100))
    file_type = request.args.get("type")
    name_filter = request.args.get("name")
    filters = {}
    if file_type:
        filters["type"] = file_type
    if name_filter:
        filters["name"] = name_filter
    try:
        all_files = drive_list_all_files(filters)
        total = len(all_files)
        start = (page - 1) * page_size
        end = start + page_size
        page_files = all_files[start:end]
        return jsonify({"page": page, "pageSize": page_size, "total": total, "files": page_files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def download_drive_file_to_temp(file_id: str) -> str:
    download_url = f"{GOOGLE_DRIVE_FILES_URL}/{file_id}?alt=media&key={API_KEY}"
    resp = requests.get(download_url, stream=True)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                tmp.write(chunk)
        tmp.flush()
    finally:
        tmp.close()
    return tmp.name

def convert_awb_to_mp3(input_path: str, output_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-f", "mp3", "-ab", "192k", output_path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError(proc.stderr.decode())

def make_cache_key(file_id: str, modified_time: str | None) -> str:
    safe_mod = modified_time.replace(":", "-") if modified_time else str(int(time.time()))
    return f"{file_id}_{safe_mod}"

def convert_and_cache(file_id: str) -> str:
    meta = drive_file_metadata(file_id)
    modified_time = meta.get("modifiedTime") or meta.get("createdTime") or ""
    key = make_cache_key(file_id, modified_time)
    cached_mp3 = CACHE_DIR / f"{key}.mp3"
    if cached_mp3.exists():
        return str(cached_mp3)
    input_tmp = download_drive_file_to_temp(file_id)
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as out_tmp:
            out_tmp_path = out_tmp.name
        convert_awb_to_mp3(input_tmp, out_tmp_path)
        os.replace(out_tmp_path, cached_mp3)
        return str(cached_mp3)
    finally:
        if os.path.exists(input_tmp):
            os.remove(input_tmp)

@app.route("/api/convert")
def api_convert():
    file_id = request.args.get("fileId")
    if not file_id:
        return jsonify({"error": "fileId required"}), 400
    try:
        meta = drive_file_metadata(file_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    modified_time = meta.get("modifiedTime") or meta.get("createdTime") or ""
    cache_key = make_cache_key(file_id, modified_time)
    cached_path = CACHE_DIR / f"{cache_key}.mp3"
    if cached_path.exists():
        return send_file(str(cached_path), mimetype="audio/mpeg", as_attachment=False)
    with ongoing_lock:
        fut = ongoing.get(cache_key)
        if fut is None:
            fut = executor.submit(convert_and_cache, file_id)
            ongoing[cache_key] = fut
    try:
        mp3_path = fut.result()
        return send_file(mp3_path, mimetype="audio/mpeg", as_attachment=False)
    finally:
        with ongoing_lock:
            if fut.done():
                ongoing.pop(cache_key, None)

@app.route("/")
def index():
    return render_template("index.html")

# ==========================
# 6️⃣ Cloudflare tunnel helpers
# ==========================
def install_cloudflared():
    if shutil.which("cloudflared"):
        print("✅ cloudflared already installed.")
        return shutil.which("cloudflared")
    print("⏳ Installing cloudflared...")
    tmp_path = Path(tempfile.gettempdir()) / "cloudflared"
    subprocess.run([
        "curl", "-fsSL",
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        "-o", str(tmp_path)
    ], check=True)
    subprocess.run(["chmod", "+x", str(tmp_path)], check=True)
    print(f"✅ cloudflared installed at {tmp_path}")
    return str(tmp_path)

def start_cloudflare_tunnel(local_port: int):
    print(f"⏳ Starting Cloudflare tunnel for http://localhost:{local_port} ...")
    cf_proc = subprocess.Popen(
        [shutil.which("cloudflared") or "./cloudflared", "tunnel", "--url", f"http://localhost:{local_port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    public_url = None
    for line in iter(cf_proc.stdout.readline, ""):
        line = line.strip()
        if line:
            print(f"[Cloudflare] {line}")
            if "trycloudflare.com" in line and not public_url:
                match = re.search(r"https://[0-9a-zA-Z\-]+\.trycloudflare\.com", line)
                if match:
                    public_url = match.group(0)
                    print(f"\n✅ Public URL ready: {public_url}\n")
                    break
    if not public_url:
        print("⚠️ Failed to detect public URL from cloudflared output.")
    return cf_proc, public_url

# ==========================
# 7️⃣ Main: run Flask + Tunnel (clean exit)
# ==========================
if __name__ == "__main__":
    LOCAL_PORT = 5000

    # Install cloudflared if missing
    install_cloudflared()

    # Run Flask in a background thread
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=LOCAL_PORT, debug=True, threaded=True, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()
    time.sleep(3)  # small delay to let Flask start

    # Start Cloudflare tunnel
    cf_proc, public_url = start_cloudflare_tunnel(LOCAL_PORT)

    if public_url:
        print(f"🌐 Your Flask app is publicly available at: {public_url}")

    try:
        # Keep main thread alive while Flask + tunnel run
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🔴 Stopping Flask + Cloudflare tunnel gracefully...")
    finally:
        if cf_proc:
            cf_proc.terminate()
            cf_proc.wait()
            print("✅ Cloudflare tunnel stopped.")
        print("✅ Flask app stopped.")
