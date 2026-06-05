"""
This module contains constants for the application, such as API endpoints, Snowflake connections, and LLM models.
"""

import os


# ------------------------------------------------------------------
# 1) Snowflake-related constants for Snowflake connection
# ------------------------------------------------------------------
SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "LUKAS_MINED_TABLES_ENRICHED")
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "PM_RCA_LUKAS")
APP_SCHEMA_PATH = f"{SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}"
SAVED_QUERIES_TABLE_NAME = "SAVED_QUERIES"
AUDIT_LOG_TABLE_NAME = "CHAT_AUDIT_LOG"

# ------------------------------------------------------------------
# 2) Local-dev constants for Snowflake connection (TOML file)
# ------------------------------------------------------------------
DEV_SNOWPARK_CONNECTION_NAME = os.getenv("SNOWPARK_CONNECTION_NAME", "lukas_rca_bot_connection")

# ------------------------------------------------------------------
# 3) Chose model for LLM-related tasks
# ------------------------------------------------------------------
NON_SQL_ANSWER_MODEL     = "mistral-large2"     # Model used for answering non-SQL questions in natural language
RCA_BOT_LLM_MODEL        = "LLAMA3.1-70B"       # Model used for answering RCA questions in natural language
CLASSIFICATION_MODEL     = "mistral-large2"     # Model used for classifying questions into SQL or non-SQL

RCA_INTERPRETATION_MODEL = "LLAMA3.1-70B"       # Model used for interpreting RCA rules in offline pipeline

# ------------------------------------------------------------------
# 4) Enable/disable additional LLM-powered features:
# ------------------------------------------------------------------
ENABLE_SMART_DATA_SUMMARY = True
SMART_DATA_SUMMARY_MODEL = "mistral-large2"
ENABLE_SMART_CHART_SUGGESTION = True
SMART_CHART_SUGGESTION_MODEL = "mistral-large2"

# ------------------------------------------------------------------
# 5) API-related constants for Cortex Analyst API
# ------------------------------------------------------------------
API_ENDPOINT = "/api/v2/cortex/analyst/message"
API_TIMEOUT = 30_000    # in milliseconds = 30 seconds
