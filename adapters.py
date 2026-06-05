"""
Pipeline adapter functions and shared helpers for the RCA Bot.
"""

import logging
from typing import Dict, List, Tuple, Optional
import streamlit as st
import pandas as pd

# Import existing functionality
from utils.llm import answer_non_sql_question
from utils.analyst import get_send_analyst_request_fnc
from utils.rca_rules import load_rca_tables_for_llm, call_rca_llm_with_context
from utils.prompts import PromptTemplates
from utils.namespaced_session import router_session
from router.models import RouterError


# ------------------------------------------------------------------
# 1) Get the current semantic model description
# ------------------------------------------------------------------
def get_current_semantic_model_description() -> str:
    # Import here to avoid circular imports
    from utils.session_state import get_semantic_model_desc_from_messages
    import yaml
    import os
    
    desc_from_chat = get_semantic_model_desc_from_messages()
    if desc_from_chat:
        return desc_from_chat

    # Fallback: load from YAML file based on selected path
    selected_path: str = st.session_state.get("selected_semantic_model_path", "")
    if not selected_path:
        return ""

    # Use only the filename and search exclusively in the 'semantic_model/' subfolder
    local_filename = selected_path.split("/")[-1]
    semantic_model_path = f"semantic_model/{local_filename}"
    if not os.path.isfile(semantic_model_path):
        return ""

    try:
        with open(semantic_model_path, "r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f)
            return str(yaml_data.get("description", ""))
    except Exception:
        return ""

# ------------------------------------------------------------------
# 2) Targets and follow-up helpers
# ------------------------------------------------------------------
SYNONYMS: Dict[str, List[str]] = {
    "CI_TARGET_LEAD_TIME_MISSED": ["lead time", "durchlaufzeit", "latency", "too long duration", "delay"],
    "CI_TARGET_IDLE_TIME_MISSED": ["idle time", "wartezeit", "downtime", "idle"],
}

def _extract_target_from_question(q: str) -> Optional[str]:
    if not q:
        return None
    ql = q.lower()
    for col, keys in SYNONYMS.items():
        if any(k in ql for k in keys):
            return col
    return None

def get_available_live_targets() -> List[str]:
    # Try DB introspection; fall back to static list
    try:
        from utils.db import get_sf_connection
        session = get_sf_connection()
        df = session.sql("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'CASE_INFORMATION'").to_pandas()
        cols = [str(c) for c in df["COLUMN_NAME"].tolist()]
        targets = [c for c in cols if c.startswith("CI_TARGET_")]
        if targets:
            return targets
    except Exception:
        pass
    # Fallback
    static = list(SYNONYMS.keys()) + [
        "CI_TARGET_PROCESSING_TIME_MISSED",
        "CI_PRODUCTION_PROCESS_TARGET_TIME_MISSED",
        "CI_PREPERATION__PICKLING_TARGET_TIME_MISSED",
        "CI_GALVANIZING_TARGET_TIME_MISSED",
        "CI_PAINTING__COATING_TARGET_TIME_MISSED",
        "CI_REWORKED_CASE",
    ]
    # Deduplicate while preserving order
    seen = set()
    ordered: List[str] = []
    for t in static:
        if t not in seen:
            ordered.append(t)
            seen.add(t)
    return ordered

# ------------------------------------------------------------------
# 3) Execute the SQL pipeline using Cortex Analyst API
# ------------------------------------------------------------------
def run_sql(messages: List[Dict]) -> Tuple[Optional[Dict], Optional[str]]:
    try:
        logging.info("🔄 Executing SQL pipeline")
        
        # Call the existing Cortex Analyst function
        result = get_send_analyst_request_fnc()(messages)
        
        if result is None:
            return None, "No response from analyst request function"
        
        response, error_msg = result
        
        if error_msg:
            return response, error_msg
        
        # Validate response structure
        if response and "message" in response and "content" in response["message"]:
            # Store SQL response for potential follow-up questions
            if response["message"]["content"]:
                content_text = ""
                if isinstance(response["message"]["content"], list):
                    for item in response["message"]["content"]:
                        if isinstance(item, dict) and item.get("type") == "text":
                            content_text += item.get("text", "")
                else:
                    content_text = str(response["message"]["content"])
                
                if content_text and not content_text.startswith("Error") and len(content_text) > 100:
                    router_session.append_last_bot_text(content_text)
                    router_session.set("last_label", "SQL")
            
            return response, None
        else:
            return None, "Invalid response format from Cortex Analyst"
        
    except Exception as e:
        error_msg = f"SQL pipeline error: {e}"
        logging.error(error_msg)
        return None, error_msg

# ------------------------------------------------------------------
# 4) Router error mapping
# ------------------------------------------------------------------
def map_router_error_to_text(err: RouterError) -> str:
    if err == RouterError.INVALID_LABEL:
        return "Sorry, I didn’t understand that request. Could you rephrase or be more specific?"
    if err == RouterError.EMPTY_DATA:
        return "🚨 No data available for the requested scope."
    # Fallback
    return "Sorry, something went wrong while routing your request."

# ------------------------------------------------------------------
# 5) Utility: build compact DataFrame sample as text
# ------------------------------------------------------------------
def df_to_compact_text(df: pd.DataFrame, rows: int = 10) -> str:
    try:
        if df is None or df.empty:
            return "<empty>"
        return df.head(rows).to_string(index=False)
    except Exception:
        try:
            return df.head(rows).to_csv(index=False)
        except Exception:
            return "<unserializable>"

# ------------------------------------------------------------------
# 6) Sanitize chat messages before sending to Analyst API
# ------------------------------------------------------------------
def sanitize_messages_for_analyst(messages: List[Dict]) -> List[Dict]:
    allowed_types = {"text", "sql"}
    sanitized: List[Dict] = []
    for msg in messages or []:
        content = msg.get("content", [])
        new_content = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in allowed_types:
                # Shallow copy
                new_item = {k: v for k, v in item.items()}
                # Optional: truncate very large text payloads
                if new_item.get("type") == "text":
                    txt = str(new_item.get("text", ""))
                    if len(txt) > 12000:
                        new_item["text"] = txt[:12000] + "\n...[truncated]"
                new_content.append(new_item)
        if new_content:
            sanitized.append({
                "role": msg.get("role", "user"),
                "content": new_content,
            })
    return sanitized

# ------------------------------------------------------------------
# 7) Show the welcome message for first-time users
# ------------------------------------------------------------------
def show_welcome_message() -> str:
    return """👋 **Welcome to the mpmX RCA Bot!**  
I am your hybrid AI assistant for data analysis and interactive root cause analysis. Here's how I can help:

- 📊 **Free-form Insights:** Ask me questions about your data definitions, processes, or metadata. I will give you clear, natural-language explanations powered by a semantic model of your data base.
- 💻 **Automated SQL Queries:** I can generate and run SQL queries against your data warehouse and provide you with table results and visualization recommendations.
- 🚀 **Live RCA (Ad Hoc):** Select a target metric, such as Lead Time or Idle Time Missed, and I will perform on-the-spot statistical analysis followed by an easy-to-understand root cause interpretation.
- 🔍 **Deep RCA (Precomputed):** Access pre-generated RCA reports that transform technical association-rule patterns into actionable business insights.

Ask me a Question and let's get started!"""