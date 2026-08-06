# Smart Contract Explainer — status (2026-08-06)

| Item | Status |
|------|--------|
| **Site** | **LIVE** https://smartcontractexplainer.xyz |
| **Serve (legacy host)** | systemd `contract-explainer` → `/home/keiko/server.py` · cloudflared → :8080 |
| **AI** | **AIVM-only** (no Ollama in prod) · `LIGHTCHAIN_PRIVATE_KEY` |
| **Illustrations** | **Disabled** · known tokens show official mark SVG |
| **Copy** | Friendly explainer; token owner ≠ DAO treasury |
| **Hub** | Listed on hub main |
| **Desktop copy** | `~/Desktop/Orca Apps/web/smart-contract-explainer/` |

## Session 167b polish (A–D)
- A: New system prompt + known LCAI/Treasury blurbs + risk softening for known tokens
- B: FLUX/Comfy art removed
- C: Badge = Lightchain AIVM · llama3-8b
- D: AIVM-only routing · `/api/health` · Railway-ready files
