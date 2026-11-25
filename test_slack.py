"""Tests for posting to Slack, and for mailer.send choosing between Slack and email.
Run: python3 test_slack.py. Nothing here reaches Slack or Gmail."""
import contextlib
import io
import json
import os
import smtplib
import sys

import requests

import mailer
import slack

# Set, though empty or fake, so values in .env can't reach a test: load_dotenv never overrides.
os.environ.update(SLACK_BOT_TOKEN="xoxb-test", SLACK_CHANNEL="C0TEST", GMAIL_ADDRESS="x@gmail.com",
                  GMAIL_APP_PASSWORD="abcd efgh ijkl mnop", EMAIL_TOO="")


class Reply:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.headers = status, headers or {}
        self._body = {"ok": True, "ts": "1.2"} if body is None else body
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@contextlib.contextmanager
def fake_slack(*replies):
    """Slack gives each reply in turn, the last one again after that (an exception is raised
    rather than returned). Yields the requests it was sent."""
    sent = []
    def post(url, data=None, headers=None, timeout=None):
        sent.append({"url": url, "data": data, "headers": headers})
        reply = replies[min(len(sent), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply
    saved = slack.requests.post
    slack.requests.post = post
    try:
        yield sent
    finally:
        slack.requests.post = saved


@contextlib.contextmanager
def fake_gmail(works=True):
    sent = []
    class FakeSMTP:
        def __init__(self, *a, **k):
            if not works:
                raise OSError("Operation timed out")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): pass
        def login(self, user, password): pass
        def send_message(self, message): sent.append(message["Subject"])
    saved = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        yield sent
    finally:
        smtplib.SMTP = saved


def quietly(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = fn(*args)
    return result, out.getvalue()


def no_wait(seconds):
    pass


def test_a_report_keeps_its_table_in_monospace_and_its_links_clickable():
    body = ("Waiver report, week 12\n\nPLAYER        POS  BID\nDemercado     RB   $18-31\n\n"
            "Sources:\n- Story <one>\n  https://example.com/a?b=1&c=2.\n  https://example.com/a?b=1&c=2")
    blocks = slack.blocks_for("Waivers week 12: 1 to bid on", body)
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": "Waivers week 12: 1 to bid on"}}
    code = blocks[1]["text"]["text"]
    assert code.startswith("```\nWaiver report") and code.endswith("$18-31\n\nSources:\n- Story &lt;one&gt;\n"
                                                                    "  https://example.com/a?b=1&amp;c=2.\n"
                                                                    "  https://example.com/a?b=1&amp;c=2\n```")
    assert blocks[2]["text"]["text"] == "• <https://example.com/a?b=1&amp;c=2>", "once, without the full stop"
    assert len(blocks) == 3


def test_a_long_body_is_split_at_line_ends_and_nothing_is_lost():
    lines = [f"row {i:04d} " + "x" * 90 for i in range(200)]
    blocks = slack.blocks_for("long", "\n".join(lines))
    texts = [b["text"]["text"] for b in blocks[1:]]
    assert all(len(t) <= slack.SECTION for t in texts) and len(texts) > 1
    assert "\n".join(t[len("```\n"):-len("\n```")] for t in texts).split("\n") == lines


def test_a_line_too_long_for_one_section_is_cut_and_still_fits():
    blocks = slack.blocks_for("wide", "&" * 5000)
    assert all(len(b["text"]["text"]) <= slack.SECTION for b in blocks[1:])
    assert "".join(b["text"]["text"].strip("`\n") for b in blocks[1:]) == "&amp;" * 5000


def test_a_body_too_long_for_one_message_says_it_was_cut():
    blocks = slack.blocks_for("huge", "\n".join("y" * 2900 for _ in range(80)) + "\nhttps://example.com/z")
    assert len(blocks) == slack.MAX_BLOCKS
    assert blocks[-2]["text"]["text"] == "• <https://example.com/z>", "the links survive the cut"
    assert blocks[-1]["elements"][0]["text"] == slack.CUT_SHORT


def test_fences_in_the_body_cannot_close_the_monospace_early():
    blocks = slack.blocks_for("s", "before\n```json\n{}\n```\nafter")
    assert blocks[1]["text"]["text"].count("```") == 2


def test_an_empty_body_posts_just_the_header():
    assert slack.blocks_for("Week 12: lineup updated", "\n\n") == [
        {"type": "header", "text": {"type": "plain_text", "text": "Week 12: lineup updated"}}]


def test_a_post_goes_to_the_channel_with_the_token_and_without_previews():
    with fake_slack(Reply()) as sent:
        assert slack.post("Trade offer from <Them> & co", [{"type": "divider"}]) == "1.2"
    (request,) = sent
    message = json.loads(request["data"])
    assert request["url"] == "https://slack.com/api/chat.postMessage"
    assert request["headers"]["Authorization"] == "Bearer xoxb-test"
    assert message == {"channel": "C0TEST", "text": "Trade offer from &lt;Them&gt; &amp; co",
                       "unfurl_links": False, "unfurl_media": False, "blocks": [{"type": "divider"}]}


def test_a_dropped_connection_and_a_server_error_are_tried_again():
    waits = []
    with fake_slack(requests.exceptions.ConnectionError("reset"), Reply(503), Reply()) as sent:
        assert slack.call("chat.postMessage", {}, sleep=waits.append)["ok"]
    assert len(sent) == 3 and waits == [2, 10]


def test_a_rate_limit_waits_as_long_as_slack_asks_up_to_half_a_minute():
    waits = []
    with fake_slack(Reply(429, headers={"Retry-After": "7"}), Reply(429, headers={"Retry-After": "600"}), Reply()):
        slack.call("chat.postMessage", {}, sleep=waits.append)
    assert waits == [7, 30]


def test_three_failures_give_up_with_the_last_reason():
    waits = []
    with fake_slack(requests.exceptions.ReadTimeout("read timed out")) as sent:
        try:
            slack.call("chat.postMessage", {}, sleep=waits.append)
            raise AssertionError("should have given up")
        except slack.SlackError as error:
            assert "ReadTimeout" in str(error) and "3 tries" in str(error)
    assert len(sent) == 3 and waits == [2, 10], "no wait after the last try"


def test_slacks_own_refusal_is_not_tried_again():
    with fake_slack(Reply(body={"ok": False, "error": "not_in_channel"})) as sent:
        try:
            slack.call("chat.postMessage", {}, sleep=no_wait)
            raise AssertionError("should have raised")
        except slack.SlackError as error:
            assert "not_in_channel" in str(error)
    assert len(sent) == 1


def test_read_methods_send_form_fields():
    with fake_slack(Reply(body={"ok": True, "channel": {"name": "fantasy-bot"}})) as sent:
        slack.call("conversations.info", {"channel": "C0TEST"}, form=True)
    assert sent[0]["data"] == {"channel": "C0TEST"} and "Content-Type" not in sent[0]["headers"]


def test_with_slack_set_up_a_message_goes_to_slack_alone():
    with fake_slack(Reply()) as sent, fake_gmail() as emails:
        assert quietly(mailer.send, "Week 12: lineup updated", "Moved A to FLEX.")[0] is True
    assert emails == [], "no email unless EMAIL_TOO is true"
    message = json.loads(sent[0]["data"])
    assert message["text"] == "Week 12: lineup updated", "no inbox prefix in Slack"


def test_while_email_too_is_true_it_goes_both_ways():
    os.environ["EMAIL_TOO"] = "true"
    try:
        with fake_slack(Reply()) as sent, fake_gmail() as emails:
            assert quietly(mailer.send, "Week 12: lineup updated", "Moved A to FLEX.")[0] is True
    finally:
        os.environ["EMAIL_TOO"] = ""
    assert len(sent) == 1 and emails == ["Fantasy Football: Week 12: lineup updated"]


def test_without_slack_it_emails_as_before():
    os.environ["SLACK_BOT_TOKEN"] = ""
    try:
        with fake_slack() as sent, fake_gmail() as emails:
            assert quietly(mailer.send, "Week 12: lineup updated", "body")[0] is True
    finally:
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
    assert sent == [] and emails == ["Fantasy Football: Week 12: lineup updated"]


def test_email_still_delivers_when_slack_is_down_during_the_trial():
    os.environ["EMAIL_TOO"] = "true"
    saved = slack.time.sleep
    slack.time.sleep = no_wait
    try:
        with fake_slack(Reply(500)), fake_gmail() as emails:
            delivered, printed = quietly(mailer.send, "Waivers week 12", "body")
    finally:
        os.environ["EMAIL_TOO"] = ""
        slack.time.sleep = saved
    assert delivered is True and len(emails) == 1
    assert "(Slack post failed: SlackError" in printed and "nothing delivered" not in printed


def test_reaching_neither_says_so_in_the_line_hosted_run_looks_for():
    os.environ["EMAIL_TOO"] = "true"
    try:
        with fake_slack(Reply(body={"ok": False, "error": "invalid_auth"})), fake_gmail(works=False):
            delivered, printed = quietly(mailer.send, "Trade offer from Them", "body")
    finally:
        os.environ["EMAIL_TOO"] = ""
    assert delivered is False, "so the trade watch tries the offer again next time"
    assert '(nothing delivered: "Trade offer from Them" reached neither Slack nor email)' in printed
    assert "invalid_auth" in printed and "Operation timed out" in printed


REFUSED = Reply(body={"ok": False, "error": "invalid_blocks"})
RICH = [{"type": "section", "text": {"type": "mrkdwn", "text": "*rich*"}}]


def test_a_layout_slack_refuses_is_posted_plain_instead():
    with fake_slack(REFUSED, Reply()) as sent:
        delivered, printed = quietly(mailer.send, "Week 12: lineup updated", "Moved A to FLEX.", None, RICH)
    assert delivered is True and len(sent) == 2
    assert json.loads(sent[0]["data"])["blocks"] == RICH
    assert json.loads(sent[1]["data"])["blocks"] == slack.blocks_for("Week 12: lineup updated", "Moved A to FLEX.")
    assert "(Slack refused the layout (chat.postMessage: invalid_blocks); posting it plainer)" in printed


def test_no_connection_is_not_tried_again_as_plain():
    saved = slack.time.sleep
    slack.time.sleep = no_wait
    try:
        with fake_slack(requests.exceptions.ConnectionError("reset")) as sent:
            delivered, printed = quietly(mailer.send, "Week 12: lineup updated", "body", None, RICH)
    finally:
        slack.time.sleep = saved
    assert delivered is False and len(sent) == 3, "three tries of the layout, none of a plainer one"
    assert "refused the layout" not in printed


def test_the_thread_reply_goes_under_the_message():
    table = [slack.table([["Player", "Bid"], ["Watson", "$3-5"]], right={1})]
    with fake_slack(Reply(body={"ok": True, "ts": "1.2"}), Reply(body={"ok": True, "ts": "1.3"})) as sent:
        assert quietly(mailer.send, "Waivers week 12", "body", None, RICH, [table])[0] is True
    reply = json.loads(sent[1]["data"])
    assert reply["thread_ts"] == "1.2" and reply["blocks"] == table
    assert "thread_ts" not in json.loads(sent[0]["data"])


def test_a_refused_table_in_the_thread_falls_back_to_the_next_layout():
    table, plain = [slack.table([["Player"], ["Watson"]])], slack.monospace("PLAYER\nWatson")
    with fake_slack(Reply(), REFUSED, Reply()) as sent:
        assert quietly(mailer.send, "Waivers week 12", "body", None, RICH, [table, plain])[0] is True
    assert [json.loads(s["data"])["blocks"] for s in sent[1:]] == [table, plain]


def test_a_lost_thread_reply_still_counts_the_message_as_delivered():
    with fake_slack(Reply(), REFUSED) as sent:
        delivered, printed = quietly(mailer.send, "Waivers week 12", "body", None, RICH, [RICH])
    assert delivered is True and len(sent) == 2
    assert "(Slack thread reply failed: SlackError: chat.postMessage: invalid_blocks)" in printed
    assert "nothing delivered" not in printed


def test_a_failure_notice_leads_with_a_warning():
    with fake_slack(Reply()) as sent:
        assert quietly(mailer.alert, "lineup run failed", "Error: ESPN rejected the cookies (401).")[0] is True
    blocks = json.loads(sent[0]["data"])["blocks"]
    assert blocks[0]["text"]["text"] == ":rotating_light: *lineup run failed*"
    assert blocks[1]["text"]["text"] == "```\nError: ESPN rejected the cookies (401).\n```"


def test_a_write_up_reads_as_paragraphs_with_its_sources_as_links():
    text = ("**Watson** starts. Bid $4 & drop <Demercado>.\n\nSkip McCarthy.\n\nSources:\n"
            "- Week 12 QB start-sit | Darnold, Watson\n  https://example.com/a?b=1&c=2\n"
            "- Yahoo Sports\n  https://sports.yahoo.com/x")
    blocks = slack.write_up(text)
    assert blocks[0]["text"]["text"] == "*Watson* starts. Bid $4 &amp; drop &lt;Demercado&gt;.\n\nSkip McCarthy."
    assert blocks[1] == slack.context("Sources: <https://example.com/a?b=1&amp;c=2|Week 12 QB start-sit - Darnold, "
                                      "Watson> · <https://sports.yahoo.com/x|Yahoo Sports>")
    assert slack.write_up("No sources here.") == [slack.section("No sources here.")]


def test_a_long_write_up_is_split_and_its_sources_capped():
    text = "\n\n".join("w" * 2000 for _ in range(3)) + "\n\nSources:\n" + "\n".join(
        f"- Story {n}\n  https://example.com/{n}" for n in range(15))
    blocks = slack.write_up(text)
    texts = [b["elements"][0]["text"] if b["type"] == "context" else b["text"]["text"] for b in blocks]
    assert all(len(t) <= slack.SECTION for t in texts)
    assert len(blocks) == 4 and texts[-1].count("<https://") == slack.MAX_LINKS


def test_a_table_aligns_numbers_right_and_shows_a_dash_for_nothing():
    block = slack.table([["Player", "Now", "Drop"], ["Watson", "+5.7", ""]], right={1})
    assert block["column_settings"] == [{"align": "left"}, {"align": "right"}, {"align": "left"}]
    assert block["rows"][1] == [{"type": "raw_text", "text": "Watson"}, {"type": "raw_text", "text": "+5.7"},
                                {"type": "raw_text", "text": "-"}]


def test_a_shadow_run_reaches_neither():
    os.environ["MAIL_TO_LOG"] = "1"
    try:
        with fake_slack() as sent, fake_gmail() as emails:
            assert quietly(mailer.send, "Waivers week 12", "body")[0] is True
    finally:
        del os.environ["MAIL_TO_LOG"]
    assert sent == [] and emails == []


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(vars().items()) if name.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"pass  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
