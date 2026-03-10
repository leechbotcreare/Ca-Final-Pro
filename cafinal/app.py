from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, abort
from functools import wraps
from pymongo import MongoClient
import os, uuid, time, hashlib, re, json
from datetime import datetime, date

app = Flask(__name__)
app.secret_key = "cafinal_change_this_secret_2024"
TOKEN_SECRET   = "cafinal_stream_secret_2024"

# ─────────────────────────────────────────
# MONGODB CONNECTION
# ─────────────────────────────────────────
MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb+srv://1stwalah_db_user:cafinalpro@clusterca.pnvpckn.mongodb.net/?appName=ClusterCA"
)
client = MongoClient(MONGO_URI)
db     = client["cafinaldb"]

users_col    = db["users"]
lectures_col = db["lectures"]
pdfs_col     = db["pdfs"]
chapters_col = db["chapters"]
ann_col      = db["announcements"]
progress_col = db["progress"]
ip_col       = db["ip_logs"]
settings_col = db["settings"]

# ─────────────────────────────────────────
# TOKEN HELPERS
# ─────────────────────────────────────────
def make_token(lecture_id, email):
    hour = str(int(time.time()) // 3600)
    raw  = f"{lecture_id}:{email}:{hour}:{TOKEN_SECRET}"
    return hashlib.sha256(raw.encode()).hexdigest()

def check_token(token, lecture_id, email):
    hour_now = int(time.time()) // 3600
    for h in [hour_now, hour_now - 1]:
        raw = f"{lecture_id}:{email}:{h}:{TOKEN_SECRET}"
        if hashlib.sha256(raw.encode()).hexdigest() == token:
            return True
    return False

# ─────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────
SETTINGS_DEFAULTS = {
    "site_name": "CAFinalPro", "tagline": "India\u2019s CA Final Learning Platform",
    "g1_price": "\u20b92000", "g2_price": "Coming Soon",
    "support_email": "", "whatsapp_number": "", "yt_api_key": "",
    "show_group2": True, "registration_open": True, "show_announcements": True,
    "pw_message": "The lectures on this platform are of PW (Physics Wallah). If you want doubt solving facility and Regular Test benefits, kindly purchase directly from PW as well.",
    "desc_FR": "", "desc_AFM": "", "desc_AUDIT": "", "desc_DT": "", "desc_IDT": "",
}

def load_settings():
    doc = settings_col.find_one({"_id": "site_settings"})
    if not doc:
        return dict(SETTINGS_DEFAULTS)
    result = dict(SETTINGS_DEFAULTS)
    result.update({k: v for k, v in doc.items() if k != "_id"})
    return result

def save_settings(data):
    data["_id"] = "site_settings"
    settings_col.replace_one({"_id": "site_settings"}, data, upsert=True)

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def clean(doc):
    if doc is None: return None
    d = dict(doc); d.pop("_id", None); return d

def clean_list(cursor):
    return [clean(d) for d in cursor]

def get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()

def track_ip_login(user_id, email):
    today = date.today().isoformat()
    ip    = get_client_ip()
    entry = ip_col.find_one({"user_id": user_id, "date": today})
    if not entry:
        entry = {"user_id": user_id, "email": email, "date": today, "ips": [], "flagged": False, "seen_by_admin": False}
    if ip not in entry["ips"]:
        entry["ips"].append(ip)
    if len(entry["ips"]) > 3:
        entry["flagged"] = True
    ip_col.replace_one({"user_id": user_id, "date": today}, entry, upsert=True)
    return entry

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "cafinal2024"

def admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get("admin_logged_in"): return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return d

def login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get("student_logged_in"): return redirect(url_for("login"))
        return f(*a, **kw)
    return d

SUBJECT_NAMES = {
    "FR": "Financial Reporting (FR)", "AFM": "Advanced Financial Management (AFM)",
    "AUDIT": "Advanced Auditing, Assurance & Professional Ethics",
    "DT": "Direct Tax Laws & International Taxation", "IDT": "Indirect Tax Law",
}

def get_chapters(subject):
    return clean_list(chapters_col.find({"subject": subject}).sort("order", 1))

def get_chapter_names(subject):
    return [c["name"] for c in get_chapters(subject)]

def ensure_chapter_exists(subject, name):
    if not chapters_col.find_one({"subject": subject, "name": name}):
        max_doc = chapters_col.find_one({"subject": subject}, sort=[("order", -1)])
        max_order = max_doc["order"] if max_doc else 0
        chapters_col.insert_one({"id": str(uuid.uuid4()), "subject": subject, "name": name, "order": max_order + 1})

def get_lectures_safe(subject=None):
    query = {"visible": True}
    if subject: query["subject"] = subject
    lecs = clean_list(lectures_col.find(query))
    safe = [{k: v for k, v in l.items() if k != "video_id"} for l in lecs]
    ch_order = {c["name"]: i for i, c in enumerate(get_chapters(subject))} if subject else {}
    safe.sort(key=lambda x: (ch_order.get(x.get("chapter", ""), 999), x.get("order", 99)))
    return safe

def get_pdfs(subject=None):
    query = {"visible": True}
    if subject: query["subject"] = subject
    return clean_list(pdfs_col.find(query))

def group_by_chapter(lectures, subject=None):
    chapters = {}
    for l in lectures:
        ch = l.get("chapter", "General")
        chapters.setdefault(ch, []).append(l)
    if subject:
        ordered = {}
        for ch_name in get_chapter_names(subject):
            if ch_name in chapters: ordered[ch_name] = chapters[ch_name]
        for ch_name, lecs in chapters.items():
            if ch_name not in ordered: ordered[ch_name] = lecs
        return ordered
    return chapters

def get_user(email):
    return clean(users_col.find_one({"email": email.lower()}))

def is_blocked(user):  return user.get("blocked", False) if user else False
def has_access(user):  return user.get("access_granted", False) if user else False

# ─────────────────────────────────────────
# STREAM
# ─────────────────────────────────────────
@app.route("/stream/<lecture_id>")
@login_required
def stream(lecture_id):
    token = request.args.get("t", ""); email = session.get("student_email", "")
    if not check_token(token, lecture_id, email): abort(403)
    user = get_user(email)
    if not user or is_blocked(user) or not has_access(user): abort(403)
    lecture = clean(lectures_col.find_one({"id": lecture_id, "visible": True}))
    if not lecture: abort(404)
    video_id = lecture.get("video_id", "")
    if not video_id: abort(404)
    url = (f"https://www.youtube-nocookie.com/embed/{video_id}"
           f"?autoplay=1&rel=0&modestbranding=1&iv_load_policy=3&showinfo=0&color=white&playsinline=1&enablejsapi=1")
    return jsonify({"url": url})

# ─────────────────────────────────────────
# STUDENT ROUTES
# ─────────────────────────────────────────
@app.route("/")
def index():
    s = load_settings()
    return render_template("index.html", settings=s,
        fr_count=len(get_lectures_safe("FR")), afm_count=len(get_lectures_safe("AFM")),
        aud_count=len(get_lectures_safe("AUDIT")))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        user = get_user(email)
        if user and user.get("password") == password:
            if is_blocked(user):
                return render_template("login.html", error="Your account has been suspended. Please contact admin.")
            session.update({"student_logged_in": True, "student_email": user["email"],
                "student_name": user.get("name", email.split("@")[0]),
                "student_id": user["id"], "has_access": has_access(user)})
            entry = track_ip_login(user["id"], user["email"])
            if entry.get("flagged"): session["ip_warning"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid email or password.")
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    settings = load_settings()
    if not settings.get("registration_open", True):
        return render_template("signup.html", error="Registration is currently closed.")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        if users_col.find_one({"email": email}):
            return render_template("signup.html", error="Email already registered.")
        new_user = {"id": str(uuid.uuid4()), "name": name, "email": email,
            "password": password, "joined": datetime.now().isoformat(),
            "access_granted": False, "blocked": False}
        users_col.insert_one(new_user)
        session.update({"student_logged_in": True, "student_email": email,
            "student_name": name, "student_id": new_user["id"], "has_access": False})
        return redirect(url_for("dashboard"))
    return render_template("signup.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    settings = load_settings()
    subjects = [
        {"code": "FR",    "name": "Financial Reporting (FR)",    "icon": "\U0001f4ca", "count": len(get_lectures_safe("FR"))},
        {"code": "AFM",   "name": "Adv. Financial Management",   "icon": "\U0001f4c8", "count": len(get_lectures_safe("AFM"))},
        {"code": "AUDIT", "name": "Adv. Auditing & Ethics",      "icon": "\U0001f50d", "count": len(get_lectures_safe("AUDIT"))},
    ]
    ip_warning = session.pop("ip_warning", False)
    return render_template("dashboard.html", settings=settings, subjects=subjects,
        announcements=clean_list(ann_col.find().sort("created_at", -1).limit(3)),
        name=session.get("student_name", "Student"), ip_warning=ip_warning,
        has_access=session.get("has_access", False))

@app.route("/subject/<code>")
@login_required
def subject(code):
    code = code.upper()
    if code not in SUBJECT_NAMES: return redirect(url_for("dashboard"))
    lectures = get_lectures_safe(code); settings = load_settings()
    return render_template("subject.html", code=code, subject_name=SUBJECT_NAMES[code],
        chapters=group_by_chapter(lectures, code), lectures=lectures, pdfs=get_pdfs(code),
        settings=settings, has_access=session.get("has_access", False),
        pw_message=settings.get("pw_message", ""), whatsapp_number=settings.get("whatsapp_number", ""))

@app.route("/watch/<lecture_id>")
@login_required
def watch(lecture_id):
    email = session.get("student_email", ""); user = get_user(email)
    if is_blocked(user): session.clear(); return redirect(url_for("login"))
    if not has_access(user):
        settings = load_settings()
        wa_num = settings.get("whatsapp_number", "").strip().replace("+", "").replace(" ", "")
        raw_lec = clean(lectures_col.find_one({"id": lecture_id, "visible": True}))
        lec_name = raw_lec.get("title", "this lecture") if raw_lec else "this lecture"
        return render_template("paywall.html", lecture_name=lec_name, whatsapp_number=wa_num,
            whatsapp_display=settings.get("whatsapp_number", ""),
            pw_message=settings.get("pw_message", ""), settings=settings)
    raw_lec = clean(lectures_col.find_one({"id": lecture_id, "visible": True}))
    if not raw_lec: return redirect(url_for("dashboard"))
    code = raw_lec.get("subject", "FR"); all_lecs = get_lectures_safe(code)
    curr_idx = next((i for i, l in enumerate(all_lecs) if l["id"] == lecture_id), 0)
    token = make_token(lecture_id, email)
    safe_lec = {k: v for k, v in raw_lec.items() if k != "video_id"}
    return render_template("watch.html", lecture=safe_lec, all_lecs=all_lecs, curr_idx=curr_idx,
        prev_lec=all_lecs[curr_idx-1] if curr_idx > 0 else None,
        next_lec=all_lecs[curr_idx+1] if curr_idx < len(all_lecs)-1 else None,
        subject_name=SUBJECT_NAMES.get(code, code), stream_token=token, settings=load_settings())

# ─────────────────────────────────────────
# PROGRESS
# ─────────────────────────────────────────
@app.route("/api/progress/save", methods=["POST"])
@login_required
def save_progress():
    data = request.get_json(silent=True) or {}
    lid = data.get("lecture_id", ""); pos = data.get("position", 0); dur = data.get("duration", 0)
    if not lid: return jsonify({"ok": False})
    uid = session.get("student_id", "")
    completed = (dur > 0 and pos / dur >= 0.9)
    progress_col.update_one({"uid": uid, "lid": lid},
        {"$set": {"position": pos, "duration": dur, "completed": completed, "updated": datetime.now().isoformat()}},
        upsert=True)
    return jsonify({"ok": True})

@app.route("/api/progress/get/<lecture_id>")
@login_required
def get_progress(lecture_id):
    uid = session.get("student_id", "")
    entry = clean(progress_col.find_one({"uid": uid, "lid": lecture_id}))
    if entry:
        return jsonify({"position": entry.get("position", 0), "duration": entry.get("duration", 0), "completed": entry.get("completed", False)})
    return jsonify({"position": 0, "duration": 0, "completed": False})

# ─────────────────────────────────────────
# YOUTUBE DURATION
# ─────────────────────────────────────────
@app.route("/admin/api/yt_duration")
@admin_required
def yt_duration():
    video_id = request.args.get("v", "").strip()
    if not video_id: return jsonify({"error": "No video ID"})
    api_key = load_settings().get("yt_api_key", "")
    if not api_key: return jsonify({"error": "No YouTube API key set in Settings"})
    import urllib.request as ur
    url = f"https://www.googleapis.com/youtube/v3/videos?part=contentDetails&id={video_id}&key={api_key}"
    try:
        with ur.urlopen(url, timeout=5) as r: d = json.loads(r.read())
        items = d.get("items", [])
        if not items: return jsonify({"error": "Video not found"})
        iso = items[0]["contentDetails"]["duration"]
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
        if not m: return jsonify({"error": "Parse error"})
        h, mi, s = int(m.group(1) or 0), int(m.group(2) or 0), int(m.group(3) or 0)
        fmt = f"{h}h {mi}m" if h else (f"{mi}m {s}s" if s else f"{mi}m")
        return jsonify({"duration": fmt})
    except Exception as e: return jsonify({"error": str(e)})

# ─────────────────────────────────────────
# ADMIN — LOGIN / DASHBOARD
# ─────────────────────────────────────────
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("username") == ADMIN_USERNAME and request.form.get("password") == ADMIN_PASSWORD:
            session["admin_logged_in"] = True; return redirect(url_for("admin_dashboard"))
        return render_template("admin/login.html", error="Invalid credentials.")
    return render_template("admin/login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None); return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    ip_alert_count = ip_col.count_documents({"flagged": True, "seen_by_admin": False})
    recent = sorted(
        clean_list(lectures_col.find().sort("created_at", -1).limit(5)) +
        clean_list(pdfs_col.find().sort("created_at", -1).limit(3)),
        key=lambda x: x.get("created_at", ""), reverse=True)[:8]
    return render_template("admin/dashboard.html",
        lec_count=lectures_col.count_documents({}), pdf_count=pdfs_col.count_documents({}),
        user_count=users_col.count_documents({}), ann_count=ann_col.count_documents({}),
        recent=recent, ip_alert_count=ip_alert_count)

# ─────────────────────────────────────────
# ADMIN — LECTURES
# ─────────────────────────────────────────
@app.route("/admin/lectures")
@admin_required
def admin_lectures():
    sf = request.args.get("subject", ""); q = request.args.get("search", "").lower()
    query = {}
    if sf: query["subject"] = sf
    if q: query["$or"] = [{"title": {"$regex": q, "$options": "i"}}, {"chapter": {"$regex": q, "$options": "i"}}]
    lectures = clean_list(lectures_col.find(query).sort([("subject",1),("chapter",1),("order",1)]))
    return render_template("admin/lectures.html", lectures=lectures, subj_filter=sf, search=q)

@app.route("/admin/lectures/add", methods=["GET", "POST"])
@admin_required
def admin_add_lecture():
    ALL_S = ["FR","AFM","AUDIT","DT","IDT"]
    if request.method == "POST":
        title=request.form.get("title","").strip(); subject=request.form.get("subject","")
        chapter=request.form.get("chapter","").strip(); new_ch=request.form.get("new_chapter","").strip()
        if new_ch: chapter=new_ch
        video_id=request.form.get("video_id","").strip()
        if not title or not subject or not chapter or not video_id:
            return render_template("admin/lecture_form.html", error="All required fields must be filled.",
                lecture={}, action="Add", all_chapters={s: get_chapter_names(s) for s in ALL_S})
        ensure_chapter_exists(subject, chapter)
        lectures_col.insert_one({"id": str(uuid.uuid4()), "title": title, "subject": subject,
            "chapter": chapter, "video_id": video_id,
            "duration": request.form.get("duration","").strip(),
            "description": request.form.get("description","").strip(),
            "order": int(request.form.get("order",99) or 99),
            "visible": request.form.get("visible")=="true", "created_at": datetime.now().isoformat()})
        flash("\u2705 Lecture added!", "success"); return redirect(url_for("admin_lectures"))
    return render_template("admin/lecture_form.html", lecture={}, action="Add",
        all_chapters={s: get_chapter_names(s) for s in ALL_S})

@app.route("/admin/lectures/edit/<lid>", methods=["GET", "POST"])
@admin_required
def admin_edit_lecture(lid):
    ALL_S = ["FR","AFM","AUDIT","DT","IDT"]
    lecture = clean(lectures_col.find_one({"id": lid}))
    if not lecture: return redirect(url_for("admin_lectures"))
    if request.method == "POST":
        subj=request.form.get("subject",""); chapter=request.form.get("chapter","").strip()
        new_ch=request.form.get("new_chapter","").strip()
        if new_ch: chapter=new_ch
        ensure_chapter_exists(subj, chapter)
        lectures_col.update_one({"id": lid}, {"$set": {
            "title": request.form.get("title","").strip(), "subject": subj, "chapter": chapter,
            "video_id": request.form.get("video_id","").strip(),
            "duration": request.form.get("duration","").strip(),
            "description": request.form.get("description","").strip(),
            "order": int(request.form.get("order",99) or 99),
            "visible": request.form.get("visible")=="true"}})
        flash("\u2705 Updated!", "success"); return redirect(url_for("admin_lectures"))
    return render_template("admin/lecture_form.html", lecture=lecture, action="Edit",
        all_chapters={s: get_chapter_names(s) for s in ALL_S})

@app.route("/admin/lectures/delete/<lid>", methods=["POST"])
@admin_required
def admin_delete_lecture(lid):
    lectures_col.delete_one({"id": lid}); flash("\U0001f5d1\ufe0f Deleted.", "info")
    return redirect(url_for("admin_lectures"))

@app.route("/admin/lectures/toggle/<lid>", methods=["POST"])
@admin_required
def admin_toggle_lecture(lid):
    doc = lectures_col.find_one({"id": lid})
    if doc: lectures_col.update_one({"id": lid}, {"$set": {"visible": not doc.get("visible", True)}})
    flash("\u2705 Visibility updated!", "success"); return redirect(url_for("admin_lectures"))

# ─────────────────────────────────────────
# ADMIN — PDFs
# ─────────────────────────────────────────
@app.route("/admin/pdfs")
@admin_required
def admin_pdfs():
    sf=request.args.get("subject",""); q=request.args.get("search","").lower()
    query={}
    if sf: query["subject"]=sf
    if q: query["title"]={"$regex":q,"$options":"i"}
    return render_template("admin/pdfs.html", pdfs=clean_list(pdfs_col.find(query)), subj_filter=sf, search=q)

@app.route("/admin/pdfs/add", methods=["GET","POST"])
@admin_required
def admin_add_pdf():
    if request.method == "POST":
        title=request.form.get("title","").strip(); subject=request.form.get("subject",""); file_id=request.form.get("file_id","").strip()
        if not title or not subject or not file_id:
            return render_template("admin/pdf_form.html", error="Required fields missing.", pdf={}, action="Add")
        pdfs_col.insert_one({"id":str(uuid.uuid4()), "title":title, "subject":subject, "file_id":file_id,
            "pages":request.form.get("pages","").strip(), "version":request.form.get("version","").strip(),
            "visible":request.form.get("visible")=="true", "created_at":datetime.now().isoformat()})
        flash("\u2705 PDF added!", "success"); return redirect(url_for("admin_pdfs"))
    return render_template("admin/pdf_form.html", pdf={}, action="Add")

@app.route("/admin/pdfs/edit/<pid>", methods=["GET","POST"])
@admin_required
def admin_edit_pdf(pid):
    pdf=clean(pdfs_col.find_one({"id":pid}))
    if not pdf: return redirect(url_for("admin_pdfs"))
    if request.method == "POST":
        pdfs_col.update_one({"id":pid},{"$set":{"title":request.form.get("title","").strip(),
            "subject":request.form.get("subject",""), "file_id":request.form.get("file_id","").strip(),
            "pages":request.form.get("pages","").strip(), "version":request.form.get("version","").strip(),
            "visible":request.form.get("visible")=="true"}})
        flash("\u2705 Updated!", "success"); return redirect(url_for("admin_pdfs"))
    return render_template("admin/pdf_form.html", pdf=pdf, action="Edit")

@app.route("/admin/pdfs/delete/<pid>", methods=["POST"])
@admin_required
def admin_delete_pdf(pid):
    pdfs_col.delete_one({"id":pid}); flash("\U0001f5d1\ufe0f Deleted.","info"); return redirect(url_for("admin_pdfs"))

@app.route("/admin/pdfs/toggle/<pid>", methods=["POST"])
@admin_required
def admin_toggle_pdf(pid):
    doc=pdfs_col.find_one({"id":pid})
    if doc: pdfs_col.update_one({"id":pid},{"$set":{"visible":not doc.get("visible",True)}})
    flash("\u2705 Visibility updated!","success"); return redirect(url_for("admin_pdfs"))

# ─────────────────────────────────────────
# ADMIN — ANNOUNCEMENTS
# ─────────────────────────────────────────
@app.route("/admin/announcements")
@admin_required
def admin_announcements():
    return render_template("admin/announcements.html",
        announcements=clean_list(ann_col.find().sort("created_at",-1)))

@app.route("/admin/announcements/add", methods=["POST"])
@admin_required
def admin_add_announcement():
    ann_col.insert_one({"id":str(uuid.uuid4()), "title":request.form.get("title","").strip(),
        "body":request.form.get("body","").strip(), "icon":request.form.get("icon","\U0001f4e2") or "\U0001f4e2",
        "created_at":datetime.now().isoformat()})
    flash("\u2705 Posted!","success"); return redirect(url_for("admin_announcements"))

@app.route("/admin/announcements/delete/<aid>", methods=["POST"])
@admin_required
def admin_delete_announcement(aid):
    ann_col.delete_one({"id":aid}); flash("\U0001f5d1\ufe0f Deleted.","info")
    return redirect(url_for("admin_announcements"))

# ─────────────────────────────────────────
# ADMIN — USERS
# ─────────────────────────────────────────
@app.route("/admin/users")
@admin_required
def admin_users():
    users = clean_list(users_col.find().sort("joined",-1))
    flag_map = {}
    for e in ip_col.find({"flagged":True}):
        uid=e["user_id"]
        if uid not in flag_map or e["date"] > flag_map[uid]["date"]: flag_map[uid]=clean(e)
    return render_template("admin/users.html", users=users, flag_map=flag_map)

@app.route("/admin/users/delete/<uid>", methods=["POST"])
@admin_required
def admin_delete_user(uid):
    users_col.delete_one({"id":uid}); flash("\U0001f5d1\ufe0f Deleted.","info"); return redirect(url_for("admin_users"))

@app.route("/admin/users/block/<uid>", methods=["POST"])
@admin_required
def admin_block_user(uid):
    users_col.update_one({"id":uid},{"$set":{"blocked":True}}); flash("\U0001f512 Blocked.","info"); return redirect(url_for("admin_users"))

@app.route("/admin/users/unblock/<uid>", methods=["POST"])
@admin_required
def admin_unblock_user(uid):
    users_col.update_one({"id":uid},{"$set":{"blocked":False}}); flash("\u2705 Unblocked.","success"); return redirect(url_for("admin_users"))

@app.route("/admin/users/grant/<uid>", methods=["POST"])
@admin_required
def admin_grant_access(uid):
    users_col.update_one({"id":uid},{"$set":{"access_granted":True}}); flash("\u2705 Access granted.","success"); return redirect(url_for("admin_users"))

@app.route("/admin/users/revoke/<uid>", methods=["POST"])
@admin_required
def admin_revoke_access(uid):
    users_col.update_one({"id":uid},{"$set":{"access_granted":False}}); flash("\U0001f512 Access revoked.","info"); return redirect(url_for("admin_users"))

# ─────────────────────────────────────────
# ADMIN — IP LOGS
# ─────────────────────────────────────────
@app.route("/admin/ip_logs")
@admin_required
def admin_ip_logs():
    all_logs = clean_list(ip_col.find().sort("date",-1))
    flagged  = [e for e in all_logs if e.get("flagged")]
    ip_col.update_many({"flagged":True},{"$set":{"seen_by_admin":True}})
    return render_template("admin/ip_logs.html", logs=flagged, all_logs=all_logs)

# ─────────────────────────────────────────
# ADMIN — SETTINGS
# ─────────────────────────────────────────
@app.route("/admin/settings", methods=["GET","POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        s = load_settings()
        s.update({"site_name":request.form.get("site_name","CAFinalPro"),
            "tagline":request.form.get("tagline",""), "g1_price":request.form.get("g1_price",""),
            "g2_price":request.form.get("g2_price",""), "support_email":request.form.get("support_email",""),
            "whatsapp_number":request.form.get("whatsapp_number","").strip(),
            "pw_message":request.form.get("pw_message",""),
            "yt_api_key":request.form.get("yt_api_key","").strip(),
            "show_group2":"show_group2" in request.form,
            "registration_open":"registration_open" in request.form,
            "show_announcements":"show_announcements" in request.form})
        for code in ["FR","AFM","AUDIT","DT","IDT"]:
            s[f"desc_{code}"] = request.form.get(f"desc_{code}","")
        new_pw = request.form.get("new_password","").strip()
        if new_pw:
            global ADMIN_PASSWORD; ADMIN_PASSWORD = new_pw
        save_settings(s); flash("\u2705 Settings saved!","success"); return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", settings=load_settings(), subject_names=SUBJECT_NAMES)

# ─────────────────────────────────────────
# ADMIN — CHAPTERS
# ─────────────────────────────────────────
@app.route("/admin/chapters")
@admin_required
def admin_chapters():
    subj=request.args.get("subject","FR"); chapters=get_chapters(subj)
    for c in chapters: c["lec_count"]=lectures_col.count_documents({"subject":subj,"chapter":c["name"]})
    return render_template("admin/chapters.html", chapters=chapters, subj=subj, subjects=SUBJECT_NAMES)

@app.route("/admin/chapters/add", methods=["POST"])
@admin_required
def admin_add_chapter():
    subj=request.form.get("subject","FR"); name=request.form.get("name","").strip()
    if name: ensure_chapter_exists(subj,name); flash("\u2705 Chapter added!","success")
    return redirect(url_for("admin_chapters",subject=subj))

@app.route("/admin/chapters/rename/<cid>", methods=["POST"])
@admin_required
def admin_rename_chapter(cid):
    new_name=request.form.get("name","").strip()
    if not new_name: flash("Name cannot be empty.","error"); return redirect(url_for("admin_chapters"))
    chapter=clean(chapters_col.find_one({"id":cid}))
    if chapter:
        old_name=chapter["name"]; subj=chapter["subject"]
        chapters_col.update_one({"id":cid},{"$set":{"name":new_name}})
        lectures_col.update_many({"subject":subj,"chapter":old_name},{"$set":{"chapter":new_name}})
        flash("\u2705 Renamed!","success"); return redirect(url_for("admin_chapters",subject=subj))
    return redirect(url_for("admin_chapters"))

@app.route("/admin/chapters/delete/<cid>", methods=["POST"])
@admin_required
def admin_delete_chapter(cid):
    chapter=clean(chapters_col.find_one({"id":cid}))
    if chapter:
        subj=chapter["subject"]; chapters_col.delete_one({"id":cid})
        flash("\U0001f5d1\ufe0f Chapter deleted.","info"); return redirect(url_for("admin_chapters",subject=subj))
    return redirect(url_for("admin_chapters"))

@app.route("/admin/chapters/reorder", methods=["POST"])
@admin_required
def admin_reorder_chapters():
    ids=request.form.getlist("ids[]")
    for i,cid in enumerate(ids): chapters_col.update_one({"id":cid},{"$set":{"order":i+1}})
    return ("",204)

# ─────────────────────────────────────────
# RUN
# ─────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
