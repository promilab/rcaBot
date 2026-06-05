### mpmX RCA Bot — User Guide

**What it is**: A Streamlit chat app that lets business users explore process mining data and run Root Cause Analysis (RCA) using Snowflake + LLMs.

This guide covers how to run the full system as an end user, including the one‑time precomputation Deep‑RCA (Multi-factor RCA) and how to start the app. If you only want Live‑RCA (Single-factor RCA) and SQL/Non‑SQL answers, you can skip the precompute step (see Quick Mode below).

### Prerequisites
- Complete the setup in `README.md` (Python env, Snowflake connection, database/schema, model access)
- Ensure `constants.py` points to your target `SNOWFLAKE_DATABASE` and `SNOWFLAKE_SCHEMA`
- Configure Deep‑RCA mining targets and thresholds in `rca_association_rule_mining/deep_rca_config.py`

### Option A — Full Capability (run once): Precompute RCA (Deep‑RCA)
Run the following scripts in order. This creates all views/tables, mines association rules, computes relevance/mapping/correlation, and writes LLM‑interpreted rule tables that the bot will use for Deep‑RCA.

PowerShell (Windows):

```powershell
# Tier‑0 / Tier‑1 — Case level
python .\rca_association_rule_mining\1_case_level\01_deploy_rca_case_level.py
python .\rca_association_rule_mining\1_case_level\02_case_analyze_top_features.py
python .\rca_association_rule_mining\1_case_level\03_case_call_fp_growth_procedure.py
python .\rca_association_rule_mining\1_case_level\04_case_clean_rules.py
python .\rca_association_rule_mining\1_case_level\05_case_compute_relevance_metrics.py
python .\rca_association_rule_mining\1_case_level\06_case_compute_rule_mapping.py
python .\rca_association_rule_mining\1_case_level\07_case_compute_rule_correlation.py
python .\rca_association_rule_mining\1_case_level\08_case_extract_cases_for_tier_2.py
python .\rca_association_rule_mining\1_case_level\09_case_llm_interpret_rules.py

# Tier‑2 — Sub‑process level (if subprocesses are defined in the data set)
python .\rca_association_rule_mining\2a_subprocess_level\10_deploy_rca_subprocess_level.py
python .\rca_association_rule_mining\2a_subprocess_level\11_sp_analyze_top_features.py
python .\rca_association_rule_mining\2a_subprocess_level\12_sp_call_fp_growth_procedure.py
python .\rca_association_rule_mining\2a_subprocess_level\13_sp_clean_rules.py
python .\rca_association_rule_mining\2a_subprocess_level\14_sp_compute_relevance_metrics.py
python .\rca_association_rule_mining\2a_subprocess_level\15_sp_compute_rule_mapping.py
python .\rca_association_rule_mining\2a_subprocess_level\16_sp_compute_rule_correlation.py
python .\rca_association_rule_mining\2a_subprocess_level\17_sp_llm_interpret_rules.py

# Tier‑2 — Activity level
python .\rca_association_rule_mining\2b_activity_level\18_deploy_rca_activity_level.py
python .\rca_association_rule_mining\2b_activity_level\19_al_analyze_top_features.py
python .\rca_association_rule_mining\2b_activity_level\20_al_call_fp_growth_procedure.py
python .\rca_association_rule_mining\2b_activity_level\21_al_clean_rules.py
python .\rca_association_rule_mining\2b_activity_level\22_al_compute_relevance_metrics.py
python .\rca_association_rule_mining\2b_activity_level\23_al_compute_rule_mapping.py
python .\rca_association_rule_mining\2b_activity_level\24_al_compute_rule_correlation.py
python .\rca_association_rule_mining\2b_activity_level\25_al_llm_interpret_rules.py
```

Notes:
- These scripts use the connection name from `constants.py` (`DEV_SNOWPARK_CONNECTION_NAME`) or `SNOWFLAKE_CONN_NAME` env var.
- You can re‑run only the interpretation scripts later (`09`, `17`, `25`) to refresh the LLM‑interpreted findings without recomputing FP‑Growth.

Targets (defaults):
- Case: `CI_TARGET_LEAD_TIME_MISSED_1`
- Subprocess: `TIER1_PROBLEM_FLAG_1_1` (case‑derived problem flag; subprocess dataset is not restricted to problem cases)
- Activity: `TIER1_PROBLEM_FLAG_1_1` (case‑derived problem flag; activity dataset is not restricted to problem cases)

Tip: SLA‑bezogene Flags wie `SP_TARGET_TIME_MISSED_CALC_*` bleiben als Features enthalten und können als Ursachenmuster im Antezedens erscheinen.

### Option B — Quick Mode (no precompute)
You can skip the scripts above and still use:
- Non‑SQL explanations (semantic model)
- SQL via Cortex Analyst (Text‑to‑SQL + charts + summaries)
- Live‑RCA (ad‑hoc analysis for selected targets)

Deep‑RCA answers will only be available after Option A has been run at least once.

### Start the App
From the repo root, start Streamlit:

```powershell
python -m streamlit run --server.runOnSave true RCA_Bot.py
```

### Using the Bot
- Ask free‑form questions (Non‑SQL) or analytical questions (SQL)
- For Live‑RCA: type "rca live" or select a target in the chat picker (e.g., Lead Time Missed, Idle Time Missed)
- For Deep‑RCA: ask RCA questions once precomputed tables exist (case/subprocess/activity)
- Save queries from SQL answers and browse them under "Saved Queries"; browse shared ones under "Public Charts"

See `docs/DEVELOPER_GUIDE.md` for full details.


