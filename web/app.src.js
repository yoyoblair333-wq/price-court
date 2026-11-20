import { createClient, createAccount } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";

const CONTRACT = window.PRICECOURT_CONTRACT;
const EXPLORER = "https://explorer-studio.genlayer.com";
const WEI = 10n ** 18n;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const short = (a) => (a ? a.slice(0, 6) + "…" + a.slice(-4) : "");
const reader = createClient({ chain: studionet });

function toObj(v) {
  if (v instanceof Map) return Object.fromEntries([...v].map(([k, x]) => [k, toObj(x)]));
  if (Array.isArray(v)) return v.map(toObj);
  if (typeof v === "bigint") return v.toString();
  return v;
}
async function read(fn, args = []) {
  return toObj(await reader.readContract({ address: CONTRACT, functionName: fn, args }));
}
function gen(wei) {
  const w = BigInt(wei || 0);
  const whole = w / WEI, frac = (w % WEI).toString().padStart(18, "0").replace(/0+$/, "");
  return frac ? `${whole}.${frac.slice(0, 4)}` : `${whole}`;
}
function txLink(h, label) {
  return `<a href="${EXPLORER}/tx/${h}" target="_blank" rel="noopener">${esc(label || short(h))}</a>`;
}
function log(msg, cls = "") {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = `<time>${new Date().toLocaleTimeString()}</time> ${msg}`;
  $("#log").prepend(li);
}

// ------------------------------------------------------------ live state
function dealCard(d) {
  const ev = d.evidence || {};
  const badge = { proposed: "amber", accepted: "blue", settled: "green", refunded: "grey", cancelled: "grey", declined: "grey" }[d.status] || "grey";
  const winner = d.winner ? (d.winner.toLowerCase() === d.proposer.toLowerCase() ? "Proposer" : "Counterparty") : "";
  return `<article class="deal">
    <header><span class="id">Deal #${esc(d.id)}</span><span class="badge ${badge}">${esc(d.status)}</span></header>
    <p class="terms">“${esc(d.terms_text)}”</p>
    <dl>
      <dt>Structured terms (fixed by consensus)</dt>
      <dd><code>${esc(d.terms.feed)} ${esc(d.terms.comparator)} ${esc(d.terms.threshold)} at ${esc(d.terms.settle_at_iso)}</code> · if true: ${esc(d.terms.winner_if_true)} wins</dd>
      <dt>Parties</dt><dd>${short(d.proposer)} vs ${short(d.counterparty)} · pot ${gen(d.pot)} GEN</dd>
      ${ev.price ? `<dt>Pyth evidence</dt><dd><b>${esc(ev.price)}</b> · published ${new Date(ev.publish_time * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC</dd>` : ""}
      ${winner ? `<dt>Verdict</dt><dd>condition ${d.condition_met ? "met" : "not met"} → <b>${winner} wins</b></dd>` : ""}
      ${d.last_error ? `<dt>Last note</dt><dd class="muted">${esc(d.last_error)}</dd>` : ""}
    </dl></article>`;
}
async function refresh() {
  try {
    const cfg = await read("get_config");
    $("#cfg").innerHTML = `
      <div><span>Deals</span><b>${cfg.deal_count}</b></div>
      <div><span>In escrow</span><b>${gen(cfg.escrowed)} GEN</b></div>
      <div><span>Feeds</span><b>${Object.keys(cfg.feeds).join(" · ")}</b></div>
      <div><span>Settlement window</span><b>${cfg.price_window_seconds}s</b></div>
      <div class="wide"><span>Consensus</span><b>${esc(cfg.consensus || "")}</b></div>`;
    const n = Number(cfg.deal_count);
    const off = Math.max(0, n - 6);
    const deals = n ? await read("list_deals", [off, 6]) : [];
    $("#deals").innerHTML = deals.length ? deals.reverse().map(dealCard).join("") : `<p class="muted">No deals yet. Start one below.</p>`;
    $("#status").textContent = "live · " + new Date().toLocaleTimeString();
    $("#status").className = "ok";
  } catch (e) {
    $("#status").textContent = "studionet unreachable, retrying…";
    $("#status").className = "bad";
  }
}

// ------------------------------------------------------------ try it
const agents = {};
async function newAgent(name) {
  const acct = createAccount();
  const client = createClient({ chain: studionet, account: acct });
  await reader.request({ method: "sim_fundAccount", params: [acct.address, 20 * 1e18] });
  agents[name] = { acct, client };
  return acct.address;
}
async function write(name, fn, args, value = 0n) {
  const { client } = agents[name];
  const hash = await client.writeContract({ address: CONTRACT, functionName: fn, args, value, leaderOnly: false });
  log(`${name} → <code>${fn}</code> sent ${txLink(hash)}`);
  const r = await client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, interval: 4000, retries: 120 });
  log(`${fn} accepted by validators ${txLink(hash, "view")}`, "ok");
  return r;
}
function settleIso(minutes) {
  const t = new Date(Date.now() + minutes * 60000);
  t.setUTCSeconds(0, 0);
  return t.toISOString().replace(".000Z", "Z");
}
let currentDeal = null;
async function runDemo() {
  const btn = $("#go");
  btn.disabled = true;
  try {
    log("Creating two throwaway agents on studionet (free test GEN)…");
    const a = await newAgent("Agent A"), b = await newAgent("Agent B");
    log(`Agent A ${short(a)} · Agent B ${short(b)} funded`, "ok");
    const iso = settleIso(3);
    const text = $("#terms").value.replace("{T}", iso);
    $("#terms").value = text;
    const before = Number((await read("get_config")).deal_count);
    await write("Agent A", "propose_deal", [b, text], 5n * WEI);
    currentDeal = String(before + 1);
    const deal = await read("get_deal", [currentDeal]);
    log(`Validators fixed the terms: <code>${esc(deal.terms.feed)} ${esc(deal.terms.comparator)} ${esc(deal.terms.threshold)} at ${esc(deal.terms.settle_at_iso)}</code>`, "ok");
    await write("Agent B", "accept_deal", [currentDeal], BigInt(deal.stake));
    refresh();
    const settleAt = Number(deal.terms.settle_at) * 1000;
    log(`Both stakes escrowed. Settlement opens at ${deal.terms.settle_at_iso}.`);
    while (Date.now() < settleAt + 15000) {
      $("#count").textContent = `settlement in ${Math.max(0, Math.round((settleAt - Date.now()) / 1000))}s`;
      await new Promise((r) => setTimeout(r, 1000));
    }
    $("#count").textContent = "settling…";
    for (let i = 0; i < 8; i++) {
      await write("Agent A", "settle", [currentDeal]);
      const d = await read("get_deal", [currentDeal]);
      if (d.status === "settled") {
        log(`<b>Settled.</b> Pyth ${esc(d.terms.feed)} = ${esc(d.evidence.price)} → ${d.winner.toLowerCase() === agents["Agent A"].acct.address.toLowerCase() ? "Agent A" : "Agent B"} wins the pot`, "ok");
        break;
      }
      log(`Not yet: ${esc(d.last_error)}. Retrying in 20s.`);
      await new Promise((r) => setTimeout(r, 20000));
    }
    $("#count").textContent = "";
    refresh();
  } catch (e) {
    log("Error: " + esc(e.message || e), "bad");
  } finally {
    btn.disabled = false;
  }
}

async function lookupRep() {
  const a = $("#rep-addr").value.trim();
  try {
    const r = await read("get_reputation", [a]);
    $("#rep-out").innerHTML = `settled <b>${r.settled}</b> · won <b>${r.won}</b> · lost <b>${r.lost}</b> · refunded ${r.refunded} · win rate <b>${(r.win_rate_bps / 100).toFixed(0)}%</b>`;
  } catch (e) { $("#rep-out").textContent = "Enter a 0x address."; }
}

$("#addr").innerHTML = `<a href="${EXPLORER}/address/${CONTRACT}" target="_blank" rel="noopener">${CONTRACT}</a>`;
$("#go").addEventListener("click", runDemo);
$("#rep-go").addEventListener("click", lookupRep);
refresh();
setInterval(refresh, 20000);
