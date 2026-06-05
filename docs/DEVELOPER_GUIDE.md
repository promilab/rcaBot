## mpmX RCA Bot - Architecture and System Design

### What this is
An interactive analytics and Root Cause Analysis (RCA) assistant for process mining data. The app combines Snowflake-native analytics (SQL, Snowpark) with Large Language Models (LLMs) via Snowflake Cortex. Users ask questions in natural language; the system routes each question to the right pipeline: Non‑SQL explanation, SQL data query, Live RCA (real‑time), or Deep RCA (precomputed).

### Key capabilities
- Natural‑language Q&A about your semantic model (Non‑SQL)
- Automated Text‑to‑SQL with result tables and smart chart suggestions (SQL)
- Single-factor RCA / Live RCA: on‑the‑spot statistical correlation analysis with LLM explanations
- Multi-factor RCA / Deep RCA: use precomputed association‑rule mining outputs, LLM‑interpreted
- Save and share generated queries and charts

---
## High‑level Architecture

- UI: Streamlit app (`RCA_Bot.py`) with chat interface, side pages for saved queries and public charts
- Router: LLM‑based decision layer (`router/`) that assigns each user turn to a pipeline and manages RCA session state tokens
- Pipelines:
  - Non‑SQL: semantic‑model backed explanations (`utils/llm.py` prompts)
  - SQL: Cortex Analyst API request/response (`utils/analyst.py`, `utils/db.py`)
  - Live‑RCA: statistical correlation table + LLM interpretation (`utils/live_rca.py`)
  - Deep‑RCA: precomputed RCA rules + LLM interpretation (`utils/rca_rules.py`)
- Data & Compute: Snowflake database, views, tables, stored procedure for FP‑Growth
- LLM: Snowflake Cortex Complete for classification, Q&A, chart suggestions, and RCA explanations (`utils/prompts.py`)
- State: Namespaced session containers for router, SQL, Live‑RCA, Deep‑RCA (`utils/namespaced_session.py`)
- Storage: Saved queries table for personal and shared gallery (`utils/storage/saved_answers.py`)
- Configuration: `constants.py` for DB/schema, connection, models, feature flags
- Configuration: `constants.py` for DB/schema, connection, models, feature flags; `rca_association_rule_mining/deep_rca_config.py` for Deep‑RCA targets and thresholds (case/subprocess/activity)

---
## Request Routing and Conversation Model

Each user turn is routed by `router/` to one of four labels:
- "Non‑SQL": descriptive/explanatory answers using the semantic model
- "SQL": send turn to Cortex Analyst API to generate SQL + run it
- "Live‑RCA": perform real‑time statistical correlation analysis + LLM write‑up
- "Deep‑RCA": use precomputed RCA rule tables + LLM interpretation

Routing logic (`router/router.py`):
- Uses Snowflake Cortex classification model (`CLASSIFICATION_MODEL`) via `build_router_prompt(...)`
- If an RCA session is active, a lightweight RCA follow‑up prompt is used first to classify an RCA control token:
  - `CONTINUE_RCA`: continue current RCA topic
  - `NEW_RCA`: start a new RCA target/topic (Live‑RCA path)
  - `EXIT_RCA`: leave RCA context
- Router context is maintained via `utils/namespaced_session.router_session` with fields like `active_rca_session`, `last_label`, `last_bot_text`, and `rca_context` (prompt + tables snapshot)

Follow‑ups: The app supports UI “Follow‑up question” mode and pipeline‑aware follow‑ups (SQL/Live/Deep) with additional context injected into the LLM prompt (`utils.llm.answer_followup_question`).

---
## Pipelines

### 1) Non‑SQL (semantic explanations)
When label is Non‑SQL, the app builds a prompt from the current semantic model (YAML description) and returns a structured, business‑friendly explanation (`utils.llm.answer_non_sql_question`).

Key parts:
- Semantic model selection in the sidebar; the app passes the selected staged YAML file path to Cortex Analyst and also loads the local YAML to extract `description` for LLM context (`adapters.get_current_semantic_model_description`).
- Model used: `NON_SQL_ANSWER_MODEL` (configurable in `constants.py`).

### 2) SQL (Cortex Analyst)
When label is SQL, the app:
1. Creates a minimal message window for Analyst (only the latest user turn) to avoid oversized history
2. Sends request via `utils.analyst.get_send_analyst_request_fnc()`
   - Local dev: builds REST call with Snowflake auth token
   - SiS: uses `snowflake.send_snow_api_request`
3. Renders the reply: SQL block + results DataFrame, with:
   - Inline SQL editor (`components/editable_query.py`) that persists edits in chat history
   - Chart tab with interactive picker (`components/chart_picker.py`)
   - Optional smart data summary (`ENABLE_SMART_DATA_SUMMARY`) invoking `get_results_summary`
4. “Save to favorites” persists prompt, SQL, chart config, and serialized plot to Snowflake (`utils/storage/saved_answers.py`)

Caching: query execution results are cached for 1 hour (`utils.db.cached_get_query_exec_result`).

Smart features (config flags in `constants.py`):
- `ENABLE_SMART_CHART_SUGGESTION`: suggests a default chart based on data (`utils.llm.get_chart_suggestion`)
- `ENABLE_SMART_DATA_SUMMARY`: appends a concise, LLM‑generated results summary

### 3) Live‑RCA (real‑time)
Goal: ad‑hoc statistical correlation analysis for a chosen target attribute, plus an LLM write‑up for non‑technical users.

Flow (`RCA_Bot.py` → `utils.live_rca`):
1. Target resolution:
   - Extract from user text (`adapters._extract_target_from_question`) using synonyms
   - Or show in‑chat picker (`LIVE_RCA_PICKER_TYPE`) with available targets from schema introspection or fallback list (`adapters.get_available_live_targets`)
2. Deploy context table: `utils.live_rca.deploy_live_context_table(target)`
   - Sets DB/Schema, derives temp attribute dim, builds `CONTEXT_INFORMATION_LIVE` with contribution metrics and standard association statistics (support, confidence, lift, leverage, conviction, interest)
3. Analyze results: `utils.live_rca.analyze_live_rca_results`
   - Selects top contributors by |CONTRIBUTION_PCT|
   - Calls `utils.llm.call_live_rca_llm` to produce a business‑level report
4. Persist RCA session context via `router_session.update_rca_context` and pin an “Exit Live‑RCA mode” control into the chat (`LIVE_RCA_EXIT_CTRL_TYPE`)

Notes:
- Tokens `NEW_RCA`/`EXIT_RCA` are honored to reset context or start a fresh Live‑RCA flow
- Errors surface as user‑friendly messages; notifications use `utils.notifications`

### 4) Deep‑RCA (precomputed)
Goal: use offline association‑rule mining outputs with additional LLM interpretation.

Runtime steps (`utils.rca_rules`):
1. Load interpreted RCA tables from the configured schema (`APP_SCHEMA_PATH`):
   - Case: `RCA_TIER_1_CASE_RULES_INTERPRETED`
   - Subprocess: `RCA_TIER_2_SP_RULES_INTERPRETED`
   - Activity: `RCA_TIER_2_AL_RULES_INTERPRETED`
2. Call `call_rca_llm_with_context(question, semantic_model_desc, tables)` to generate a natural‑language answer that references the rules
3. Store small CSV head snapshots in router context to power follow‑ups

If no RCA data is present, the app guides the user to run the offline pipeline or use Live‑RCA.

---
## Offline RCA Pipeline (Precompute)

The `rca_association_rule_mining/` folder implements a three‑stage drill‑down workflow (overview):
- Tier‑0/1 Case level: deploy enriched and one‑hot views; mine FP‑Growth rules; compute relevance, mapping, correlation; extract Tier‑2 keys
- Tier‑2 Subprocess level: deploy enriched/bucketed views; shortlist top features; mine FP‑Growth; post‑process
- Tier‑2 Activity level: deploy enriched/bucketed views; shortlist; mine FP‑Growth; post‑process

### FP-Growth and association rules (primer)

Association rules express patterns of the form “if A happens, B often also happens”. The FP‑Growth algorithm efficiently discovers frequent itemsets and derives rules from them.

For each rule:
- Antecedent and Consequent: the “if … then …” parts of the rule.
- Support: how often the combination occurs in the data.
- Confidence: how reliably B occurs when A occurs (P(B | A)).
- Lift: strength of association relative to independence (Confidence / P(B)).
  - Lift > 1: positive association; ≈ 1: independent; < 1: negative association.

Thresholds control the trade‑off between quantity and strength of rules:
- Lower support/confidence/lift thresholds → more rules.
- Higher support/confidence/lift thresholds → fewer, but stronger rules.

### Feature engineering and one‑hot encoding

- Case level (case one‑hot): Important case attributes are one‑hot encoded. Continuous attributes are discretized into buckets first (e.g., duration, counts, amounts, and other numeric metrics) and then encoded. This yields a compact, case‑level indicator space suitable for association‑rule mining.
- Subprocess & Activity level (feature one‑hot): A pure 1/0 feature encoding is constructed that combines:
  - original categorical feature values,
  - presence indicators (feature exists/occurs),
  - frequency buckets,
  - time and numeric quantile buckets.

This setup provides expressive antecedents without leaking the target: in Tier‑2, all runs/events are included and a case‑derived problem flag is used as the target while SLA flags remain features.

### Files

Stored procedure: `00_fpgrowth.py` is deployed as `RCA_FPGROWTH` inside Snowflake.

Targets and bias control (defaults):
- Case target: `CI_TARGET_LEAD_TIME_MISSED_1`.
- Subprocess target: `TIER1_PROBLEM_FLAG_1_1` (case‑derived). The subprocess enriched view includes all runs and adds a case‑level problem flag; SLA flags remain as features.
- Activity target: `TIER1_PROBLEM_FLAG_1_1` (case‑derived). The activity enriched view includes all events and adds the same problem flag.

Rationale: Mining using only “problem” cases distorts patterns. With the complete dataset and a case-derived problem flag as the target, you obtain robust, discriminative rules; SLA flags (e.g., `SP_TARGET_TIME_MISSED_CALC_*`) appear as antecedent features when relevant.

LLM interpretation scripts convert technical rules into business findings tables used by Deep‑RCA:
- Case (`09_case_llm_interpret_rules.py`) → `RCA_TIER_1_CASE_RULES_INTERPRETED`
- Subprocess (`17_sp_llm_interpret_rules.py`) → `RCA_TIER_2_SP_RULES_INTERPRETED`
- Activity (`25_al_llm_interpret_rules.py`) → `RCA_TIER_2_AL_RULES_INTERPRETED`

For Live‑RCA development and reuse, use `utils/live_rca.py`—call `deploy_live_context_table(...)` (alias: `deploy_dynamic_context_table(...)`) which builds the same statistics as the in‑app deploy function.

---
## State Management

`utils/namespaced_session.py` provides isolated containers:
- `router_session`: RCA session state (active flag, session id), last label/text, RCA context (prompt + tables)
- `sql_session`, `live_rca_session`, `deep_rca_session`: feature‑specific scratchpads
- Helper: `reset_all_namespaces()` to clean state between model switches

Chat history lives in `st.session_state["messages"]` and is rendered via `display_conversation()`; custom content types are used for Live‑RCA picker and exit controls.

---
## Audit Logging (Chat Turn Log)

Every user turn and the corresponding analyst reply are written to a compact audit table in Snowflake.

- Table: `{APP_SCHEMA_PATH}.CHAT_AUDIT_LOG` (auto‑created on first use)
- Columns: `ADDED_ON` (DB timestamp), `USER`, `LABEL` (Non‑SQL/SQL/Live‑RCA/Deep‑RCA/Follow‑up/ERROR), `IS_FOLLOW_UP` (BOOLEAN), `PROMPT` (STRING), `RESPONSE_TEXT` (STRING), `SEMANTIC_MODEL_PATH` (STRING)
- Implementation:
  - Helper: `utils/storage/audit_log.py` with `log_conversation_turn(...)`
  - App hook: `_audit_log_turn(...)` in `RCA_Bot.py` calls the helper at all response points (Non‑SQL, SQL, Live‑RCA, Deep‑RCA, Follow‑ups, global error handler)

---
## Saved Queries and Galleries

- Table: `{APP_SCHEMA_PATH}.SAVED_QUERIES` (auto‑created on first save)
- Operations in `utils/storage/saved_answers.py` (create/read/update/delete)
- Pages:
  - `pages/1_Saved_Queries.py`: personal gallery with edit/delete and re‑render
  - `pages/2_Public_Charts.py`: all shared charts in a two‑column gallery

Serialization: plots are saved as Plotly JSON; chart configs are persisted and re‑hydrated for re‑rendering.

---
## Configuration and Feature Flags (`constants.py`)

- Snowflake location: `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA` → `APP_SCHEMA_PATH`
- Local dev connection: `DEV_SNOWPARK_CONNECTION_NAME`
- LLM models:
  - `CLASSIFICATION_MODEL`, `NON_SQL_ANSWER_MODEL`, `RCA_BOT_LLM_MODEL`, `RCA_INTERPRETATION_MODEL`
- Smart features:
  - `ENABLE_SMART_DATA_SUMMARY`, `ENABLE_SMART_CHART_SUGGESTION`
- Cortex Analyst API path/timeout: `API_ENDPOINT`, `API_TIMEOUT`

Environment detection: `utils.misc.is_local()` uses Streamlit’s `st.user` to switch between local REST and SiS native API.

---
## Security and Data Privacy

- Data stays in Snowflake; LLM calls run inside the account via Cortex
- No external LLM API keys are required
- Authentication is controlled by Snowflake; local dev obtains a Snowflake session token; SiS uses platform authentication

---
## Performance Characteristics

- Offline RCA pipeline: ~30+ minutes one‑time (depending on data size)
- Live‑RCA: deployment + LLM write‑up in tens of seconds
- SQL answers: typically < 10 seconds

---
## Extensibility Guidelines

- Add new Live‑RCA targets: extend `_extract_target_from_question` synonyms or rely on DB introspection; update LLM mappings in `utils.llm.call_live_rca_llm`
- Add new pipelines: create a new label and handler in `RCA_Bot.process_user_input` and the router validation literals in `router/models.py`
- Adjust models: change model names in `constants.py`
- Customize prompts: centralize in `utils/prompts.py`

---
## Directory Map (selected)

- `RCA_Bot.py` — main Streamlit app (chat UI, orchestration)
- `router/` — query router, prompt builder, typed results, token logic
- `utils/` — db session, LLM helpers, Live‑RCA, prompts, session state, plots, notifications
- `components/` — chart picker, editable SQL, query gallery entry
- `pages/` — Saved Queries and Public Charts Streamlit pages
- `rca_association_rule_mining/` — offline RCA pipeline (views, FP‑Growth, post‑processing, LLM interpretation)
- `semantic_model/` — YAML semantic models (staged into Snowflake for Analyst)
