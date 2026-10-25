# BSE SHP UI — Tech & Patterns Reference

Agent-oriented overview of the BSE Shareholding Patterns UI. Use this when cloning the same stack and patterns in another application.

Canonical product guide: [bse.md](bse.md). Operator cheat sheet: `p01/Quick_Ref.txt`.

---

## Stack (use the same)

| Layer | Choice |
|--------|--------|
| API | **FastAPI** + **uvicorn** |
| Frontend | **React 19** + **TypeScript** + **Vite 6** + **react-router-dom 7** |
| Charts | **Apache ECharts 5** via **`echarts-for-react`** |
| CSS | **Vanilla CSS** + CSS variables + `[data-theme="dark\|light"]` (no Tailwind/Bootstrap) |
| DB | **DuckDB** (read-only at serve time) |
| Templating | **None** — SPA only; FastAPI returns JSON + optional static `ui/dist` |

**Not used:** HTMX, server-rendered Jinja, form POSTs for primary UX, Plotly, React Query as a required pattern.

**Install**

```powershell
pip install -e ".[shp-ui]"
cd ui
npm install
cd ..
```

---

## Architecture pattern

```
raw files → offline index CLI → DuckDB feature tables
                ↓
         FastAPI /api/* (JSON)
                ↓
    React SPA (fetch) + heavy time-series from disk JSON
```

- **Split jobs:** `*_ui_index` (build) vs `*_ui_serve` (API). Never index at request time.
- **Dev:** API `:8765` + Vite `:5173` with `/api` proxy.
- **Prod-like:** `npm run build` → API serves `ui/dist` and SPA-fallbacks non-`api/` routes to `index.html`.
- Backend routes are **thin**; SQL/domain lives in modules (`search.py`, `screeners.py`, …).

### Request flow (this repo)

```
XBRL/XML → bse_shp_transform → SHPDATA/<code>/shp_history_detailed_v2.json
                                      ↓
                           bse_shp_ui_index → shp_ui.duckdb
                             (securities_dim, shp_features,
                              quarter_closes, shp_money_*, screener_ratios*)
                                      ↓
                           bse_shp_ui_serve (FastAPI)
                                      ↓
                    React SPA fetch(/api/...) + chart JSON from disk
```

Optional parallel DBs (holders / names / groups / discovery) may exist alongside `shp_ui.duckdb`. The UI fails soft with 503 / banners when indexes are missing (`GET /api/health`).

### Serve commands

```powershell
# Terminal 1 — API
bse_shp_ui_serve
# http://127.0.0.1:8765

# Terminal 2 — Vite (dev)
cd ui
npm run dev
# http://127.0.0.1:5173 — proxies /api → :8765

# Or single-process after build
cd ui
npm run build
cd ..
bse_shp_ui_serve
# open http://127.0.0.1:8765 — serves ui/dist when present
```

---

## Frontend patterns

- **SPA routes** in `ui/src/App.tsx`; chrome in `TopBar`; pages under `ui/src/pages/`.
- **Single `api.ts`:** typed `apiFetch` wrapper; all data via JSON `fetch`.
- **Search:** debounced autocomplete → `GET /api/search?q=`.
- **Charts:** `fetch` chart payload → `useMemo` → ECharts `option` → `<ReactECharts notMerge lazyUpdate />`.
- **Screeners:** compose clauses or code-defined presets → `POST` → results table (sort, column picker, soft filters).
- **Cross-page handoffs:** `localStorage` (e.g. Discover → Screener); charts often open in new tabs.
- **Prefs/theme:** `localStorage` + early theme boot in `ui/index.html`.
- **Health/stale:** `GET /api/health` + schema version banner when index is missing/outdated.

### Primary routes

| Route | Purpose |
|-------|---------|
| `/compose`, `/results` | Screener compose → results |
| `/discover` | Index / sector / mcap universe |
| `/chart`, `/chart/:code` | Shareholding time-series chart |
| `/holders`, `/holders/:id` | Holder census |
| `/holder-names`, … | Name census + alias suggestions |
| `/holder-groups`, … | Editable groups + group value charts |

### Chart embedding pattern

1. Persist **time-series chart JSON** per entity on disk (periods + series arrays).
2. Index **screening features** into DuckDB (not full series).
3. `GET .../chart` reads file; optionally enrich from DuckDB (closes → money arrays).
4. React: `fetch` → state → `useMemo` → ECharts `option` → `<ReactECharts notMerge lazyUpdate />`.
5. Keep series selection / focus chips client-side.

---

## Backend / data patterns

- Index **screening features** into DuckDB; keep **full time-series chart JSON on disk** per entity.
- Chart endpoint: read file → optionally enrich from DuckDB (e.g. closes → money metrics).
- Screeners: return base hits → **batch enrich** in Python (ratios, flows, etc.).
- Errors: `404` missing entity, `503` missing index, `400` bad params.
- Schema: integer `INDEX_SCHEMA_VERSION`; bump when UI requires rebuild.
- Self-tests: `python -m pkg.feature_selftest` per domain module.
- Long full-corpus index jobs are operator-run; prefer capped/sample inputs in agent sessions.

### DuckDB files (paths from `p01.bse.bse_config`)

| Path | Used for |
|------|----------|
| `DB/shp_ui.duckdb` | Search, screeners, money, ratios, quarter closes |
| `DB/shp_holders.duckdb` | Holders UI |
| `DB/shp_holder_names.duckdb` | Names UI |
| `DB/shp_holder_groups.duckdb` | Suggestions / blocks |
| `DB/shp_holder_llm.duckdb` | LLM clusters |
| `DB/shp_discovery.duckdb` | Discover catalog |

Also JSON config: `holder_aliases.json`, `holder_groups.json`, `security_tags.json`.

### Query modules (UI does not embed SQL in routes)

| Module | Role |
|--------|------|
| `search.py` | Autocomplete |
| `screeners.py` | Preset SQL filters + result enrichment |
| `screener_query.py` | AND clause engine + templates |
| `money_query.py` | Batch flow / valuation attaches |
| `screener_ratios.py` | Batch ratios |
| `result_enrich.py` | Discover → full result rows |
| `money_flow.py` | Build-time money + chart payload enrichment |
| `project_chart.py` | detailed JSON → chart shape |
| `holders.py` / `holder_names.py` / `holder_groups.py` / `discovery.py` | Feature-area APIs |

### API endpoint style

```python
def create_app() -> FastAPI:
    app = FastAPI(...)
    app.add_middleware(CORSMiddleware, allow_origins=[vite, self])

    @app.get("/api/health")
    def health(): ...  # paths + db_exists flags

    @app.get("/api/search")
    def api_search(q: str = Query(...)): ...

    @app.post("/api/screeners/run")
    def api_screeners_run(body: ScreenerRunBody): ...

    # SPA last
    if UI_DIST.is_dir():
        app.mount("/assets", StaticFiles(...))
        @app.get("/{full_path:path}")
        def spa_fallback(...): return FileResponse(index.html)
```

Client helpers (illustrative):

```ts
runScreener(id, params) → POST /api/screeners/run { id, params, limit, universe? }
fetchChart(code) → GET /api/securities/${code}/chart
```

---

## Layout to mirror

```
pkg/
  config.py           # data roots, DB paths, host/port
  db.py               # DDL, connect(read_only), schema_version
  build_index.py      # offline extract → DuckDB
  app.py              # create_app(); /api/*; mount ui/dist
  search.py / screeners.py / …  # one module per domain
ui/
  vite.config.ts      # proxy /api → API host
  src/api.ts          # apiFetch + typed helpers
  src/App.tsx         # routes
  src/pages/…         # views
  src/components/…    # chart, table, topbar, search
  src/styles.css      # tokens + themes
```

**CLIs:** `*_ui_index`, `*_ui_serve`  
**API style:** `/api/<resource>`, nested like `/api/securities/{code}/chart`  
**Client storage keys:** prefixed (e.g. `shp_ui_*_v1`)

---

## Minimal vertical slice to clone first

1. Config + DuckDB dim table + search  
2. FastAPI `/api/health`, `/api/search`, `/api/{id}/chart`  
3. Vite React: TopBar + search + ChartPage + ECharts  
4. Then: feature index + screener presets → compose query → other domains  

---

## Key paths in this repo

### Backend (`p01/shp_ui/`)

| Path | Role |
|------|------|
| `app.py` | FastAPI app, `/api` routes, static SPA mount, uvicorn CLI |
| `db.py` | DDL, connect, schema version / health |
| `build_index.py` | `bse_shp_ui_index` |
| `build_ratios_index.py` | `bse_shp_ui_ratios_index` |
| `screeners.py` / `screener_query.py` | Presets + compose query |
| `search.py` | Security search |
| `money_flow.py` / `money_query.py` / `price_cache.py` / `price_series.py` | Money & price |
| `holders.py`, `holder_names.py`, `holder_groups.py`, … | Holder ecosystem |
| `discovery.py`, `security_tags.py`, `mcap_bands.py` | Discover / tags |
| `project_chart.py` | Detailed → chart projection |
| `*_selftest.py` | Module self-tests |

### Frontend (`ui/`)

| Path | Role |
|------|------|
| `package.json`, `vite.config.ts` | Deps + `/api` proxy |
| `src/App.tsx`, `main.tsx`, `api.ts`, `types.ts` | Shell, client, types |
| `src/pages/ChartPage.tsx`, `components/ShpChart.tsx` | Main chart UX |
| `src/pages/ScreenerComposePage.tsx`, `ScreenerResultsPage.tsx` | Screener UX |
| `src/pages/DiscoverPage.tsx` | Universe browse |
| `src/pages/Holders*.tsx`, `HolderNames*.tsx`, `HolderGroup*.tsx` | Holder UIs |
| `src/components/TopBar.tsx`, `SecuritySearch.tsx`, `ResultsTable.tsx` | Chrome / search / tables |
| `src/styles.css` | Theming / layout |

### Config / docs / entrypoints

| Path | Role |
|------|------|
| `p01/bse/bse_config.py` | Paths, host/port |
| `pyproject.toml` | `[shp-ui]` extra + console scripts |
| `docs/guides/bse.md` | Full product / API guide |
| `p01/Quick_Ref.txt` | Operator commands |
