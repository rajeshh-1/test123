import fs from "node:fs";
import { createWalletClient, encodeFunctionData, http, zeroHash } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { polygon } from "viem/chains";
import { RelayClient, RelayerTxType } from "@polymarket/builder-relayer-client";
import { BuilderConfig } from "@polymarket/builder-signing-sdk";
import { deriveSafe } from "@polymarket/builder-relayer-client/dist/builder/derive.js";
import { getContractConfig } from "@polymarket/builder-relayer-client/dist/config/index.js";

const DATA_API_HOST = "https://data-api.polymarket.com";
const DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com";
const CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
const USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";

const CTF_REDEEM_ABI = [
  {
    constant: false,
    inputs: [
      { name: "collateralToken", type: "address" },
      { name: "parentCollectionId", type: "bytes32" },
      { name: "conditionId", type: "bytes32" },
      { name: "indexSets", type: "uint256[]" },
    ],
    name: "redeemPositions",
    outputs: [],
    payable: false,
    stateMutability: "nonpayable",
    type: "function",
  },
];

function loadEnvFile(path) {
  if (!path || !fs.existsSync(path)) return;
  const raw = fs.readFileSync(path, "utf8");
  for (const line of raw.split(/\r?\n/)) {
    const v = line.trim();
    if (!v || v.startsWith("#") || !v.includes("=")) continue;
    const idx = v.indexOf("=");
    const key = v.slice(0, idx).trim();
    const value = v.slice(idx + 1).trim().replace(/^['"]|['"]$/g, "");
    if (key && !(key in process.env)) process.env[key] = value;
  }
}

function parseArgs() {
  const args = process.argv.slice(2);
  const out = {
    envFile: ".env.claim",
    conditionId: "",
    maxClaims: 1,
    dryRun: false,
    includeZero: false,
  };
  for (let i = 0; i < args.length; i += 1) {
    const a = args[i];
    if (a === "--env-file") out.envFile = args[++i];
    else if (a === "--condition-id") out.conditionId = args[++i];
    else if (a === "--max-claims") out.maxClaims = Number(args[++i] || "1");
    else if (a === "--dry-run") out.dryRun = true;
    else if (a === "--include-zero") out.includeZero = true;
  }
  return out;
}

function requireEnv(name) {
  const v = process.env[name];
  if (!v) throw new Error(`Missing env var: ${name}`);
  return v;
}

async function getRedeemablePositions(user, conditionId = "") {
  const url = new URL(`${DATA_API_HOST}/positions`);
  url.searchParams.set("user", user);
  url.searchParams.set("redeemable", "true");
  url.searchParams.set("size", "200");
  if (conditionId) url.searchParams.set("conditionId", conditionId);
  const res = await fetch(url, { method: "GET" });
  if (!res.ok) throw new Error(`positions failed: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data) ? data : [];
}

function groupByCondition(positions) {
  const map = new Map();
  for (const p of positions) {
    const cond = String(p.conditionId || "").toLowerCase();
    if (!cond.startsWith("0x") || cond.length !== 66) continue;
    const negRisk = Boolean(p.negativeRisk);
    const key = `${cond}|${negRisk ? "1" : "0"}`;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(p);
  }
  return map;
}

function createCtfRedeemTx(conditionId) {
  const data = encodeFunctionData({
    abi: CTF_REDEEM_ABI,
    functionName: "redeemPositions",
    args: [USDC_E_ADDRESS, zeroHash, conditionId, [1n, 2n]],
  });
  return { to: CTF_ADDRESS, data, value: "0" };
}

async function main() {
  const opts = parseArgs();
  loadEnvFile(opts.envFile);

  const pk = requireEnv("POLY_PRIVATE_KEY");
  const funder = requireEnv("POLY_FUNDER").toLowerCase();
  const apiKey = requireEnv("POLY_BUILDER_API_KEY");
  const secret = requireEnv("POLY_BUILDER_SECRET");
  const passphrase = requireEnv("POLY_BUILDER_PASSPHRASE");
  const relayerUrl = process.env.POLYMARKET_RELAYER_URL || DEFAULT_RELAYER_URL;
  const rpcUrl = process.env.POLY_RPC_URL || "https://polygon-rpc.com";

  const account = privateKeyToAccount(pk);
  const wallet = createWalletClient({
    account,
    chain: polygon,
    transport: http(rpcUrl),
  });

  const builderConfig = new BuilderConfig({
    localBuilderCreds: {
      key: apiKey,
      secret,
      passphrase,
    },
  });

  const derivedSafe = deriveSafe(account.address, getContractConfig(137).SafeContracts.SafeFactory);
  const txType = derivedSafe.toLowerCase() === funder ? RelayerTxType.SAFE : RelayerTxType.PROXY;
  console.log(
    `[RELAYER-CLAIM] signer=${account.address} derivedSafe=${derivedSafe} funder=${funder} mode=${txType}`
  );

  const relayClient = new RelayClient(
    relayerUrl,
    137,
    wallet,
    builderConfig,
    txType
  );

  const positions = await getRedeemablePositions(funder, opts.conditionId || "");
  console.log(`[RELAYER-CLAIM] funder=${funder} redeemable_positions=${positions.length}`);
  if (!positions.length) return;

  const grouped = groupByCondition(positions);
  const items = Array.from(grouped.entries())
    .map(([key, rows]) => {
      const [conditionId, neg] = key.split("|");
      const estValue = rows.reduce(
        (acc, r) => acc + Number(r.currentValue || 0),
        0
      );
      const hasPayout = rows.some(
        (r) => Number(r.curPrice || 0) > 0 || Number(r.currentValue || 0) > 0
      );
      return { conditionId, negativeRisk: neg === "1", rows, estValue, hasPayout };
    })
    .filter((x) => !x.negativeRisk)
    .filter((x) => (opts.includeZero ? true : x.hasPayout))
    .sort((a, b) => Number(b.estValue) - Number(a.estValue));

  if (!items.length) {
    console.log(
      "[RELAYER-CLAIM] sem condicoes com payout estimado (>0). Use --include-zero para claimar tudo."
    );
    return;
  }

  let sent = 0;
  for (const item of items) {
    if (sent >= opts.maxClaims) break;
    console.log(
      `[RELAYER-CLAIM] condition=${item.conditionId} outcomes=${item.rows
        .map((r) => r.outcome)
        .join(",")} estValue=${item.estValue.toFixed(4)}`
    );
    if (opts.dryRun) {
      sent += 1;
      continue;
    }
    const tx = createCtfRedeemTx(item.conditionId);
    const resp = await relayClient.execute([tx], `redeem ${item.conditionId}`);
    const result = await resp.wait();
    console.log(`[RELAYER-CLAIM] txHash=${result.transactionHash} proxy=${result.proxyAddress || ""}`);
    sent += 1;
  }
  console.log(`[RELAYER-CLAIM] finished sent=${sent}`);
}

main().catch((e) => {
  console.error(`[RELAYER-CLAIM][ERRO] ${e?.message || e}`);
  process.exit(1);
});
