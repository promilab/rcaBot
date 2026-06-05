"""
This app allows users to interact with their process miningdata using natural language. Non-SQL, SQL, Live-RCA and Deep-RCA are supported.
"""


# ------------------------------------------------------------------
# 1) Import necessary libraries and files
# ------------------------------------------------------------------
import json
 
from typing import Dict, List, cast
import logging
 
import sys
sys.path.append("semantic_model")
 

import pandas as pd
import streamlit as st
from plotly.io import to_json

from components.chart_picker import chart_picker
from components.editable_query import editable_query
from constants import (
    ENABLE_SMART_CHART_SUGGESTION,
    ENABLE_SMART_DATA_SUMMARY,
    APP_SCHEMA_PATH,
)
from utils.analyst import get_send_analyst_request_fnc
from utils.db import cached_get_query_exec_result
from utils.llm import (
    get_chart_suggestion,
    get_results_summary,
    answer_non_sql_question,
)
from utils.notifications import add_to_notification_queue, handle_notification_queue
from utils.plots import ChartConfigDict, plotly_fig_from_config
from utils.session_state import (
    get_last_chat_message_idx,
    last_chat_message_contains_sql,
    message_idx_to_question,
    update_analysts_sql_response_message_in_state,
)
from utils.storage.saved_answers import save_analyst_answer

# YAML file defining a semantic model
AVAILABLE_SEMANTIC_MODELS_PATHS = [
    "SEMANTIC_MODELS_STAGE/semantic_model_detailed.yaml",
]

TEXT_TYPE = "text"
SQL_TYPE = "sql"
LIVE_RCA_PICKER_TYPE = "live_rca_picker"
LIVE_RCA_EXIT_CTRL_TYPE = "live_rca_exit_ctrl"

# ------------------------------------------------------------------
# 2) Reset session state variables to initial values for a fresh chat session
# ------------------------------------------------------------------
def reset_session_state() -> None:
    st.session_state["messages"] = []
    st.session_state["active_suggestion"] = None
    st.session_state["suggested_charts_memory"] = {}
    
    # Reset all namespaced session states EXCEPT router context
    # Router context should persist across chat resets to maintain turn counting
    from utils.namespaced_session import sql_session, live_rca_session, deep_rca_session
    sql_session.clear()
    live_rca_session.clear()
    deep_rca_session.clear()

# ------------------------------------------------------------------
# 3) Reset session state variables to initial values for a fresh chat session
# ------------------------------------------------------------------
def reset_session_state_full() -> None:
    st.session_state["messages"] = []
    st.session_state["active_suggestion"] = None
    st.session_state["suggested_charts_memory"] = {}
    
    # Reset all namespaced session states INCLUDING router context
    from utils.namespaced_session import reset_all_namespaces
    reset_all_namespaces()

# ------------------------------------------------------------------
# 4) Sets up the main page header and sidebar with semantic model selector and reset functionality
# ------------------------------------------------------------------
def show_header_and_sidebar():
    st.image("requirements_and_others/mpmx_logo.png", width=150)
    st.title("💬 RCA Bot")
    st.markdown(
        "Welcome to the mpmX RCA Bot! Type your questions below to interact with your process mining data. "
    )
    with st.sidebar:
        if len(AVAILABLE_SEMANTIC_MODELS_PATHS) > 1:
            # Multiple models available - show selectbox
            st.selectbox(
                "Selected semantic model:",
                AVAILABLE_SEMANTIC_MODELS_PATHS,
                format_func=lambda s: s.split("/")[-1],  # show only file name
                key="selected_semantic_model_path",
                on_change=reset_session_state_full,
            )
        else:
            # Only one model available - show info
            model_name = AVAILABLE_SEMANTIC_MODELS_PATHS[0].split("/")[-1]
            st.info(f"**Semantic Model:** {model_name}")
            # Ensure the session state is set correctly
            if "selected_semantic_model_path" not in st.session_state:
                st.session_state["selected_semantic_model_path"] = AVAILABLE_SEMANTIC_MODELS_PATHS[0]
        st.divider()
        if st.button("Clear Chat History", type="primary", use_container_width=True):
            reset_session_state()

# ------------------------------------------------------------------
# 5a) Audit logging helper – safe no-op on failure
# ------------------------------------------------------------------
def _audit_log_turn(prompt: str, analyst_message: Dict, label: str | None, is_follow_up: bool) -> None:
    try:
        from utils.storage.audit_log import log_conversation_turn
        import json as _json

        # Collect plain text parts for quick filtering
        response_text = ""
        content = analyst_message.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    response_text += (item.get("text") or "") + "\n"
        response_text = response_text.strip()

        semantic_model_path = st.session_state.get("selected_semantic_model_path", "")
        user_name = st.user.get("user_name")
        if not isinstance(user_name, str):
            user_name = "UNKNOWN USER"

        log_conversation_turn(
            user=user_name,
            prompt=prompt,
            label=(label or "Unknown"),
            is_follow_up=bool(is_follow_up),
            response_text=response_text,
            semantic_model_path=semantic_model_path,
        )
    except Exception:
        # Never break user flow due to logging issues
        pass

# ------------------------------------------------------------------
# 5b) Processes user input from chat interface or suggestion button clicks
# ------------------------------------------------------------------
def handle_user_inputs():
    # Follow-up toggle (stateless; defaults to OFF each run)
    is_follow_up_toggle = st.toggle(
        "Follow-up question (include previous context)",
        value=False,
        help="Enable only if your question builds directly on the previous answer and you need that context to understand it better.",
    )

    user_input = st.chat_input("What is your question?")
    if user_input:
        process_user_input(user_input, is_follow_up=bool(is_follow_up_toggle))

    # Handle suggested question click (only used by Live-RCA picker)
    elif st.session_state.active_suggestion is not None:
        suggestion = st.session_state.active_suggestion
        st.session_state.active_suggestion = None
        process_user_input(suggestion, is_follow_up=bool(is_follow_up_toggle))
    # Handle Live-RCA target selection stored via session (from button click)
    elif "live_rca_button_clicked" in st.session_state:
        try:
            from utils.namespaced_session import live_rca_session as _live_rca_session
            target = st.session_state.pop("live_rca_button_clicked")
            _live_rca_session.set("selected_target", target)
            print(f"🔍 DEBUG: handle_user_inputs picked up pending target -> {target}; invoking Live-RCA pipeline")
        except Exception as _e:
            print(f"🔍 DEBUG: handle_user_inputs failed to set pending target: {_e}")
        # Trigger pipeline with a generic Live-RCA prompt
        process_user_input("rca live")

# ------------------------------------------------------------------
# 6) Handles a user's text input by adding it to conversation and getting analyst response
# ------------------------------------------------------------------
def process_user_input(prompt: str, is_follow_up: bool = False):
    # Initialize variables that might be used across different branches
    last_sql_stmt = None
    
    try:
        # ============================================================================
        # STEP 1: Route the message using the Query Router (with fast-path for Live-RCA keywords)
        # ============================================================================
        from router import route_message
        
        lp = prompt.strip().lower()
        if ("rca" in lp and "live" in lp) or lp in ("live rca", "rca live"):
            routing_result = {"is_follow_up": False, "label": "Live-RCA"}
        else:
            routing_result = route_message(prompt)
        # Handle immediate RCA exit token at orchestration level
        try:
            if routing_result.get("rca_token") == "EXIT_RCA":
                from utils.namespaced_session import router_session as _router_session
                _router_session.clear_rca_context()
        except Exception:
            pass
        
        # Enhanced logging for debugging
        print(
            f"🎯 ROUTER DECISION: is_follow_up={routing_result.get('is_follow_up')}, "
            f"label={routing_result.get('label')}, rca_token={routing_result.get('rca_token')}"
        )
        logging.info(f"🎯 Router result: {routing_result}")
        print(f"🔎 DEBUG: is_follow_up_ui={is_follow_up}")
        
        # ============================================================================
        # STEP 2: Create user message and display it
        # ============================================================================
        new_user_message = {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
        }
        st.session_state.messages.append(new_user_message)
        with st.chat_message("user"):
            display_message(new_user_message["content"], len(st.session_state.messages) - 1)
        
        # ============================================================================
        # STEP 3: Execute the appropriate pipeline based on label
        # ============================================================================
        # Follow-up UI override: handle in a unified follow-up pipeline using last session label
        if is_follow_up:
            from adapters import get_current_semantic_model_description
            from utils.namespaced_session import router_session as _router_session
            from utils.llm import answer_followup_question
            from utils.db import cached_get_query_exec_result

            with st.chat_message("analyst"):
                with st.spinner("Answering follow-up..."):
                    sm_desc = get_current_semantic_model_description()
                    prev_text = _router_session.get("last_bot_text") or ""
                    last_label = _router_session.get("last_label")

                    # Ensure last_sql_stmt is initialized in this scope
                    last_sql_stmt = None

                    extra_ctx: Dict[str, str] = {}
                    # SQL context: last SQL statement + small result sample
                    if last_label == "SQL":
                        for msg in reversed(st.session_state.messages):
                            if msg.get("role") == "analyst":
                                for item in msg.get("content", []):
                                    if isinstance(item, dict) and item.get("type") == "sql":
                                        last_sql_stmt = item.get("statement")
                                        break
                            if last_sql_stmt:
                                break
                    if last_sql_stmt:
                        # Truncate long SQL to avoid API size issues
                        short_stmt = str(last_sql_stmt)
                        if len(short_stmt) > 4000:
                            short_stmt = short_stmt[:4000] + "\n-- [truncated]"
                        extra_ctx["SQL statement"] = short_stmt
                        df, sql_err = cached_get_query_exec_result(last_sql_stmt)
                        if sql_err is None and df is not None:
                            extra_ctx["SQL result sample"] = df.head(10).to_string(index=False)

                    # Live-RCA context: table name and target
                    if last_label == "Live-RCA":
                        tables_info = _router_session.get_rca_tables_info()
                        if tables_info.get("live_rca_table"):
                            extra_ctx["LIVE_RCA_table"] = str(tables_info.get("live_rca_table"))
                        if tables_info.get("live_rca_target"):
                            extra_ctx["LIVE_RCA_target"] = str(tables_info.get("live_rca_target"))

                    # Deep-RCA context: small CSV samples captured earlier
                    if last_label == "Deep-RCA":
                        tables_info = _router_session.get_rca_tables_info()
                        for k in ("deep_rca_case_rules_sample", "deep_rca_sp_rules_sample", "deep_rca_al_rules_sample"):
                            if k in tables_info and tables_info[k]:
                                extra_ctx[k] = str(tables_info[k])

                    answer_text, error = answer_followup_question(
                        prompt,
                        previous_answer=prev_text,
                        semantic_model_desc=sm_desc,
                        extra_context=extra_ctx,
                    )
                    if error:
                        answer_text = f"❌ Error in Follow-up pipeline: {error}"

                # Display follow-up answer
                analyst_message = {
                    "role": "analyst",
                    "content": [{"type": "text", "text": answer_text}],
                    "request_id": None,
                }
                st.session_state.messages.append(analyst_message)
                display_message(analyst_message["content"], len(st.session_state.messages) - 1)
                _audit_log_turn(prompt, analyst_message, "Follow-up", True)

            # Update router context with follow-up answer
            _router_session.append_last_bot_text(answer_text)
            # Keep last_label unchanged (follow-up to the same pipeline)
            # Re-render so the toggle & input sit below the latest messages
            st.rerun()
            return

        with st.chat_message("analyst"):
            
            if routing_result.get("label") == "Non-SQL":
                # Non-SQL Pipeline: Process mining explanations and definitions
                from utils.llm import answer_non_sql_question
                from adapters import get_current_semantic_model_description
                from utils.namespaced_session import router_session
                
                with st.spinner("Generating answer..."):
                    sm_desc = get_current_semantic_model_description()
                    # If UI marks as follow-up, use the generalized follow-up LLM
                    if is_follow_up:
                        prev_text = router_session.get("last_bot_text") or ""
                        from utils.llm import answer_followup_question
                        extra_ctx = {}  # Non-SQL has no extra tables
                        answer_text, error = answer_followup_question(
                            prompt,
                            previous_answer=prev_text,
                            semantic_model_desc=sm_desc,
                            extra_context=extra_ctx,
                        )
                    else:
                        answer_text, error = answer_non_sql_question(prompt, sm_desc)
                    
                    if error:
                        answer_text = f"❌ Error in Non-SQL pipeline: {error}"
                # Update router context
                router_session.set("last_label", "Non-SQL")
                router_session.append_last_bot_text(answer_text)
            
            elif routing_result.get("label") == "SQL":
                # SQL Pipeline: Cortex Analyst API for data queries
                with st.spinner("Waiting for the SQL-Analyst's response..."):
                    if is_follow_up:
                        # Use follow-up LLM with SQL context
                        from adapters import get_current_semantic_model_description
                        from utils.namespaced_session import router_session as _router_session
                        from utils.db import cached_get_query_exec_result
                        from utils.llm import answer_followup_question

                        sm_desc = get_current_semantic_model_description()

                        # Ensure last_sql_stmt is initialized in this scope
                        last_sql_stmt = None

                        # Find last SQL content in chat history
                        for msg in reversed(st.session_state.messages):
                            if msg.get("role") == "analyst":
                                for item in msg.get("content", []):
                                    if isinstance(item, dict) and item.get("type") == "sql":
                                        last_sql_stmt = item.get("statement")
                                        break
                            if last_sql_stmt:
                                break

                        sql_sample = ""
                        if last_sql_stmt:
                            df, sql_err = cached_get_query_exec_result(last_sql_stmt)
                            if sql_err is None and df is not None:
                                # Create a compact sample
                                head_rows = df.head(10)
                                sql_sample = head_rows.to_string(index=False)

                        prev_text = _router_session.get("last_bot_text") or ""
                        extra_ctx = {}
                        if last_sql_stmt:
                            extra_ctx["SQL statement"] = str(last_sql_stmt)
                        if sql_sample:
                            extra_ctx["SQL result sample"] = sql_sample

                        answer_text, error = answer_followup_question(
                            prompt,
                            previous_answer=prev_text,
                            semantic_model_desc=sm_desc,
                            extra_context=extra_ctx,
                        )
                        if error:
                            answer_text = f"❌ Error in SQL follow-up pipeline: {error}"
                        # Append and display the follow-up text response as analyst message
                        analyst_message = {
                            "role": "analyst",
                            "content": [{"type": "text", "text": answer_text}],
                            "request_id": None,
                        }
                        st.session_state.messages.append(analyst_message)
                        display_message(analyst_message["content"], len(st.session_state.messages) - 1)
                        # Update router context for continuity
                        _router_session.set("last_label", "SQL")
                        _router_session.append_last_bot_text(answer_text)
                        _audit_log_turn(prompt, analyst_message, "SQL", True)
                        return
                    else:
                        # Minimal message window to avoid oversized/invalid history after RCA
                        # Send only the latest user turn to the Analyst API
                        last_user_msg = st.session_state.messages[-1]
                        temp_messages = [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": str(last_user_msg["content"][0]["text"])},
                                ],
                            }
                        ]
                        result = get_send_analyst_request_fnc()(temp_messages)
                        if result is not None:
                            response, error_msg = result
                        else:
                            response, error_msg = None, "Unknown error: No response from analyst request function."
                        if error_msg is None:
                            if response is not None and "message" in response and "content" in response["message"] and "request_id" in response:
                                analyst_message = {
                                    "role": "analyst",
                                    "content": response["message"]["content"],
                                    "request_id": response["request_id"],
                                }
                            else:
                                analyst_message = {
                                    "role": "analyst",
                                    "content": [{"type": "text", "text": "Error: Analyst response could not be processed."}],
                                    "request_id": None,
                                }
                        else:
                            analyst_message = {
                                "role": "analyst",
                                "content": [{"type": "text", "text": error_msg}],
                                "request_id": response["request_id"] if response is not None and "request_id" in response else None,
                            }
                        st.session_state.messages.append(analyst_message)
                        display_message(analyst_message["content"], len(st.session_state.messages) - 1)
                        _audit_log_turn(prompt, analyst_message, "SQL", False)
                        # Update router context
                        if analyst_message["content"]:
                            content_text = ""
                            if isinstance(analyst_message["content"], list):
                                for item in analyst_message["content"]:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        content_text += item.get("text", "")
                            else:
                                content_text = str(analyst_message["content"])
                            if content_text and not content_text.startswith("Error") and len(content_text) > 100:
                                from utils.namespaced_session import router_session as _router_session
                                _router_session.set("last_label", "SQL")
                                _router_session.append_last_bot_text(content_text)
                
                # Additional features if enabled - run OUTSIDE the main spinner but INSIDE chat_message
                if last_chat_message_contains_sql() and ENABLE_SMART_DATA_SUMMARY:
                    get_and_display_smart_data_summary()
                # Ensure controls render below the latest messages
                st.rerun()
            
            elif routing_result.get("label") == "Live-RCA":
                # Live-RCA Pipeline: Real-time statistical analysis
                from utils.live_rca import deploy_live_context_table, analyze_live_rca_results
                from adapters import _extract_target_from_question, get_available_live_targets
                from utils.namespaced_session import router_session, live_rca_session
                
                print("🔍 DEBUG: Entered Live-RCA pipeline")
                # Handle NEW_RCA token by resetting RCA context and showing picker
                if routing_result.get("rca_token") == "NEW_RCA":
                    router_session.clear_rca_context()
                    print("🔍 DEBUG: RCA token NEW_RCA detected -> cleared RCA context")

                # Determine target through extraction or UI picker
                extracted_from_prompt = _extract_target_from_question(prompt)
                from_session = live_rca_session.get("selected_target")
                print(f"🔍 DEBUG: Target resolution -> extracted_from_prompt={extracted_from_prompt}, from_session={from_session}")
                selected_target = extracted_from_prompt or from_session
                # Check if we have a pending button click from session state
                if "live_rca_button_clicked" in st.session_state:
                    clicked_target = st.session_state["live_rca_button_clicked"]
                    del st.session_state["live_rca_button_clicked"]  # Clear the flag
                    selected_target = clicked_target
                    print(f"🔍 DEBUG: Found pending button click in session state -> selected_target={selected_target}")
                
                if not selected_target:
                    # Persist a UI message in chat so widgets survive reruns
                    print("🔍 DEBUG: No target resolved -> appending live_rca_picker message and returning")
                    picker_message = {
                        "role": "analyst",
                        "content": [{"type": LIVE_RCA_PICKER_TYPE}],
                        "request_id": None,
                    }
                    st.session_state.messages.append(picker_message)
                    display_message(picker_message["content"], len(st.session_state.messages) - 1)
                    # One-time toast to indicate Live-RCA mode is active
                    if not st.session_state.get("live_rca_mode_active", False):
                        st.session_state["live_rca_mode_active"] = True
                        try:
                            from utils.notifications import add_to_notification_queue
                            add_to_notification_queue("Live RCA mode activated", "info")
                        except Exception:
                            pass
                    # Rerun to ensure all controls render properly below the latest message
                    st.rerun()
                    return
                else:
                    # Ensure we don't keep stale selection after using it once
                    live_rca_session.delete("selected_target")
                    print(f"🔍 DEBUG: Using pre-resolved target -> {selected_target}; cleared session key")

                with st.spinner("Performing live RCA analysis..."):
                    try:
                        print(f"🔍 DEBUG: Starting Live-RCA for target: {selected_target}")
                        context_table = deploy_live_context_table(selected_target)
                        print(f"🔍 DEBUG: Context table deployed: {context_table}")
                        # Persist table info in router context for follow-ups
                        router_session.set_rca_tables_info({
                            "live_rca_table": context_table,
                            "live_rca_target": selected_target,
                        })
                        answer_text = analyze_live_rca_results(context_table, selected_target)
                        print(f"🔍 DEBUG: Analysis completed, response length: {len(answer_text)}")
                        error = None
                    except Exception as e:
                        error = f"Live-RCA pipeline error: {e}"
                        answer_text = ""
                        print(f"🔍 DEBUG: Live-RCA error: {error}")
                        st.error(error)
                    if error:
                        answer_text = f"❌ Error in Live-RCA pipeline: {error}"
                    
                    # Fallback if no answer text
                    if not answer_text:
                        answer_text = f"✅ Live-RCA analysis started for {selected_target.replace('CI_TARGET_', '').replace('_', ' ').title()}, but no detailed results were generated."

                # Render single control: Exit Live-RCA mode
                with st.container():
                    st.divider()
                    # Control is now appended after displaying the analysis message to ensure it appears below
                    pass

                # Update router context for RCA session
                router_session.set("last_label", "Live-RCA")
                router_session.update_rca_context(answer_text)
            
            elif routing_result.get("label") == "Deep-RCA":
                # Deep-RCA Pipeline: Pre-computed association rules
                from utils.rca_rules import load_rca_tables_for_llm, call_rca_llm_with_context
                from adapters import get_current_semantic_model_description
                from utils.namespaced_session import router_session
                
                with st.spinner("Analyzing RCA data with AI..."):
                    try:
                        # Get semantic model description and load RCA tables
                        sm_desc = get_current_semantic_model_description()
                        rca_tables, error = load_rca_tables_for_llm()
                        
                        if error:
                            answer_text = f"❌ Error loading RCA data: {error}"
                        else:
                            # Check if we have any RCA data
                            total_rules = sum(len(df) for df in rca_tables.values() if not df.empty)
                            if total_rules == 0:
                                answer_text = "No pre-computed RCA analysis data available. Please run the RCA analysis first or try Live-RCA."
                            else:
                                # Call LLM with RCA context
                                answer_text, error = call_rca_llm_with_context(prompt, sm_desc, rca_tables)
                                if error:
                                    answer_text = f"❌ Error analyzing RCA data: {error}"
                        error = None
                    except Exception as e:
                        error = f"Deep-RCA pipeline error: {e}"
                        answer_text = ""
                    
                    if error:
                        answer_text = f"❌ Error in Deep-RCA pipeline: {error}"
                # Update router context for RCA session
                # Persist snapshot info for follow-ups (store compact CSVs if available)
                try:
                    from io import StringIO
                    import pandas as pd
                    snap: Dict[str, str] = {}
                    if rca_tables.get("case_rules") is not None and not rca_tables["case_rules"].empty:
                        snap["deep_rca_case_rules_sample"] = rca_tables["case_rules"].head(10).to_csv(index=False)
                    if rca_tables.get("subprocess_rules") is not None and not rca_tables["subprocess_rules"].empty:
                        snap["deep_rca_sp_rules_sample"] = rca_tables["subprocess_rules"].head(10).to_csv(index=False)
                    if rca_tables.get("activity_rules") is not None and not rca_tables["activity_rules"].empty:
                        snap["deep_rca_al_rules_sample"] = rca_tables["activity_rules"].head(10).to_csv(index=False)
                    router_session.set_rca_tables_info(snap)
                except Exception:
                    pass

                router_session.set("last_label", "Deep-RCA")
                router_session.update_rca_context(answer_text)

                # Optional follow-up LLM if flagged as follow-up
                if is_follow_up:
                    from adapters import get_current_semantic_model_description
                    from utils.llm import answer_followup_question
                    sm_desc = get_current_semantic_model_description()
                    tables_info = router_session.get_rca_tables_info()
                    prev_text = router_session.get("last_bot_text") or answer_text
                    fup_text, fup_err = answer_followup_question(
                        prompt,
                        previous_answer=prev_text,
                        semantic_model_desc=sm_desc,
                        extra_context={k: v for k, v in tables_info.items()},
                    )
                    if not fup_err:
                        answer_text = fup_text
            
            else:
                # Fallback to Non-SQL for unknown labels
                from utils.llm import answer_non_sql_question
                from adapters import get_current_semantic_model_description
                from utils.namespaced_session import router_session
                
                logging.warning(f"Unknown label '{routing_result.label}', falling back to Non-SQL")
                with st.spinner("Generating answer..."):
                    sm_desc = get_current_semantic_model_description()
                    answer_text, error = answer_non_sql_question(prompt, sm_desc)
                    
                    if error:
                        answer_text = f"❌ Error in fallback pipeline: {error}"
                router_session.set("last_label", "Non-SQL")
                router_session.append_last_bot_text(answer_text)
            
            # Display the response (except for SQL which handles its own display)
            if routing_result.get("label") != "SQL":
                analyst_message = {
                    "role": "analyst", 
                    "content": [{"type": "text", "text": answer_text}],
                    "request_id": None,
                }
                st.session_state.messages.append(analyst_message)
                display_message(analyst_message["content"], len(st.session_state.messages) - 1)
                _audit_log_turn(prompt, analyst_message, routing_result.get("label"), is_follow_up)

                # If this was a Live-RCA turn, append the persistent Exit control AFTER the analysis content
                if routing_result.get("label") == "Live-RCA":
                    exit_ctrl_message = {
                        "role": "analyst",
                        "content": [{"type": LIVE_RCA_EXIT_CTRL_TYPE}],
                        "request_id": None,
                    }
                    st.session_state.messages.append(exit_ctrl_message)
                    display_message(exit_ctrl_message["content"], len(st.session_state.messages) - 1)
        
        st.rerun()
    
    except Exception as e:
        # Error handling for the entire process
        logging.error(f"Critical error in process_user_input: {e}")
        
        # Display error message to user
        with st.chat_message("analyst"):
            error_text = f"🚨 Sorry, I encountered an unexpected error: {str(e)}"
            analyst_message = {
                "role": "analyst",
                "content": [{"type": "text", "text": error_text}],
                "request_id": None,
            }
            st.session_state.messages.append(analyst_message)
            display_message(analyst_message["content"], len(st.session_state.messages) - 1)
            try:
                _audit_log_turn(prompt, analyst_message, "ERROR", is_follow_up)
            except Exception:
                pass

    # 7) Sends user messages (deprecated – no longer used)
def get_and_display_analyst_response():
    return None

# ------------------------------------------------------------------
# 8) Generates and displays AI-powered summary of SQL query results
# ------------------------------------------------------------------
def get_and_display_smart_data_summary():
    # Check if summary already exists in session state to avoid duplication
    message_content = st.session_state.messages[-1]["content"]
    has_summary = any(item.get("type") == "text" and item.get("text", "").startswith("__Results summary:__") for item in message_content)
    
    if has_summary:
        # Summary already exists, display it from session state without adding again
        for item in message_content:
            if item.get("type") == "text" and item.get("text", "").startswith("__Results summary:__"):
                st.divider()
                st.markdown(item["text"])
                break
        return
    
    with st.spinner("Generating results summary..."):
        # Get cached SQL execution result
        sql_idx = next(
            (i for i, c in enumerate(st.session_state.messages[-1]["content"]) if c.get("type") == "sql"),
            None
        )
        if sql_idx is None:
            # Error handling
            return

        df, err_msg = cached_get_query_exec_result(
            st.session_state.messages[-1]["content"][sql_idx]["statement"]
        )
        # If query execution results in error, skip it
        if err_msg:
            return

        # Get data summary response
        question = message_idx_to_question(get_last_chat_message_idx())
        if df is None or df.empty:
            results_summary_text = "__Results summary:__\n\nError: No data available for summary."
        else:
            results_summary, _ = get_results_summary(question, df)
            results_summary_text = f"__Results summary:__\n\n{results_summary}"
        
        # Add to session state for persistence
        st.session_state.messages[-1]["content"].append(
            {"type": "text", "text": results_summary_text}
        )
        
        # Trigger a rerun so that the newly added summary is rendered via the regular
        # chat history display logic, avoiding a temporary duplicate during loading
        st.rerun()

# ------------------------------------------------------------------
# 9) Renders different types of message content (text, SQL, suggestions) in the chat
# ------------------------------------------------------------------
def display_message(content: List[Dict[str, str]], message_index: int):
    for item in content:
        if item["type"] == TEXT_TYPE:
            # Add an extra divider before "Suggested followups" section.
            if item["text"] == "__Suggested followups:__" or item["text"].startswith(
                "__Results summary:__"
            ):
                st.divider()
            st.markdown(item["text"])
        elif item["type"] == SQL_TYPE:
            # Display the SQL query and results
            display_sql_query(item["statement"], message_index)
        elif item["type"] == LIVE_RCA_PICKER_TYPE:
            # Render persistent Live-RCA picker inside the chat history
            from adapters import get_available_live_targets
            from utils.namespaced_session import live_rca_session as _lrca, router_session as _router_session

            # If Live-RCA mode is not active anymore, skip rendering the picker UI
            if not (_router_session.get("active_rca_session") or st.session_state.get("live_rca_mode_active", False)):
                return

            targets = get_available_live_targets()
            
            # Use proper target mapping from utils.llm to get the display name
            from utils.llm import get_target_display_name
            label_map = {t: get_target_display_name(t) for t in targets}
            reverse_label_map = {v: k for k, v in label_map.items()}

            st.markdown("Select a Live-RCA target:")
            label_choices = list(label_map.values())
            selected_label = st.radio(
                "Target",
                options=label_choices,
                key=f"live_rca_target_selector_{message_index}",
                label_visibility="collapsed",
            )
            
            # Two buttons side by side
            col1, col2 = st.columns(2)
            with col1:
                start_clicked = st.button(
                    "Start Live-RCA",
                    key=f"live_rca_start_btn_{message_index}",
                    type="primary",
                    use_container_width=True,
                )
            with col2:
                exit_clicked = st.button(
                    "Exit Live-RCA mode",
                    key=f"live_rca_exit_btn_picker_{message_index}",
                    type="secondary",
                    use_container_width=True,
                )
            
            print(f"🔍 DEBUG: [picker:{message_index}] selected_label='{selected_label}', start_clicked={start_clicked}, exit_clicked={exit_clicked}")
            
            if start_clicked:
                selected_target = reverse_label_map.get(selected_label)
                print(f"🔍 DEBUG: [picker:{message_index}] start clicked -> target={selected_target}")
                if selected_target:
                    _lrca.set("selected_target", selected_target)
                    # trigger a follow-up turn in the same run
                    st.session_state.active_suggestion = f"Live RCA for {selected_label}"
                    st.rerun()
            elif exit_clicked:
                print(f"🔍 DEBUG: [picker:{message_index}] exit clicked -> deactivating Live-RCA mode")
                _router_session.clear_rca_context()
                st.session_state["live_rca_mode_active"] = False
                try:
                    from utils.notifications import add_to_notification_queue
                    add_to_notification_queue("Live RCA mode deactivated", "info")
                except Exception:
                    pass
                # Force immediate re-render to hide any lingering pickers
                st.rerun()
            # If not clicked, do nothing; the picker stays visible
        elif item["type"] == LIVE_RCA_EXIT_CTRL_TYPE:
            # Persistent exit control pinned to this chat turn
            if st.button(
                "Exit Live-RCA mode",
                key=f"live_rca_exit_btn_{message_index}",
                type="secondary",
                use_container_width=True,
            ):
                from utils.namespaced_session import router_session as _router_session
                _router_session.clear_rca_context()
                st.session_state["live_rca_mode_active"] = False
                try:
                    from utils.notifications import add_to_notification_queue
                    add_to_notification_queue("Live RCA mode deactivated", "info")
                except Exception:
                    pass
                # Force immediate re-render to hide any lingering pickers
                st.rerun()
        else:
            # Handle other content types if necessary
            pass

# ------------------------------------------------------------------
# 10) Displays the complete conversation history between user and analyst
# ------------------------------------------------------------------
def display_conversation():
    for idx, message in enumerate(st.session_state.messages):
        role = message.get("role", "user")
        content = message.get("content", [])
        with st.chat_message(role):
            display_message(content, idx)

# ------------------------------------------------------------------
# 11) Saves a query with its SQL, data, and chart configuration to user's favorites
# ------------------------------------------------------------------
def save_query(
    prompt: str,
    sql: str,
    df: pd.DataFrame,
    plot_config: ChartConfigDict,
):
    if plot_config.get("type") is not None:
        fig = plotly_fig_from_config(df, plot_config)
        serialized_plot = json.dumps(to_json(fig))
    else:
        serialized_plot = "{}"

    serialized_plot_cfg = json.dumps(plot_config)
    semantic_model_path = st.session_state.selected_semantic_model_path
    
    # Safely get user name ensuring it's a string
    user_name = st.user.get("user_name")
    if not isinstance(user_name, str):
        user_name = "UNKNOWN USER"
    
    success, err_msg = save_analyst_answer(
        user=user_name,
        prompt=prompt,
        sql=sql,
        serialized_plot_cfg=serialized_plot_cfg,
        serialized_plot=serialized_plot,
        semantic_model_path=semantic_model_path,
    )
    if success:
        st.toast("Query saved!", icon="ℹ️")
    else:
        st.toast(f"Could not save the query, error msg: {err_msg}", icon="🚨")

# ------------------------------------------------------------------
# 12) Displays SQL query with editable code, execution results, charts, and save functionality
# ------------------------------------------------------------------
def display_sql_query(sql: str, message_index: int):
    with st.expander("SQL Query", expanded=False):
        query_edit_btn, edited_sql = editable_query(sql, f"chat_{message_index}")
        if query_edit_btn:
            # We need to update source message object in order to persist edits app between rerenders
            update_analysts_sql_response_message_in_state(edited_sql, message_index)
            sql = edited_sql

    # Display the results of the SQL query
    with st.expander("Results", expanded=True):
        # No spinner here - SQL execution is already covered by main spinner
        df, err_msg = cached_get_query_exec_result(sql)
        if df is None:
            st.error(f"Could not execute generated SQL query. Error: {err_msg}")
            return

        if df.empty:
            st.write("Query returned no data")
            return

        # Show query results in two tabs
        data_tab, chart_tab = st.tabs(["Data 📄", "Chart 📉"])
        with data_tab:
            st.dataframe(df)

        with chart_tab:
            plot_cfg = show_chart_tab(df, message_index)

    save_to_favorites = st.button(
        "⭐ Save this query and chart ⭐",
        key=f"sql_msg__save_query_btn__{message_index}",
        type="secondary",
        use_container_width=True,
    )
    if save_to_favorites:
        question = message_idx_to_question(message_index)
        save_query(prompt=question, sql=sql, df=df, plot_config=plot_cfg)

# ------------------------------------------------------------------
# 13) Renders chart creation interface with smart suggestions and live preview
# ------------------------------------------------------------------
@st.fragment
def show_chart_tab(df: pd.DataFrame, message_index: int) -> ChartConfigDict:
    default_plot_cfg = None

    # If smart chart suggestion is enabled, get the default chart-picker configuration
    if ENABLE_SMART_CHART_SUGGESTION:
        question = message_idx_to_question(message_index)
        default_plot_cfg = get_suggested_plot_config(question, df, message_index)

    # Display the chart picker and get the selected chart configuration
    plot_cfg_dict = chart_picker(
        df, default_config=default_plot_cfg, component_idx=message_index
    )
    
    # Safely cast to ChartConfigDict, ensuring it has the required structure
    plot_cfg: ChartConfigDict = {
        "type": plot_cfg_dict.get("type", ""),
        "params": plot_cfg_dict.get("params", {})
    }
    
    # If a chart type is selected, generate and display the chart
    if plot_cfg.get("type") is not None and plot_cfg.get("type"):
        plotly_fig = plotly_fig_from_config(df, plot_cfg)
        st.plotly_chart(plotly_fig, key=f"chart_{message_index}_{plot_cfg.get('type')}_{id(df)}")

    return plot_cfg

# ------------------------------------------------------------------
# 14) Gets AI-suggested chart configuration and caches it in session state
# ------------------------------------------------------------------
def get_suggested_plot_config(
    question: str, df: pd.DataFrame, message_index: int
) -> ChartConfigDict:
    suggested_charts_memory = st.session_state.setdefault("suggested_charts_memory", {})
    suggested_config = suggested_charts_memory.get(message_index)

    # If no configuration is stored, generate a new one and store it in the session state
    if suggested_config is None:
        suggested_config, _ = get_chart_suggestion(question, df)
        if suggested_config is None:
            # Return a default empty configuration if LLM fails
            suggested_config = {"type": "", "params": {}}
        suggested_charts_memory[message_index] = suggested_config

    # Ensure the returned value is properly typed as ChartConfigDict
    result: ChartConfigDict = {
        "type": suggested_config.get("type", ""),
        "params": suggested_config.get("params", {})
    }
    return result


def show_warning_if_error(error: str, message: str) -> None:
    return None

st.set_page_config(layout="centered")
handle_notification_queue()

# Initialize session state
if "messages" not in st.session_state:
    reset_session_state()

# ------------------------------------------------------------------
# 15) Initialize initial selected semantic model path 
# ------------------------------------------------------------------
if "selected_semantic_model_path" not in st.session_state:
    # 0 = only model (semantic_model_detailed.yaml)
    st.session_state["selected_semantic_model_path"] = AVAILABLE_SEMANTIC_MODELS_PATHS[0]

show_header_and_sidebar()
display_conversation()
handle_user_inputs()
if len(st.session_state.messages) == 0:
    # Initialize with welcome message automatically
    from adapters import show_welcome_message
    
    # Show welcome message directly without going through router
    with st.chat_message("analyst"):
        welcome_text = show_welcome_message()
        # Format as markdown message
        st.markdown(welcome_text)
        
        # Add to session state with correct formatting
        analyst_message = {
            "role": "analyst",
            "content": [{"type": "text", "text": welcome_text}],
            "request_id": None,
        }
        st.session_state.messages.append(analyst_message)