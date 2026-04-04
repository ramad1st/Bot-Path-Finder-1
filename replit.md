# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## CamelBot (attached_assets/)

Python tile-matching game bot that intercepts game traffic via mitmproxy.

### Key Files
- `optimized_bot_fixed.py` — Main bot with mitmproxy addon, beam search, DFS pre-planner
- `test_bot.py` — Offline testing harness with multiple search strategies
- `level_data*.json` — 6 level data files (225 tiles each)

### Search Strategies (in order of effectiveness)
1. **Beam search** — Original deterministic approach (~120 tiles avg)
2. **DFS backtracking** — With v1/v2 scorers, 3000 backtracks (~133-171 tiles)
3. **Wide beam search** — bw=30 with DFS fallback, helps L1 (147 tiles)
4. **Noisy DFS** — Gaussian noise injection across restarts, most effective overall
   - Key: vary noise levels [200, 500, 1000, 2000] and scorers [v1, v2, hybrid]
   - 15-20 restarts per combo, 400 backtracks per restart

### Testing
```bash
cd attached_assets
python3 test_bot.py level_data.json --multi 5000  # single level
```

### Performance (typical/best seen per level)
- L1: 147/147, L2: 133/153, L3: 189/195, L4: 171/171, L5: 123/147, L6: 123/138
- Total: ~886-921 out of 1350 (stochastic due to noisy search)
