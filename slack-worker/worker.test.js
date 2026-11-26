// Tests for the Slack Worker. Run: node slack-worker/worker.test.js (Node 18 or later; nothing to install).
// Nothing here reaches Slack or GitHub: fetch is replaced by a fake that records each request.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import worker, { bidForm, fromSlack } from "./worker.js";

const ENV = { SLACK_SIGNING_SECRET: "shh", SLACK_BOT_TOKEN: "xoxb-test", SLACK_USER_ID: "U0HARI",
  GITHUB_TOKEN: "github-test" };
const BID = { p: 5082269, n: "Dean Connors", t: "RB · LAR", r: [3, 5], k: "bid", c: 4, y: "starter out",
  d: 4362478, o: [[4362478, "Emari Demercado"], [4427095, "DeMario Douglas"]], f: 55, w: 12 };

function signed(payload, { secret = "shh", time = Math.floor(Date.now() / 1000), method = "POST" } = {}) {
  const body = new URLSearchParams({ payload: JSON.stringify(payload) }).toString();
  const signature = "v0=" + createHmac("sha256", secret).update(`v0:${time}:${body}`).digest("hex");
  return new Request("https://fantasy-bot.example.workers.dev/", { method, body: method === "POST" ? body : undefined,
    headers: { "x-slack-request-timestamp": String(time), "x-slack-signature": signature,
      "content-type": "application/x-www-form-urlencoded" } });
}

let sent = [];
let replies = {};
globalThis.fetch = async (url, init) => {
  sent.push({ url, init, body: init && init.body ? JSON.parse(init.body) : null });
  const reply = replies[url] || { status: 200, json: { ok: true } };
  return new Response(reply.status === 204 ? null : JSON.stringify(reply.json || {}), { status: reply.status });
};

function tap(user = "U0HARI") {
  return { type: "block_actions", user: { id: user }, trigger_id: "trig-1", channel: { id: "C0CHAN" },
    container: { message_ts: "1.2", channel_id: "C0CHAN" },
    actions: [{ action_id: "bid", value: JSON.stringify(BID) }] };
}

function submission(amount, drop = "4362478", dropName = "Emari Demercado") {
  const view = bidForm(BID, { ch: "C0CHAN", ts: "1.2" });
  return { type: "view_submission", user: { id: "U0HARI" }, view: { ...view, state: { values: {
    amount: { amount: { type: "number_input", value: String(amount) } },
    drop: { drop: { type: "static_select", selected_option: { value: drop, text: { type: "plain_text", text: dropName } } } },
  } } } };
}

const DISPATCH = "https://api.github.com/repos/hravikum/fantasy-football-bot/actions/workflows/bot.yml/dispatches";
const tests = {
  async "only Slack's signed, recent requests get through"() {
    assert.equal((await worker.fetch(signed(tap(), { secret: "wrong" }), ENV)).status, 401);
    assert.equal((await worker.fetch(signed(tap(), { time: Math.floor(Date.now() / 1000) - 600 }), ENV)).status, 401);
    const request = signed(tap());
    assert.equal(await fromSlack(request, await request.clone().text(), "shh"), true);
    assert.equal((await worker.fetch(new Request("https://x.example/"), ENV)).status, 200, "a browser visit is fine");
    assert.equal(sent.length, 0);
  },

  async "a tap from anyone but you does nothing"() {
    const response = await worker.fetch(signed(tap("U0SOMEONE")), ENV);
    assert.equal(response.status, 200);
    assert.equal(sent.length, 0);
  },

  async "Bid opens the form with Claude's bid and the report's drop filled in"() {
    await worker.fetch(signed(tap()), ENV);
    const [call] = sent;
    assert.equal(call.url, "https://slack.com/api/views.open");
    assert.equal(call.init.headers.Authorization, "Bearer xoxb-test");
    assert.equal(call.body.trigger_id, "trig-1");
    const view = call.body.view;
    assert.equal(view.callback_id, "bid");
    assert.deepEqual(JSON.parse(view.private_metadata), { p: 5082269, n: "Dean Connors", f: 55, ch: "C0CHAN", ts: "1.2" });
    assert.match(view.blocks[0].text.text, /\*Dean Connors\*   RB · LAR\nClaude: \*bid \$4\* · _starter out_\nRange \$3-5 · FAAB left \$55/);
    const amount = view.blocks[1].element;
    assert.deepEqual([amount.initial_value, amount.min_value, amount.max_value, amount.is_decimal_allowed], ["4", "0", "55", false]);
    const drop = view.blocks[2].element;
    assert.equal(drop.initial_option.value, "4362478");
    assert.deepEqual(drop.options.map((o) => o.value), ["4362478", "4427095", "none"]);
  },

  async "a player Claude skipped opens with no amount, and a missing drop with none chosen"() {
    const view = bidForm({ ...BID, k: "skip", c: null, d: null }, { ch: "C0CHAN", ts: "1.2" });
    assert.equal(view.blocks[1].element.initial_value, undefined);
    assert.equal(view.blocks[2].element.initial_option, undefined);
    assert.match(view.blocks[0].text.text, /Claude: skip/);
  },

  async "submitting starts the claim job on GitHub and says so in the report's thread"() {
    replies[DISPATCH] = { status: 204 };
    const response = await worker.fetch(signed(submission(0)), ENV);
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "", "an empty answer closes the form");
    const [dispatch, note] = sent;
    assert.equal(dispatch.url, DISPATCH);
    assert.equal(dispatch.init.headers.Authorization, "Bearer github-test");
    assert.ok(dispatch.init.headers["User-Agent"], "GitHub refuses a request with no User-Agent");
    assert.equal(dispatch.body.ref, "main");
    assert.equal(dispatch.body.inputs.job, "claim");
    assert.deepEqual(JSON.parse(dispatch.body.inputs.args),
      { player: 5082269, name: "Dean Connors", bid: 0, drop: 4362478, drop_name: "Emari Demercado", ts: "1.2" });
    assert.equal(note.url, "https://slack.com/api/chat.postMessage");
    assert.deepEqual([note.body.channel, note.body.thread_ts], ["C0CHAN", "1.2"]);
    assert.match(note.body.text, /^Placing \$0 on Dean Connors, dropping Emari Demercado…/);
  },

  async "nobody to drop sends no drop"() {
    replies[DISPATCH] = { status: 204 };
    await worker.fetch(signed(submission(3, "none", "Nobody: I have room on my roster")), ENV);
    const args = JSON.parse(sent[0].body.inputs.args);
    assert.deepEqual([args.drop, args.drop_name], [null, null]);
    assert.doesNotMatch(sent[1].body.text, /dropping/);
  },

  async "an amount past your FAAB keeps the form open with the reason"() {
    for (const amount of [56, -1, 2.5]) {
      const response = await worker.fetch(signed(submission(amount)), ENV);
      const answer = await response.json();
      assert.equal(answer.response_action, "errors");
      assert.match(answer.errors.amount, /from 0 to 55/);
    }
    assert.equal(sent.length, 0, "nothing started");
  },

  async "if GitHub won't start the job, the form stays open and says so"() {
    replies[DISPATCH] = { status: 401, json: { message: "Bad credentials" } };
    const answer = await (await worker.fetch(signed(submission(3)), ENV)).json();
    assert.match(answer.errors.amount, /GitHub didn't start the bid \(HTTP 401\)/);
    assert.equal(sent.length, 1, "no 'Placing' note for a bid that didn't start");
  },
};

let failures = 0;
for (const [name, test] of Object.entries(tests)) {
  sent = [];
  replies = {};
  try {
    await test();
    console.log(`pass  ${name}`);
  } catch (error) {
    failures++;
    console.log(`FAIL  ${name}: ${error.message}`);
  }
}
console.log(`\n${Object.keys(tests).length - failures}/${Object.keys(tests).length} passed`);
process.exit(failures ? 1 : 0);
