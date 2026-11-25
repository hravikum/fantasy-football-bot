"""Sends the bot's messages: to Slack (slack.py), and by email through Gmail, using an app password
rather than your Google password, when Slack isn't set up or while EMAIL_TOO is "true"."""
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import certifi
from dotenv import load_dotenv

import slack

SUBJECT_PREFIX = "Fantasy Football: "   # on every email the bot sends, so an inbox filter can catch them all


def _tls_context():
    """python.org's macOS builds leave the CA store empty until Install Certificates.command
    is run, so fall back to certifi's bundle rather than failing to verify Gmail."""
    context = ssl.create_default_context()
    if not context.cert_store_stats().get("x509_ca"):
        context.load_verify_locations(certifi.where())
    return context


def send(subject, body, html=None, blocks=None, thread=None):
    """Send one message. Returns True if it reached you, by Slack or by email.

    It goes to Slack when SLACK_BOT_TOKEN and SLACK_CHANNEL are set, and by email (plain text with
    an optional HTML version) when they aren't, or while EMAIL_TOO is "true", the trial of Slack
    beside email. When it reaches neither, it prints "(nothing delivered: ...)", and hosted_run.sh
    then fails the run, so GitHub's own email tells you.

    In Slack, blocks lay the message out; without them, or if Slack refuses them, it goes as the
    subject over the body in monospace. thread is a list of layouts for one reply under the
    message, for detail that would crowd the channel: the first that Slack takes is posted.

    With MAIL_TO_LOG set, as in a shadow run on GitHub (hosted_run.sh), nothing is sent: the
    subject is printed and the message counts as delivered, so a trade it has reported on once
    isn't reported again when the runs go live.
    """
    if os.getenv("MAIL_TO_LOG"):
        print(f'(shadow run: would have emailed "{SUBJECT_PREFIX + subject}")')
        return True
    load_dotenv(Path(__file__).with_name(".env"))
    delivered = False
    if slack.configured():
        delivered = _post(subject, body, blocks, thread)
    if not slack.configured() or os.getenv("EMAIL_TOO") == "true":
        delivered = _email(SUBJECT_PREFIX + subject, body, html) or delivered
    if not delivered:
        print(f'(nothing delivered: "{subject}" reached neither Slack nor email)')
    return delivered


def alert(subject, body):
    """A failure notice: in Slack it leads with a warning line rather than a monospace block."""
    return send(subject, body, blocks=slack.alert(subject, body))


def _post(subject, body, blocks=None, thread=None):
    """Without the "Fantasy Football: " prefix: the channel does what the inbox filter did."""
    plain = slack.blocks_for(subject, body)
    try:
        posted = slack.post_first(subject, [blocks, plain] if blocks else [plain])
    except Exception as problem:  # broad on purpose: a dead notifier must not kill a run that already acted
        print(f"(Slack post failed: {type(problem).__name__}: {problem})")
        return False
    if thread:
        try:
            slack.post_first(subject, thread, thread_ts=posted)
        except Exception as problem:  # the message is in; only the detail under it is lost
            print(f"(Slack thread reply failed: {type(problem).__name__}: {problem})")
    return True


def _email(subject, body, html):
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
    if html:
        message.add_alternative(html, subtype="html")  # clients that can't render it fall back to the text
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls(context=_tls_context())
            smtp.login(address, password)
            smtp.send_message(message)
        return True
    except Exception as problem:  # broad on purpose: a dead notifier must not kill a run that already acted
        print(f"(email failed: {type(problem).__name__}: {problem})")
        return False
