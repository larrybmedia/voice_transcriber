import os
import tempfile

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from groq import Groq


# Load environment variables
load_dotenv()


# Create Flask application
app = Flask(__name__)

# Allow Flutter Web to communicate with Flask
CORS(app)


# Get Groq API key from .env
groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise RuntimeError(
        "GROQ_API_KEY is not set. "
        "Please add GROQ_API_KEY to your .env file."
    )


# Create Groq client
client = Groq(
    api_key=groq_api_key
)


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "Voice Transcriber API is running",
        "provider": "Groq"
    })


@app.route("/api/transcribe", methods=["POST"])
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


    try:

        # Save uploaded audio temporarily
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".webm"
        ) as temp:

            audio_file.save(temp.name)

            temp_path = temp.name


        print(
            f"Audio received: "
            f"{os.path.getsize(temp_path)} bytes"
        )


        # Open the temporary audio file
        with open(temp_path, "rb") as audio:

            transcription = client.audio.transcriptions.create(
                file=audio,
                model="whisper-large-v3-turbo",
                response_format="json"
            )


        # Get transcript
        text = transcription.text


        print("Transcription successful.")
        print("Transcript:", text)


        return jsonify({
            "success": True,
            "text": text
        })


    except Exception as e:

        print("TRANSCRIPTION ERROR:")
        print(e)


        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


    finally:

        # Delete temporary audio file
        if temp_path and os.path.exists(temp_path):

            try:
                os.remove(temp_path)

            except Exception as cleanup_error:

                print(
                    "Could not delete temporary file:",
                    cleanup_error
                )


if __name__ == "__main__":

    print("")
    print("=" * 50)
    print("VOICE TRANSCRIBER BACKEND")
    print("=" * 50)
    print("Provider: Groq")
    print("Model: whisper-large-v3-turbo")
    print("Server: https://voice-transcriber-3982.onrender.com")
    print("=" * 50)
    print("")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )