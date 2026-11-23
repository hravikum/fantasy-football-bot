"""Posts the bot's messages to your Slack channel.

A bot token rather than an incoming webhook: a webhook can't post into a thread or edit a message,
and the bid buttons planned next need both. Two keys, from .env or the environment:
    SLACK_BOT_TOKEN   xoxb-..., the app's Bot User OAuth Token (OAuth & Permissions page)
    SLACK_CHANNEL     the channel's id, C followed by letters and digits (bottom of its About tab)
The app needs the chat:write scope to post and channels:read for hosted_check.py, and the bot has
to be invited to the channel. slack-app-manifest.yml sets up both scopes.
"""
import json
import os
import re
import time

import requests

API = "https://slack.com/api/"
TRIES = 3
WAITS = (2, 10)        # seconds before the second and third try, unless Slack says how long
MAX_WAIT = 30
SECTION = 3000         # characters one section block may hold
HEADER = 150           # characters a header block may hold
MAX_BLOCKS = 50        # blocks one message may hold
FENCE = "```"
LINK = re.compile(r"https?://[^\s<>|]+")
CUT_SHORT = "Cut short: too long for one Slack message. The run's log has all of it."


class SlackError(Exception):
    pass


def configured():
    return bool(os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_CHANNEL"))


def call(method, fields, form=False, sleep=None):
    """One Slack Web API call. Returns Slack's answer, or raises SlackError.

    Tried three times when the failure may pass: no connection, a timeout, a 5xx, or a 429 (after
    the wait Slack asks for). Slack's own refusals, such as a bad token or a channel the bot isn't
    in, are raised at once. A timeout can hide a post that landed, so very rarely a message may
    arrive twice; better than not at all. Methods that only read, such as auth.test and
    conversations.info, take form fields (form=True); posting takes JSON.
    """
    sleep = sleep or time.sleep
    headers = {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"}
    if form:
        payload = fields
    else:
        headers["Content-Type"] = "application/json; charset=utf-8"
        payload = json.dumps(fields).encode()
    problem = None
    for attempt in range(TRIES):
        wait = WAITS[min(attempt, len(WAITS) - 1)]
        try:
            resp = requests.post(API + method, data=payload, headers=headers, timeout=20)
        except requests.exceptions.RequestException as error:
            problem = f"{type(error).__name__}: {error}"
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                problem = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    wait = min(_seconds(resp.headers.get("Retry-After"), wait), MAX_WAIT)
            else:
                try:
                    answer = resp.json()
                except ValueError:
                    raise SlackError(f"{method}: HTTP {resp.status_code}, not JSON: {resp.text[:100]}")
                if not answer.get("ok"):
                    raise SlackError(f"{method}: {answer.get('error', 'refused')}")
                return answer
        if attempt + 1 < TRIES:
            sleep(wait)
    raise SlackError(f"{method}: {problem} ({TRIES} tries)")


def _seconds(value, fallback):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return fallback


def post(text, blocks=None):
    """Post one message to SLACK_CHANNEL and return its ts (Slack's id for it). text is what a
    phone's notification shows; blocks, when given, are what the channel shows."""
    message = {"channel": os.environ["SLACK_CHANNEL"], "text": escape(text),
               "unfurl_links": False, "unfurl_media": False}
    if blocks:
        message["blocks"] = blocks
    return call("chat.postMessage", message)["ts"]


def escape(text):
    """Slack reads &, < and > as markup, in code blocks too."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def blocks_for(subject, body):
    """The subject as a header, then the body in monospace, so the reports' tables line up as in
    the email's text version, then the body's links, which can't be clicked inside monospace."""
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": subject[:HEADER]}}]
    room = SECTION - len(f"{FENCE}\n\n{FENCE}")
    chunks = _chunks(body.strip("\n").replace(FENCE, "'''"), room) if body.strip() else []
    links = _links(body)
    fits = MAX_BLOCKS - len(blocks) - bool(links) - 1      # leaves room for the cut-short note
    blocks += [_section(f"{FENCE}\n{chunk}\n{FENCE}") for chunk in chunks[:fits]]
    if links:
        blocks.append(_section(links))
    if len(chunks) > fits:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": CUT_SHORT}]})
    return blocks


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _chunks(text, room):
    """The text, escaped, in pieces that each fit a section, cut at line ends where it can be."""
    lines = []
    for line in text.split("\n"):
        while len(escape(line)) > room:
            piece = room // len("&amp;")               # escaping makes a piece at most this much longer
            lines.append(escape(line[:piece]))
            line = line[piece:]
        lines.append(escape(line))
    chunks, current = [], []
    for line in lines:
        if current and len("\n".join(current + [line])) > room:
            chunks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _links(body):
    """Each link in the body once, in order, as clickable lines that fit one section."""
    found = []
    for link in LINK.findall(body):
        link = link.rstrip(".,;:)]'\"")
        if link not in found:
            found.append(link)
    lines = []
    for link in found:
        line = f"• <{escape(link)}>"
        if len("\n".join(lines + [line])) > SECTION:
            break
        lines.append(line)
    return "\n".join(lines)
