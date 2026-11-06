"""Kumati Manager - Flask + Firebase Firestore.

Run:  python -m pip install -r requirements.txt  &&  python app.py
Open: http://localhost:5000

Data lives in Firestore (collections: entries, persons, settings). The app keeps
a copy in memory so pages open fast and Firebase reads stay low; every change is
written to Firebase first, then to the copy.
"""
import json
import math
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from zoneinfo import ZoneInfo

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import auth, credentials, firestore
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException

# ---------- configuration (override with environment variables) ----------
HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
KEY_FILE = os.getenv("FIREBASE_KEY", os.path.join(HERE, "serviceAccountKey.json"))
DEFAULT_UNIT_PRICE = int(os.getenv("KUMATI_UNIT_PRICE", "1000"))  # starting value only

# Firebase login: set FIREBASE_WEB_API_KEY, ADMIN_EMAIL and ADMIN_PASSWORD.
# APP_PASSWORD remains available as a local single-password fallback.
APP_PASSWORD = os.getenv("APP_PASSWORD", "")      # empty = no login (local use only)
FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ONLINE = bool(os.getenv("RENDER"))                # Render sets this automatically
TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Karachi"))  # servers run in UTC; use Pakistan time

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=ONLINE, PERMANENT_SESSION_LIFETIME=timedelta(days=14))


def today_date():
    return datetime.now(TZ).date()
LOCK = threading.RLock()
db = None
entries = {}     # entry id  -> dict
persons = {}     # person key (lower-case name) -> dict
settings = {"UnitPrice": DEFAULT_UNIT_PRICE}
ROLES = ("admin", "manager", "viewer")


# ------------------------------ firebase ------------------------------
def init_db():
    global db
    if ONLINE and not FIREBASE_WEB_API_KEY and not APP_PASSWORD:
        sys.exit("Set FIREBASE_WEB_API_KEY or APP_PASSWORD before running online.")
    raw = os.getenv("FIREBASE_KEY_JSON")      # online: key stored as a secret variable
    if raw:
        try:
            cred = credentials.Certificate(json.loads(raw))
        except ValueError:
            sys.exit("FIREBASE_KEY_JSON is not valid. Paste the whole content of the key file.")
    elif os.path.exists(KEY_FILE):            # local: key stored in a file
        cred = credentials.Certificate(KEY_FILE)
    else:
        sys.exit("Firebase key file not found:\n  " + KEY_FILE +
                 "\nFirebase console -> Project settings -> Service accounts -> "
                 "Generate new private key, then save it as serviceAccountKey.json "
                 "next to app.py.")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    load_all()
    ensure_initial_admin()


def ensure_initial_admin():
    if not FIREBASE_WEB_API_KEY or not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return
    try:
        user = auth.get_user_by_email(ADMIN_EMAIL)
    except auth.UserNotFoundError:
        user = auth.create_user(email=ADMIN_EMAIL, password=ADMIN_PASSWORD)
    auth.set_custom_user_claims(user.uid, {"role": "admin"})
    db.collection("users").document(user.uid).set({
        "Email": user.email,
        "Role": "admin",
        "Disabled": bool(user.disabled),
        "UpdatedAt": now_iso(),
    }, merge=True)


def load_all():
    with LOCK:
        entries.clear()
        persons.clear()
        for d in db.collection("entries").stream():
            x = d.to_dict()
            x["Id"] = d.id
            entries[d.id] = x
        for d in db.collection("persons").stream():
            x = d.to_dict()
            x["Id"] = d.id
            persons[d.id] = x
        ref = db.collection("settings").document("config")
        snap = ref.get()
        if snap.exists and "UnitPrice" in snap.to_dict():
            settings["UnitPrice"] = snap.to_dict()["UnitPrice"]
        else:
            ref.set({"UnitPrice": DEFAULT_UNIT_PRICE}, merge=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def person_key(name):
    """Names are matched ignoring upper/lower case and extra spaces."""
    k = re.sub(r"\s+", " ", (name or "").strip()).lower()
    k = k.replace("/", "_").replace("\\", "_")
    if k in (".", "..") or (k.startswith("__") and k.endswith("__")):
        k = "p" + k
    return k


def snapshot():
    with LOCK:
        return list(entries.values())


def public(e):
    return {k: e.get(k) for k in ("Id", "EntryDate", "EntryType", "PersonName",
                                  "Phone", "PayMethod", "Units", "Amount", "Note")}


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception(e)
    return jsonify(error="Server error. Check the application logs."), 500


# ------------------------------ login ---------------------------------
@app.before_request
def require_login():
    if request.endpoint in ("login", "healthz", "static"):
        return None
    if session.get("ok") and session.get("uid"):
        return None
    if not FIREBASE_WEB_API_KEY and not APP_PASSWORD:
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Please log in again"), 401
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not FIREBASE_WEB_API_KEY and not APP_PASSWORD:
        return redirect(url_for("index"))
    if request.method == "POST":
        if FIREBASE_WEB_API_KEY:
            body = request.get_json(silent=True) or {}
            email = str(body.get("email") or "").strip().lower()
            password = str(body.get("password") or "")
            try:
                token = firebase_login(email, password)
                user = auth.verify_id_token(token["idToken"])
                profile = db.collection("users").document(user["uid"]).get()
                role = (profile.to_dict() or {}).get("Role", "viewer") if profile.exists else "viewer"
                if role not in ROLES:
                    return jsonify(error="Your account has no valid role"), 403
                session.clear()
                session.update(ok=True, uid=user["uid"], email=user.get("email", email), role=role)
                session.permanent = True
                return jsonify(ok=True)
            except (ValueError, KeyError, auth.InvalidIdTokenError):
                return jsonify(error="Invalid email or password"), 401
        given = request.form.get("password", "")
        if given == APP_PASSWORD:
            session.clear()
            session.update(ok=True, uid="legacy", email="local", role="admin")
            session.permanent = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Wrong password", firebase_auth=False)
    return render_template("login.html", error="", firebase_auth=bool(FIREBASE_WEB_API_KEY))


def firebase_login(email, password):
    if not email or not password:
        raise ValueError("Email and password are required")
    endpoint = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key="
    payload = json.dumps({"email": email, "password": password, "returnSecureToken": True}).encode()
    req = urllib.request.Request(endpoint + urllib.parse.quote(FIREBASE_WEB_API_KEY, safe=""),
                                 data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ValueError("Firebase login failed") from exc


def role_required(*allowed):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if session.get("role") not in allowed:
                return jsonify(error="You do not have permission for this action"), 403
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def audit(action, details):
    db.collection("audit_logs").add({
        "Action": action,
        "Details": details,
        "UserId": session.get("uid"),
        "Email": session.get("email"),
        "CreatedAt": now_iso(),
    })


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/users")
@role_required("admin")
def list_users():
    out = []
    for doc in db.collection("users").stream():
        user = doc.to_dict()
        out.append(dict(Id=doc.id, Email=user.get("Email", ""), Role=user.get("Role", "viewer"),
                        Disabled=bool(user.get("Disabled", False))))
    out.sort(key=lambda item: item["Email"].lower())
    return jsonify(out)


@app.post("/api/users")
@role_required("admin")
def create_user():
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("Email") or "").strip().lower()
    password = str(payload.get("Password") or "")
    role = str(payload.get("Role") or "viewer").lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="Enter a valid email address"), 400
    if len(password) < 6:
        return jsonify(error="Password must be at least 6 characters"), 400
    if role not in ROLES:
        return jsonify(error="Invalid role"), 400
    try:
        user = auth.create_user(email=email, password=password)
    except (auth.EmailAlreadyExistsError, ValueError) as exc:
        return jsonify(error="That email is already in use"), 400
    auth.set_custom_user_claims(user.uid, {"role": role})
    db.collection("users").document(user.uid).set({
        "Email": email, "Role": role, "Disabled": False, "UpdatedAt": now_iso()
    })
    audit("user_created", {"UserId": user.uid, "Email": email, "Role": role})
    return jsonify(ok=True), 201


@app.put("/api/users/<uid>")
@role_required("admin")
def update_user(uid):
    payload = request.get_json(silent=True) or {}
    role = str(payload.get("Role") or "").lower()
    password = str(payload.get("Password") or "")
    if role not in ROLES:
        return jsonify(error="Invalid role"), 400
    if password and len(password) < 6:
        return jsonify(error="Password must be at least 6 characters"), 400
    try:
        user = auth.get_user(uid)
    except auth.UserNotFoundError:
        return jsonify(error="User not found"), 404
    if user.uid == session.get("uid") and role != "admin":
        return jsonify(error="You cannot remove your own admin role"), 400
    auth.set_custom_user_claims(uid, {"role": role})
    if password:
        auth.update_user(uid, password=password)
    db.collection("users").document(uid).set({"Email": user.email, "Role": role,
                                               "Disabled": bool(user.disabled),
                                               "UpdatedAt": now_iso()}, merge=True)
    audit("user_updated", {"UserId": uid, "Email": user.email, "Role": role,
                           "PasswordReset": bool(password)})
    return jsonify(ok=True)


@app.delete("/api/users/<uid>")
@role_required("admin")
def disable_user(uid):
    if uid == session.get("uid"):
        return jsonify(error="You cannot disable your own account"), 400
    try:
        user = auth.get_user(uid)
    except auth.UserNotFoundError:
        return jsonify(error="User not found"), 404
    auth.update_user(uid, disabled=True)
    db.collection("users").document(uid).set({"Disabled": True, "UpdatedAt": now_iso()}, merge=True)
    audit("user_disabled", {"UserId": uid, "Email": user.email})
    return jsonify(ok=True)


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/backup")
@role_required("admin")
def backup():
    return send_file(BytesIO(json.dumps({
        "exported_at": now_iso(),
        "entries": snapshot(),
        "persons": list(persons.values()),
        "settings": settings,
    }, indent=2, ensure_ascii=False).encode("utf-8")),
                    mimetype="application/json", as_attachment=True,
                    download_name=f"kumati-backup-{today_date().isoformat()}.json")


# ------------------------------- pages --------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# -------------------------- month-wise summary ------------------------
def build_summary():
    """Table 2. remaining = previous month remaining + IN - OUT - EXPENSE."""
    zero = lambda m: dict(Month=m, InUnits=0, InAmount=0.0, OutUnits=0,
                          OutAmount=0.0, Expense=0.0)
    by, outs = {}, {}
    for e in snapshot():
        m = e["EntryDate"][:7]
        d = by.setdefault(m, zero(m))
        amt, units, t = float(e.get("Amount") or 0), int(e.get("Units") or 0), e["EntryType"]
        if t == "IN":
            d["InUnits"] += units
            d["InAmount"] += amt
        elif t == "OUT":
            d["OutUnits"] += units
            d["OutAmount"] += amt
            o = outs.setdefault(m, {}).setdefault(e["PersonName"], dict(PersonName=e["PersonName"], Units=0, Amount=0.0))
            o["Units"] += units
            o["Amount"] += amt
        else:
            d["Expense"] += amt

    current = today_date().strftime("%Y-%m")
    by.setdefault(current, zero(current))
    data, carry = [], 0.0
    for m in sorted(by):
        d = by[m]
        d["OutTo"] = sorted(outs.get(m, {}).values(), key=lambda o: o["PersonName"].lower())
        d["Opening"] = carry
        d["MonthNet"] = d["InAmount"] - d["OutAmount"] - d["Expense"]
        carry += d["MonthNet"]
        d["Remaining"] = carry
        data.append(d)
    return current, data


@app.get("/api/summary")
def summary():
    current, data = build_summary()
    return jsonify(dict(current=current, months=data))


# ----------------------------- entries (Table 1) ----------------------
def fetch_entries(month):
    rows = [e for e in snapshot() if e["EntryDate"][:7] == month]
    rows.sort(key=lambda e: (e["EntryDate"], e.get("CreatedAt") or "", e["Id"]))
    return [public(e) for e in rows]


@app.get("/api/entries")
def list_entries():
    return jsonify(fetch_entries(request.args.get("month", today_date().strftime("%Y-%m"))))


def read_payload():
    j = request.get_json(force=True, silent=True) or {}
    etype = j.get("EntryType")
    if etype not in ("IN", "OUT", "EXPENSE"):
        raise ValueError("Type must be IN, OUT or EXPENSE")
    name = re.sub(r"\s+", " ", (j.get("PersonName") or "").strip())[:100]
    if not name:
        raise ValueError("Name / where used is required")
    try:
        d = date.fromisoformat(j.get("EntryDate") or today_date().isoformat())
    except ValueError:
        raise ValueError("Enter a valid date")
    try:
        units = int(float(j.get("Units") or 0))
        amount = float(j.get("Amount") or 0)
    except (TypeError, ValueError):
        raise ValueError("Count and amount must be numbers")
    if not math.isfinite(amount) or amount < 0 or amount > 1e9 or units < 0 or units > 100000:
        raise ValueError("Count or amount is out of range")

    phone = method = None
    if etype == "EXPENSE":
        if amount <= 0:
            raise ValueError("Expense amount must be more than 0")
        units = 0
    else:
        if units <= 0:
            raise ValueError("Kumati count must be at least 1")
        if amount <= 0:
            amount = units * float(settings["UnitPrice"])
        phone = str(j.get("Phone") or "").strip()[:20] or None
        method = str(j.get("PayMethod") or "").strip()[:20] or None
        existing = persons.get(person_key(name))
        if existing:                       # "danish" and "Danish" are one person
            name = existing["Name"]
    return dict(EntryDate=d.isoformat(), EntryType=etype, PersonName=name,
                Phone=phone, PayMethod=method, Units=units, Amount=amount,
                Note=str(j.get("Note") or "").strip()[:200] or None)


def worst_balance(items):
    """Lowest month-end remaining cash and the month where it happens."""
    net = {}
    for e in items:
        m = e["EntryDate"][:7]
        amt = float(e.get("Amount") or 0)
        net[m] = net.get(m, 0.0) + (amt if e["EntryType"] == "IN" else -amt)
    run, worst, where = 0.0, 0.0, None
    for m in sorted(net):
        run += net[m]
        if run < worst:
            worst, where = run, m
    return worst, where


def ensure_cash_ok(after_items):
    """Refuse a change that would push remaining cash below zero."""
    before, _ = worst_balance(list(entries.values()))
    after, month = worst_balance(after_items)
    if after < -0.005 and after < before - 0.005:
        raise ValueError(f"Not enough cash: this would make remaining cash negative in {month}. "
                         "Add the kumati in first, or reduce the amount.")


def save_person(name, phone, method):
    """Remember a person forever (added once, phone/method updated later)."""
    key = person_key(name)
    cur = persons.get(key, {})
    doc = dict(Name=cur.get("Name", name), Phone=phone or cur.get("Phone"),
               PayMethod=method or cur.get("PayMethod"),
               CreatedAt=cur.get("CreatedAt") or now_iso())
    db.collection("persons").document(key).set(doc)
    persons[key] = dict(doc, Id=key)


@app.post("/api/entries")
@role_required("admin", "manager")
def add_entry():
    try:
        p = read_payload()
        with LOCK:
            ensure_cash_ok(list(entries.values()) + [p])
            doc = dict(p, CreatedAt=now_iso())
            ref = db.collection("entries").document()
            ref.set(doc)
            entries[ref.id] = dict(doc, Id=ref.id)
            if p["EntryType"] != "EXPENSE":
                save_person(p["PersonName"], p["Phone"], p["PayMethod"])
            audit("entry_created", {"EntryId": ref.id, "EntryType": p["EntryType"]})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True), 201


@app.put("/api/entries/<eid>")
@role_required("admin", "manager")
def edit_entry(eid):
    try:
        p = read_payload()
        with LOCK:
            old = entries.get(eid)
            if not old:
                raise ValueError("Entry not found (it may have been deleted)")
            after = [dict(p, Id=eid) if k == eid else v for k, v in entries.items()]
            ensure_cash_ok(after)
            doc = dict(p, CreatedAt=old.get("CreatedAt") or now_iso())
            db.collection("entries").document(eid).set(doc)
            entries[eid] = dict(doc, Id=eid)
            if p["EntryType"] != "EXPENSE":
                save_person(p["PersonName"], p["Phone"], p["PayMethod"])
            audit("entry_updated", {"EntryId": eid, "EntryType": p["EntryType"]})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.delete("/api/entries/<eid>")
@role_required("admin", "manager")
def delete_entry(eid):
    try:
        with LOCK:
            if eid not in entries:
                raise ValueError("Entry not found (it may have been deleted)")
            ensure_cash_ok([v for k, v in entries.items() if k != eid])
            db.collection("entries").document(eid).delete()
            entries.pop(eid)
            audit("entry_deleted", {"EntryId": eid})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


# ------------------------------ settings -------------------------------
@app.get("/api/settings")
def read_settings():
    return jsonify(UnitPrice=settings["UnitPrice"],
                   Auth=bool(FIREBASE_WEB_API_KEY or APP_PASSWORD),
                   Role=session.get("role", "admin"), Email=session.get("email", ""))


@app.put("/api/settings")
@role_required("admin", "manager")
def save_settings():
    try:
        price = float((request.get_json(force=True, silent=True) or {}).get("UnitPrice"))
    except (TypeError, ValueError):
        return jsonify(error="Enter a valid price"), 400
    if not math.isfinite(price) or price <= 0 or price > 100000000:
        return jsonify(error="Price must be more than 0"), 400
    price = int(price) if price.is_integer() else price
    with LOCK:
        db.collection("settings").document("config").set({"UnitPrice": price}, merge=True)
        settings["UnitPrice"] = price
        audit("unit_price_changed", {"UnitPrice": price})
    return jsonify(UnitPrice=price)


# ------------------------------ saved persons --------------------------
def person_of(e):
    return person_key(e["PersonName"])


@app.get("/api/persons")
def list_persons():
    agg = {}
    for e in snapshot():
        if e["EntryType"] == "EXPENSE":
            continue
        a = agg.setdefault(person_of(e), dict(InUnits=0, InAmount=0.0, OutUnits=0, OutAmount=0.0))
        k = "In" if e["EntryType"] == "IN" else "Out"
        a[k + "Units"] += int(e.get("Units") or 0)
        a[k + "Amount"] += float(e.get("Amount") or 0)
    with LOCK:
        plist = list(persons.values())
    zero = dict(InUnits=0, InAmount=0.0, OutUnits=0, OutAmount=0.0)
    out = [dict(Id=p["Id"], Name=p["Name"], Phone=p.get("Phone"), PayMethod=p.get("PayMethod"),
                **agg.get(p["Id"], zero)) for p in plist]
    out.sort(key=lambda p: p["Name"].lower())
    return jsonify(out)


@app.get("/api/person-entries")
def person_entries():
    key = request.args.get("key", "")
    with LOCK:
        p = persons.get(key)
    if not p:
        return jsonify(error="Person not found"), 404
    rows = [public(e) for e in snapshot() if e["EntryType"] != "EXPENSE" and person_of(e) == key]
    rows.sort(key=lambda e: (e["EntryDate"], e["Id"]), reverse=True)
    return jsonify(person=dict(Id=p["Id"], Name=p["Name"], Phone=p.get("Phone"),
                               PayMethod=p.get("PayMethod")), entries=rows)


def make_receipt_pdf(entry):
    from reportlab.lib.pagesizes import A5
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A5, leftMargin=28, rightMargin=28,
                            topMargin=28, bottomMargin=28,
                            title=f"Kumati receipt {entry['Id']}")
    rows = [
        ["Date", entry["EntryDate"]],
        ["Type", {"IN": "Kumati in", "OUT": "Kumati out", "EXPENSE": "Expense"}[entry["EntryType"]]],
        ["Person / where used", entry["PersonName"]],
        ["Phone", entry.get("Phone") or ""],
        ["Payment method", entry.get("PayMethod") or ""],
        ["Kumati", "" if entry["EntryType"] == "EXPENSE" else f"{int(entry.get('Units') or 0):,}"],
        ["Amount", f"Rs {money(entry.get('Amount'))}"],
        ["Note", entry.get("Note") or ""],
    ]
    story = [Paragraph("Kumati Manager", styles["Title"]),
             Paragraph("Transaction receipt", styles["Heading2"]), Spacer(1, 10)]
    table = Table(rows, colWidths=[120, 210])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, "#d9cfe8"),
        ("BACKGROUND", (0, 0), (0, -1), "#f6eeff"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(table)
    doc.build(story)
    return buf.getvalue()


@app.get("/api/receipt/<eid>")
def receipt(eid):
    entry = entries.get(eid)
    if not entry:
        return jsonify(error="Entry not found"), 404
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return jsonify(error="PDF receipts need the reportlab package"), 500
    return send_file(BytesIO(make_receipt_pdf(entry)), mimetype="application/pdf",
                     as_attachment=True, download_name=f"receipt-{eid}.pdf")


# ------------------------------ PDF report ----------------------------
def money(n):
    return f"{float(n or 0):,.0f}"


def month_name(m):
    return date(int(m[:4]), int(m[5:7]), 1).strftime("%B %Y")


def make_pdf(month, rows_in, months):
    """Build a PDF with Table 1 (entries of the month) and Table 2 (summary)."""
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=ss["BodyText"], fontSize=8, leading=10)
    head = ParagraphStyle("head", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    title = ParagraphStyle("title", parent=ss["Title"], fontSize=20, alignment=0,
                           textColor=colors.HexColor("#32116f"), spaceAfter=2)
    sub = ParagraphStyle("sub", parent=ss["Heading2"], fontSize=13,
                         textColor=colors.HexColor("#32116f"), spaceBefore=12, spaceAfter=6)

    def P(t, st=cell):
        return Paragraph(escape(str(t if t is not None else "")), st)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24,
                            topMargin=26, bottomMargin=26, title=f"Kumati report {month}")
    story = [Paragraph(f"Kumati Report - {month_name(month)}", title),
             P(f"Generated on {today_date():%d %b %Y}"), Spacer(1, 6),
             Paragraph("Table 1 - Entries", sub)]

    # ---- Table 1 ----
    cols = ["Date", "Type", "Person / where used", "Phone", "Easypaisa / Jazz / Bank",
            "Kumati", "Amount", "Note"]
    rows, tint = [[P(c, head) for c in cols]], []
    label = {"IN": "In", "OUT": "Out", "EXPENSE": "Expense"}
    for i, e in enumerate(rows_in, start=1):
        x = e["EntryType"] == "EXPENSE"
        rows.append([e["EntryDate"], label[e["EntryType"]], P(e["PersonName"]),
                     e["Phone"] or "", e["PayMethod"] or "",
                     "" if x else f"{int(e['Units']):,}", money(e["Amount"]), P(e["Note"])])
        if x:
            tint.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fff3e8")))
        elif e["EntryType"] == "OUT":
            tint.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fff0f0")))
    if rows_in:
        t1 = Table(rows, colWidths=[62, 46, 150, 84, 92, 50, 66, 244], repeatRows=1)
        t1.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7c2de0")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9cfe8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (5, 1), (6, -1), "RIGHT"),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ] + tint))
        story.append(t1)
    else:
        story.append(P("No entries in this month."))

    m = next((x for x in months if x["Month"] == month), None)
    if m:
        story += [Spacer(1, 6), Paragraph(
            f"Opening Rs {money(m['Opening'])} + In Rs {money(m['InAmount'])} "
            f"less Out Rs {money(m['OutAmount'])} less Expense Rs {money(m['Expense'])} = "
            f"<b>Remaining cash Rs {money(m['Remaining'])}</b>",
            ParagraphStyle("tot", parent=cell, fontSize=10, leading=13))]

    # ---- Table 2 ----
    story.append(Paragraph("Table 2 - Summary month wise", sub))
    cols2 = ["Month", "Total kumati in (no)", "Total kumati price", "Kumati out (no)",
             "Kumati out Rs", "Kumati out to", "Expense", "Total remaining cash"]
    rows2, tint2 = [[P(c, head) for c in cols2]], []
    for i, x in enumerate(months, start=1):
        outto = ", ".join(f"{o['PersonName']} ({money(o['Amount'])})" for o in x["OutTo"]) or "-"
        rows2.append([month_name(x["Month"]), f"{int(x['InUnits']):,}", money(x["InAmount"]),
                      f"{int(x['OutUnits']):,}", money(x["OutAmount"]), P(outto),
                      money(x["Expense"]), money(x["Remaining"])])
        if x["Month"] == month:
            tint2.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#dff7ea")))
    t2 = Table(rows2, colWidths=[90, 70, 90, 70, 80, 184, 80, 130], repeatRows=1)
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0aa84a")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfe9da")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (4, -1), "RIGHT"), ("ALIGN", (6, 1), (7, -1), "RIGHT"),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ] + tint2))
    story.append(t2)
    doc.build(story)
    return buf.getvalue()


@app.get("/api/pdf")
def pdf_report():
    month = request.args.get("month", today_date().strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return "Invalid month", 400
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return "PDF needs the reportlab package. In the terminal run: python -m pip install reportlab", 500
    _, months = build_summary()
    return send_file(BytesIO(make_pdf(month, fetch_entries(month), months)),
                     mimetype="application/pdf", as_attachment=True,
                     download_name=f"kumati-{month}.pdf")


@app.get("/api/xlsx")
def xlsx_report():
    month = request.args.get("month", today_date().strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return "Invalid month", 400
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        return "Excel export needs openpyxl. Install requirements.txt first.", 500

    _, months = build_summary()
    rows = fetch_entries(month)
    workbook = Workbook()
    entries_sheet = workbook.active
    entries_sheet.title = "Entries"
    headers = ["Date", "Type", "Person / where used", "Phone", "Payment method",
               "Kumati", "Amount", "Note"]
    entries_sheet.append(headers)
    for cell in entries_sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="7C2DE0")
    for e in rows:
        entries_sheet.append([
            e["EntryDate"], e["EntryType"], e["PersonName"], e.get("Phone") or "",
            e.get("PayMethod") or "", e.get("Units") or 0, e.get("Amount") or 0,
            e.get("Note") or "",
        ])
    entries_sheet.freeze_panes = "A2"
    entries_sheet.auto_filter.ref = entries_sheet.dimensions
    for column in entries_sheet.columns:
        width = min(max(len(str(cell.value or "")) for cell in column) + 2, 35)
        entries_sheet.column_dimensions[column[0].column_letter].width = width

    summary_sheet = workbook.create_sheet("Summary")
    summary_headers = ["Month", "Kumati in", "Kumati in Rs", "Kumati out",
                       "Kumati out Rs", "Expense Rs", "Opening cash", "Remaining cash"]
    summary_sheet.append(summary_headers)
    for cell in summary_sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0AA84A")
    for item in months:
        summary_sheet.append([
            item["Month"], item["InUnits"], item["InAmount"], item["OutUnits"],
            item["OutAmount"], item["Expense"], item["Opening"], item["Remaining"],
        ])
    summary_sheet.freeze_panes = "A2"
    summary_sheet.auto_filter.ref = summary_sheet.dimensions
    for column in summary_sheet.columns:
        width = min(max(len(str(cell.value or "")) for cell in column) + 2, 22)
        summary_sheet.column_dimensions[column[0].column_letter].width = width

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"kumati-{month}.xlsx")


if __name__ == "__main__":                    # local:  python app.py
    init_db()
    app.run(debug=True, use_reloader=False)
elif os.getenv("KUMATI_SKIP_INIT") != "1":    # online: gunicorn imports this file
    init_db()
