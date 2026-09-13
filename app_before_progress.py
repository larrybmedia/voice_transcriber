import os
import tempfile
import secrets
import hashlib
import subprocess
import shutil

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from groq import Groq
from flask_migrate import Migrate
from models import db, User

import jwt
from datetime import datetime, timedelta, timezone


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()


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
            "origins": "*"
        }
    }
)


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

    # Create user
    user = User(
        email=email
    )

    # Hash password
    user.set_password(password)

    # Save user
    db.session.add(user)
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
            "a password reset request has been created."
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

    # Development-only response.
    # Do NOT expose this token in production.
    return jsonify({
        **generic_response,
        "development_reset_token": reset_token
    }), 200


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

    if not token:
        return jsonify({
            "success": False,
            "error": "Reset token is required."
        }), 400

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

    # Hash the supplied reset token.
    token_hash = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()

    # Find the user using the hashed token.
    user = User.query.filter_by(
        password_reset_token_hash=token_hash,
        password_reset_used=False
    ).first()

    if not user:
        return jsonify({
            "success": False,
            "error": "Invalid or expired reset token."
        }), 400

    # Check token expiration.
    now = datetime.now(timezone.utc)

    expires_at = user.password_reset_expires_at

    # SQLite may return timezone-naive datetimes.
    # Treat them as UTC before comparing.
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(
            tzinfo=timezone.utc
        )

    if (
        not expires_at
        or expires_at <= now
    ):
        print(
            "RESET DEBUG: token found but expired."
        )

        return jsonify({
            "success": False,
            "error": "Invalid or expired reset token."
        }), 400

    # Set the new password.
    user.set_password(new_password)

    # Invalidate the reset token immediately.
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    user.password_reset_used = True

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Password reset successfully."
    }), 200


# ============================================================
# TRANSCRIPTION
# ============================================================

@app.route(
    "/api/transcribe",
    methods=["POST"]
)
def transcribe():

    # Check that an audio file was uploaded
    if "audio" not in request.files:
        return jsonify({
            "success": False,
            "error": "No audio file provided"
        }), 400

    audio_file = request.files["audio"]

    # Check filename
    if audio_file.filename == "":
        return jsonify({
            "success": False,
            "error": "No audio filename provided"
        }), 400

    temp_path = None
    chunk_directory = None

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

            audio_file.save(temp.name)
            temp_path = temp.name

        file_size = os.path.getsize(temp_path)

        print(
            f"Audio received: {file_size} bytes"
        )

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
                "FFmpeg error:"
            )

            print(
                result.stderr
            )

            return jsonify({
                "success": False,
                "error": (
                    "Unable to process the audio file. "
                    "Please try again with a shorter recording."
                )
            }), 500

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
            return jsonify({
                "success": False,
                "error": "No audio chunks were created."
            }), 500

        print(
            f"Created {len(chunk_files)} audio chunks."
        )

        # ----------------------------------------------------
        # TRANSCRIBE EACH CHUNK
        # ----------------------------------------------------

        transcripts = []

        for index, chunk_path in enumerate(
            chunk_files
        ):

            chunk_number = index + 1
            total_chunks = len(chunk_files)

            print(
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

                print(
                    f"Chunk "
                    f"{chunk_number}/{total_chunks} "
                    f"completed."
                )

            except Exception as chunk_error:

                error_text = str(
                    chunk_error
                )

                print(
                    f"Chunk "
                    f"{chunk_number}/{total_chunks} "
                    f"failed:"
                )

                print(
                    error_text
                )

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

                    return jsonify({
                        "success": False,
                        "error": (
                            "Groq's hourly transcription "
                            "limit has been reached. "
                            "Your recording is longer than "
                            "the available transcription "
                            "allowance. Please try again "
                            "later or use a shorter recording."
                        ),
                        "completed_chunks": index,
                        "total_chunks": total_chunks
                    }), 429

                raise

        # ----------------------------------------------------
        # COMBINE TRANSCRIPTS
        # ----------------------------------------------------

        final_text = "\n\n".join(
            transcripts
        )

        print(
            "Transcription successful."
        )

        print(
            f"Total chunks: {len(chunk_files)}"
        )

        print(
            f"Transcript length: "
            f"{len(final_text)} characters"
        )

        # ----------------------------------------------------
        # RETURN RESULT
        # ----------------------------------------------------

        return jsonify({
            "success": True,
            "text": final_text,
            "chunks": len(chunk_files)
        }), 200

    except Exception as e:

        print(
            "TRANSCRIPTION ERROR:"
        )

        print(e)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

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