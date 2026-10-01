from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"
DB_PATH = Path(os.getenv("JOBCONNECT_DB", str(BASE_DIR / "database" / "jobconnect.db")))

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="/static")


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}_{secrets.token_hex(8)}"


def dumps(value):
    return json.dumps(value, separators=(",", ":"))


def loads(value, default):
    try:
        return json.loads(value) if value is not None else default
    except (TypeError, json.JSONDecodeError):
        return default


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                phone TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('jobseeker','organization')),
                profile_json TEXT NOT NULL DEFAULT '{}',
                profile_complete INTEGER NOT NULL DEFAULT 0,
                organization_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                organization_email TEXT NOT NULL,
                organization_verified INTEGER NOT NULL DEFAULT 0,
                company TEXT NOT NULL,
                logo TEXT,
                title TEXT NOT NULL,
                location TEXT,
                mode TEXT,
                type TEXT,
                salary TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                description TEXT,
                application_deadline TEXT,
                pass_mark INTEGER NOT NULL DEFAULT 60,
                psychometric_questions_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'published',
                published_at TEXT NOT NULL,
                views INTEGER NOT NULL DEFAULT 0,
                applicants INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                applicant_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                psychometric_score INTEGER,
                pass_mark INTEGER NOT NULL,
                interview_eligible INTEGER,
                organization_reviewed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(job_id, applicant_id)
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'info',
                date TEXT NOT NULL,
                read INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reset_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
            """
        )


def profile_completion(user):
    p = loads(user["profile_json"], {})
    if user["role"] == "jobseeker":
        fields = [
            user["first_name"], user["last_name"], user["email"], user["phone"],
            p.get("title"), p.get("location"), p.get("summary"), p.get("skills"),
            p.get("education"), p.get("workExperience"), p.get("cvName"),
            p.get("identityNumber"),
        ]
    else:
        fields = [
            user["first_name"], user["last_name"], user["email"], user["phone"],
            p.get("organizationName"), p.get("industry"), p.get("description"),
        ]
    return round(sum(bool(x) for x in fields) / len(fields) * 100)


def user_json(row):
    return {
        "id": row["id"],
        "firstName": row["first_name"],
        "lastName": row["last_name"],
        "email": row["email"],
        "phone": row["phone"],
        "role": row["role"],
        "profile": loads(row["profile_json"], {}),
        "profileComplete": bool(row["profile_complete"]),
        "organizationVerified": bool(row["organization_verified"]),
        "createdAt": row["created_at"],
    }


def job_json(row):
    return {
        "id": row["id"],
        "organizationId": row["organization_id"],
        "organizationEmail": row["organization_email"],
        "organizationVerified": bool(row["organization_verified"]),
        "company": row["company"],
        "logo": row["logo"],
        "title": row["title"],
        "location": row["location"],
        "mode": row["mode"],
        "type": row["type"],
        "salary": row["salary"],
        "tags": loads(row["tags_json"], []),
        "description": row["description"],
        "applicationDeadline": row["application_deadline"],
        "passMark": row["pass_mark"],
        "psychometricQuestions": loads(row["psychometric_questions_json"], []),
        "status": row["status"],
        "publishedAt": row["published_at"],
        "views": row["views"],
        "applicants": row["applicants"],
    }


def app_json(row):
    return {
        "id": row["id"],
        "jobId": row["job_id"],
        "title": row["title"],
        "company": row["company"],
        "applicantName": row["applicant_name"],
        "applicantEmail": row["applicant_email"],
        "status": row["status"],
        "date": row["applied_at"],
        "psychometricScore": row["psychometric_score"],
        "passMark": row["pass_mark"],
        "interviewEligible": None if row["interview_eligible"] is None else bool(row["interview_eligible"]),
        "organizationReviewed": bool(row["organization_reviewed"]),
    }


def current_user():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    with get_db() as db:
        return db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>?",
            (token, now()),
        ).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify(message="Authentication required."), 401
        return fn(user, *args, **kwargs)
    return wrapper


def notify(user_id, message, kind="info"):
    with get_db() as db:
        db.execute(
            "INSERT INTO notifications(id,user_id,message,type,date) VALUES(?,?,?,?,?)",
            (uid("n"), user_id, message, kind, now()),
        )


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/health")
def health():
    return jsonify(status="ok", service="JobConnect API")


@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    required = ["first_name", "last_name", "email", "phone", "password", "role"]
    if any(not str(data.get(k, "")).strip() for k in required):
        return jsonify(message="All registration fields are required."), 400
    if data["role"] not in ("jobseeker", "organization"):
        return jsonify(message="Invalid account role."), 400
    if len(str(data["password"])) < 6:
        return jsonify(message="Password must contain at least 6 characters."), 400
    email = data["email"].strip().lower()
    with get_db() as db:
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            return jsonify(message="This email is already associated with an existing account."), 409
        db.execute(
            "INSERT INTO users(id,first_name,last_name,email,phone,password_hash,role,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uid("user"), data["first_name"].strip(), data["last_name"].strip(), email,
             data["phone"].strip(), generate_password_hash(data["password"]), data["role"], now()),
        )
    return jsonify(message="Account created successfully."), 201


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], str(data.get("password", ""))):
            return jsonify(message="Invalid credentials. Check your email and password."), 401
        token = secrets.token_urlsafe(40)
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (token, user["id"], expiry))
    return jsonify(token=token, user=user_json(user))


@app.post("/api/auth/logout")
@login_required
def logout(user):
    header = request.headers.get("Authorization", "")
    token = header[7:].strip()
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
    return jsonify(message="Logged out.")


@app.post("/api/auth/change-password")
@login_required
def change_password(user):
    data = request.get_json(silent=True) or {}
    if not check_password_hash(user["password_hash"], data.get("current_password", "")):
        return jsonify(message="Current password is incorrect."), 400
    new_password = str(data.get("new_password", ""))
    if len(new_password) < 6:
        return jsonify(message="New password must contain at least 6 characters."), 400
    with get_db() as db:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), user["id"]))
    return jsonify(message="Password changed successfully.")


@app.post("/api/auth/request-reset")
def request_reset():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    with get_db() as db:
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            return jsonify(message="Recovery request accepted.")
        token = secrets.token_urlsafe(24)
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        db.execute("INSERT INTO reset_tokens(token,user_id,expires_at) VALUES(?,?,?)", (token, user["id"], expiry))
    return jsonify(message="Recovery request accepted.", reset_token=token)


@app.post("/api/auth/reset-password")
def reset_password():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    token = str(data.get("token", ""))
    password = str(data.get("new_password", ""))
    if len(password) < 6:
        return jsonify(message="Password must contain at least 6 characters."), 400
    with get_db() as db:
        row = db.execute(
            "SELECT rt.token,u.id FROM reset_tokens rt JOIN users u ON u.id=rt.user_id WHERE rt.token=? AND u.email=? AND rt.expires_at>?",
            (token, email, now()),
        ).fetchone()
        if not row:
            return jsonify(message="Reset token is invalid or expired."), 400
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), row["id"]))
        db.execute("DELETE FROM reset_tokens WHERE token=?", (token,))
    return jsonify(message="Password reset successfully.")


@app.get("/api/me")
@login_required
def me(user):
    return jsonify(user=user_json(user))


@app.patch("/api/me/profile")
@login_required
def update_profile(user):
    data = request.get_json(silent=True) or {}
    profile = data.get("profile") or {}
    as_dict = dict(user)
    as_dict["profile_json"] = dumps(profile)
    complete = profile_completion(as_dict)
    verified = int(user["role"] == "organization" and complete >= 100)
    with get_db() as db:
        db.execute(
            "UPDATE users SET profile_json=?,profile_complete=?,organization_verified=? WHERE id=?",
            (dumps(profile), int(complete >= 100), verified, user["id"]),
        )
        updated = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return jsonify(user=user_json(updated))


@app.get("/api/stats/public")
def public_stats():
    with get_db() as db:
        jobs = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='published'").fetchone()["c"]
        organizations = db.execute("SELECT COUNT(*) c FROM users WHERE role='organization'").fetchone()["c"]
        jobseekers = db.execute("SELECT COUNT(*) c FROM users WHERE role='jobseeker'").fetchone()["c"]
    return jsonify(jobs=jobs, organizations=organizations, jobseekers=jobseekers)


@app.get("/api/jobs")
def list_jobs():
    query = request.args.get("q", "").strip().lower()
    mode = request.args.get("mode", "").strip()
    job_type = request.args.get("type", "").strip()
    with get_db() as db:
        rows = db.execute("SELECT * FROM jobs WHERE status='published' ORDER BY published_at DESC").fetchall()
    results = []
    for row in rows:
        job = job_json(row)
        haystack = " ".join([job["title"], job["company"], job["location"] or "", *job["tags"]]).lower()
        if query and query not in haystack:
            continue
        if mode and job["mode"] != mode:
            continue
        if job_type and job["type"] != job_type:
            continue
        results.append(job)
    return jsonify(jobs=results)


@app.post("/api/jobs")
@login_required
def create_job(user):
    if user["role"] != "organization":
        return jsonify(message="Only organizations can publish jobs."), 403
    if profile_completion(user) < 100:
        return jsonify(message="Complete your organization profile before publishing a job."), 400
    data = request.get_json(silent=True) or {}
    questions = data.get("psychometric_questions") or []
    if len(questions) != 20:
        return jsonify(message="A job must contain exactly 20 assessment questions."), 400
    if not str(data.get("title", "")).strip():
        return jsonify(message="Job title is required."), 400
    for question in questions:
        if (
            not isinstance(question, list)
            or len(question) != 3
            or not question[0]
            or not isinstance(question[1], list)
            or len(question[1]) != 4
            or not isinstance(question[2], int)
            or not 0 <= question[2] < 4
        ):
            return jsonify(message="Each assessment question needs text, four options, and a valid correct option."), 400
    profile = loads(user["profile_json"], {})
    job_id = uid("job")
    with get_db() as db:
        db.execute(
            """INSERT INTO jobs(
                id,organization_id,organization_email,organization_verified,company,logo,title,location,mode,type,salary,
                tags_json,description,application_deadline,pass_mark,psychometric_questions_json,status,published_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, user["id"], user["email"], 1,
                profile.get("organizationName") or f"{user['first_name']} {user['last_name']}",
                (profile.get("organizationName") or user["first_name"])[:2].upper(),
                str(data["title"]).strip(), str(data.get("location", "")).strip(), str(data.get("mode", "")),
                str(data.get("type", "")), str(data.get("salary", "Not specified")).strip(),
                dumps(data.get("tags") or []), str(data.get("description", "")).strip(),
                data.get("application_deadline"), int(data.get("pass_mark", 60)), dumps(questions), "published", now(),
            ),
        )
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return jsonify(job=job_json(row)), 201


@app.post("/api/jobs/<job_id>/applications")
@login_required
def apply_job(user, job_id):
    if user["role"] != "jobseeker":
        return jsonify(message="Only job seekers can apply."), 403
    if profile_completion(user) < 100:
        return jsonify(message="Complete your profile before applying."), 400
    if not loads(user["profile_json"], {}).get("cvName"):
        return jsonify(message="Upload your CV before applying."), 400
    with get_db() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=? AND status='published'", (job_id,)).fetchone()
        if not job:
            return jsonify(message="Job not found."), 404
        if db.execute("SELECT id FROM applications WHERE job_id=? AND applicant_id=?", (job_id, user["id"])).fetchone():
            return jsonify(message="You have already applied for this job."), 409
        application_id = uid("app")
        db.execute(
            "INSERT INTO applications(id,job_id,applicant_id,status,applied_at,pass_mark) VALUES(?,?,?,?,?,?)",
            (application_id, job_id, user["id"], "Psychometric test required", now(), job["pass_mark"]),
        )
        db.execute("UPDATE jobs SET applicants=applicants+1,views=views+1 WHERE id=?", (job_id,))
        row = db.execute(
            "SELECT a.*,j.title,j.company,u.first_name||' '||u.last_name applicant_name,u.email applicant_email "
            "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.applicant_id WHERE a.id=?",
            (application_id,),
        ).fetchone()
    return jsonify(application=app_json(row)), 201


def application_rows(sql, args):
    with get_db() as db:
        return [app_json(row) for row in db.execute(sql, args).fetchall()]


@app.get("/api/applications/mine")
@login_required
def my_applications(user):
    return jsonify(
        applications=application_rows(
            "SELECT a.*,j.title,j.company,u.first_name||' '||u.last_name applicant_name,u.email applicant_email "
            "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.applicant_id "
            "WHERE a.applicant_id=? ORDER BY a.applied_at DESC",
            (user["id"],),
        )
    )


@app.get("/api/applications/employer")
@login_required
def employer_applications(user):
    return jsonify(
        applications=application_rows(
            "SELECT a.*,j.title,j.company,u.first_name||' '||u.last_name applicant_name,u.email applicant_email "
            "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.applicant_id "
            "WHERE j.organization_id=? ORDER BY a.applied_at DESC",
            (user["id"],),
        )
    )


@app.post("/api/applications/<app_id>/assessment")
@login_required
def submit_assessment(user, app_id):
    if user["role"] != "jobseeker":
        return jsonify(message="Only job seekers can submit assessments."), 403
    data = request.get_json(silent=True) or {}
    answers = data.get("answers") or []
    with get_db() as db:
        row = db.execute(
            "SELECT a.*,j.title,j.psychometric_questions_json,u.first_name||' '||u.last_name applicant_name,u.email applicant_email "
            "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.applicant_id "
            "WHERE a.id=? AND a.applicant_id=?",
            (app_id, user["id"]),
        ).fetchone()
        if not row:
            return jsonify(message="Application not found."), 404
        questions = loads(row["psychometric_questions_json"], [])
        if len(answers) != len(questions):
            return jsonify(message="All assessment questions must be answered."), 400
        score = round(sum(1 for i, q in enumerate(questions) if answers[i] == q[2]) / len(questions) * 100)
        db.execute(
            "UPDATE applications SET psychometric_score=?,status='Awaiting organization review',interview_eligible=NULL,organization_reviewed=0 WHERE id=?",
            (score, app_id),
        )
        updated = db.execute(
            "SELECT a.*,j.title,j.company,u.first_name||' '||u.last_name applicant_name,u.email applicant_email "
            "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.applicant_id WHERE a.id=?",
            (app_id,),
        ).fetchone()
    notify(user["id"], f"Your psychometric test for {row['title']} was scored at {score}%. The organization can now review your application.")
    return jsonify(application=app_json(updated))


@app.post("/api/applications/<app_id>/review")
@login_required
def review_application(user, app_id):
    if user["role"] != "organization":
        return jsonify(message="Only organizations can review candidates."), 403
    decision = (request.get_json(silent=True) or {}).get("decision")
    if decision not in ("interview", "not_selected"):
        return jsonify(message="Invalid review decision."), 400
    with get_db() as db:
        row = db.execute(
            "SELECT a.*,j.title,u.id applicant_user_id FROM applications a "
            "JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.applicant_id "
            "WHERE a.id=? AND j.organization_id=?",
            (app_id, user["id"]),
        ).fetchone()
        if not row:
            return jsonify(message="You cannot review this application."), 403
        if row["psychometric_score"] is None:
            return jsonify(message="The job seeker must complete the test first."), 400
        eligible = decision == "interview"
        status = "Interview" if eligible else "Not selected"
        db.execute(
            "UPDATE applications SET interview_eligible=?,organization_reviewed=1,status=? WHERE id=?",
            (int(eligible), status, app_id),
        )
        updated = db.execute(
            "SELECT a.*,j.title,j.company,u.first_name||' '||u.last_name applicant_name,u.email applicant_email "
            "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.applicant_id WHERE a.id=?",
            (app_id,),
        ).fetchone()
    message = (
        f"You have been invited to interview for {row['title']}."
        if eligible
        else f"Your application for {row['title']} was not selected for interview based on the assessment result."
    )
    notify(row["applicant_user_id"], message, "success" if eligible else "info")
    return jsonify(application=app_json(updated))


@app.get("/api/notifications")
@login_required
def notifications(user):
    with get_db() as db:
        rows = db.execute(
            "SELECT id,message,type,date,read FROM notifications WHERE user_id=? ORDER BY date DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
    return jsonify(notifications=[dict(row) for row in rows])


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True)
