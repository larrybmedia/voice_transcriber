import os
import resend
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("RESEND_API_KEY")
from_email = os.getenv("RESEND_FROM_EMAIL")

resend.api_key = api_key

to_email = input("Enter the email address to receive the test email: ").strip()

response = resend.Emails.send({
    "from": from_email,
    "to": [to_email],
    "subject": "NabTranscriber Email Test",
    "html": """
        <h2>NabTranscriber Email Test</h2>
        <p>This is a test email from NabTranscriber.</p>
        <p>Your Resend email configuration is working correctly.</p>
    """,
})

print("Email sent successfully.")
print("Response:", response)