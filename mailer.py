"""Email through Gmail, using an app password rather than your Google password."""
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import certifi
from dotenv import load_dotenv


def _tls_context():
    """python.org's macOS builds leave the CA store empty until Install Certificates.command
    is run, so fall back to certifi's bundle rather than failing to verify Gmail."""
    context = ssl.create_default_context()
    if not context.cert_store_stats().get("x509_ca"):
        context.load_verify_locations(certifi.where())
    return context


def send(subject, body):
    """Send one plain-text email. Returns True if it went out."""
    load_dotenv(Path(__file__).with_name(".env"))
    address = os.getenv("GMAIL_ADDRESS")
    password = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")  # Google shows it in groups of four
    if not (address and password):
        print("(no email sent: add GMAIL_ADDRESS and GMAIL_APP_PASSWORD to .env)")
        return False

    message = EmailMessage()
    message["From"] = address
    message["To"] = os.getenv("EMAIL_TO") or address
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls(context=_tls_context())
            smtp.login(address, password)
            smtp.send_message(message)
        return True
    except Exception as problem:  # broad on purpose: a dead notifier must not kill a run that already acted
        print(f"(email failed: {type(problem).__name__}: {problem})")
        return False
