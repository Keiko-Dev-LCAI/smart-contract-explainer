# Smart Contract Explainer — status (2026-08-06)

| Item | Status |
|------|--------|
| **Site** | **LIVE** https://smartcontractexplainer.xyz |
| **Railway backup** | https://web-production-8b2c6.up.railway.app |
| **Serve (primary)** | `/home/keiko/server.py` · cloudflared → :8080 |
| **AI** | **AIVM-only** · free-forever OFF · **5 free** then payment gate |
| **Illustrations** | Disabled · known contracts show identity marks only |
| **Risk** | **Type-aware rubric** from structured signals — brand never sets risk |
| **Golden suite** | `python3 scripts/run_golden.py` → 12/12 offline |
| **Hub** | Listed on hub main |

## Quality system (A–E)

| Phase | What |
|-------|------|
| **A** | Contract taxonomy + type-specific risk rubric in `SYSTEM_PROMPT` |
| **B** | Model must emit `## STRUCTURED` JSON before the user report |
| **C** | Client `processAnalysisOutput()` scores risk, strips bad phrases, rewrites risk line |
| **D** | `golden/fixtures.json` + `scripts/run_golden.py` (+ `sce_rubric.py`) |
| **E** | Known catalog = logos/names only (LCAI, Treasury, USDC, USDT, WETH, DAI, UNI, routers, factory, Multicall3, WBTC, LINK, AIVM JobRegistry) |

## Commands

```bash
# Offline regression (no AIVM cost)
cd ~/Desktop/Orca\ Apps/web/smart-contract-explainer
python3 scripts/run_golden.py
```
