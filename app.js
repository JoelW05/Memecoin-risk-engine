const EVM_ADDR_RE = /0x[a-fA-F0-9]{40}/;
// Intentionally permissive (not strict base58): Dexscreener lowercases addresses in its
// URL slugs, which can turn a valid uppercase char (e.g. 'L') into one base58 excludes
// (lowercase 'l'). Dexscreener's own API matches case-insensitively, so we just need a
// plausible-length alphanumeric candidate here, not a strictly valid base58 string.
const SOL_ADDR_RE = /[A-Za-z0-9]{32,44}/g;

const form = document.getElementById("check-form");
const input = document.getElementById("token-input");
const btn = document.getElementById("check-btn");
const statusEl = document.getElementById("status");
const reportEl = document.getElementById("report");
const recentEl = document.getElementById("recent");
const recentChipsEl = document.getElementById("recent-chips");

const RECENT_KEY = "memecheck.recent";

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function extractCandidateAddress(text) {
  text = (text || "").trim();
  const evm = text.match(EVM_ADDR_RE);
  if (evm) return { addr: evm[0], chain: "base" };
  const sol = text.match(SOL_ADDR_RE);
  if (sol && sol.length) {
    sol.sort((a, b) => b.length - a.length);
    return { addr: sol[0], chain: "solana" };
  }
  return null;
}

async function fetchJson(url, timeoutMs = 10000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { headers: { Accept: "application/json" }, signal: controller.signal });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function dexscreenerTokens(chainId, address) {
  const data = await fetchJson(`https://api.dexscreener.com/latest/dex/tokens/${address}`);
  const pairs = (data && data.pairs) || [];
  return pairs.filter((p) => p.chainId === chainId);
}

async function dexscreenerPair(chainId, pairAddress) {
  const data = await fetchJson(`https://api.dexscreener.com/latest/dex/pairs/${chainId}/${pairAddress}`);
  return (data && data.pairs) || [];
}

async function resolveToken(inputStr) {
  const cand = extractCandidateAddress(inputStr);
  if (!cand) return null;
  const chainId = cand.chain === "base" ? "base" : "solana";
  let pairs = await dexscreenerTokens(chainId, cand.addr);
  let tokenAddress = cand.addr;
  if (!pairs.length) {
    const pairPairs = await dexscreenerPair(chainId, cand.addr);
    if (pairPairs.length) {
      tokenAddress = pairPairs[0].baseToken.address;
      pairs = (await dexscreenerTokens(chainId, tokenAddress)) || pairPairs;
    }
  }
  return { chain: chainId, tokenAddress, pairs };
}

async function goplusSecurity(chain, address) {
  const url =
    chain === "base"
      ? `https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses=${address}`
      : `https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses=${address}`;
  const data = await fetchJson(url);
  const result = data && data.result;
  if (!result) return null;
  if (chain === "base") return result[address.toLowerCase()] || null;
  return result[address] || Object.values(result)[0] || null;
}

async function rugcheckReport(mint) {
  return fetchJson(`https://api.rugcheck.xyz/v1/tokens/${mint}/report`);
}

async function honeypotCheck(address) {
  return fetchJson(`https://api.honeypot.is/v2/IsHoneypot?address=${address}&chainID=8453`);
}

function fmtUsd(n) {
  n = Number(n);
  if (!isFinite(n)) return "n/a";
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}

function fmtPrice(n) {
  n = Number(n);
  if (!isFinite(n)) return "n/a";
  if (n === 0) return "$0";
  const abs = Math.abs(n);
  if (abs >= 1) return "$" + n.toFixed(4);
  const decimals = clamp(Math.ceil(-Math.log10(abs)) + 3, 4, 12);
  return "$" + n.toFixed(decimals);
}

function fmtAge(createdMs) {
  if (!createdMs) return "unknown";
  const hours = (Date.now() - createdMs) / 3_600_000;
  if (hours < 1) return `${Math.floor(hours * 60)}m ago`;
  if (hours < 48) return `${hours.toFixed(1)}h ago`;
  return `${(hours / 24).toFixed(1)}d ago`;
}

function pct(value) {
  const v = Number(value);
  if (!isFinite(v)) return null;
  return v <= 1 ? v * 100 : v;
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

// Social/website links come from token-supplied metadata (Dexscreener passes through
// whatever the deployer or a self-serve form filled in) — only allow http(s), never
// render a javascript:/data: URI as a clickable href.
function isSafeUrl(url) {
  if (typeof url !== "string") return false;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

// Rough "how wild does this thing move" proxy from Dexscreener's own reported
// price-change windows. Not a real ATR (no OHLC history available from the free
// API) — just the largest swing seen across the windows we do have.
function estimateVolatilityPct(bestPair) {
  const pc = (bestPair && bestPair.priceChange) || {};
  const windows = [pc.m5, pc.h1, pc.h6, pc.h24]
    .map((v) => Math.abs(Number(v)))
    .filter((v) => isFinite(v));
  if (!windows.length) return null;
  return Math.max(...windows);
}

// Stop-loss / take-profit distances derived purely from this pair's own recent
// volatility. Memecoins swing far more than traditional assets, so bands are
// deliberately wide — a tight stop just gets wicked out by normal noise.
function computeTradeLevels(priceUsd, volatilityPct) {
  const price = Number(priceUsd);
  if (!isFinite(price) || price <= 0 || volatilityPct === null) return null;
  const slPct = clamp(volatilityPct * 1.5, 20, 65);
  const tpPct = clamp(volatilityPct * 2.5, 30, 150);
  return {
    slPct,
    tpPct,
    slPrice: price * (1 - slPct / 100),
    tpPrice: price * (1 + tpPct / 100),
    rr: (tpPct / slPct).toFixed(1),
  };
}

// Generic risk-score-scaled sizing band, expressed as a share of a dedicated
// speculative/high-risk allocation — not of net worth, savings, or any actual
// portfolio we know nothing about.
function suggestPositionSize(score) {
  if (score >= 71) return { range: "Sit this one out", tier: "high", note: "Automated checks put this in the extreme-risk band — position size 0%." };
  if (score >= 46) return { range: "≤ 1%", tier: "high", note: "High risk — keep any position token-sized, not portfolio-sized." };
  if (score >= 21) return { range: "1–3%", tier: "med", note: "Moderate risk — small speculative slice only." };
  return { range: "3–5%", tier: "low", note: "Lower (relative) risk for a memecoin — still cap it hard." };
}

function analyze(chain, pairs, security, rugcheck, honeypot) {
  const flags = [];
  let score = 0;

  let bestPair = null;
  for (const p of pairs) {
    const liq = (p.liquidity && p.liquidity.usd) || 0;
    if (!bestPair || liq > ((bestPair.liquidity && bestPair.liquidity.usd) || 0)) bestPair = p;
  }

  const liquidityUsd = bestPair && bestPair.liquidity ? bestPair.liquidity.usd : null;
  const volume24h = bestPair && bestPair.volume ? bestPair.volume.h24 : null;
  const priceUsd = bestPair ? bestPair.priceUsd : null;
  const fdv = bestPair ? bestPair.fdv || bestPair.marketCap : null;
  const createdMs = bestPair ? bestPair.pairCreatedAt : null;
  const priceChange24h = bestPair && bestPair.priceChange ? bestPair.priceChange.h24 : null;
  const txns24h = (bestPair && bestPair.txns && bestPair.txns.h24) || {};
  const name = bestPair && bestPair.baseToken ? bestPair.baseToken.name : null;
  const symbol = bestPair && bestPair.baseToken ? bestPair.baseToken.symbol : null;
  const info = (bestPair && bestPair.info) || {};
  const websites = (info.websites || []).filter((w) => isSafeUrl(w.url));
  const socials = (info.socials || []).filter((s) => isSafeUrl(s.url));

  if (bestPair && !websites.length && !socials.length) {
    flags.push(["medium", "No official website or social links (Twitter/Telegram/Discord) found on Dexscreener — most legitimate projects list at least one. Could just be a very fresh listing, so verify manually."]);
    score += 5;
  }

  if (liquidityUsd === null || liquidityUsd === undefined) {
    flags.push(["critical", "No liquidity pool found on Dexscreener — token may be untradeable, unlisted, or the address is wrong."]);
    score += 30;
  } else if (liquidityUsd < 3000) {
    flags.push(["critical", `Extremely low liquidity (${fmtUsd(liquidityUsd)}) — trivial to manipulate price.`]);
    score += 30;
  } else if (liquidityUsd < 15000) {
    flags.push(["high", `Low liquidity (${fmtUsd(liquidityUsd)}) — expect heavy slippage.`]);
    score += 18;
  } else if (liquidityUsd < 50000) {
    flags.push(["medium", `Moderate liquidity (${fmtUsd(liquidityUsd)}).`]);
    score += 6;
  }

  if (createdMs) {
    const ageHours = (Date.now() - createdMs) / 3_600_000;
    if (ageHours < 6) {
      flags.push(["high", `Pool created ${fmtAge(createdMs)} — extremely new, classic rug window.`]);
      score += 15;
    } else if (ageHours < 48) {
      flags.push(["medium", `Pool created ${fmtAge(createdMs)} — still very new.`]);
      score += 8;
    }
  }

  if (volume24h !== null && volume24h !== undefined && liquidityUsd) {
    if (volume24h < liquidityUsd * 0.05) {
      flags.push(["medium", "24h volume is very low relative to liquidity — thin/inactive trading."]);
      score += 5;
    }
  }

  if (chain === "solana") {
    if (security) {
      const mintStatus = security.mintable && security.mintable.status;
      const freezeStatus = security.freezable && security.freezable.status;
      if (mintStatus === "1") {
        flags.push(["critical", "Mint authority is NOT renounced — the dev can mint unlimited new tokens at will."]);
        score += 25;
      } else if (mintStatus === "0") {
        flags.push(["good", "Mint authority renounced."]);
      }
      if (freezeStatus === "1") {
        flags.push(["critical", "Freeze authority is NOT renounced — the dev can freeze any wallet's tokens."]);
        score += 25;
      } else if (freezeStatus === "0") {
        flags.push(["good", "Freeze authority renounced."]);
      }
      const top10 = pct(security.top10_holder_percent);
      if (top10 !== null) {
        if (top10 > 70) {
          flags.push(["critical", `Top 10 holders control ${top10.toFixed(0)}% of supply — extreme concentration.`]);
          score += 20;
        } else if (top10 > 40) {
          flags.push(["high", `Top 10 holders control ${top10.toFixed(0)}% of supply.`]);
          score += 10;
        }
      }
    }
    if (rugcheck && Array.isArray(rugcheck.risks)) {
      const seen = new Set();
      for (const r of rugcheck.risks) {
        const level = (r.level || "").toLowerCase();
        const desc = r.description || r.name;
        if (!desc || seen.has(desc)) continue;
        seen.add(desc);
        if (level === "danger" || level === "high") {
          flags.push(["high", `RugCheck: ${desc}`]);
          score += 8;
        } else if (level === "warn") {
          flags.push(["medium", `RugCheck: ${desc}`]);
          score += 3;
        }
      }
    }
  } else {
    if (security) {
      const isFlagged = (key) => String(security[key]) === "1";
      if (isFlagged("is_honeypot")) {
        flags.push(["critical", "GoPlus flags this as a HONEYPOT — you may not be able to sell."]);
        score += 40;
      }
      if (security.is_open_source === "0") {
        flags.push(["high", "Contract source code is NOT verified/open source."]);
        score += 12;
      }
      if (isFlagged("cannot_sell_all")) {
        flags.push(["critical", "Cannot sell full balance — sell restriction detected."]);
        score += 20;
      }
      if (isFlagged("hidden_owner")) {
        flags.push(["critical", "Hidden owner detected — ownership may not actually be renounced."]);
        score += 20;
      }
      if (isFlagged("can_take_back_ownership")) {
        flags.push(["critical", "Ownership can be reclaimed by the deployer even if it looks renounced."]);
        score += 20;
      }
      if (isFlagged("selfdestruct")) {
        flags.push(["critical", "Contract has a self-destruct function."]);
        score += 20;
      }
      if (isFlagged("is_mintable")) {
        flags.push(["high", "Token is mintable — supply can be inflated."]);
        score += 15;
      }
      for (const [key, label] of [["buy_tax", "Buy"], ["sell_tax", "Sell"]]) {
        const taxp = pct(security[key]);
        if (taxp !== null) {
          if (taxp >= 20) {
            flags.push(["critical", `${label} tax is ${taxp.toFixed(0)}% — likely a scam/near-unsellable token.`]);
            score += 20;
          } else if (taxp >= 8) {
            flags.push(["medium", `${label} tax is ${taxp.toFixed(0)}%.`]);
            score += 6;
          }
        }
      }
      const owner = security.owner_address;
      if (owner && owner !== "0x0000000000000000000000000000000000000000" && !isFlagged("hidden_owner")) {
        flags.push(["medium", "Contract ownership is not renounced."]);
        score += 5;
      }
      const holderCount = Number(security.holder_count);
      if (!isNaN(holderCount) && security.holder_count !== undefined && holderCount < 50) {
        flags.push(["medium", `Only ${security.holder_count} holders — very small/early holder base.`]);
        score += 5;
      }
    }
    if (honeypot) {
      const hr = honeypot.honeypotResult || {};
      const alreadyFlagged = flags.some(([sev, msg]) => sev === "critical" && msg.includes("HONEYPOT"));
      if (hr.isHoneypot && !alreadyFlagged) {
        const reason = hr.honeypotReason || "reason unspecified";
        flags.push(["critical", `Honeypot.is confirms HONEYPOT: ${reason}`]);
        score += 40;
      }
      const sim = honeypot.simulationResult || {};
      if (sim.sellTax !== undefined && sim.sellTax >= 20 && !flags.some(([, msg]) => msg.includes("Sell tax"))) {
        flags.push(["high", `Honeypot.is simulation: sell tax ${sim.sellTax}%.`]);
        score += 10;
      }
    }
  }

  score = Math.min(score, 100);
  const volatilityPct = estimateVolatilityPct(bestPair);
  const tradeLevels = computeTradeLevels(priceUsd, volatilityPct);
  const positionSize = suggestPositionSize(score);
  return {
    name, symbol, priceUsd, liquidityUsd, volume24h, fdv, priceChange24h,
    txns24h, createdMs, flags, score, volatilityPct, tradeLevels, positionSize,
    websites, socials,
  };
}

function socialLabel(type) {
  const labels = {
    twitter: "Twitter / X",
    x: "Twitter / X",
    telegram: "Telegram",
    discord: "Discord",
    website: "Website",
    Website: "Website",
  };
  return labels[type] || (type ? type.charAt(0).toUpperCase() + type.slice(1) : "Link");
}

function riskLabel(score) {
  if (score >= 71) return ["EXTREME RISK", "high"];
  if (score >= 46) return ["HIGH RISK", "high"];
  if (score >= 21) return ["MODERATE RISK", "med"];
  return ["LOWER RISK", "low"];
}

function renderReport(chain, tokenAddress, result) {
  const [label, tier] = riskLabel(result.score);
  const name = result.name || "?";
  const symbol = result.symbol || "?";

  let html = `
    <div class="report-head">
      <div>
        <div class="token-title">${escapeHtml(name)} (${escapeHtml(symbol)})</div>
        <div class="token-addr"><span class="chain-badge">${chain.toUpperCase()}</span>${escapeHtml(tokenAddress)}</div>
      </div>
      <div class="score-block">
        <div class="score-num score-${tier}">${result.score}<span style="font-size:16px">/100</span></div>
        <div class="score-label label-${tier}">${label}</div>
      </div>
    </div>

    <div class="snapshot">
      <div><div class="stat-label">Price</div><div class="stat-value">${result.priceUsd ? "$" + escapeHtml(result.priceUsd) : "n/a"}</div></div>
      <div><div class="stat-label">Liquidity</div><div class="stat-value">${result.liquidityUsd != null ? fmtUsd(result.liquidityUsd) : "n/a"}</div></div>
      <div><div class="stat-label">24h Volume</div><div class="stat-value">${result.volume24h != null ? fmtUsd(result.volume24h) : "n/a"}</div></div>
      <div><div class="stat-label">FDV / MCap</div><div class="stat-value">${result.fdv != null ? fmtUsd(result.fdv) : "n/a"}</div></div>
      <div><div class="stat-label">24h Change</div><div class="stat-value ${result.priceChange24h >= 0 ? "up" : "down"}">${result.priceChange24h != null ? (result.priceChange24h >= 0 ? "+" : "") + result.priceChange24h.toFixed(1) + "%" : "n/a"}</div></div>
      <div><div class="stat-label">Pool Age</div><div class="stat-value">${fmtAge(result.createdMs)}</div></div>
      <div><div class="stat-label">24h Txns</div><div class="stat-value">${result.txns24h.buys ?? "?"}B / ${result.txns24h.sells ?? "?"}S</div></div>
    </div>

    <div class="trade-params">
      <h3>Suggested trade parameters <span class="heuristic-tag">heuristic</span></h3>
      ${
        result.tradeLevels
          ? `
      <div class="snapshot trade-grid">
        <div><div class="stat-label">Stop-loss</div><div class="stat-value down">${fmtPrice(result.tradeLevels.slPrice)}<br><span class="stat-sub">&minus;${result.tradeLevels.slPct.toFixed(0)}%</span></div></div>
        <div><div class="stat-label">Take-profit</div><div class="stat-value up">${fmtPrice(result.tradeLevels.tpPrice)}<br><span class="stat-sub">+${result.tradeLevels.tpPct.toFixed(0)}%</span></div></div>
        <div><div class="stat-label">Reward : Risk</div><div class="stat-value">${result.tradeLevels.rr} : 1</div></div>
        <div><div class="stat-label">Suggested size</div><div class="stat-value">${escapeHtml(result.positionSize.range)}</div></div>
      </div>
      <p class="trade-note">Stop-loss/take-profit are derived only from this pair's own recent price-swing volatility (5m/1h/6h/24h change) — bands are wide on purpose, since memecoins swing far more than typical assets, and past volatility is no guarantee of future behavior. Suggested size is a generic band scaled only by the automated risk score above, as a share of a dedicated speculative allocation — not your net worth, savings, or actual portfolio, which this tool knows nothing about. ${escapeHtml(result.positionSize.note)} None of this is financial advice.</p>`
          : `
      <p class="trade-note">Not enough price-history data from Dexscreener to estimate stop-loss/take-profit levels. Suggested size: <strong>${escapeHtml(result.positionSize.range)}</strong> — ${escapeHtml(result.positionSize.note)} This is not financial advice.</p>`
      }
    </div>

    <div class="socials-block">
      <h3>Socials &amp; further research</h3>
      ${
        result.websites.length || result.socials.length
          ? `<div class="link-row">${[...result.websites, ...result.socials]
              .map((s) => `<a class="link-pill" href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(socialLabel(s.type || s.label))}</a>`)
              .join("")}</div>`
          : `<p class="trade-note">No official links found on Dexscreener for this pair.</p>`
      }
      <p class="trade-note" style="margin-top:10px;">This tool can't pull live mention counts or sentiment — X/Twitter's search API is paid-only and Reddit blocks browser requests. Use these to check manually instead:</p>
      <div class="link-row">
        <a class="link-pill search" href="https://x.com/search?q=${encodeURIComponent(`${name || ""} ${symbol || ""}`.trim())}&f=live" target="_blank" rel="noopener noreferrer">Search X</a>
        <a class="link-pill search" href="https://www.reddit.com/search/?q=${encodeURIComponent(`${name || ""} ${symbol || ""}`.trim())}" target="_blank" rel="noopener noreferrer">Search Reddit</a>
        <a class="link-pill search" href="https://www.google.com/search?q=${encodeURIComponent(`${name || tokenAddress} ${symbol || ""} scam`.trim())}" target="_blank" rel="noopener noreferrer">Search Google</a>
      </div>
    </div>
  `;

  const groups = [
    ["critical", "Critical red flags", "[X]"],
    ["high", "High-risk flags", "[X]"],
    ["medium", "Warnings", "[!]"],
    ["good", "Positive signals", "[OK]"],
  ];
  let anyBad = false;
  for (const [severity, heading, marker] of groups) {
    const items = result.flags.filter(([sev]) => sev === severity);
    if (!items.length) continue;
    if (severity !== "good") anyBad = true;
    html += `<div class="flag-group ${severity}"><h3>${heading}</h3>`;
    for (const [, msg] of items) {
      html += `<div class="flag-item"><span class="marker">${marker}</span><span>${escapeHtml(msg)}</span></div>`;
    }
    html += `</div>`;
  }
  if (!anyBad) {
    html += `<div class="no-flags">No major red flags detected by automated checks.</div>`;
  }

  reportEl.innerHTML = html;
  reportEl.hidden = false;
}

function getRecent() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveRecent(inputStr, symbol) {
  let recent = getRecent().filter((r) => r.input !== inputStr);
  recent.unshift({ input: inputStr, label: symbol || inputStr.slice(0, 10) });
  recent = recent.slice(0, 6);
  localStorage.setItem(RECENT_KEY, JSON.stringify(recent));
  renderRecent();
}

function renderRecent() {
  const recent = getRecent();
  if (!recent.length) {
    recentEl.hidden = true;
    return;
  }
  recentEl.hidden = false;
  recentChipsEl.innerHTML = "";
  for (const r of recent) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = r.label;
    chip.addEventListener("click", () => {
      input.value = r.input;
      form.requestSubmit();
    });
    recentChipsEl.appendChild(chip);
  }
}

function setStatus(msg, isError) {
  if (!msg) {
    statusEl.hidden = true;
    return;
  }
  statusEl.hidden = false;
  statusEl.textContent = msg;
  statusEl.className = "status" + (isError ? " error" : "");
}

async function runCheck(rawInput) {
  reportEl.hidden = true;
  setStatus("Resolving token…");
  btn.disabled = true;
  btn.textContent = "Analyzing…";
  try {
    const resolved = await resolveToken(rawInput);
    if (!resolved) {
      setStatus("Could not find a Solana address or Base (0x…) address/link in that input.", true);
      return;
    }
    const { chain, tokenAddress, pairs } = resolved;
    if (!pairs.length) {
      setStatus(`No Dexscreener pairs found for ${tokenAddress} on ${chain}. It may be too new, unlisted, or the address may be wrong. Still checking security data…`);
    } else {
      setStatus("Checking contract security…");
    }

    const [security, rugcheck, honeypot] = await Promise.all([
      goplusSecurity(chain, tokenAddress),
      chain === "solana" ? rugcheckReport(tokenAddress) : Promise.resolve(null),
      chain === "base" ? honeypotCheck(tokenAddress) : Promise.resolve(null),
    ]);

    const result = analyze(chain, pairs, security, rugcheck, honeypot);
    setStatus(null);
    renderReport(chain, tokenAddress, result);
    saveRecent(rawInput, result.symbol);
  } catch (err) {
    setStatus(`Something went wrong: ${err.message || err}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Analyze";
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const val = input.value.trim();
  if (!val) return;
  runCheck(val);
});

renderRecent();
