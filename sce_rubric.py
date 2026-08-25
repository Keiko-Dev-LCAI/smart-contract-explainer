#!/usr/bin/env python3
"""
Smart Contract Explainer — type-aware risk rubric (shared with frontend logic).

Identity (known logos/names) NEVER changes risk.
Risk comes only from structured signals extracted from source.
"""

from __future__ import annotations

from typing import Any

VALID_TYPES = {
    "token",
    "wrapper",
    "router",
    "factory",
    "treasury",
    "vault",
    "staking",
    "proxy",
    "infra",
    "access",
    "unknown",
}

GOVERNED = {"timelock", "governor", "multisig"}
LEVELS = ("low", "medium", "high")
RANK = {"low": 0, "medium": 1, "high": 2}


def _norm_type(t: str | None) -> str:
    t = (t or "unknown").strip().lower()
    aliases = {
        "erc20": "token",
        "erc-20": "token",
        "stablecoin": "token",
        "weth": "wrapper",
        "wrap": "wrapper",
        "dex": "router",
        "swap": "router",
        "multicall": "infra",
        "utility": "infra",
        "payment": "access",
        "timelock": "access",
        "governor": "access",
    }
    t = aliases.get(t, t)
    return t if t in VALID_TYPES else "unknown"


def _norm_owner(o: str | None) -> str:
    o = (o or "unknown").strip().lower()
    if o in ("eoa", "timelock", "governor", "multisig", "none", "unknown"):
        return o
    if "timelock" in o:
        return "timelock"
    if "governor" in o or "dao" in o:
        return "governor"
    if "multi" in o or "safe" in o:
        return "multisig"
    if o in ("owner", "admin", "ownable"):
        return "eoa"
    return "unknown"


def _norm_risk(r: str | None) -> str | None:
    if not r:
        return None
    u = str(r).strip().upper().replace(" RISK", "")
    if u in ("LOW", "MEDIUM", "HIGH"):
        return u.lower()
    return None


def score_risk_from_signals(signals: dict[str, Any] | None) -> dict[str, Any]:
    """
    Return {risk: low|medium|high, reasons: [str], contract_type: str}.
    """
    s = dict(signals or {})
    ctype = _norm_type(s.get("contract_type"))
    owner = _norm_owner(s.get("owner_kind"))
    upgradeable = bool(s.get("upgradeable"))
    mint_unlimited = bool(s.get("mint_unlimited"))
    pause = bool(s.get("pause"))
    blacklist = bool(s.get("blacklist"))
    transfer_fee = bool(s.get("transfer_fee"))
    honeypot = bool(s.get("honeypot_signals"))
    holds_user = bool(s.get("holds_user_deposits"))
    native_treasury = bool(s.get("native_treasury_spend"))
    is_infra = bool(s.get("is_router_or_infra")) or ctype in (
        "router",
        "factory",
        "infra",
    )

    reasons: list[str] = []
    risk = "low"

    def bump(level: str, reason: str):
        nonlocal risk
        if RANK[level] > RANK[risk]:
            risk = level
        reasons.append(reason)

    # Hard HIGH
    if honeypot:
        bump("high", "Honeypot / trap-sell signals in source")
    if mint_unlimited and owner == "eoa" and ctype in ("token", "unknown"):
        bump("high", "Unlimited mint controlled by EOA-style owner")

    # Type-aware defaults
    if ctype == "treasury" or native_treasury:
        if owner in GOVERNED:
            # Governed treasury spend is expected — not "user rug"
            risk = "low"
            reasons.append(
                "Treasury with Timelock/Governor/multisig owner — spend is governance-controlled"
            )
            if upgradeable:
                reasons.append(
                    "Upgradeable under same governed owner (ops risk noted, not user honeypot)"
                )
        elif owner == "eoa":
            bump("medium", "Treasury spend controlled by EOA-style owner")
        else:
            bump("medium", "Treasury with unclear owner kind")

    elif ctype in ("router", "factory", "infra") or is_infra:
        risk = "low"
        reasons.append(f"{ctype or 'infra'} infrastructure — not a holdable scam token")
        if honeypot:
            bump("high", "Trap signals even on infra-shaped code")

    elif ctype == "wrapper":
        risk = "low"
        reasons.append("Deposit/withdraw wrapper pattern")

    elif ctype == "access":
        risk = "low"
        reasons.append("Access/payment/utility contract")

    elif ctype == "vault" or holds_user:
        if owner in GOVERNED:
            bump("medium", "Holds user deposits with governed admin (protocol risk)")
        else:
            bump("medium", "Holds user deposits with privileged withdraw path")
        if owner == "eoa" and upgradeable:
            bump("high", "User deposits + upgradeable + EOA admin")

    elif ctype == "staking":
        if owner == "eoa" and (mint_unlimited or upgradeable):
            bump("medium", "Staking with strong admin controls")
        else:
            risk = "low" if risk == "low" else risk
            reasons.append("Staking/rewards surface")

    elif ctype in ("token", "proxy", "unknown"):
        if pause and blacklist:
            bump("medium", "Pause + blacklist/freeze on transfers")
        if transfer_fee:
            bump("medium", "Transfer fee / tax mechanics")
        if upgradeable and owner == "eoa":
            bump("medium", "Upgradeable with EOA-style admin")
        if mint_unlimited and owner in GOVERNED:
            bump("medium", "Unlimited mint under governance (inflation risk)")
        if not reasons and risk == "low":
            reasons.append("Standard token/proxy surface without clear theft mechanics")

    # Generic upgradeable EOA bump if still low and not treasury/infra
    if (
        upgradeable
        and owner == "eoa"
        and ctype not in ("treasury", "router", "factory", "infra", "wrapper")
        and RANK[risk] < RANK["medium"]
    ):
        bump("medium", "Upgradeable with centralized admin")

    model_sug = _norm_risk(s.get("suggested_risk"))
    return {
        "risk": risk,
        "reasons": reasons,
        "contract_type": ctype,
        "owner_kind": owner,
        "model_suggested": model_sug,
        "model_agreed": model_sug == risk if model_sug else None,
    }


# Phrases that are almost always wrong for a type (validator)
BANNED_PHRASES_BY_TYPE = {
    "treasury": [
        r"approve this router",
        r"approve.*before (withdraw|claim)",
        r"users must approve",
        r"scam token",
        r"pull user (lp|liquidity)",
        r"honeypot",
        r"run away",
    ],
    "router": [
        r"meme token",
        r"token holders can be drained",
        r"fixed supply of",
    ],
    "factory": [
        r"meme token",
        r"holders approve",
    ],
    "infra": [
        r"meme token",
        r"rug pull",
    ],
    "wrapper": [
        r"approve this router",
        r"governance token",
    ],
}


def find_banned_phrases(text: str, contract_type: str) -> list[str]:
    import re

    ctype = _norm_type(contract_type)
    hits = []
    for pat in BANNED_PHRASES_BY_TYPE.get(ctype, []):
        if re.search(pat, text or "", re.I):
            hits.append(pat)
    # Global FUD unless high
    for pat in (r"\brun away\b", r"\bscary\b", r"\bdangerous\b"):
        if re.search(pat, text or "", re.I):
            hits.append(pat)
    return hits


def apply_risk_line(report: str, risk: str, reason: str | None = None) -> str:
    """Ensure the report ends with a consistent risk rating line."""
    import re

    risk_u = risk.upper()
    line = f"## 🛡️ Risk Rating\n{risk_u} RISK"
    if reason:
        line += f"\n{reason}"
    line += (
        "\n\n_Risk band set by type-aware checklist (structured signals), "
        "not by brand recognition._"
    )

    # Replace existing risk section if present
    pat = re.compile(
        r"(##\s*🛡[^\n]*Risk Rating\s*\n)([\s\S]*?)(?=\n---|\n##\s|\Z)",
        re.I,
    )
    if pat.search(report or ""):
        return pat.sub(line + "\n\n", report, count=1).rstrip() + "\n"
    # Append
    body = (report or "").rstrip()
    if "---" in body[-80:]:
        return body + "\n\n" + line + "\n"
    return body + "\n\n" + line + "\n\n---\n*AI-assisted analysis, not financial advice.*\n"
