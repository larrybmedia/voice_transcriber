import os
import tempfile
import secrets
import hashlib
import subprocess
import shutil
import threading
import uuid

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from groq import Groq
from flask_migrate import Migrate
from models import (
    db,
    User,
    Subscription,
    UserCredit,
    CreditTransaction,
)

from subscription_service import (
    authorize_action,
    check_action,
)

import resend

import jwt
from datetime import datetime, timedelta, timezone


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")

RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL",
    "noreply@bestvisionenterprises.ng"
)


# ============================================================
# CREATE FLASK APPLICATION
# ============================================================

app = Flask(__name__)


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

database_url = os.getenv(
    "DATABASE_URL",
    "sqlite:///voice_transcriber.db"
)

# Render/PostgreSQL sometimes provides postgres://
# SQLAlchemy expects postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace(
        "postgres://",
        "postgresql://",
        1
    )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


# ============================================================
# INITIALIZE DATABASE
# ============================================================

db.init_app(app)

migrate = Migrate(
    app,
    db
)


# ============================================================
# CORS
# ============================================================

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "https://voice-transcribe-11.web.app",
                "https://voice-transcribe-11.firebaseapp.com",
                r"^http://localhost:\d+$",
                r"^http://127\.0\.0\.1:\d+$",
            ],
            "methods": [
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
            ],
            "allow_headers": [
                "Content-Type",
                "Authorization",
            ],
            "supports_credentials": True,
        }
    }
)

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return "", 204


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return "", 204


# ============================================================
# GROQ CONFIGURATION
# ============================================================

groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise RuntimeError(
        "GROQ_API_KEY is not set. "
        "Please add GROQ_API_KEY to your .env file."
    )


client = Groq(
    api_key=groq_api_key
)


# ============================================================
# JWT CONFIGURATION
# ============================================================

jwt_secret_key = os.getenv("JWT_SECRET_KEY")

if not jwt_secret_key:
    raise RuntimeError(
        "JWT_SECRET_KEY is not set. "
        "Please add JWT_SECRET_KEY to your .env file."
    )

JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24


# ----------------------------------------------------
# TRANSCRIPTION JOBS
# ----------------------------------------------------

transcription_jobs = {}


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "ok",
        "message": "Voice Transcriber API is running",
        "provider": "Groq"
    })


# ============================================================
# AUTHENTICATION - REGISTER
# ============================================================

@app.route(
    "/api/auth/register",
    methods=["POST"]
)
def register():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    email = data.get("email")
    password = data.get("password")

    # Validate email
    if not email:
        return jsonify({
            "success": False,
            "error": "Email is required."
        }), 400

    # Validate password
    if not password:
        return jsonify({
            "success": False,
            "error": "Password is required."
        }), 400

    email = email.strip().lower()

    # Basic email validation
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({
            "success": False,
            "error": "Please provide a valid email address."
        }), 400

    # Password length
    if len(password) < 8:
        return jsonify({
            "success": False,
            "error": "Password must be at least 8 characters."
        }), 400

    # Check whether user already exists
    existing_user = User.query.filter_by(
        email=email
    ).first()

    if existing_user:
        return jsonify({
            "success": False,
            "error": "An account with this email already exists."
        }), 409

    # ============================================================
    # CREATE USER
    # ============================================================

    user = User(
        email=email
    )

    # Hash password
    user.set_password(password)

    # Add user first so we get the user ID.
    db.session.add(user)
    db.session.flush()


    # ============================================================
    # CREATE FREE SUBSCRIPTION
    # ============================================================

    free_subscription = Subscription(
        user_id=user.id,
        plan="free",
        billing_cycle="free",
        amount=0,
        start_date=datetime.now(timezone.utc),
        end_date=None,
        status="active",
    )

    db.session.add(free_subscription)


    # ============================================================
    # CREATE 5 FREE CREDITS
    # ============================================================

    credit_account = UserCredit(
        user_id=user.id,
        credits=5,
        used_credits=0,
    )

    db.session.add(credit_account)


    # ============================================================
    # RECORD INITIAL CREDIT ALLOCATION
    # ============================================================

    credit_transaction = CreditTransaction(
        user_id=user.id,
        action="registration",
        credits_used=0,
        recording_id=None,
    )

    db.session.add(credit_transaction)


    # ============================================================
    # SAVE EVERYTHING
    # ============================================================

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Account created successfully.",
        "user": user.to_dict()
    }), 201


# ============================================================
# AUTHENTICATION - LOGIN
# ============================================================

@app.route(
    "/api/auth/login",
    methods=["POST"]
)
def login():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    email = data.get("email")
    password = data.get("password")

    if not email:
        return jsonify({
            "success": False,
            "error": "Email is required."
        }), 400

    if not password:
        return jsonify({
            "success": False,
            "error": "Password is required."
        }), 400

    email = email.strip().lower()

    # Find user
    user = User.query.filter_by(
        email=email
    ).first()

    # Do not reveal whether the email exists
    if not user or not user.check_password(password):
        return jsonify({
            "success": False,
            "error": "Invalid email or password."
        }), 401

    # Create JWT payload
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "iat": now,
        "exp": now + timedelta(
            hours=JWT_EXPIRATION_HOURS
        )
    }

    # Generate token
    token = jwt.encode(
        payload,
        jwt_secret_key,
        algorithm=JWT_ALGORITHM
    )

    return jsonify({
        "success": True,
        "message": "Login successful.",
        "token": token,
        "token_type": "Bearer",
        "expires_in": JWT_EXPIRATION_HOURS * 60 * 60,
        "user": user.to_dict()
    }), 200


def get_authenticated_user():

    auth_header = request.headers.get("Authorization")

    if not auth_header:
        return None, (
            jsonify({
                "success": False,
                "error": "Authorization header is required."
            }),
            401
        )

    if not auth_header.startswith("Bearer "):
        return None, (
            jsonify({
                "success": False,
                "error": "Invalid authorization format."
            }),
            401
        )

    token = auth_header.split(" ", 1)[1].strip()

    if not token:
        return None, (
            jsonify({
                "success": False,
                "error": "Token is required."
            }),
            401
        )

    try:

        payload = jwt.decode(
            token,
            jwt_secret_key,
            algorithms=[JWT_ALGORITHM]
        )

        user_id = payload.get("sub")

        if not user_id:
            return None, (
                jsonify({
                    "success": False,
                    "error": "Invalid token."
                }),
                401
            )

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return None, (
                jsonify({
                    "success": False,
                    "error": "Invalid user ID in token."
                }),
                401
            )

        user = db.session.get(
            User,
            user_id
        )

        if not user:
            return None, (
                jsonify({
                    "success": False,
                    "error": "User not found."
                }),
                404
            )

        return user, None

    except jwt.ExpiredSignatureError:

        return None, (
            jsonify({
                "success": False,
                "error": "Token has expired."
            }),
            401
        )

    except jwt.InvalidTokenError:

        return None, (
            jsonify({
                "success": False,
                "error": "Invalid token."
            }),
            401
        )

    
# ============================================================
# AUTHENTICATION - CURRENT USER
# ============================================================

@app.route(
    "/api/auth/me",
    methods=["GET"]
)
def current_user():

    auth_header = request.headers.get("Authorization")

    if not auth_header:
        return jsonify({
            "success": False,
            "error": "Authorization header is required."
        }), 401

    if not auth_header.startswith("Bearer "):
        return jsonify({
            "success": False,
            "error": "Invalid authorization format."
        }), 401

    token = auth_header.split(" ", 1)[1].strip()

    if not token:
        return jsonify({
            "success": False,
            "error": "Token is required."
        }), 401

    try:
        payload = jwt.decode(
            token,
            jwt_secret_key,
            algorithms=[JWT_ALGORITHM]
        )

        user_id = payload.get("sub")

        if not user_id:
            return jsonify({
                "success": False,
                "error": "Invalid token."
            }), 401

        user = db.session.get(
            User,
            int(user_id)
        )

        if not user:
            return jsonify({
                "success": False,
                "error": "User not found."
            }), 404

        return jsonify({
            "success": True,
            "user": user.to_dict()
        }), 200

    except jwt.ExpiredSignatureError:
        return jsonify({
            "success": False,
            "error": "Token has expired."
        }), 401

    except jwt.InvalidTokenError:
        return jsonify({
            "success": False,
            "error": "Invalid token."
        }), 401

    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "error": "Invalid user ID in token."
        }), 401


# ============================================================
# AUTHENTICATION - LOGOUT
# ============================================================

@app.route(
    "/api/auth/logout",
    methods=["POST"]
)
def logout():

    # JWT is currently stateless.
    # The client should delete its stored token.
    return jsonify({
        "success": True,
        "message": "Logout successful. Please remove the access token from the client."
    }), 200


# ============================================================
# AUTHENTICATION - FORGOT PASSWORD
# ============================================================

@app.route(
    "/api/auth/forgot-password",
    methods=["POST"]
)
def forgot_password():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    email = data.get("email")

    if not email:
        return jsonify({
            "success": False,
            "error": "Email is required."
        }), 400

    email = email.strip().lower()

    # Always return the same general response.
    # This prevents revealing whether an email exists.
    generic_response = {
        "success": True,
        "message": (
            "If an account with that email exists, "
            "a password reset link has been sent."
        )
    }

    user = User.query.filter_by(
        email=email
    ).first()

    if not user:
        return jsonify(generic_response), 200

    # Generate a cryptographically secure random token.
    reset_token = secrets.token_urlsafe(32)

    # Store only the SHA-256 hash of the token.
    token_hash = hashlib.sha256(
        reset_token.encode("utf-8")
    ).hexdigest()

    # Token expires after 30 minutes.
    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(minutes=30)
    )

    user.password_reset_token_hash = token_hash
    user.password_reset_expires_at = expires_at
    user.password_reset_used = False

    db.session.commit()

    # Frontend page that will handle the password reset.
    reset_link = (
        "https://voice-transcribe-11.web.app/#/reset-password"
        f"?token={reset_token}"
    )

    try:
        resend.Emails.send({
            "from": RESEND_FROM_EMAIL,
            "to": [email],
            "subject": "Reset your NabTranscriber password",
            "html": f"""
                <div style="
                    font-family: Arial, sans-serif;
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 30px;
                    color: #222;
                ">
                    <h2 style="margin-bottom: 10px;">
                        Reset Your Password
                    </h2>

                    <p>
                        We received a request to reset your
                        NabTranscriber password.
                    </p>

                    <p>
                        Click the button below to create a new password.
                    </p>

                    <p style="margin: 30px 0;">
                        <a href="{reset_link}"
                           style="
                               background: #1B5E20;
                               color: white;
                               padding: 14px 24px;
                               text-decoration: none;
                               border-radius: 6px;
                               display: inline-block;
                               font-weight: bold;
                           ">
                            RESET PASSWORD
                        </a>
                    </p>

                    <p>
                        This link will expire in
                        <strong>30 minutes</strong>.
                    </p>

                    <p>
                        If you did not request a password reset,
                        you can safely ignore this email.
                    </p>

                    <hr style="
                        margin: 30px 0;
                        border: none;
                        border-top: 1px solid #ddd;
                    ">

                    <p style="
                        font-size: 12px;
                        color: #777;
                    ">
                        NabTranscriber<br>
                        Automated security notification
                    </p>
                </div>
            """
        })

    except Exception as email_error:
        print(
            "Password reset email failed:",
            email_error
        )

        return jsonify({
            "success": False,
            "error": (
                "We could not send the password reset email. "
                "Please try again later."
            )
        }), 500

    return jsonify(generic_response), 200


# ============================================================
# AUTHENTICATION - RESET PASSWORD
# ============================================================

@app.route(
    "/api/auth/reset-password",
    methods=["POST"]
)
def reset_password():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    token = data.get("token")
    new_password = data.get("password")

    # --------------------------------------------------------
    # VALIDATE TOKEN
    # --------------------------------------------------------

    if not token:
        return jsonify({
            "success": False,
            "error": "Reset token is required."
        }), 400

    # --------------------------------------------------------
    # VALIDATE NEW PASSWORD
    # --------------------------------------------------------

    if not new_password:
        return jsonify({
            "success": False,
            "error": "New password is required."
        }), 400

    if len(new_password) < 8:
        return jsonify({
            "success": False,
            "error": "Password must be at least 8 characters."
        }), 400

    # --------------------------------------------------------
    # HASH THE TOKEN
    # --------------------------------------------------------

    token_hash = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()

    # --------------------------------------------------------
    # FIND USER
    # --------------------------------------------------------

    user = User.query.filter_by(
        password_reset_token_hash=token_hash
    ).first()

    if not user:
        print(
        )

        return jsonify({
            "success": False,
            "error": "Invalid or expired reset token."
        }), 400

    # --------------------------------------------------------
    # CHECK WHETHER TOKEN HAS ALREADY BEEN USED
    # --------------------------------------------------------

    if user.password_reset_used:
        return jsonify({
            "success": False,
            "error": "This reset link has already been used."
        }), 400

    # --------------------------------------------------------
    # CHECK TOKEN EXPIRATION
    # --------------------------------------------------------

    if not user.password_reset_expires_at:
        return jsonify({
            "success": False,
            "error": "Invalid or expired reset token."
        }), 400

    expires_at = user.password_reset_expires_at

    # Handle databases that return a naive datetime.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(
            tzinfo=timezone.utc
        )

    if datetime.now(timezone.utc) > expires_at:
        return jsonify({
            "success": False,
            "error": "This reset link has expired. Please request a new one."
        }), 400

    # --------------------------------------------------------
    # UPDATE PASSWORD
    # --------------------------------------------------------

    user.set_password(new_password)

    # --------------------------------------------------------
    # INVALIDATE RESET TOKEN
    # --------------------------------------------------------

    user.password_reset_used = True
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Password reset successfully. You can now log in."
    }), 200


# ============================================================
# SUBSCRIPTION / CREDIT CHECK
# ============================================================

@app.route(
    "/api/subscription/check",
    methods=["POST"]
)
def check_subscription_action():

    # --------------------------------------------------------
    # AUTHENTICATE USER
    # --------------------------------------------------------

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    # --------------------------------------------------------
    # READ REQUEST
    # --------------------------------------------------------

    data = request.get_json(silent=True) or {}

    action = data.get("action")

    if not action:
        return jsonify({
            "success": False,
            "error": "Action is required."
        }), 400

    # --------------------------------------------------------
    # ALLOWED ACTIONS
    # --------------------------------------------------------

    allowed_actions = {
        "record",
        "meeting_record",
        "upload",
    }

    if action not in allowed_actions:
        return jsonify({
            "success": False,
            "error": "Invalid action."
        }), 400

    # --------------------------------------------------------
    # CHECK SUBSCRIPTION + CREDIT
    # --------------------------------------------------------

    success, message = check_action(
        user_id=user.id,
        action=action,
    )

    if not success:
        return jsonify({
            "success": False,
            "error": message,
        }), 403

    # --------------------------------------------------------
    # GET CURRENT CREDIT BALANCE
    # --------------------------------------------------------

    credit_account = UserCredit.query.filter_by(
        user_id=user.id
    ).first()

    remaining_credits = (
        credit_account.credits
        if credit_account
        else 0
    )

    return jsonify({
        "success": True,
        "message": message,
        "action": action,
        "remaining_credits": remaining_credits,
    }), 200


# ============================================================
# SUBSCRIPTION / CREDIT AUTHORIZATION
# ============================================================

@app.route(
    "/api/subscription/authorize",
    methods=["POST"]
)
def authorize_subscription_action():

    # --------------------------------------------------------
    # AUTHENTICATE USER
    # --------------------------------------------------------

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    # --------------------------------------------------------
    # READ REQUEST
    # --------------------------------------------------------

    data = request.get_json(silent=True) or {}

    action = data.get("action")
    recording_id = data.get("recording_id")

    if not action:
        return jsonify({
            "success": False,
            "error": "Action is required."
        }), 400

    # --------------------------------------------------------
    # ALLOWED ACTIONS
    # --------------------------------------------------------

    allowed_actions = {
        "record",
        "meeting_record",
        "upload",
    }

    if action not in allowed_actions:
        return jsonify({
            "success": False,
            "error": "Invalid action."
        }), 400

    # --------------------------------------------------------
    # CHECK SUBSCRIPTION + CREDIT
    # --------------------------------------------------------

    success, message = authorize_action(
        user_id=user.id,
        action=action,
        recording_id=recording_id,
    )

    if not success:
        return jsonify({
            "success": False,
            "error": message,
        }), 403

    # --------------------------------------------------------
    # GET UPDATED CREDIT BALANCE
    # --------------------------------------------------------

    credit_account = UserCredit.query.filter_by(
        user_id=user.id
    ).first()

    remaining_credits = (
        credit_account.credits
        if credit_account
        else 0
    )

    return jsonify({
        "success": True,
        "message": message,
        "action": action,
        "remaining_credits": remaining_credits,
    }), 200


# ----------------------------------------------------
# TRANSCRIPTION STATUS
# ----------------------------------------------------

@app.route(
    "/api/transcription-status/<job_id>",
    methods=["GET"]
)
def transcription_status(job_id):

    job = transcription_jobs.get(job_id)

    if not job:
        return jsonify({
            "success": False,
            "error": "Transcription job not found."
        }), 404

    return jsonify({
        "success": True,
        "job_id": job_id,
        "status": job.get("status"),
        "completed_chunks": job.get(
            "completed_chunks",
            0
        ),
        "total_chunks": job.get(
            "total_chunks",
            0
        ),
        "progress": job.get(
            "progress",
            0
        ),
        "text": job.get("text"),
        "error": job.get("error")
    }), 200


# ============================================================
# BACKGROUND TRANSCRIPTION WORKER
# ============================================================

def process_transcription_job(
    job_id,
    temp_path,
    file_extension
):

    chunk_directory = None

    try:

        # ----------------------------------------------------
        # CREATE CHUNK DIRECTORY
        # ----------------------------------------------------

        chunk_directory = tempfile.mkdtemp(
            prefix="voice_transcriber_chunks_"
        )

        # ----------------------------------------------------
        # SPLIT AUDIO INTO 10-MINUTE CHUNKS
        # ----------------------------------------------------

        chunk_pattern = os.path.join(
            chunk_directory,
            "chunk_%04d" + file_extension
        )

        ffmpeg_command = [
            "ffmpeg",
            "-y",
            "-i",
            temp_path,
            "-f",
            "segment",
            "-segment_time",
            "600",
            "-reset_timestamps",
            "1",
            "-c",
            "copy",
            chunk_pattern
        ]

        print(
            f"[{job_id}] "
            "Splitting audio into 10-minute chunks..."
        )

        result = subprocess.run(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:

            print(
                f"[{job_id}] FFmpeg error:"
            )

            print(result.stderr)

            transcription_jobs[job_id][
                "status"
            ] = "failed"

            transcription_jobs[job_id][
                "error"
            ] = (
                "Unable to process the audio file. "
                "Please try again with a shorter recording."
            )

            return

        # ----------------------------------------------------
        # GET CHUNKS
        # ----------------------------------------------------

        chunk_files = sorted(
            [
                os.path.join(
                    chunk_directory,
                    filename
                )
                for filename in os.listdir(
                    chunk_directory
                )
                if filename.startswith("chunk_")
            ]
        )

        if not chunk_files:

            transcription_jobs[job_id][
                "status"
            ] = "failed"

            transcription_jobs[job_id][
                "error"
            ] = "No audio chunks were created."

            return

        total_chunks = len(
            chunk_files
        )

        transcription_jobs[job_id][
            "total_chunks"
        ] = total_chunks

        print(
            f"[{job_id}] "
            f"Created {total_chunks} audio chunks."
        )

        # ----------------------------------------------------
        # TRANSCRIBE EACH CHUNK
        # ----------------------------------------------------

        transcripts = []

        for index, chunk_path in enumerate(
            chunk_files
        ):

            chunk_number = index + 1

            print(
                f"[{job_id}] "
                f"Transcribing chunk "
                f"{chunk_number}/{total_chunks}..."
            )

            try:

                with open(
                    chunk_path,
                    "rb"
                ) as chunk_audio:

                    transcription = (
                        client.audio.transcriptions.create(
                            file=chunk_audio,
                            model="whisper-large-v3-turbo",
                            response_format="json"
                        )
                    )

                chunk_text = (
                    transcription.text
                    or ""
                ).strip()

                if chunk_text:

                    transcripts.append(
                        chunk_text
                    )

                # ------------------------------------------------
                # UPDATE PROGRESS
                # ------------------------------------------------

                progress = int(
                    (
                        chunk_number
                        / total_chunks
                    ) * 100
                )

                transcription_jobs[job_id][
                    "completed_chunks"
                ] = chunk_number

                transcription_jobs[job_id][
                    "progress"
                ] = progress

                print(
                    f"[{job_id}] "
                    f"Chunk "
                    f"{chunk_number}/{total_chunks} "
                    f"completed "
                    f"({progress}%)."
                )

            except Exception as chunk_error:

                error_text = str(
                    chunk_error
                )

                print(
                    f"[{job_id}] "
                    f"Chunk "
                    f"{chunk_number}/{total_chunks} "
                    "failed:"
                )

                print(error_text)

                # ------------------------------------------------
                # GROQ HOURLY LIMIT
                # ------------------------------------------------

                if (
                    "rate_limit_exceeded"
                    in error_text
                    or "Request too large"
                    in error_text
                    or "seconds of audio per hour"
                    in error_text
                ):

                    error_message = (
                        "Groq's hourly transcription "
                        "limit has been reached. "
                        "Please try again later or "
                        "use a shorter recording."
                    )

                    transcription_jobs[job_id][
                        "status"
                    ] = "failed"

                    transcription_jobs[job_id][
                        "error"
                    ] = error_message

                    return

                # ------------------------------------------------
                # OTHER TRANSCRIPTION ERROR
                # ------------------------------------------------

                transcription_jobs[job_id][
                    "status"
                ] = "failed"

                transcription_jobs[job_id][
                    "error"
                ] = error_text

                return

        # ----------------------------------------------------
        # COMBINE TRANSCRIPTS
        # ----------------------------------------------------

        final_text = "\n\n".join(
            transcripts
        )

        # ----------------------------------------------------
        # MARK JOB AS COMPLETED
        # ----------------------------------------------------

        transcription_jobs[job_id][
            "status"
        ] = "completed"

        transcription_jobs[job_id][
            "completed_chunks"
        ] = total_chunks

        transcription_jobs[job_id][
            "progress"
        ] = 100

        transcription_jobs[job_id][
            "text"
        ] = final_text

        print(
            f"[{job_id}] "
            "Transcription successful."
        )

        print(
            f"[{job_id}] "
            f"Total chunks: {total_chunks}"
        )

        print(
            f"[{job_id}] "
            f"Transcript length: "
            f"{len(final_text)} characters"
        )

    except Exception as e:

        print(
            f"[{job_id}] "
            "TRANSCRIPTION ERROR:"
        )

        print(e)

        transcription_jobs[job_id][
            "status"
        ] = "failed"

        transcription_jobs[job_id][
            "error"
        ] = str(e)

    finally:

        # ----------------------------------------------------
        # DELETE ORIGINAL FILE
        # ----------------------------------------------------

        if (
            temp_path
            and os.path.exists(temp_path)
        ):

            try:

                os.remove(
                    temp_path
                )

            except Exception as cleanup_error:

                print(
                    "Could not delete "
                    "temporary file:",
                    cleanup_error
                )

        # ----------------------------------------------------
        # DELETE CHUNKS
        # ----------------------------------------------------

        if (
            chunk_directory
            and os.path.exists(
                chunk_directory
            )
        ):

            try:

                shutil.rmtree(
                    chunk_directory
                )

            except Exception as cleanup_error:

                print(
                    "Could not delete "
                    "chunk directory:",
                    cleanup_error
                )


# ============================================================
# TRANSCRIPTION
# ============================================================

@app.route(
    "/api/transcribe",
    methods=["POST"]
)
def transcribe():

    # --------------------------------------------------------
    # AUTHENTICATE USER
    # --------------------------------------------------------

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    # --------------------------------------------------------
    # CHECK THAT AN AUDIO FILE WAS UPLOADED
    # --------------------------------------------------------

    if "audio" not in request.files:

        return jsonify({
            "success": False,
            "error": "No audio file provided"
        }), 400

    audio_file = request.files["audio"]

    # --------------------------------------------------------
    # CHECK FILENAME
    # --------------------------------------------------------

    if audio_file.filename == "":

        return jsonify({
            "success": False,
            "error": "No audio filename provided"
        }), 400

    temp_path = None

    # --------------------------------------------------------
    # CREATE UNIQUE TRANSCRIPTION JOB
    # --------------------------------------------------------

    job_id = str(
        uuid.uuid4()
    )

    transcription_jobs[job_id] = {
        "status": "processing",
        "completed_chunks": 0,
        "total_chunks": 0,
        "progress": 0,
        "text": None,
        "error": None
    }

    try:

        # ----------------------------------------------------
        # PRESERVE FILE EXTENSION
        # ----------------------------------------------------

        original_filename = (
            audio_file.filename
            or "recording.webm"
        )

        _, file_extension = os.path.splitext(
            original_filename
        )

        if not file_extension:

            file_extension = ".webm"

        # ----------------------------------------------------
        # SAVE ORIGINAL AUDIO
        # ----------------------------------------------------

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=file_extension
        ) as temp:

            audio_file.save(
                temp.name
            )

            temp_path = temp.name

        file_size = os.path.getsize(
            temp_path
        )

        print(
            f"[{job_id}] "
            f"Audio received: "
            f"{file_size} bytes"
        )

        # ----------------------------------------------------
        # START BACKGROUND WORKER
        # ----------------------------------------------------

        transcription_thread = threading.Thread(
            target=process_transcription_job,
            args=(
                job_id,
                temp_path,
                file_extension
            ),
            daemon=True
        )

        transcription_thread.start()

        print(
            f"[{job_id}] "
            "Background transcription started."
        )

        # ----------------------------------------------------
        # RETURN IMMEDIATELY
        # ----------------------------------------------------

        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "processing",
            "completed_chunks": 0,
            "total_chunks": 0,
            "progress": 0,
            "message": (
                "Transcription started successfully."
            )
        }), 202

    except Exception as e:

        print(
            f"[{job_id}] "
            "TRANSCRIPTION START ERROR:"
        )

        print(e)

        transcription_jobs[job_id][
            "status"
        ] = "failed"

        transcription_jobs[job_id][
            "error"
        ] = str(e)

        # ----------------------------------------------------
        # CLEANUP IF WORKER DID NOT START
        # ----------------------------------------------------

        if (
            temp_path
            and os.path.exists(temp_path)
        ):

            try:

                os.remove(
                    temp_path
                )

            except Exception as cleanup_error:

                print(
                    "Could not delete "
                    "temporary file:",
                    cleanup_error
                )

        return jsonify({
            "success": False,
            "job_id": job_id,
            "error": str(e)
        }), 500


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    print("")
    print("=" * 50)
    print("VOICE TRANSCRIBER BACKEND")
    print("=" * 50)
    print("Provider: Groq")
    print("Model: whisper-large-v3-turbo")
    print(
        "Database:",
        database_url
    )
    print(
        "Server: "
        "https://voice-transcriber-3982.onrender.com"
    )
    print("=" * 50)
    print("")


    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )
