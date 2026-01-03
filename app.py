from flask import Flask, jsonify, request, render_template, send_file
from flask_cors import CORS
import json, requests, tempfile, subprocess, os, threading, time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from dotenv import load_dotenv
# ==========================
# 1️⃣ Flask app + CORS
# ==========================
app = Flask(__name__)
CORS(app)
load_dotenv()
# ==========================
# 2️⃣ Load Google Drive keys
# ==========================
# 🔐 RECOMMENDED: use Railway ENV vars
FOLDER_ID = os.getenv("FOLDER_ID")
API_KEY   = os.getenv("GOOGLE_API_KEY")

# ❗ fallback for local testing only
if not FOLDER_ID or not API_KEY:
    with open("keys.json", "r") as f:
        keys = json.load(f)
    FOLDER_ID = keys["folder_id"]
    API_KEY   = keys["api_key"]

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
    r = requests.get(f"{GOOGLE_DRIVE_FILES_URL}/{file_id}", params=params)
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
@app.route("/api/files")
def api_files():
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("pageSize", 100))
    filters = {}

    if request.args.get("type"):
        filters["type"] = request.args["type"]
    if request.args.get("name"):
        filters["name"] = request.args["name"]

    all_files = drive_list_all_files(filters)
    total = len(all_files)

    start = (page - 1) * page_size
    end = start + page_size

    return jsonify({
        "page": page,
        "pageSize": page_size,
        "total": total,
        "files": all_files[start:end]
    })


def download_drive_file_to_temp(file_id: str) -> str:
    url = f"{GOOGLE_DRIVE_FILES_URL}/{file_id}?alt=media&key={API_KEY}"
    r = requests.get(url, stream=True)
    r.raise_for_status()

    tmp = tempfile.NamedTemporaryFile(delete=False)
    for chunk in r.iter_content(8192):
        tmp.write(chunk)
    tmp.close()
    return tmp.name


def convert_awb_to_mp3(input_path, output_path):
    cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-ab", "192k", output_path]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode())


def make_cache_key(file_id, modified_time):
    safe = modified_time.replace(":", "-") if modified_time else str(int(time.time()))
    return f"{file_id}_{safe}"


def convert_and_cache(file_id):
    meta = drive_file_metadata(file_id)
    modified_time = meta.get("modifiedTime") or meta.get("createdTime")

    key = make_cache_key(file_id, modified_time)
    cached = CACHE_DIR / f"{key}.mp3"

    if cached.exists():
        return str(cached)

    src = download_drive_file_to_temp(file_id)
    try:
        tmp_mp3 = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name
        convert_awb_to_mp3(src, tmp_mp3)
        os.replace(tmp_mp3, cached)
        return str(cached)
    finally:
        os.remove(src)


@app.route("/api/convert")
def api_convert():
    file_id = request.args.get("fileId")
    if not file_id:
        return jsonify({"error": "fileId required"}), 400

    meta = drive_file_metadata(file_id)
    modified_time = meta.get("modifiedTime") or meta.get("createdTime")

    key = make_cache_key(file_id, modified_time)
    cached = CACHE_DIR / f"{key}.mp3"

    if cached.exists():
        return send_file(cached, mimetype="audio/mpeg")

    with ongoing_lock:
        fut = ongoing.get(key)
        if not fut:
            fut = executor.submit(convert_and_cache, file_id)
            ongoing[key] = fut

    try:
        return send_file(fut.result(), mimetype="audio/mpeg")
    finally:
        with ongoing_lock:
            ongoing.pop(key, None)


@app.route("/")
def index():
    return render_template("index.html")


# ==========================
# 6️⃣ Railway entrypoint
# ==========================
if __name__ == "__main__":
    port = 5000
    app.run(host="0.0.0.0", port=port)
