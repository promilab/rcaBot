# mpmX RCA Bot - Setup Guide

This repository contains the mpmX RCA Bot, a Streamlit application for interactive Root Cause Analysis (RCA) using natural language queries.

## Quick Start

### Prerequisites
- Python 3.11
- Snowflake account with Cortex LLM access
- Semantic model YAML file

### Local Development Setup

#### 1. Install Python and Dependencies
```bash
pyenv install 3.11
python -m virtualenv -p ~/.pyenv/versions/3.11/bin/python venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 2. Configure Snowflake Connection
Create `~/.snowflake/connections.toml`:
```toml
[mpmX-rca-bot]
host = "<host>"
account = "<account>"
user = "<user>"
password = "<password>"
warehouse = "<warehouse>"
role = "<role>"
```

#### 3. Set Up Database Schema
```sql
USE DATABASE cortex_analyst_demo;
CREATE SCHEMA mpmX_rca_bot_schema;
```

#### 4. Configure App Settings
Update `constants.py` with your settings:
- `SNOWFLAKE_DATABASE`
- `SNOWFLAKE_SCHEMA`
- `DEV_SNOWPARK_CONNECTION_NAME`

#### 5. Run the App
```bash
python -m streamlit run --server.runOnSave true RCA_Bot.py
```

#### Full Deep‑RCA (one‑time)
For full Deep‑RCA functionality (precomputed association rules + LLM interpretations) run the offline RCA scripts first — see the `USER_GUIDE.md` for copy‑paste commands. If you prefer, you can skip precompute and use Quick Mode (Live‑RCA, SQL, Non‑SQL) immediately.

### Deep‑RCA targets (defaults)
- Case: `CI_TARGET_LEAD_TIME_MISSED_1`
- Subprocess: `TIER1_PROBLEM_FLAG_1_1` (derived from case target; dataset includes all subprocess runs)
- Activity: `TIER1_PROBLEM_FLAG_1_1` (derived from case target; dataset includes all activity events)

Notes:
- Subprocess and Activity pipelines enrich each row with a case‑level problem flag and mine rules against that target while keeping SLA flags (e.g., `SP_TARGET_TIME_MISSED_CALC_*`) as features. This avoids biased mining on a dataset containing only problem cases.
- You can change thresholds and targets in `rca_association_rule_mining/deep_rca_config.py`.

## SiS (Streamlit in Snowflake) Deployment

### 1. Prepare Schema and Stage
```sql
USE DATABASE mined_data_mpmX;
CREATE SCHEMA mpmX_rca_bot_schema;
CREATE STAGE mined_data_mpmX.mpmX_rca_bot_schema.app_code;
```

### 2. Upload Files to Stage
Upload all repository files to `@mined_data_mpmX.mpmX_rca_bot_schema.app_code` stage.

**Using SnowSQL:**
```bash
snow sql -c mpmX-rca-bot -f sis_setup/upload_files_to_stage.sql
```

## Documentation

- **[docs/DEVELOPER_GUIDE.md](./docs/DEVELOPER_GUIDE.md)** - Architecture and system design
- **[docs/USER_GUIDE.md](./docs/USER_GUIDE.md)** - End‑user guide (run commands & quick mode)
- **[docs/THESIS.md](./docs/THESIS.md)** - Academic context and research contribution

## Troubleshooting

### Common Issues
- **Connection errors:** Check your `connections.toml` configuration
- **Import errors:** Ensure all dependencies are installed

For technical issues, check the developer guide or user guide for detailed information about the system's functionality.

### Environment Variables
- `SNOWPARK_CONNECTION_NAME`: Override default connection name
- `SNOWFLAKE_DATABASE`: Set target database
- `SNOWFLAKE_SCHEMA`: Set target schema