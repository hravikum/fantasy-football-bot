// Fantasy bot: the Slack end of the Bid button. Paste this whole file into a Cloudflare Worker.
//
// Slack sends every tap on a Bid button, and every bid form you submit, to this Worker's address. It
// checks the request really came from Slack and from you, opens the bid form when you tap Bid, and when
// you submit the form it starts the bot's "claim" job on GitHub (claim.py), which places the claim in
// ESPN and says how it went in the waiver report's thread. It keeps nothing itself: the button carries
// all the form needs (waivers.bid_button), and the form carries all the job needs.
//
// Secrets, set in Cloudflare on the Worker (Settings, Variables and Secrets), never in this file:
//   SLACK_SIGNING_SECRET  the Slack app's Signing Secret (api.slack.com/apps, Basic Information)
//   SLACK_BOT_TOKEN       the same xoxb- token as the repository secret
//   SLACK_USER_ID         your Slack member ID; a tap from anyone else does nothing
//   GITHUB_TOKEN          a fine-grained token for this repository only, Actions: read and write
//
// Tests: node slack-worker/worker.test.js

const REPO = "hravikum/fantasy-football-bot";
const MAX_AGE = 300; // seconds: an older request is refused, so a copied one can't be sent again later

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Fantasy bot: Slack posts here.");
    }
    const body = await request.text();
    if (!(await fromSlack(request, body, env.SLACK_SIGNING_SECRET))) {
      return new Response("bad signature", { status: 401 });
    }
    const payload = JSON.parse(new URLSearchParams(body).get("payload") || "{}");
    if (!payload.user || payload.user.id !== env.SLACK_USER_ID) {
      return ok();
    }
    if (payload.type === "block_actions") {
      return openForm(payload, env);
    }
    if (payload.type === "view_submission" && payload.view && payload.view.callback_id === "bid") {
      return submit(payload, env, ctx);
    }
    return ok();
  },
};

// Slack signs each request with the app's signing secret: v0= and the HMAC-SHA256 of "v0:time:body".
export async function fromSlack(request, body, secret, now = Date.now()) {
  const time = request.headers.get("x-slack-request-timestamp");
  const signature = request.headers.get("x-slack-signature") || "";
  if (!secret || !time || Math.abs(now / 1000 - Number(time)) > MAX_AGE) {
    return false;
  }
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`v0:${time}:${body}`));
  const expected = "v0=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return sameText(expected, signature);
}

function sameText(a, b) {
  // Compares every character whatever happens, so the time taken gives nothing away.
  if (a.length !== b.length) {
    return false;
  }
  let difference = 0;
  for (let i = 0; i < a.length; i++) {
    difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return difference === 0;
}

async function openForm(payload, env) {
  const action = (payload.actions || [])[0];
  if (!action || action.action_id !== "bid") {
    return ok();
  }
  const where = {
    ch: (payload.channel && payload.channel.id) || (payload.container && payload.container.channel_id),
    ts: (payload.container && payload.container.message_ts) || (payload.message && payload.message.ts),
  };
  const answer = await slack("views.open", { trigger_id: payload.trigger_id, view: bidForm(JSON.parse(action.value), where) }, env);
  if (!answer.ok) {
    console.log(`views.open failed: ${answer.error}`);
  }
  return ok();
}

// The form: who he is, Claude's call, the range and FAAB left, then the amount and the drop.
export function bidForm(bid, where) {
  const call = bid.k === "bid" ? `*bid $${bid.c}*` : bid.k === "skip" ? "skip" : "no call";
  const range = bid.r[0] === bid.r[1] ? `$${bid.r[0]}` : `$${bid.r[0]}-${bid.r[1]}`;
  const options = bid.o.map(([id, name]) => option(String(id), name));
  options.push(option("none", "Nobody: I have room on my roster"));
  const amount = { type: "number_input", action_id: "amount", is_decimal_allowed: false, min_value: "0",
    max_value: String(bid.f) };
  if (bid.c !== null && bid.c !== undefined) {
    amount.initial_value = String(Math.min(bid.c, bid.f));
  }
  const drop = { type: "static_select", action_id: "drop", options };
  const suggested = options.find((o) => o.value === String(bid.d));
  if (suggested) {
    drop.initial_option = suggested;
  }
  return {
    type: "modal",
    callback_id: "bid",
    title: plain("Bid"),
    submit: plain("Place bid"),
    close: plain("Cancel"),
    private_metadata: JSON.stringify({ p: bid.p, n: bid.n, f: bid.f, ch: where.ch, ts: where.ts }),
    blocks: [
      { type: "section", text: mrkdwn(`*${esc(bid.n)}*   ${esc(bid.t)}\nClaude: ${call}`
        + (bid.y ? ` · _${esc(bid.y)}_` : "") + `\nRange ${range} · FAAB left $${bid.f}`) },
      { type: "input", block_id: "amount", label: plain("Your bid, in dollars"), element: amount },
      { type: "input", block_id: "drop", label: plain("Drop"), element: drop },
      { type: "context", elements: [mrkdwn("This places a claim in ESPN. It goes through at the next waiver run "
        + "unless someone outbids you; until then you can change or cancel it in ESPN.")] },
    ],
  };
}

async function submit(payload, env, ctx) {
  const form = JSON.parse(payload.view.private_metadata);
  const values = payload.view.state.values;
  const amount = Number(values.amount.amount.value);
  if (!Number.isInteger(amount) || amount < 0 || amount > form.f) {
    return errors({ amount: `A whole number of dollars from 0 to ${form.f}.` });
  }
  const chosen = values.drop.drop.selected_option;
  const drop = chosen && chosen.value !== "none" ? Number(chosen.value) : null;
  const dropName = drop === null ? null : chosen.text.text;
  const args = { player: form.p, name: form.n, bid: amount, drop, drop_name: dropName, ts: form.ts };
  const started = await startClaim(args, env);
  if (started.status !== 204) {
    return errors({ amount: `GitHub didn't start the bid (HTTP ${started.status}). Try again in a minute.` });
  }
  const placing = slack("chat.postMessage", { channel: form.ch, thread_ts: form.ts, unfurl_links: false,
    text: `Placing $${amount} on ${esc(form.n)}` + (drop === null ? "" : `, dropping ${esc(dropName)}`)
      + "… the answer follows here in about half a minute." }, env);
  if (ctx && ctx.waitUntil) {
    ctx.waitUntil(placing); // the form closes now; the note follows a moment later
  } else {
    await placing;
  }
  return ok();
}

// The same request cron-job.org sends for the scheduled runs (timers.py), with the bid as the job's input.
function startClaim(args, env) {
  return fetch(`https://api.github.com/repos/${REPO}/actions/workflows/bot.yml/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "fantasy-bot-slack-worker",
      "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: { job: "claim", args: JSON.stringify(args) } }),
  });
}

async function slack(method, body, env) {
  const resp = await fetch(`https://slack.com/api/${method}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.SLACK_BOT_TOKEN}`, "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  });
  return resp.json().catch(() => ({ ok: false, error: `HTTP ${resp.status}` }));
}

function errors(byBlock) {
  // Keeps the form open with the message under the field, rather than closing it.
  return new Response(JSON.stringify({ response_action: "errors", errors: byBlock }),
    { headers: { "Content-Type": "application/json" } });
}

function ok() {
  return new Response("");
}

function plain(text) {
  return { type: "plain_text", text };
}

function mrkdwn(text) {
  return { type: "mrkdwn", text };
}

function option(value, text) {
  return { text: plain(text.slice(0, 75)), value };
}

function esc(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
