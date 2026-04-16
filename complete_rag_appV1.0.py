"""
Complete RAG-Based ECU Testing System
======================================
Streamlit app that:
1. At startup: discovers all Data_v1, Data_v2, Data_v3, ... folders and loads them (CAPL_script + Python_Script) into the RAG vector store in one go; no storage of generated results.
2. Takes DBC file and requirement as input.
3. Generates test cases → analysis → CAPL and Python scripts in parallel, using RAG retrieval from the loaded Data_v* folders to improve generation.
"""

import streamlit as st
from streamlit.delta_generator import DeltaGenerator
import cantools
import tempfile
import os
import re
import json
import hashlib
import time
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from pathlib import Path
import io

try:
    import pypdf
except ImportError:
    pypdf = None  # PDF upload disabled; install with: pip install pypdf

try:
    import pandas as pd
except ImportError:
    pd = None  # Excel upload disabled; install with: pip install pandas openpyxl (and xlrd for .xls)

try:
    # Load .env from the same directory as this script (optional; no API keys needed for Ollama)
    from pathlib import Path as _Path
    from dotenv import load_dotenv
    _env_path = _Path(__file__).resolve().parent / ".env"
    load_dotenv(_env_path, override=True)
except ImportError:
    pass
except Exception:
    pass

# LLM imports
from langchain_ollama import OllamaLLM
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

# RAG imports
import sys
from pathlib import Path


# Add parent directory to path to import rag_vector_store
parent_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(parent_dir))

from rag_vector_store import (
    RAGVectorStore,
    RetrievalResult,
    create_rag_context_prompt,
    build_enhanced_analysis_prompt,
    _format_result_block,
)

# Import extended RAG store (Qdrant-backed)
code_dir = Path(__file__).parent
sys.path.insert(0, str(code_dir))
from rag_vector_store_qdrant import ExtendedRAGVectorStore

# =============================================================================
# LIGHTWEIGHT LOGGING + EVALUATION DATASET HELPERS
# =============================================================================

EVAL_DATASET_PATH = Path(__file__).parent / "evaluation_dataset.json"


def _log_debug(message: str) -> None:
    """Lightweight logger: keeps messages in session_state and prints to console."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    # Console (useful when running via terminal)
    try:
        print(line)
    except Exception:
        pass
    # Streamlit session log buffer
    try:
        logs = st.session_state.get("debug_logs") or []
        logs.append(line)
        st.session_state["debug_logs"] = logs
    except Exception:
        # If session_state not available (e.g. during import), ignore
        pass


def _extract_first_json_object(text: str) -> Optional[str]:
    """
    Extract the first complete JSON object from text. Handles LLM output that contains
    multiple JSON objects or explanatory text (which causes 'Extra data' on json.loads).
    Uses brace-counting to find the matching closing brace.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    quote_char = None
    i = start
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
        elif c == "\\" and in_string:
            escape = True
        elif in_string:
            if c == quote_char:
                in_string = False
        elif c in ('"', "'"):
            in_string = True
            quote_char = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def _init_or_load_eval_dataset() -> Dict:
    """Load existing evaluation dataset JSON or create a new skeleton."""
    if EVAL_DATASET_PATH.is_file():
        try:
            with EVAL_DATASET_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
                # Basic validation
                if "dataset_info" in data and "test_cases" in data:
                    return data
        except Exception as e:
            _log_debug(f"Failed to load existing evaluation dataset: {e}")
    # New skeleton
    return {
        "dataset_info": {
            "name": "ECU Test Automation Evaluation Dataset",
            "version": "1.0",
            "source_document": "DBC and Requirement Documents",
            "created_date": time.strftime("%Y-%m-%d"),
            "total_test_cases": 0,
            "application_type": "rag",
            "categories": {
                "single_ecu_transmit": "auto-generated",
                "gateway_transform": "auto-generated",
                "reactive_behavior": "auto-generated",
                "signal_validation": "reserved",
                "timing_compliance": "reserved",
            },
        },
        "test_cases": [],
    }


def _append_eval_entry(entry: Dict) -> None:
    """Append one evaluation entry to the dataset file."""
    data = _init_or_load_eval_dataset()
    data["test_cases"].append(entry)
    data["dataset_info"]["total_test_cases"] = len(data["test_cases"])
    try:
        with EVAL_DATASET_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _log_debug(f"Appended evaluation entry {entry.get('test_case_id')} to {EVAL_DATASET_PATH.name}")
    except Exception as e:
        _log_debug(f"Failed to write evaluation dataset: {e}")


def _next_eval_test_case_id() -> str:
    """Return the next sequential test_case_id like TC001, TC002, ... based on current dataset length."""
    data = _init_or_load_eval_dataset()
    idx = len(data.get("test_cases", [])) + 1
    return f"TC{idx:03d}"


def _render_test_cases_section(container: Optional[DeltaGenerator], test_cases: Optional[Dict]) -> None:
    """Render the test cases section in the provided container."""
    if container is None:
        return
    container.empty()
    if not test_cases or not test_cases.get("test_cases"):
        return
    tc_list = test_cases.get("test_cases", []) or []
    container.markdown(f"**{len(tc_list)} test case(s)** for this requirement.")
    for i, tc in enumerate(tc_list, start=1):
        container.markdown(f"**{tc.get('test_id', f'TC_{i:03d}')}:** {tc.get('name', '—')}")
        desc = str(tc.get("description", ""))
        container.caption(
            f"Type: {tc.get('type', '—')} | Description: {desc[:200]}" + ("..." if len(desc) > 200 else "")
        )
        container.json(tc)
        container.markdown("---")


def _render_capl_section(
    container: Optional[DeltaGenerator], capl_script: Optional[str], key_suffix: Optional[str] = None
) -> None:
    """Render the CAPL section in the provided container. key_suffix makes keys unique when rendering multiple tabs."""
    if container is None:
        return
    container.empty()
    if not capl_script:
        return
    container.caption("Single CAPL simulation script for this requirement (used by all test cases).")
    container.code(capl_script, language="c")
    btn_key = f"dl_capl_{key_suffix}" if key_suffix is not None else "dl_capl"
    container.download_button(
        "Download CAPL",
        capl_script,
        "simulation.can",
        "text/plain",
        key=btn_key,
    )


# PYTHON SCRIPT RENDERING DISABLED
# def _render_python_section(
#     container: Optional[DeltaGenerator], python_script: Optional[str], key_suffix: Optional[str] = None
# ) -> None:
#     """Render the Python section in the provided container. key_suffix makes keys unique when rendering multiple tabs."""
#     if container is None:
#         return
#     container.empty()
#     if not python_script:
#         return
#     container.caption("Python test script that exercises all test cases above using the CAPL simulation.")
#     container.code(python_script, language="python")
#     btn_key = f"dl_python_{key_suffix}" if key_suffix is not None else "dl_python"
#     container.download_button(
#         "Download Python",
#         python_script,
#         "test_suite.py",
#         "text/plain",
#         key=btn_key,
#     )

# =============================================================================
# CONFIGURATION
# =============================================================================

# Ollama GPU and Local configuration with fallback support
OLLAMA_BASE_URL_GPU = "http://172.16.117.136:11435"
OLLAMA_BASE_URL_LOCAL = "http://localhost:11434"
OLLAMA_MODEL_GPU = "llama3.3:70b"
OLLAMA_MODEL_GPU_SMALL = "llama3.1:8b"
OLLAMA_MODEL_LOCAL = "llama3.1:8b"
OLLAMA_MODEL_LOCAL_SMALL = "llama3.2:latest"
USE_LOCALHOST_FALLBACK = True
FORCE_GPU_FIRST = True
OLLAMA_GPU_TEST_TIMEOUT = 30.0

# Data_v* folders: pre-loaded CAPL and Python examples for RAG (Data_v1, Data_v2, Data_v3, ... loaded at startup in one go).
DATA_V1_PATH = "Data_v1"

# PYTHON SCRIPT PROCESSING DISABLED
# Pytest structure reference (python-can based, per Pytest Explanation.txt)
# PYTEST_EXPLANATION_PATH = Path(__file__).parent / "Pytest Explanation.txt"
# CAPL Simulation predefined rules (authoritative rules for CAPL generation)
CAPL_PREDEFINED_RULES_PATH = Path(__file__).parent / "CAPL Simulation predefined rules.txt"
CAPL_CORRECTED_PROMPT_PATH = Path(__file__).parent / "Corrected CAPL Prompt.txt"
# CORRECTED_PYTHON_PROMPT_PATH = Path(__file__).parent / "Corrected Python Pytest script Prompt.txt"


def _discover_data_v_folders(parent_dir: Path) -> List[str]:
    """Discover all Data_vN folders (Data_v1, Data_v2, ...) under parent_dir, sorted by N."""
    if not parent_dir.is_dir():
        return []
    found = []
    for p in parent_dir.iterdir():
        if p.is_dir() and re.match(r"^Data_v(\d+)$", p.name, re.IGNORECASE):
            found.append(p.name)
    def sort_key(name: str):
        m = re.match(r"^Data_v(\d+)$", name, re.IGNORECASE)
        return int(m.group(1)) if m else 0
    found.sort(key=sort_key)
    return found


# ~200 tokens each at 800 chars (1 token ≈ 4 chars) - reduced for faster inference
_TRUNCATE_DBC_SUMMARY_CHARS = 800
_TRUNCATE_RAG_CONTEXT_CHARS = 800
_TRUNCATE_PYTEST_REF_CHARS = 800
_TRUNCATE_PYTHON_SETUP_CHARS = 800
_TRUNCATE_CAPL_EXAMPLES_CHARS = 800
_TRUNCATE_CAPL_RULES_CHARS = 4500  # Corrected prompt is longer; avoid truncation
_TRUNCATE_CAPL_SCRIPT_CHARS = 800
# Unbounded inputs - now capped for prompt size
_TRUNCATE_REQUIREMENT_CHARS = 800
_TRUNCATE_TEST_CASES_JSON_CHARS = 5000  # Increased to ensure all test cases (max 5) are included


def _truncate_to_chars(text: str, max_chars: int, suffix: str = "\n... (truncated)") -> str:
    """Truncate text to max_chars, appending suffix if truncated. Preserves structure when possible."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[: max_chars - len(suffix)].rstrip() + suffix


# Common LLM preamble/suffix patterns to strip from streaming preview (so only code is shown)
_STREAMING_PREAMBLE_PATTERNS = [
    r"^Here\s+is\s+the\s+(?:CAPL|Python)\s+script\s+that\s+meets\s+all\s+the\s+requirements:[\s\n]*",
    r"^Here\s+is\s+the\s+(?:CAPL|Python)\s+(?:script|code):[\s\n]*",
    r"^Here\s+is\s+the\s+generated\s+(?:CAPL|Python)[\s\S]*?:\s*\n+",
    r"^```(?:capl|c|python)\s*\n?",
    r"^```\s*\n?",
]


def _strip_streaming_preview(text: str, content_type: str) -> str:
    """Strip preamble and suffix from streaming preview so only code is shown during stream."""
    if not text or not text.strip():
        return text
    out = text.strip()
    # Strip preambles
    for pat in _STREAMING_PREAMBLE_PATTERNS:
        out = re.sub(pat, "", out, flags=re.IGNORECASE)
        out = out.lstrip()
    lines = out.split("\n")
    # Find start: /* or // or variables for CAPL; import for Python
    start_idx = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if content_type == "capl":
            if s.startswith("/*") or s.startswith("//") or "variables" in line.lower():
                start_idx = i
                break
        else:  # python
            if s.startswith("import ") or s.startswith("from "):
                start_idx = i
                break
    if start_idx > 0:
        out = "\n".join(lines[start_idx:])
        lines = out.split("\n")
    # Truncate suffix: CAPL at last }; Python at last def test_ or before Notes
    if content_type == "capl":
        last_brace = -1
        for i in range(len(lines) - 1, -1, -1):
            if "}" in lines[i]:
                last_brace = i
                break
        if last_brace >= 0:
            out = "\n".join(lines[: last_brace + 1])
    else:  # python: truncate before Notes: or # Notes:
        truncate_at = len(lines)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.match(r"^#\s*Notes?:", stripped, re.I) or re.match(r"^Notes?:", stripped, re.I):
                truncate_at = i
                break
        if truncate_at < len(lines):
            out = "\n".join(lines[:truncate_at]).rstrip()
    return out.strip() if out.strip() else text.strip()


# PYTHON SCRIPT PROCESSING DISABLED
# def _remove_notes_sections_from_python(code: str) -> str:
#     """Remove 'notes' sections that LLM sometimes adds to Python output. Should not appear in UI."""
#     if not code or not code.strip():
#         return code
#     # Remove triple-quoted blocks that are "Notes:" sections (docstring-style)
#     code = re.sub(
#         r'\n\s*"""\s*\n\s*Notes?:[\s\S]*?"""\s*\n?',
#         '\n',
#         code,
#         flags=re.IGNORECASE,
#     )
#     code = re.sub(
#         r"\n\s*'''\s*\n\s*Notes?:[\s\S]*?'''\s*\n?",
#         '\n',
#         code,
#         flags=re.IGNORECASE,
#     )
#     # Remove # Notes: / # Note: comment blocks (consecutive #-prefixed lines after Notes:)
#     code = re.sub(
#         r'\n\s*#\s*Notes?:[^\n]*(?:\n\s*#[^\n]*)*',
#         '',
#         code,
#         flags=re.IGNORECASE,
#     )
#     # Remove plain "Notes:" or "Note:" section at end (lines starting with - or * bullets)
#     code = re.sub(
#         r'\n\s*\n\s*Notes?:\s*\n(?:\s*[-*]\s+[^\n]*\n?)+',
#         '',
#         code,
#         flags=re.IGNORECASE,
#     )
#     return code.strip()


# PYTHON SCRIPT PROCESSING DISABLED
# def _strip_python_explanatory_preamble_suffix(code: str) -> str:
#     """Remove explanatory prose at start/end (e.g. 'This script meets...' and bullet lists). Keep only Python code."""
#     if not code or not code.strip():
#         return code
#     lines = code.split("\n")
#     # Drop leading lines until we see a line that looks like Python code start
#     start = 0
#     for i, line in enumerate(lines):
#         s = line.strip()
#         if not s:
#             continue
#         if s.startswith(("import ", "from ", "def ", "@", "class ", "#")) or re.match(r"^[A-Z_]+\s*=", s):
#             start = i
#             break
#     # Drop trailing lines that are bullets or "This script..." prose (from the end backwards)
#     end = len(lines)
#     for i in range(len(lines) - 1, -1, -1):
#         raw = lines[i]
#         s = raw.strip()
#         if not s:
#             continue
#         if re.match(r"^\s*[-*+]\s+", raw):
#             continue
#         if re.search(r"(?:this script|the script|meets all the requirements|specified in the prompt)", s, re.IGNORECASE):
#             continue
#         end = i + 1
#         break
#     result = "\n".join(lines[start:end]).strip()
#     return result if result else code.strip()


def _infer_requirement_metadata(requirement: str) -> Dict[str, str]:
    """
    Infer transmission_mode and simulation_type from requirement text using keywords.
    Used for filtering during retrieval (gateway vs TCM, cyclic vs immediate).
    """
    if not (requirement and requirement.strip()):
        return {}
    text = requirement.lower()
    out: Dict[str, str] = {}
    # Transmission mode: IMMEDIATE vs CYCLIC
    if any(k in text for k in ("immediately", "upon", "as soon as", "when received", "when ... received")):
        out["transmission_mode"] = "IMMEDIATE"
    elif any(k in text for k in ("periodically", "every", "cyclic", "at intervals")):
        out["transmission_mode"] = "CYCLIC"
    # Simulation type: GATEWAY vs REACTIVE vs SINGLE_ECU_TRANSMIT
    if ("gateway" in text or "forward" in text) and ("forward" in text or "gateway" in text):
        out["simulation_type"] = "GATEWAY"
    elif ("when received" in text or "upon receipt" in text) and "forward" not in text:
        out["simulation_type"] = "REACTIVE"
    elif ("shall transmit" in text or "shall send" in text or "ecm shall" in text or "tcm shall" in text) and "forward" not in text:
        out["simulation_type"] = "SINGLE_ECU_TRANSMIT"
    return out


def _parse_capl_blocks(capl_script: str) -> List[Tuple[str, str]]:
    """
    Split CAPL script into semantic blocks and tag each with capl_pattern for retrieval.
    Returns list of (block_text, capl_pattern).
    Patterns: variables, on_start, cyclic_timer, reactive_message, reactive_key, other.
    """
    if not (capl_script or capl_script.strip()):
        return []
    blocks: List[Tuple[str, str]] = []
    # Block start patterns: variables {, on start {, on timer X {, on message X {, on key X {
    pattern = re.compile(
        r"(variables\s*\{|on\s+start\s*\{|on\s+timer\s+\w+\s*\{|on\s+message\s+[^{]+\{|on\s+key\s+[^{]+\{)",
        re.IGNORECASE | re.DOTALL,
    )
    text = capl_script.strip()
    for m in pattern.finditer(text):
        start = m.start()
        block_start = m.group(1).strip().lower()
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        depth = 1
        i = brace + 1
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        block_text = text[start:i].strip()
        if not block_text:
            continue
        if block_start.startswith("variables"):
            pattern_name = "variables"
        elif block_start.startswith("on start"):
            pattern_name = "on_start"
        elif block_start.startswith("on timer"):
            pattern_name = "cyclic_timer"
        elif block_start.startswith("on message"):
            pattern_name = "reactive_message"
        elif block_start.startswith("on key"):
            pattern_name = "reactive_key"
        else:
            pattern_name = "other"
        blocks.append((block_text, pattern_name))
    return blocks


def _process_capl_json(rag_store: ExtendedRAGVectorStore, data: dict) -> int:
    """Process one CAPL-style JSON (DBC + requirements). Chunk DBC by message, CAPL by semantic block. Returns chunk count."""
    count = 0
    dbc_data = data.get("DBC_outout_after_parsing") or data.get("DBC_output_after_parsing")
    dbc_id = ""
    if dbc_data:
        dbc_id = "dbc_" + hashlib.md5(json.dumps(dbc_data, sort_keys=True).encode()).hexdigest()[:8]
        # Chunk DBC by message: one chunk per message with its signals and sender
        messages = dbc_data.get("messages") or []
        for msg in messages:
            msg_name = msg.get("name", "")
            senders = msg.get("senders") or []
            signals = msg.get("signals") or []
            msg_summary = {
                "name": msg_name,
                "frame_id": msg.get("frame_id"),
                "length": msg.get("length"),
                "senders": senders,
                "signals": [{"name": s.get("name"), "start": s.get("start"), "length": s.get("length"),
                            "scale": s.get("scale"), "offset": s.get("offset"), "unit": s.get("unit")} for s in signals],
            }
            msg_text = json.dumps(msg_summary, indent=2)
            rag_store.add_dbc_context(msg_text, {"dbc_id": dbc_id, "type": "dbc_message", "message_name": msg_name})
            count += 1
        # Also add one full DBC summary chunk for broad context (truncated if huge)
        full_summary = json.dumps(dbc_data, indent=2)
        if len(full_summary) > 12000:
            full_summary = full_summary[:12000] + "\n..."
        rag_store.add_dbc_context(full_summary, {"dbc_id": dbc_id, "type": "dbc_context"})
        count += 1
    for req in data.get("requirements", []):
        rid = req.get("requirement_id", "")
        rtext = req.get("requirement_text", "")
        capl = req.get("capl_script", "")
        meta_req = {"requirement_id": rid, "type": "requirement"}
        if dbc_id:
            meta_req["dbc_id"] = dbc_id
        meta_req.update(_infer_requirement_metadata(rtext or ""))
        if rtext:
            rag_store.add_requirement(rtext, meta_req)
            count += 1
        if capl:
            # Chunk CAPL by semantic block with capl_pattern for pattern-based retrieval
            # Prepend requirement text to each block for better semantic matching
            blocks = _parse_capl_blocks(capl)
            capl_prefix = f"Requirement: {rtext}\n\nCAPL:\n" if rtext else ""
            if blocks:
                for block_text, capl_pattern in blocks:
                    meta_capl = {"requirement_id": rid, "type": "capl_implementation", "capl_pattern": capl_pattern}
                    if dbc_id:
                        meta_capl["dbc_id"] = dbc_id
                    enriched_content = capl_prefix + block_text
                    rag_store.add_capl_script(enriched_content, meta_capl)
                    count += 1
            else:
                # Fallback: add whole script with pattern "full_script"
                meta_capl = {"requirement_id": rid, "type": "capl_implementation", "capl_pattern": "full_script"}
                if dbc_id:
                    meta_capl["dbc_id"] = dbc_id
                enriched_content = capl_prefix + capl
                rag_store.add_capl_script(enriched_content, meta_capl)
                count += 1
    return count


# PYTHON SCRIPT PROCESSING DISABLED
# def _process_python_json(rag_store: ExtendedRAGVectorStore, data: dict) -> int:
#     """Process one Python_Script-style JSON (requirement + test_cases). Returns chunk count."""
#     count = 0
#     req_block = data.get("requirement", {})
#     rid = req_block.get("requirement_id", "")
#     desc = req_block.get("description", "")
#     setup = req_block.get("python_test_setup", "")
#     test_cases = data.get("test_cases", [])
#     if desc:
#         meta_req = {"requirement_id": rid, "type": "requirement", **_infer_requirement_metadata(desc)}
#         rag_store.add_requirement(desc, meta_req)
#         count += 1
#     for tc in test_cases:
#         tc_id = tc.get("test_case_id", "")
#         tc_text = (
#             f"Title: {tc.get('title', '')}\n"
#             f"Precondition: {tc.get('precondition', '')}\n"
#             f"Steps: {json.dumps(tc.get('steps', []))}\n"
#             f"Expected: {tc.get('expected_result', '')}"
#         )
#         rag_store.add_test_case(tc_text, {"test_case_id": tc_id, "requirement_id": rid, "type": "test_case"})
#         count += 1
#         snippet = tc.get("python_test_script", "")
#         if snippet:
#             rag_store.add_python_script(snippet, {
#                 "requirement_id": rid,
#                 "test_case_id": tc_id,
#                 "type": "python_test_script",
#             })
#             count += 1
#     parts = [setup] if setup else []
#     for tc in test_cases:
#         parts.append(tc.get("python_test_script", ""))
#     full_python = "\n\n".join(p for p in parts if p)
#     if full_python:
#         rag_store.add_python_script(full_python, {"requirement_id": rid, "type": "python_test_script_full"})
#         count += 1
#     return count


def _load_one_data_folder_into_rag(rag_store: ExtendedRAGVectorStore, base: Path) -> int:
    """Load one Data_vN folder (CAPL_script + Python_Script, or CAPL_*.json + Python_Script_*) into RAG. Returns chunk count. Does not clear the store."""
    count = 0
    # CAPL: 1) subfolder CAPL_script/*.json  2) or JSON files directly in base (e.g. CAPL_Data_01.json)
    capl_dir = base / "CAPL_script"
    capl_json_files: List[Path] = []
    if capl_dir.is_dir():
        capl_json_files = sorted(capl_dir.glob("*.json"))
    else:
        capl_json_files = sorted(base.glob("*.json"))  # CAPL_Data_01.json etc. in Data_V1/
    for jf in capl_json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if data.get("DBC_outout_after_parsing") is None and data.get("DBC_output_after_parsing") is None and not data.get("requirements"):
            continue  # skip if not CAPL-style (e.g. Python_Script JSON in base)
        count += _process_capl_json(rag_store, data)

    # Python: 1) subfolder Python_Script/*.json  2) or any subfolder starting with Python_Script (e.g. Python_Script_data_01)
    py_dirs: List[Path] = []
    py_dir = base / "Python_Script"
    if py_dir.is_dir():
        py_dirs = [py_dir]
    else:
        for p in base.iterdir():
            if p.is_dir() and p.name.lower().startswith("python_script"):
                py_dirs.append(p)
    for py_dir in sorted(py_dirs, key=lambda x: x.name):
        for jf in sorted(py_dir.glob("*.json")):
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not data.get("requirement") and not data.get("test_cases"):
                continue
            # PYTHON SCRIPT PROCESSING DISABLED - skip loading Python JSON
            # count += _process_python_json(rag_store, data)
    return count

# =============================================================================
# DATA STRUCTURES 
# =============================================================================


@dataclass
class SignalInfo:
    """Complete signal information from DBC."""

    name: str
    message_name: str
    start_bit: int
    bit_length: int
    byte_order: str
    is_signed: bool
    scale: float
    offset: float
    minimum: float
    maximum: float
    unit: str
    choices: Optional[Dict[int, str]]
    initial_value: Optional[float]
    is_counter: bool
    is_crc: bool
    receivers: List[str]

    @property
    def requires_float(self) -> bool:
        return self.scale != 1.0 or self.offset != 0.0

    @property
    def capl_type(self) -> str:
        return "float" if self.requires_float else "int"

    @property
    def value_definitions_str(self) -> str:
        if not self.choices:
            return "No predefined values"
        return ", ".join([f"{k}={v}" for k, v in self.choices.items()])

    @property
    def default_value(self) -> str:
        if self.initial_value is not None:
            return str(self.initial_value)
        return "0.0" if self.requires_float else "0"


@dataclass
class MessageInfo:
    """Complete message information from DBC."""

    name: str
    frame_id: int
    dlc: int
    transmitter: str
    cycle_time: Optional[int]
    send_type: str
    start_delay: Optional[int]
    signals: List[SignalInfo]
    is_multiplexed: bool


@dataclass
class ECUInfo:
    """ECU information with its transmitted messages."""

    name: str
    tx_messages: List[str]


@dataclass
class DBCContext:
    """Complete DBC context with ECU-Message-Signal hierarchy."""

    messages: List[MessageInfo]
    all_signals: List[SignalInfo]
    ecus: List[ECUInfo]
    signal_to_message: Dict[str, str]
    message_to_ecu: Dict[str, str]
    signal_info_map: Dict[str, SignalInfo]
    message_info_map: Dict[str, MessageInfo]
    ecu_to_messages: Dict[str, List[str]]
    raw_dbc_summary: str


@dataclass
class SimulationAnalysis:
    """Analysis result for simulation script generation."""

    raw_requirement: str
    simulation_type: str
    transmission_mode: str
    target_ecu: str
    target_messages: List[str]
    input_messages: List[str]
    input_signals: List[str]
    output_signals: List[str]
    signal_values: Dict[str, float]
    cycle_time_overrides: Dict[str, int]
    counter_signals: List[str]
    crc_signals: List[str]
    reactive_behaviors: List[Dict]
    logic_description: str
    pseudo_code: str
    warnings: List[str]
    signal_data_types: Dict[str, str]
    signal_value_definitions: Dict[str, Dict[int, str]]
    # Byte-level generation guidance (derived from DBC + requirement)
    byte_packing_required: bool
    capl_byte_access_style_hint: str
    dbc_bit_layout_text: str
    byte_packing_snippets: str
    # Pre-computed code snippets to reduce LLM errors
    variable_declarations: str
    signal_initializations: str
    input_signal_reads: str


def _build_dbc_bit_layout_text(
    dbc_ctx: DBCContext,
    input_messages: List[str],
    target_messages: List[str],
    input_signals: List[str],
    output_signals: List[str],
) -> str:
    """
    Build a compact, CAPL-oriented bit layout reference for the LLM.
    Includes: DLC, byte order, start_bit, bit_length, scale/offset, and whether counter/CRC was detected.
    """
    lines: List[str] = []

    def _msg_header(msg_name: str) -> None:
        msg = dbc_ctx.message_info_map.get(msg_name)
        if not msg:
            return
        lines.append(f"MESSAGE {msg.name} (DLC={msg.dlc})")

    # Input message layouts (signals the gateway reads)
    if input_messages:
        lines.append("== INPUT MESSAGES (read/parse) ==")
        for msg_name in input_messages:
            _msg_header(msg_name)
            msg = dbc_ctx.message_info_map.get(msg_name)
            if not msg:
                continue
            for sig in msg.signals:
                if input_signals and sig.name not in input_signals:
                    continue
                lines.append(
                    f"- {sig.name}: start_bit={sig.start_bit}, len={sig.bit_length}, "
                    f"order={sig.byte_order}, signed={sig.is_signed}, scale={sig.scale}, offset={sig.offset}"
                )
            lines.append("")

    # Output message layouts (signals the gateway transmits)
    if target_messages:
        lines.append("== OUTPUT MESSAGES (pack/transmit) ==")
        for msg_name in target_messages:
            _msg_header(msg_name)
            msg = dbc_ctx.message_info_map.get(msg_name)
            if not msg:
                continue
            for sig in msg.signals:
                if output_signals and sig.name not in output_signals:
                    continue
                flags = []
                if sig.is_counter:
                    flags.append("counter")
                if sig.is_crc:
                    flags.append("crc")
                flag_str = f" flags={','.join(flags)}" if flags else ""
                lines.append(
                    f"- {sig.name}: start_bit={sig.start_bit}, len={sig.bit_length}, "
                    f"order={sig.byte_order}, signed={sig.is_signed}, scale={sig.scale}, offset={sig.offset}{flag_str}"
                )
            lines.append("")

    return "\n".join(lines).strip() if lines else "(No bit layout available.)"


def _infer_output_signals_from_requirement(
    requirement: str,
    dbc_ctx: DBCContext,
    target_messages: List[str],
) -> List[str]:
    """
    Heuristically infer output signals from the requirement text, constrained strictly to DBC.
    - Searches signals in target_messages whose names contain requirement keywords
      (e.g. 'engine speed' -> signals containing 'speed' or 'rpm').
    - Returns a list of valid DBC signal names.
    """
    req_lower = requirement.lower()
    keywords: List[str] = []

    # ------------------------------------------------------------------
    # 1) Global DBC search for strong semantic matches (engine/vehicle speed)
    # ------------------------------------------------------------------
    engine_speed_signals: List[str] = []
    vehicle_speed_signals: List[str] = []
    for sig_name, sig_info in dbc_ctx.signal_info_map.items():
        name_l = sig_name.lower()
        unit_l = (sig_info.unit or "").lower()
        # Numeric-only signals (exclude boolean flags/warnings)
        is_numeric = sig_info.bit_length > 2 and (unit_l or sig_info.maximum > sig_info.minimum)

        # Engine speed: prefer RPM-like units and "engine" in the name
        if is_numeric and ("rpm" in unit_l or "rev/min" in unit_l):
            if "engine" in name_l or "eng" in name_l:
                engine_speed_signals.append(sig_name)

        # Vehicle speed: km/h-like units and "vehicle" or "veh" in the name
        if is_numeric and ("km/h" in unit_l or "kph" in unit_l):
            if "vehicle" in name_l or "veh" in name_l:
                vehicle_speed_signals.append(sig_name)

    # If requirement explicitly mentions engine speed, prefer engine RPM signals
    if "engine speed" in req_lower and engine_speed_signals:
        return engine_speed_signals

    # If requirement mentions vehicle speed but not engine speed, prefer vehicle speed signals
    if "vehicle speed" in req_lower and "engine speed" not in req_lower and vehicle_speed_signals:
        return vehicle_speed_signals

    # ------------------------------------------------------------------
    # 2) Fallback: heuristic within target_messages only
    # ------------------------------------------------------------------
    if not target_messages:
        return []

    # Basic keyword extraction from requirement text for generic matching
    if "engine speed" in req_lower or "speed" in req_lower:
        keywords.extend(["speed", "rpm", "engspd"])
    if "vehicle speed" in req_lower:
        keywords.append("speed")

    # Fallback: use all alphabetic tokens longer than 3 chars as loose keywords
    tokens = re.findall(r"[a-zA-Z_]{4,}", req_lower)
    for t in tokens:
        if t not in keywords:
            keywords.append(t)

    matched: List[str] = []
    for msg_name in target_messages:
        msg = dbc_ctx.message_info_map.get(msg_name)
        if not msg:
            continue
        for sig in msg.signals:
            name_l = sig.name.lower()
            unit_l = (sig.unit or "").lower()
            # Skip obvious warning/boolean flags when requirement talks about a "value"
            if "value" in req_lower and sig.bit_length <= 2 and not unit_l:
                continue
            if any(kw in name_l for kw in keywords):
                matched.append(sig.name)

    # Deduplicate while preserving order
    seen = set()
    result: List[str] = []
    for s in matched:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def _build_dbc_authority_text(
    dbc_ctx: DBCContext,
    analysis: SimulationAnalysis,
) -> str:
    """
    Build a short 'DBC authority' block: exact message and signal names from the DBC
    that the CAPL must use. Reduces misalignment by making the single source of truth explicit.
    """
    lines: List[str] = []
    lines.append("USE ONLY THESE NAMES FROM THE DBC (do not invent or substitute):")
    if analysis.input_messages:
        lines.append(f"  Input message (on message handler): {', '.join(analysis.input_messages)}")
    if analysis.input_signals:
        lines.append(f"  Input signals: {', '.join(analysis.input_signals)}")
    if analysis.target_messages:
        lines.append(f"  Output message(s) to transmit: {', '.join(analysis.target_messages)}")
    if analysis.output_signals:
        lines.append(f"  Output signals: {', '.join(analysis.output_signals)}")
    # Optional: one line per output message listing its signals from DBC
    for msg_name in analysis.target_messages or []:
        msg = dbc_ctx.message_info_map.get(msg_name)
        if not msg:
            continue
        out_sigs = [s.name for s in msg.signals if s.name in (analysis.output_signals or [])]
        if out_sigs:
            lines.append(f"  Message '{msg_name}' signals in DBC: {', '.join(out_sigs)}")
    return "\n".join(lines) if lines else ""


def _analysis_to_capl_pattern(analysis: SimulationAnalysis) -> Optional[str]:
    """Map analysis (transmission_mode, simulation_type) to capl_pattern for pattern-based CAPL retrieval."""
    if analysis.transmission_mode == "CYCLIC":
        return "cyclic_timer"
    if analysis.transmission_mode == "IMMEDIATE":
        if analysis.input_messages:
            return "reactive_message"
        return "reactive_key"
    return None


# PYTHON SCRIPT PROCESSING DISABLED
# def _build_python_test_setup_info(
#     dbc_ctx: DBCContext,
#     analysis: SimulationAnalysis,
# ) -> str:
#     """
#     Build Python test setup info from DBC and analysis: message IDs, decode functions, collect_messages.
#     Used to guide accurate python-can based pytest generation (per Pytest Explanation.txt).
#     """
#     lines: List[str] = []
#     if not analysis.target_messages:
#         return ""
#
#     for msg_name in analysis.target_messages:
#         msg = dbc_ctx.message_info_map.get(msg_name)
#         if not msg:
#             continue
#         arb_id = msg.frame_id
#         lines.append(f"MESSAGE: {msg_name} | CAN ID (arbitration_id): 0x{arb_id:X} ({arb_id} decimal)")
#         lines.append("")
#
#         # Decode functions for output signals
#         for sig_name in (analysis.output_signals or []):
#             sig = dbc_ctx.signal_info_map.get(sig_name)
#             if not sig or sig.message_name != msg_name:
#                 continue
#             byte_idx = sig.start_bit // 8
#             bit_offset = sig.start_bit % 8
#             mask = (1 << sig.bit_length) - 1
#             if sig.bit_length <= 8 and byte_idx < 8:
#                 decode_expr = f"(msg.data[{byte_idx}] >> {bit_offset}) & 0x{mask:X}"
#             else:
#                 decode_expr = f"(msg.data[{byte_idx}] >> {bit_offset}) & 0x{mask:X}  # multi-byte: adapt if needed"
#             func_name = f"decode_{sig_name.lower().replace('-', '_')}"
#             lines.append(f"def {func_name}(msg):")
#             lines.append(f'    """Extract {sig_name} from CAN message. start_bit={sig.start_bit}, length={sig.bit_length}, {sig.byte_order}"""')
#             lines.append(f"    return {decode_expr}")
#             lines.append("")
#
#     lines.append("def collect_messages(bus, arb_id, duration_sec):")
#     lines.append('    """Collect CAN messages for arbitration_id over duration_sec."""')
#     lines.append("    messages = []")
#     lines.append("    start_time = time.time()")
#     lines.append("    while time.time() - start_time < duration_sec:")
#     lines.append("        msg = bus.recv(timeout=0.1)")
#     lines.append("        if msg and msg.arbitration_id == arb_id:")
#     lines.append("            messages.append(msg)")
#     lines.append("    return messages")
#
#     return "\n".join(lines) if lines else ""


# =============================================================================
# LLM CONFIGURATION (OLLAMA GPU / LOCAL)
# =============================================================================

def _test_ollama_connection(base_url: str, timeout: float = 5.0) -> bool:
    """
    Test if Ollama is reachable at the given base_url.
    """
    import requests
    try:
        response = requests.get(f"{base_url}/api/tags", timeout=timeout)
        return response.status_code == 200
    except Exception as e:
        _log_debug(f"Ollama connection test failed for {base_url}: {e}")
        return False


def _get_ollama_base_url() -> str:
    """
    Determine the best Ollama base URL to use based on configuration and availability.
    
    Priority:
    1. If FORCE_GPU_FIRST=True, try GPU first; if available, use it
    2. If GPU unavailable and USE_LOCALHOST_FALLBACK=True, fall back to localhost
    3. Otherwise use GPU URL (will likely fail, but respects config)
    """
    if FORCE_GPU_FIRST:
        gpu_available = _test_ollama_connection(OLLAMA_BASE_URL_GPU, OLLAMA_GPU_TEST_TIMEOUT)
        if gpu_available:
            _log_debug(f"GPU Ollama server available at {OLLAMA_BASE_URL_GPU}")
            return OLLAMA_BASE_URL_GPU
        elif USE_LOCALHOST_FALLBACK:
            _log_debug(f"GPU Ollama unavailable; falling back to localhost at {OLLAMA_BASE_URL_LOCAL}")
            return OLLAMA_BASE_URL_LOCAL
        else:
            _log_debug(f"GPU Ollama unavailable and fallback disabled; using GPU URL anyway")
            return OLLAMA_BASE_URL_GPU
    else:
        _log_debug(f"FORCE_GPU_FIRST=False; using localhost at {OLLAMA_BASE_URL_LOCAL}")
        return OLLAMA_BASE_URL_LOCAL


def _get_ollama_model(base_url: str, task_type: str = "general") -> str:
    """
    Select appropriate Ollama model based on base URL and task type.
    """
    is_gpu = base_url == OLLAMA_BASE_URL_GPU
    
    # For now, use large model; can be extended for task-specific selection
    if is_gpu:
        return OLLAMA_MODEL_GPU
    else:
        return OLLAMA_MODEL_LOCAL


def get_ollama_llm(
    temperature: float = 0.1,
    max_tokens: int = 3000,
    stop: Optional[List[str]] = None,
    task_type: str = "general",
) -> OllamaLLM:
    """
    Return an Ollama LLM instance with GPU/local fallback support.
    - Attempts GPU first if FORCE_GPU_FIRST=True
    - Falls back to localhost if USE_LOCALHOST_FALLBACK=True and GPU unavailable
    - task_type can be used for model selection (general, analysis, code_gen, etc.)
    """
    base_url = _get_ollama_base_url()
    model = _get_ollama_model(base_url, task_type)
    
    _log_debug(f"get_ollama_llm: base_url={base_url}, model={model}, temperature={temperature}")
    print(f"[DEBUG] get_ollama_llm: base_url={base_url}, model={model}, temperature={temperature}")
    
    kwargs: Dict[str, Any] = {
        "base_url": base_url,
        "model": model,
        "temperature": temperature,
    }
    # Note: OllamaLLM uses num_predict instead of max_tokens
    if max_tokens:
        kwargs["num_predict"] = max_tokens
    if stop:
        kwargs["stop"] = stop
    
    return OllamaLLM(**kwargs)

# =============================================================================
# DBC PARSER (RICH VERSION FROM app14 1.py)
# =============================================================================


def detect_counter_signal(name: str, bit_length: int) -> bool:
    counter_patterns = ["alive", "counter", "cnt", "rollcnt", "seqnum", "msgcnt", "mc_"]
    name_lower = name.lower()
    return any(p in name_lower for p in counter_patterns) and bit_length in [4, 8]


def detect_crc_signal(name: str, bit_length: int) -> bool:
    crc_patterns = ["crc", "checksum", "chk", "cks"]
    name_lower = name.lower()
    return any(p in name_lower for p in crc_patterns) and bit_length == 8


def parse_dbc_file(dbc_content: bytes) -> DBCContext:
    """Parse DBC file with complete ECU ownership tracking."""
    with tempfile.NamedTemporaryFile(suffix=".dbc", delete=False) as tmp:
        tmp.write(dbc_content)
        tmp_path = tmp.name

    try:
        db = cantools.database.load_file(tmp_path)
    finally:
        os.unlink(tmp_path)

    messages: List[MessageInfo] = []
    all_signals: List[SignalInfo] = []
    signal_to_message: Dict[str, str] = {}
    message_to_ecu: Dict[str, str] = {}
    signal_info_map: Dict[str, SignalInfo] = {}
    message_info_map: Dict[str, MessageInfo] = {}
    ecu_tx_messages: Dict[str, List[str]] = {}

    summary_lines: List[str] = []
    summary_lines.append("=" * 70)
    summary_lines.append("DBC DATABASE - ECU AND MESSAGE OWNERSHIP")
    summary_lines.append("=" * 70)
    summary_lines.append("")
    summary_lines.append("CRITICAL: Each ECU can ONLY transmit its own messages!")
    summary_lines.append("")

    for msg in db.messages:
        cycle_time = getattr(msg, "cycle_time", None)
        send_type = "Cyclic"
        start_delay = None

        if hasattr(msg, "dbc") and msg.dbc:
            attrs = getattr(msg.dbc, "attributes", {})
            if "GenMsgSendType" in attrs:
                send_type = attrs["GenMsgSendType"]
            if "GenMsgStartDelayTime" in attrs:
                start_delay = int(attrs["GenMsgStartDelayTime"])

        transmitter = "Unknown"
        if msg.senders:
            transmitter = msg.senders[0]

        if transmitter not in ecu_tx_messages:
            ecu_tx_messages[transmitter] = []
        ecu_tx_messages[transmitter].append(msg.name)
        message_to_ecu[msg.name] = transmitter

        is_multiplexed = any(getattr(sig, "is_multiplexer", False) for sig in msg.signals)

        signals: List[SignalInfo] = []
        for sig in msg.signals:
            sig_min = sig.minimum if sig.minimum is not None else 0
            sig_max = sig.maximum if sig.maximum is not None else (2 ** sig.length - 1)
            scale = sig.scale if sig.scale else 1
            offset = sig.offset if sig.offset else 0
            choices = dict(getattr(sig, "choices", {})) if getattr(sig, "choices", None) else None

            initial_value = None
            if hasattr(sig, "dbc") and sig.dbc:
                attrs = getattr(sig.dbc, "attributes", {})
                if "GenSigStartValue" in attrs:
                    initial_value = float(attrs["GenSigStartValue"])

            byte_order = "little_endian" if sig.byte_order == "little_endian" else "big_endian"
            is_counter = detect_counter_signal(sig.name, sig.length)
            is_crc = detect_crc_signal(sig.name, sig.length)
            receivers = list(getattr(sig, "receivers", []))

            signal_info = SignalInfo(
                name=sig.name,
                message_name=msg.name,
                start_bit=sig.start,
                bit_length=sig.length,
                byte_order=byte_order,
                is_signed=sig.is_signed,
                scale=scale,
                offset=offset,
                minimum=sig_min,
                maximum=sig_max,
                unit=sig.unit if sig.unit else "",
                choices=choices,
                initial_value=initial_value,
                is_counter=is_counter,
                is_crc=is_crc,
                receivers=receivers,
            )

            signals.append(signal_info)
            all_signals.append(signal_info)
            signal_to_message[sig.name] = msg.name
            signal_info_map[sig.name] = signal_info

        msg_info = MessageInfo(
            name=msg.name,
            frame_id=msg.frame_id,
            dlc=msg.length,
            transmitter=transmitter,
            cycle_time=cycle_time,
            send_type=send_type,
            start_delay=start_delay,
            signals=signals,
            is_multiplexed=is_multiplexed,
        )
        messages.append(msg_info)
        message_info_map[msg.name] = msg_info

    # Build ECU summary
    ecus: List[ECUInfo] = []
    for ecu_name, tx_msgs in ecu_tx_messages.items():
        ecus.append(ECUInfo(name=ecu_name, tx_messages=tx_msgs))
        summary_lines.append(f"ECU: {ecu_name}")
        summary_lines.append(f"  TRANSMITS: {', '.join(tx_msgs)}")
        summary_lines.append("")

    summary_lines.append("=" * 70)
    summary_lines.append("MESSAGE AND SIGNAL DETAILS")
    summary_lines.append("=" * 70)

    for msg in messages:
        summary_lines.append("")
        summary_lines.append(f"MESSAGE: {msg.name}")
        summary_lines.append(f"  Transmitter: {msg.transmitter}")
        summary_lines.append(f"  CAN ID: 0x{msg.frame_id:X}")
        summary_lines.append(f"  Cycle Time: {msg.cycle_time or 100} ms")
        summary_lines.append("  Signals:")

        for sig in msg.signals:
            summary_lines.append(f"    SIGNAL: {sig.name}")
            summary_lines.append(
                f"      Data Type: {sig.capl_type} (scale={sig.scale}, offset={sig.offset})"
            )
            summary_lines.append(f"      Range: [{sig.minimum} to {sig.maximum}] {sig.unit}")
            if sig.choices:
                summary_lines.append(f"      VALUE DEFINITIONS: {sig.value_definitions_str}")

    return DBCContext(
        messages=messages,
        all_signals=all_signals,
        ecus=ecus,
        signal_to_message=signal_to_message,
        message_to_ecu=message_to_ecu,
        signal_info_map=signal_info_map,
        message_info_map=message_info_map,
        ecu_to_messages=ecu_tx_messages,
        raw_dbc_summary="\n".join(summary_lines),
    )


# =============================================================================
# REQUIREMENT PARSER (PDF/TXT/EXCEL SUPPORT)
# =============================================================================


def extract_text_from_pdf(pdf_content: bytes) -> str:
    """Extract text from a PDF requirement file."""
    if pypdf is None:
        raise ImportError("PDF support requires the 'pypdf' package. Install with: pip install pypdf")
    reader = pypdf.PdfReader(io.BytesIO(pdf_content))
    return "\n".join([page.extract_text() or "" for page in reader.pages])


def extract_text_from_excel(excel_content: bytes, filename: str) -> str:
    """Extract text from an Excel requirement file (.xlsx/.xls)."""
    if pd is None:
        raise ImportError(
            "Excel support requires 'pandas'. Install with: pip install pandas openpyxl "
            "(and install xlrd as well if you need legacy .xls support)."
        )

    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    engine = None
    if ext == "xlsx":
        engine = "openpyxl"
    elif ext == "xls":
        # pandas typically requires xlrd for .xls
        engine = None

    book = pd.read_excel(io.BytesIO(excel_content), sheet_name=None, header=None, engine=engine)
    parts: List[str] = []
    for sheet_name, df in book.items():
        # Convert all cells to a readable plain-text block
        safe = df.fillna("").astype(str)
        lines = ["\t".join(row).strip() for row in safe.values.tolist()]
        sheet_text = "\n".join([ln for ln in lines if ln.strip()])
        if sheet_text.strip():
            parts.append(f"SHEET: {sheet_name}\n{sheet_text}")
    return "\n\n".join(parts).strip()


def parse_requirement_file(content: bytes, filename: str) -> str:
    """Parse requirement content from PDF, Excel, or text file."""
    if filename.lower().endswith(".pdf"):
        return extract_text_from_pdf(content)
    if filename.lower().endswith((".xlsx", ".xls")):
        return extract_text_from_excel(content, filename)
    return content.decode("utf-8", errors="ignore")


def _parse_requirements_from_text(text: str) -> List[str]:
    """Extract individual requirements from raw text. Supports numbered (1. 2. 1) 2)), bulleted (- • *), and blank-line separation."""
    if not text or not text.strip():
        return []
    lines = text.strip().split("\n")
    requirements: List[str] = []
    current: List[str] = []
    # Patterns: "1.", "2.", "1)", "2)", "(1)", "- ", "• ", "* ", "– "
    start_pattern = re.compile(
        r"^\s*(?:\d+[\.\)]\s*|\(\d+\)\s*|[-•*–]\s+|[▪▪●]\s*)\s*",
        re.IGNORECASE
    )

    def flush_current() -> None:
        if current:
            req = "\n".join(current).strip()
            if req:
                requirements.append(req)
            current.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                flush_current()
            continue
        # Check if this line starts a new requirement (number/bullet at start)
        if start_pattern.match(stripped):
            flush_current()
            current.append(stripped)
        elif current:
            current.append(stripped)
        else:
            # No current requirement yet - first line might not have bullet
            current.append(stripped)

    flush_current()
    return requirements if requirements else [text.strip()] if text.strip() else []

# =============================================================================
# GENERATION PROMPTS
# =============================================================================

TEST_CASE_GENERATION_PROMPT = PromptTemplate(
    input_variables=["requirement", "dbc_summary"],
    template='''You are an Automotive Software Test Engineer responsible for creating software validation test cases for ECU requirements.

Generate clear, structured, and easy-to-understand test cases based on the given requirement.

You MUST output EXACTLY 5 test cases (no more, no less).

Follow the STRICT JSON structure below. Output ONLY the JSON (no explanations, no markdown, no comments):

[
  {{
    "test_case_id": "",
    "test_objective": "",
    "test_case_design_techniques": "",
    "pre_conditions": [],
    "test_case_type": "",
    "test_steps": [],
    "expected_output": []
  }}
]

Guidelines:

1. "test_case_id"
- Use sequential format:
  - "TC_001"
  - "TC_002"
  - "TC_003"
  - "TC_004"
  - "TC_005"

2. "test_objective"
- Clearly explain what functionality or behavior is being validated.
- It should help a tester quickly understand the goal of the test.

3. "test_case_design_techniques"
- Choose the most appropriate technique, e.g.:
  - "Requirement Based Testing"
  - "Decision Table Testing"
  - "Boundary Value Analysis"
  - "Equivalence Partitioning"
  - "State Transition Testing"

4. "pre_conditions"
- List the required setup before executing the test.
- Examples:
  - "ECU is powered ON"
  - "Software is flashed to ECU"
  - "CAN communication is active"
  - "Required tools (e.g., CANoe) are connected"

5. "test_case_type"
- Use one of the following strings:
  - "Positive"
  - "Negative"
  - "Boundary"
  - "Timing"
  - "State"

6. "test_steps"
- Steps must be sequential and easy to execute.
- Describe tester actions clearly.
- Include stimulation of signals/messages if relevant.
- Avoid technical field-level structures like action/signal/value objects.
- Use simple natural language strings, for example:
  - "Power ON the ECU"
  - "Start CANoe measurement"
  - "Send CAN message with required signal value"
  - "Monitor the corresponding ECU response"

7. "expected_output"
- Describe the expected system behavior in plain language strings.
- Mention signals, ECU responses, message transmissions, or timing conditions when applicable.
- Ensure the expected output clearly validates the requirement.
- Avoid vague statements such as:
  - "Verify message works correctly"
- Prefer explicit statements like:
  - "Verify that the message is transmitted every 100 ms ±10 ms"

8. The test cases must be easy for automotive validation engineers to understand and execute.

9. Use message and signal names that are consistent with the DBC context when relevant.

10. Output ONLY the JSON array of 5 test case objects. Do NOT include explanations, markdown, or any text before or after the JSON.

Requirement:
{requirement}

Additional Context (if available):
{dbc_summary}
''',
)

# PYTHON SCRIPT PROCESSING DISABLED
# def _load_pytest_explanation() -> str:
#     """Load Pytest Explanation.txt as reference structure for Python test generation."""
#     if PYTEST_EXPLANATION_PATH.is_file():
#         try:
#             return PYTEST_EXPLANATION_PATH.read_text(encoding="utf-8")
#         except Exception:
#             pass
#     return ""


def _load_capl_predefined_rules() -> str:
    """Load CAPL Simulation predefined rules (fallback; primary is Corrected prompt)."""
    if CAPL_PREDEFINED_RULES_PATH.is_file():
        try:
            return CAPL_PREDEFINED_RULES_PATH.read_text(encoding="utf-8")
        except Exception:
            pass
    return ""


def _load_corrected_capl_prompt() -> str:
    """Load Corrected CAPL Prompt (primary rules for CAPL generation; preferred over predefined rules)."""
    if CAPL_CORRECTED_PROMPT_PATH.is_file():
        try:
            return CAPL_CORRECTED_PROMPT_PATH.read_text(encoding="utf-8")
        except Exception:
            pass
    # Fallback to predefined rules if Corrected file not found
    return _load_capl_predefined_rules()


# PYTHON SCRIPT PROCESSING DISABLED
# def _load_corrected_python_prompt() -> str:
#     """Load Corrected Python Pytest Prompt (primary rules for Python generation)."""
#     if CORRECTED_PYTHON_PROMPT_PATH.is_file():
#         try:
#             return CORRECTED_PYTHON_PROMPT_PATH.read_text(encoding="utf-8")
#         except Exception:
#             pass
#     return ""


# PYTHON SCRIPT GENERATION PROMPT DISABLED
# PYTHON_TEST_GENERATION_PROMPT = PromptTemplate(
#     input_variables=[
#         "requirement",
#         "test_cases",
#         "capl_script",
#         "rag_context",
#         "python_setup_info",
#         "pytest_structure_reference",
#     ],
#     template='''Generate a Python pytest test script for CAN bus testing using the **python-can** library.
# The CAPL script runs the simulation (transmits messages); the Python script **receives** and validates.
#
# =============================================================================
# REQUIRED STRUCTURE (follow exactly)
# =============================================================================
# 1. Imports: import can, pytest, time
# 2. Constants: MESSAGE_ID = 0xXXX  (hex, from DBC frame_id for target message)
# 3. Session fixture: @pytest.fixture(scope="session") def can_bus():
#    - bus = can.Bus(interface="socketcan" or "vector" or "pcan", channel="can0", bitrate=500000)
#    - yield bus; bus.shutdown()
# 4. collect_messages(bus, arb_id, duration_sec): Loop for duration_sec, bus.recv(timeout=0.1), filter by arbitration_id, return list
# 5. decode_* functions: Extract signals from msg.data using bit masking (e.g. msg.data[0] & 0x01 for bit 0)
# 6. Test functions: test_tc_*_description(can_bus) - collect messages, assert received, decode, assert signal values
#
# DO NOT use CANoe API (send_message, get_received_messages). Use python-can: can.Bus, bus.recv().
#
# =============================================================================
# DBC-DERIVED SETUP (use these message IDs and decode logic)
# =============================================================================
# {python_setup_info}
#
# =============================================================================
# REFERENCE STRUCTURE (Pytest Explanation.txt)
# =============================================================================
# {pytest_structure_reference}
#
# =============================================================================
# REQUIREMENT
# =============================================================================
# {requirement}
#
# =============================================================================
# TEST CASES TO IMPLEMENT
# =============================================================================
# {test_cases}
#
# =============================================================================
# CAPL SCRIPT (simulation transmits; Python receives and asserts)
# =============================================================================
# {capl_script}
#
# =============================================================================
# ADDITIONAL PYTHON EXAMPLES (from RAG)
# =============================================================================
# {rag_context}
#
# =============================================================================
# OUTPUT
# =============================================================================
# Generate complete Python code only. No markdown fences, no explanations.
# Test names: test_tc_<domain>_<number>_<short_description> (e.g. test_tc_gw_door_001_all_closed).
# '''
# )

# =============================================================================
# STRONGER RAG-ENHANCED ANALYSIS + CAPL GENERATION 
# =============================================================================


ECU_ALIASES = {
    "engine controller": ["ECM", "ECU", "Engine"],
    "engine control module": ["ECM", "ECU", "Engine"],
    "ecm": ["ECM", "ECU", "Engine"],
    "body controller": ["BCM", "Body"],
    "body control module": ["BCM", "Body"],
    "bcm": ["BCM", "Body"],
    "gateway": ["GW", "Gateway", "CGW"],
    "instrument cluster": ["IC", "Cluster", "IPC"],
    "ic": ["IC", "Cluster", "IPC"],
}


def find_ecu_by_alias(requirement: str, available_ecus: List[str]) -> Optional[str]:
    """Find ECU name from requirement text using aliases."""
    req_lower = requirement.lower()

    for alias, possible_names in ECU_ALIASES.items():
        if alias in req_lower:
            for name in possible_names:
                for ecu in available_ecus:
                    if name.lower() == ecu.lower() or name.lower() in ecu.lower():
                        return ecu

    for ecu in available_ecus:
        if ecu.lower() in req_lower:
            return ecu

    return None


SIMULATION_ANALYSIS_PROMPT = PromptTemplate(
    input_variables=["requirement", "dbc_summary", "ecu_message_mapping"],
    template='''You are analyzing ONE requirement to generate a CAPL simulation script.
Analyze ONLY the single requirement below. Do NOT analyze multiple requirements.
Return exactly ONE JSON object. No explanations, no preamble, no "Requirement 1/2/3" labels.

REQUIREMENT:
{requirement}

{dbc_summary}

ECU-MESSAGE OWNERSHIP:
{ecu_message_mapping}

=============================================================================
STEP 1: IDENTIFY THE ECU
=============================================================================
Which ECU is mentioned in the requirement?
- "engine controller" = ECM
- "body controller" = BCM  
- "gateway" = GW
- "instrument cluster" = IC

=============================================================================
STEP 2: IDENTIFY WHAT THAT ECU TRANSMITS
=============================================================================
Look at ECU-MESSAGE OWNERSHIP above.
The target_messages MUST be from that ECU's transmit list.

Example:
- If requirement says "engine controller shall transmit" -> ECM transmits ECM_Vehicle_Data
- If requirement says "body controller shall broadcast" -> BCM transmits BCM_Status_1

=============================================================================
STEP 3: IDENTIFY TRANSMISSION MODE
=============================================================================
IMMEDIATE (output inside on message handler):
- "immediately", "instant", "upon", "when detected", "as soon as"

CYCLIC (output inside on timer handler):
- "periodically", "cyclic", "every", "continuous"

=============================================================================
STEP 4: IDENTIFY SIMULATION TYPE
=============================================================================
SINGLE_ECU_TRANSMIT:
- ECU transmits its OWN data
- Example: "ECM shall periodically transmit vehicle speed"
- NO input_messages needed
- NO on message handler needed

GATEWAY:
- ECU receives from another ECU and forwards
- Example: "Gateway shall transmit speed based on received ECM data"
- HAS input_messages
- HAS on message handler
- CRITICAL: target_messages and output_signals must include ONLY what the requirement asks for.
  Example: "gateway shall periodically transmit engine speed to cluster" -> ONLY the message that carries engine speed to the cluster (e.g. GW_Consolidated_Status2 with SpeedValue/RpmValue), NOT every message the gateway transmits. output_signals = only the signals mentioned (e.g. SpeedValue, RpmValue). input_messages/input_signals = only the message/signal that provides the received engine speed data.

REACTIVE:
- ECU transmits immediately when event received
- Example: "BCM shall immediately broadcast when hazard detected"
- HAS input_messages
- output() inside on message handler

=============================================================================
STEP 5: IDENTIFY SIGNALS (ONLY WHAT THE REQUIREMENT NEEDS)
=============================================================================
output_signals: ONLY the signals in the OUTPUT message that are mentioned or clearly implied by the requirement (e.g. "engine speed" -> SpeedValue, RpmValue). Do NOT list every signal in the message.
input_signals: ONLY the signals from INPUT message needed for the requirement (only for GATEWAY/REACTIVE). Example: "most recently received engine speed" -> only the engine speed signal from the input message.
target_messages: For GATEWAY, list ONLY the output message(s) that carry the data the requirement asks to transmit (e.g. only the message that carries engine speed to cluster), NOT all messages the ECU transmits.

For each signal, note:
- Data type: float if scale != 1.0, otherwise int
- Value definitions if they exist (e.g., 0=OFF, 1=ON, 3=HAZARD)

=============================================================================
RESPOND WITH EXACTLY ONE JSON OBJECT (no other text):
=============================================================================
{{
    "simulation_type": "SINGLE_ECU_TRANSMIT or GATEWAY or REACTIVE",
    "transmission_mode": "CYCLIC or IMMEDIATE",
    "target_ecu": "exact ECU name from DBC",
    "target_messages": ["ONLY the message(s) that carry the data the requirement asks for; for GATEWAY do NOT list every message the ECU transmits"],
    "input_messages": ["exact message name if GATEWAY/REACTIVE, else empty"],
    "input_signals": ["exact signal names from input_messages"],
    "output_signals": ["exact signal names from target_messages mentioned in requirement"],
    "signal_data_types": {{"signal_name": "int or float based on scale"}},
    "signal_value_definitions": {{"signal_name": {{"0": "OFF", "1": "ON"}}}},
    "signal_values": {{}},
    "cycle_time_overrides": {{}},
    "counter_signals": [],
    "crc_signals": [],
    "reactive_behaviors": [],
    "logic_description": "one sentence description",
    "pseudo_code": "simple pseudo code",
    "warnings": []
}}

JSON:'''
)


def get_similar_requirement_and_linked_ids(
    rag_store: RAGVectorStore,
    requirement: str,
    req_k: int = 1,
    req_metadata_filter: Optional[Dict[str, str]] = None,
) -> Tuple[RetrievalResult, Optional[str], Optional[str]]:
    """
    Step 1: Find similar requirement(s) to the user query.
    Step 2: From the best match, read requirement_id and dbc_id from metadata (for linked retrieval).
    Returns (req_results, requirement_id, dbc_id). Ids may be None if no chunk or no metadata.
    """
    try:
        try:
            req_results = rag_store.retrieve(
                requirement, top_k=req_k, source_filter="requirement",
                metadata_filter=req_metadata_filter,
            )
        except TypeError:
            req_results = rag_store.retrieve(
                requirement, top_k=req_k, source_filter="requirement",
            )
        requirement_id: Optional[str] = None
        dbc_id: Optional[str] = None
        if req_results.chunks:
            first = req_results.chunks[0]
            requirement_id = (first.metadata.get("requirement_id") or "").strip() or None
            dbc_id = (first.metadata.get("dbc_id") or "").strip() or None
        return (req_results, requirement_id, dbc_id)
    except Exception as e:
        st.warning(f"RAG retrieval warning (similar requirement): {str(e)}")
        return (RetrievalResult(chunks=[], scores=[], context_text=""), None, None)


def get_rag_enhanced_context(rag_store: RAGVectorStore, requirement: str) -> str:
    """
    Get RAG-enhanced context for requirement analysis.
    Strategy: find similar requirement first, then pull DBC and CAPL linked to that requirement (same dbc_id).
    """
    try:
        req_meta = _infer_requirement_metadata(requirement)
        req_meta_filter = {k: v for k, v in req_meta.items() if v} if req_meta else None
        req_results, requirement_id, dbc_id = get_similar_requirement_and_linked_ids(
            rag_store, requirement, req_k=1, req_metadata_filter=req_meta_filter
        )
        dbc_results = RetrievalResult(chunks=[], scores=[], context_text="")
        capl_results = RetrievalResult(chunks=[], scores=[], context_text="")
        if dbc_id:
            try:
                dbc_results = rag_store.retrieve(
                    requirement, top_k=5, source_filter="dbc",
                    metadata_filter={"dbc_id": dbc_id},
                )
                capl_results = rag_store.retrieve(
                    requirement, top_k=2, source_filter="capl",
                    metadata_filter={"dbc_id": dbc_id},
                )
            except TypeError:
                pass
        print(f"[DEBUG] get_rag_enhanced_context: req={len(req_results.chunks)}, dbc_id={dbc_id!r}, dbc={len(dbc_results.chunks)}, capl={len(capl_results.chunks)}")
        rag_context = create_rag_context_prompt(
            requirement, (req_results, dbc_results, capl_results)
        )
        return _truncate_to_chars(rag_context, _TRUNCATE_RAG_CONTEXT_CHARS)
    except Exception as e:
        st.warning(f"RAG retrieval warning: {str(e)}")
        return ""


def analyze_requirement_for_simulation(
    requirement: str,
    dbc_ctx: DBCContext,
    model_choice: str = "Ollama",
    rag_store: Optional[RAGVectorStore] = None,
    retrieved_log: Optional[List[str]] = None,
) -> SimulationAnalysis:
    """Analyze requirement with strict ECU ownership validation and RAG enhancement."""

    ecu_mapping_lines: List[str] = []
    for ecu in dbc_ctx.ecus:
        msgs = dbc_ctx.ecu_to_messages.get(ecu.name, [])
        ecu_mapping_lines.append(f"{ecu.name} transmits: {', '.join(msgs)}")
    ecu_mapping = "\n".join(ecu_mapping_lines)

    # Use Ollama LLM
    llm = get_ollama_llm(temperature=0.05, max_tokens=3000)

    # Get RAG-enhanced context if available
    rag_context = ""
    if rag_store:
        try:
            rag_context = get_rag_enhanced_context(rag_store, requirement)
        except Exception as e:
            st.warning(f"RAG context retrieval failed: {str(e)}")
    if retrieved_log is not None and rag_context:
        retrieved_log.append(f"[analysis] RAG-enhanced context:\n{rag_context}")

    # Build enhanced requirement with RAG context (truncate requirement for prompt size)
    requirement_for_prompt = _truncate_to_chars(requirement, _TRUNCATE_REQUIREMENT_CHARS)
    if rag_context:
        enhanced_requirement = build_enhanced_analysis_prompt(requirement_for_prompt, rag_context)
    else:
        enhanced_requirement = requirement_for_prompt

    dbc_summary = _truncate_to_chars(dbc_ctx.raw_dbc_summary, _TRUNCATE_DBC_SUMMARY_CHARS)
    chain = SIMULATION_ANALYSIS_PROMPT | llm | StrOutputParser()

    raw_output = chain.invoke(
        {
            "requirement": enhanced_requirement,
            "dbc_summary": dbc_summary,
            "ecu_message_mapping": ecu_mapping,
        }
    )

    try:
        json_str = _extract_first_json_object(raw_output)
        if json_str:
            json_str = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", json_str)
            analysis = json.loads(json_str)
        else:
            raise ValueError("No JSON found in LLM response")

        valid_signals = {sig.name for sig in dbc_ctx.all_signals}
        valid_messages = {msg.name for msg in dbc_ctx.messages}
        valid_ecus = {ecu.name for ecu in dbc_ctx.ecus}

        # Get and validate target ECU
        target_ecu = analysis.get("target_ecu", "")
        if target_ecu not in valid_ecus:
            found_ecu = find_ecu_by_alias(requirement, list(valid_ecus))
            if found_ecu:
                target_ecu = found_ecu

        # Get messages that this ECU actually transmits
        ecu_tx_messages = dbc_ctx.ecu_to_messages.get(target_ecu, [])

        # Validate target_messages belong to target_ecu
        target_messages = analysis.get("target_messages", [])
        validated_target_messages = [m for m in target_messages if m in ecu_tx_messages]
        if not validated_target_messages and ecu_tx_messages:
            validated_target_messages = ecu_tx_messages
        target_messages = validated_target_messages

        input_messages = [
            m for m in analysis.get("input_messages", []) if m in valid_messages
        ]
        input_signals = [
            s for s in analysis.get("input_signals", []) if s in valid_signals
        ]
        output_signals = [
            s for s in analysis.get("output_signals", []) if s in valid_signals
        ]

        sim_type = analysis.get("simulation_type", "SINGLE_ECU_TRANSMIT")

        # If no output signals:
        # - For SINGLE_ECU with one target message: default to all signals of that message.
        # - For GATEWAY/REACTIVE: infer from requirement text so we don't pull in unrelated signals.
        if not output_signals and target_messages:
            if sim_type == "SINGLE_ECU_TRANSMIT" and len(target_messages) == 1:
                msg = dbc_ctx.message_info_map.get(target_messages[0])
                if msg:
                    output_signals.extend([sig.name for sig in msg.signals])
            elif sim_type in ("GATEWAY", "REACTIVE"):
                inferred = _infer_output_signals_from_requirement(
                    requirement, dbc_ctx, target_messages
                )
                if inferred:
                    output_signals = inferred

        # For GATEWAY/REACTIVE: keep only target messages that actually carry the required output signals
        if output_signals and len(target_messages) > 1 and sim_type in ("GATEWAY", "REACTIVE"):
            out_set = set(output_signals)
            target_messages = [
                m for m in target_messages
                if dbc_ctx.message_info_map.get(m)
                and out_set & {s.name for s in dbc_ctx.message_info_map[m].signals}
            ]
            if not target_messages:
                target_messages = analysis.get("target_messages", [])[:1]  # fallback to first

        # Build signal data types from DBC (authoritative source)
        signal_data_types: Dict[str, str] = {}
        signal_value_definitions: Dict[str, Dict[int, str]] = {}
        for sig_name in input_signals + output_signals:
            if sig_name in dbc_ctx.signal_info_map:
                sig_info = dbc_ctx.signal_info_map[sig_name]
                signal_data_types[sig_name] = sig_info.capl_type
                if sig_info.choices:
                    signal_value_definitions[sig_name] = sig_info.choices

        # Decide whether byte-level packing should be REQUIRED for CAPL.
        # Project decision: ALWAYS require byte-level handling + bounded loops for every requirement,
        # so that all generated scripts use a consistent byte/bit-based style.
        byte_packing_required = True

        # Byte access style hint for CAPL (varies by CANoe configuration).
        # Keep this as a hint only; the prompt will instruct the model to match examples if present.
        capl_byte_access_style_hint = (
            "Prefer CANoe-style message byte access like msg.byte(0) / this.byte(0). "
            "If your environment uses msg.data[0], use that consistently instead."
        )

        dbc_bit_layout_text = _build_dbc_bit_layout_text(
            dbc_ctx,
            input_messages=input_messages,
            target_messages=target_messages,
            input_signals=input_signals,
            output_signals=output_signals,
        )

        byte_packing_snippets = build_byte_packing_snippets(
            dbc_ctx,
            target_messages=target_messages,
            output_signals=output_signals,
        )

        # Detect transmission mode: prefer keyword evidence; do NOT default to CYCLIC when ambiguous
        raw_mode = (analysis.get("transmission_mode") or "").strip().upper()
        transmission_mode = raw_mode if raw_mode in ("CYCLIC", "IMMEDIATE") else None
        req_lower = requirement.lower()

        immediate_keywords = [
            "immediately",
            "immediate",
            "instantly",
            "instant",
            "upon",
            "when detected",
            "right away",
            "at once",
            "as soon as",
        ]
        cyclic_keywords = [
            "periodically",
            "periodic",
            "cyclically",
            "cyclic",
            "every",
            "interval",
            "continuous",
        ]

        has_immediate = any(kw in req_lower for kw in immediate_keywords)
        has_cyclic = any(kw in req_lower for kw in cyclic_keywords)

        if has_immediate and not has_cyclic:
            transmission_mode = "IMMEDIATE"
        elif has_cyclic:
            transmission_mode = "CYCLIC"
        # If still unknown (no keywords, or LLM gave invalid value): per Corrected prompt, default to CYCLIC
        if transmission_mode not in ("CYCLIC", "IMMEDIATE"):
            transmission_mode = raw_mode if raw_mode in ("CYCLIC", "IMMEDIATE") else "CYCLIC"

        simulation_type = analysis.get("simulation_type", "SINGLE_ECU_TRANSMIT")

        # Clear input messages for SINGLE_ECU_TRANSMIT
        if simulation_type == "SINGLE_ECU_TRANSMIT":
            input_messages = []
            input_signals = []
        # For GATEWAY with no input_messages but known output_signals, infer candidate input_messages
        elif simulation_type == "GATEWAY" and output_signals and not input_messages:
            # Find messages from other ECUs that carry any of the output_signals
            out_set = set(output_signals)
            inferred_inputs: List[str] = []
            for msg in dbc_ctx.messages:
                if msg.transmitter == target_ecu:
                    continue
                sig_names = {s.name for s in msg.signals}
                if out_set & sig_names:
                    inferred_inputs.append(msg.name)
            if inferred_inputs:
                input_messages = inferred_inputs[:1]
                input_signals = [s for s in output_signals if s in dbc_ctx.signal_info_map]

        # Pre-compute code snippets (reduces LLM errors)
        variable_declarations = build_variable_declarations(
            target_messages,
            input_signals,
            output_signals,
            signal_data_types,
            dbc_ctx,
            transmission_mode,
            simulation_type,
        )

        signal_initializations = build_signal_initializations(
            target_messages,
            output_signals,
            signal_data_types,
            dbc_ctx,
        )

        input_signal_reads = build_input_signal_reads(
            input_messages,
            input_signals,
            signal_data_types,
            dbc_ctx,
        )

        warnings = analysis.get("warnings", [])
        if not target_ecu:
            warnings.append("Could not determine target ECU from requirement")

        return SimulationAnalysis(
            raw_requirement=requirement,
            simulation_type=simulation_type,
            transmission_mode=transmission_mode,
            target_ecu=target_ecu,
            target_messages=target_messages,
            input_messages=input_messages,
            input_signals=input_signals,
            output_signals=output_signals,
            signal_values=analysis.get("signal_values", {}),
            cycle_time_overrides=analysis.get("cycle_time_overrides", {}),
            counter_signals=[],
            crc_signals=[],
            reactive_behaviors=analysis.get("reactive_behaviors", []),
            logic_description=analysis.get("logic_description", ""),
            pseudo_code=analysis.get("pseudo_code", "").replace("\\n", "\n"),
            warnings=warnings,
            signal_data_types=signal_data_types,
            signal_value_definitions=signal_value_definitions,
            byte_packing_required=byte_packing_required,
            capl_byte_access_style_hint=capl_byte_access_style_hint,
            dbc_bit_layout_text=dbc_bit_layout_text,
            byte_packing_snippets=byte_packing_snippets,
            variable_declarations=variable_declarations,
            signal_initializations=signal_initializations,
            input_signal_reads=input_signal_reads,
        )

    except Exception as e:
        st.error(f"Analysis Error: {str(e)}\nRaw Output:\n{raw_output}")
        raise


def build_variable_declarations(
    target_messages: List[str],
    input_signals: List[str],
    output_signals: List[str],
    signal_data_types: Dict[str, str],
    dbc_ctx: DBCContext,
    transmission_mode: str,
    simulation_type: str,
) -> str:
    """Build exact variable declarations for CAPL."""
    lines: List[str] = []

    # Message variables (only for output messages)
    for msg_name in target_messages:
        var_name = f"msg{msg_name.replace('_', '')}"
        lines.append(f"  message {msg_name} {var_name};")

    # Timer (only for CYCLIC mode)
    if transmission_mode == "CYCLIC":
        for msg_name in target_messages:
            timer_name = f"tm{msg_name.replace('_', '')}"
            lines.append(f"  msTimer {timer_name};")

    # Storage variables for input signals (only for GATEWAY/REACTIVE)
    if simulation_type in ["GATEWAY", "REACTIVE"] and input_signals:
        for sig_name in input_signals:
            dtype = signal_data_types.get(sig_name, "int")
            var_name = f"stored_{sig_name}"
            default = "0.0" if dtype == "float" else "0"
            lines.append(f"  {dtype} {var_name} = {default};")

    # Cycle time constant
    if transmission_mode == "CYCLIC" and target_messages:
        msg = dbc_ctx.message_info_map.get(target_messages[0])
        cycle_time = msg.cycle_time if msg and msg.cycle_time else 100
        lines.append(f"  const int cCycleTime = {cycle_time};")

    return "\n".join(lines)


def build_signal_initializations(
    target_messages: List[str],
    output_signals: List[str],
    signal_data_types: Dict[str, str],
    dbc_ctx: DBCContext,
) -> str:
    """Build exact signal initialization code."""
    lines: List[str] = []

    for msg_name in target_messages:
        var_name = f"msg{msg_name.replace('_', '')}"
        msg = dbc_ctx.message_info_map.get(msg_name)
        if msg:
            for sig in msg.signals:
                if sig.name in output_signals:
                    default = sig.default_value
                    lines.append(f"  {var_name}.{sig.name} = {default};")

    return "\n".join(lines)


def build_input_signal_reads(
    input_messages: List[str],
    input_signals: List[str],
    signal_data_types: Dict[str, str],
    dbc_ctx: DBCContext,
) -> str:
    """Build exact input signal read code."""
    lines: List[str] = []

    for sig_name in input_signals:
        var_name = f"stored_{sig_name}"
        lines.append(f"  {var_name} = this.{sig_name};")

    return "\n".join(lines)


def build_byte_packing_snippets(
    dbc_ctx: DBCContext,
    target_messages: List[str],
    output_signals: List[str],
) -> str:
    """
    Build deterministic, CAPL-oriented byte-packing snippets for each target message.
    These are fed into the CAPL prompt so the LLM can wrap them into handlers instead of
    inventing byte-level math.
    """
    lines: List[str] = []

    for msg_name in target_messages:
        msg = dbc_ctx.message_info_map.get(msg_name)
        if not msg:
            continue
        var_name = f"msg{msg_name.replace('_', '')}"
        dlc = msg.dlc

        lines.append(f"// BYTE PACKING FOR MESSAGE {msg_name} (DLC={dlc})")
        lines.append(f"// Clear payload bytes before packing")
        lines.append(f"for (i = 0; i < {dlc}; i++) {var_name}.byte(i) = 0;")
        lines.append("")

        for sig in msg.signals:
            if output_signals and sig.name not in output_signals:
                continue

            sb = sig.start_bit
            bl = sig.bit_length
            byte_idx = sb // 8
            bit_off = sb % 8
            scale = sig.scale
            offset = sig.offset

            phys_var = f"{sig.name}_phys"
            raw_var = f"raw_{sig.name}"

            lines.append(
                f"// {sig.name}: start_bit={sb}, len={bl}, order={sig.byte_order}, "
                f"scale={scale}, offset={offset}"
            )

            # Assign and clamp: no declarations here (CAPL allows declarations only in variables block)
            if scale == 1.0 and offset == 0.0:
                lines.append(f"{raw_var} = {phys_var};")
            else:
                lines.append(
                    f"{raw_var} = (int)(({phys_var} - {offset}) / {scale});"
                )
            lines.append(f"if ({phys_var} < 0) {raw_var} = 0;")

            # Simple cases we can express directly:
            # 1) 8-bit, byte-aligned
            if bl == 8 and bit_off == 0:
                lines.append(
                    f"{var_name}.byte({byte_idx}) = (byte)({raw_var} & 0xFF);"
                )
            # 2) 16-bit, byte-aligned (Intel)
            elif bl == 16 and bit_off == 0 and sig.byte_order == "little_endian":
                lines.append(
                    f"{var_name}.byte({byte_idx}) = (byte)({raw_var} & 0xFF);"
                )
                lines.append(
                    f"{var_name}.byte({byte_idx + 1}) = (byte)(({raw_var} >> 8) & 0xFF);"
                )
            # 3) Small bitfield within a single byte
            elif bl <= 8 and bit_off + bl <= 8:
                mask = (1 << bl) - 1
                lines.append(
                    f"{var_name}.byte({byte_idx}) = "
                    f"({var_name}.byte({byte_idx}) & (byte)~(0x{mask:X} << {bit_off})) | "
                    f"(byte)(({raw_var} & 0x{mask:X}) << {bit_off});"
                )
            else:
                # Fallback text for complex layouts – the LLM can refine this with the DBC layout
                lines.append(
                    f"// TODO: multi-byte or non-aligned packing for {sig.name} "
                    f"(start_bit={sb}, len={bl}) must follow DBC exactly."
                )

            lines.append("")

    return "\n".join(lines).strip() if lines else "(No byte packing snippets available.)"


def build_deterministic_capl_script(
    dbc_ctx: DBCContext,
    analysis: SimulationAnalysis,
) -> str:
    """
    Deterministic, byte-level CAPL generator used as a fallback when the LLM
    does not produce byte() + loop based payload handling.
    Uses:
      - analysis.variable_declarations for message/timer/cCycleTime
      - analysis.byte_packing_snippets for concrete byte operations
    """
    lines: List[str] = []

    # Header comment
    try:
        req_preview = analysis.raw_requirement.strip().replace("\n", " ")
        if len(req_preview) > 200:
            req_preview = req_preview[:197] + "..."
        lines.append("/*")
        lines.append("  Auto-generated CAPL simulation script (deterministic byte-level fallback)")
        lines.append(f"  Requirement: {req_preview}")
        lines.append(f"  Target ECU: {analysis.target_ecu or 'N/A'}")
        lines.append(f"  Target messages: {', '.join(analysis.target_messages) or 'N/A'}")
        lines.append(f"  Simulation type: {analysis.simulation_type} | Mode: {analysis.transmission_mode}")
        lines.append("*/")
        lines.append("")
    except Exception:
        pass

    # Variables block
    lines.append("variables")
    lines.append("{")
    if analysis.variable_declarations:
        lines.append(analysis.variable_declarations)

    # Loop index
    lines.append("  int i;")

    # Physical and raw variables for each output signal (declarations only in variables block)
    for sig_name in analysis.output_signals:
        sig_info = dbc_ctx.signal_info_map.get(sig_name)
        if not sig_info:
            continue
        capl_type = "float" if sig_info.requires_float else "int"
        default = "0.0" if capl_type == "float" else "0"
        lines.append(f"  {capl_type} {sig_name}_phys = {default};")
        lines.append(f"  int raw_{sig_name} = 0;")

    lines.append("}")
    lines.append("")

    # on start handler
    lines.append("on start")
    lines.append("{")
    lines.append('  log("Simulation started (deterministic byte-level).");')
    # Set message preconditions (DLC / FDF / BRS) for all target messages
    for msg_name in analysis.target_messages:
        msg_info = dbc_ctx.message_info_map.get(msg_name)
        if msg_info:
            var_name = f"msg{msg_name.replace('_', '')}"
            lines.append(f"  {var_name}.dlc = {msg_info.dlc};")
            # Default FDF/BRS to 0 unless project specifies otherwise
            lines.append(f"  {var_name}.FDF = 0;")
            lines.append(f"  {var_name}.BRS = 0;")
    if analysis.transmission_mode == "CYCLIC":
        # Start timers for all target messages (if any)
        for msg_name in analysis.target_messages:
            timer_name = f"tm{msg_name.replace('_', '')}"
            lines.append(f"  setTimer({timer_name}, cCycleTime);")
    lines.append("}")
    lines.append("")

    # Main handler: timer for CYCLIC, message for IMMEDIATE/REACTIVE
    if analysis.transmission_mode == "CYCLIC":
        if analysis.target_messages:
            # Use first target message timer as the primary trigger
            primary_msg = analysis.target_messages[0]
            timer_name = f"tm{primary_msg.replace('_', '')}"
            lines.append(f"on timer {timer_name}")
            lines.append("{")
            # Insert byte-level snippets (already contain for-loop + msg.byte operations)
            if analysis.byte_packing_snippets:
                for ln in analysis.byte_packing_snippets.splitlines():
                    lines.append(f"  {ln}")
            # Output all target messages after packing
            for msg_name in analysis.target_messages:
                var_name = f"msg{msg_name.replace('_', '')}"
                lines.append(f"  output({var_name});")
            lines.append(f"  setTimer({timer_name}, cCycleTime);")
            lines.append("}")
    else:
        # IMMEDIATE / REACTIVE style: use on message for first input or target
        in_msg = analysis.input_messages[0] if analysis.input_messages else (
            analysis.target_messages[0] if analysis.target_messages else "UNKNOWN_MESSAGE"
        )
        lines.append(f"on message {in_msg}")
        lines.append("{")
        # Read incoming bytes into a buffer to respect on message byte-read rule
        buf_name = f"input_{in_msg.replace('_', '')}"
        lines.append(f"  // Read incoming message bytes into buffer")
        lines.append(f"  for (i = 0; i < 8; i++) {buf_name}[i] = this.byte(i);")
        if analysis.byte_packing_snippets:
            for ln in analysis.byte_packing_snippets.splitlines():
                lines.append(f"  {ln}")
        for msg_name in analysis.target_messages:
            var_name = f"msg{msg_name.replace('_', '')}"
            lines.append(f"  output({var_name});")
        lines.append("}")

    return "\n".join(lines)


def build_deterministic_python_script(
    dbc_ctx: Optional[DBCContext],
    analysis: Optional[SimulationAnalysis],
    test_cases: Dict,
) -> str:
    """
    Deterministic Python pytest script generator used as a fallback when the LLM
    does not follow the required pytest structure or test count.
    Generates:
      - imports (can, pytest, time)
      - MESSAGE_ID from DBC (first target message) if available
      - can_bus fixture
      - collect_messages helper
      - simple decode_* helpers for each output signal (if DBC available)
      - one test function per test_case_id in test_cases["test_cases"]
    """
    lines: List[str] = []

    # Imports
    lines.append("import can")
    lines.append("import pytest")
    lines.append("import time")
    lines.append("")

    # MESSAGE_ID constant from first target message if available
    msg_id_hex = "0x0"
    if dbc_ctx and analysis and analysis.target_messages:
        msg = dbc_ctx.message_info_map.get(analysis.target_messages[0])
        if msg:
            msg_id_hex = f"0x{msg.frame_id:X}"
    lines.append(f"MESSAGE_ID = {msg_id_hex}")
    lines.append("")

    # CAN bus fixture
    lines.append("@pytest.fixture(scope=\"session\")")
    lines.append("def can_bus():")
    lines.append("    bus = can.Bus(interface=\"socketcan\", channel=\"can0\", bitrate=500000)")
    lines.append("    yield bus")
    lines.append("    bus.shutdown()")
    lines.append("")

    # collect_messages helper
    lines.append("def collect_messages(bus, arb_id, duration_sec):")
    lines.append("    start = time.time()")
    lines.append("    msgs = []")
    lines.append("    while time.time() - start < duration_sec:")
    lines.append("        msg = bus.recv(timeout=0.1)")
    lines.append("        if msg and msg.arbitration_id == arb_id:")
    lines.append("            msgs.append(msg)")
    lines.append("    return msgs")
    lines.append("")

    # decode_* helpers: simple stubs using msg.data[0] as placeholder if DBC is missing
    if dbc_ctx and analysis:
        created: set[str] = set()
        for sig_name in analysis.output_signals:
            if sig_name in created:
                continue
            func_name = f"decode_{sig_name.lower()}".replace("-", "_")
            created.add(sig_name)
            lines.append(f"def {func_name}(msg):")
            lines.append('    """Decode signal from CAN message (stub based on DBC)."""')
            # Use simple extraction; detailed bit math is already provided to CAPL, here we keep it minimal
            lines.append("    if msg is None or msg.data is None:")
            lines.append("        raise AssertionError('No message received')")
            lines.append("    return msg.data[0]")
            lines.append("")

    # One pytest test per test_case_id from generated test cases
    cases = test_cases.get("test_cases", []) or []
    for tc in cases:
        tc_id = tc.get("test_case_id") or tc.get("test_id") or "TC_XXX"
        name_safe = tc_id.lower()
        name_safe = re.sub(r"[^a-z0-9_]+", "_", name_safe)
        func_name = f"test_{name_safe}"
        objective = tc.get("test_objective", "").replace("\"", "'")

        lines.append(f"def {func_name}(can_bus):")
        if objective:
            lines.append(f"    # {objective}")
        lines.append("    msgs = collect_messages(can_bus, MESSAGE_ID, duration_sec=1.0)")
        lines.append("    assert len(msgs) > 0")
        lines.append("    # TODO: decode signals and assert expected values based on test case details")
        lines.append("")

    return "\n".join(lines)


CAPL_PROMPT_SINGLE_ECU = PromptTemplate(
    input_variables=[
        "requirement",
        "target_ecu",
        "target_message",
        "output_signals",
        "variable_declarations",
        "signal_initializations",
        "cycle_time",
        "signal_value_defs",
        "byte_packing_required",
        "capl_byte_access_style_hint",
        "dbc_bit_layout_text",
        "dbc_authority",
        "byte_packing_snippets",
        "capl_corrected_rules",
        "capl_examples",
    ],
    template='''You are generating a CAPL script for a SINGLE_ECU_TRANSMIT simulation for ECU {target_ecu}.

=============================================================================
DBC AUTHORITY – USE ONLY THESE NAMES (from DBC; do not invent or substitute)
=============================================================================
{dbc_authority}

=============================================================================
CORRECTED CAPL PROMPT (Primary – Authoritative, Must Follow)
=============================================================================
{capl_corrected_rules}

=============================================================================
REQUIREMENT
=============================================================================
{requirement}

=============================================================================
DBC BIT/BYTE LAYOUT (Authoritative)
=============================================================================
{dbc_bit_layout_text}

=============================================================================
BYTE-LEVEL REQUIREMENT (Must Follow)
=============================================================================
- Byte-packing required: {byte_packing_required} (This will always be True for this project.)
- Byte access style hint: {capl_byte_access_style_hint}
- You MUST:
  - Clear the outgoing message payload using a bounded for-loop over DLC bytes before packing.
  - Pack each required output signal into the payload using shifts/masks per the DBC start_bit/len/byte order and scale/offset.
  - Avoid infinite loops. Use only bounded loops (for/while with a clear upper bound).
  - Treat DBC layout as authoritative; do NOT rely solely on msg.Signal = ... as the final payload definition.
  - A CAPL script that does NOT contain both (1) at least one bounded loop and (2) explicit byte-level access (e.g. msg.byte(i) or msg.data[i]) is INCORRECT. Do NOT output such a script.

=============================================================================
MANDATORY BYTE-LEVEL SNIPPETS (DO NOT CHANGE, JUST USE)
=============================================================================
{byte_packing_snippets}

=============================================================================
OPTIONAL REFERENCE (RAG – Lower Priority, Use Only If Helpful)
=============================================================================
{capl_examples}

=============================================================================
MANDATORY STRUCTURE FOR THIS SIMULATION TYPE (maps to A: SINGLE_ECU_TRANSMIT + CYCLIC)
=============================================================================
- Use a single variables block.
- Declare ALL message/timer variables EXACTLY as in:
  {variable_declarations}
- In on start:
  - Write a log line that simulation started.
  - Initialize output signals EXACTLY as in:
    {signal_initializations}
  - Start a timer with period {cycle_time} ms.
- In on timer:
  - Call output(...) for the message.
  - Restart the timer.
- DO NOT create any on message handler.

=============================================================================
SIGNAL AND VALUE RULES
=============================================================================
- Output signals: {output_signals}
- Use these value definitions (numeric constants) when relevant:
{signal_value_defs}

=============================================================================
OUTPUT FORMAT
=============================================================================
Return ONLY valid CAPL code, no markdown fences and no explanations.
Ensure the code compiles and follows the structure above.''',
)


CAPL_PROMPT_GATEWAY = PromptTemplate(
    input_variables=[
        "requirement",
        "target_message",
        "input_message",
        "input_signals",
        "output_signals",
        "variable_declarations",
        "signal_initializations",
        "input_signal_reads",
        "cycle_time",
        "signal_data_types",
        "signal_value_defs",
        "byte_packing_required",
        "capl_byte_access_style_hint",
        "dbc_bit_layout_text",
        "dbc_authority",
        "byte_packing_snippets",
        "capl_corrected_rules",
        "capl_examples",
    ],
    template='''You are generating a CAPL GATEWAY simulation script.

=============================================================================
DBC AUTHORITY – USE ONLY THESE NAMES (from DBC; do not invent or substitute)
=============================================================================
{dbc_authority}

=============================================================================
CORRECTED CAPL PROMPT (Primary – Authoritative, Must Follow)
=============================================================================
{capl_corrected_rules}

=============================================================================
REQUIREMENT
=============================================================================
{requirement}

=============================================================================
DBC BIT/BYTE LAYOUT (Authoritative)
=============================================================================
{dbc_bit_layout_text}

=============================================================================
BYTE-LEVEL REQUIREMENT (Must Follow)
=============================================================================
- Byte-packing required: {byte_packing_required} (This will always be True for this project.)
- Byte access style hint: {capl_byte_access_style_hint}
- You MUST:
  - In on message {input_message}: parse incoming signals using bytes/bits as needed OR store raw bytes, but do NOT output() here (Gateway rule).
  - In on timer: clear the outgoing message payload using a bounded for-loop, then pack outgoing bytes with shifts/masks per DBC.
  - Use stored variables (e.g., stored_* or local decoded values) to hold the most recently received values.
  - Avoid infinite loops; only bounded loops allowed.
  - Treat DBC layout as authoritative; do NOT rely solely on msg.Signal = ... as the final payload definition.
  - A CAPL script that does NOT contain both (1) at least one bounded loop and (2) explicit byte-level access (e.g. msg.byte(i) or msg.data[i]) is INCORRECT. Do NOT output such a script.

=============================================================================
MANDATORY BYTE-LEVEL SNIPPETS (DO NOT CHANGE, JUST USE)
=============================================================================
{byte_packing_snippets}

=============================================================================
OPTIONAL REFERENCE (RAG – Lower Priority, Use Only If Helpful)
=============================================================================
{capl_examples}

=============================================================================
MANDATORY STRUCTURE (maps to C: GATEWAY + CYCLIC)
=============================================================================
- Use a variables block with:
  - Output message + timer variables.
  - Storage variables for input signals.
  Exactly as specified in:
  {variable_declarations}
- In on start:
  - Write a log that the gateway simulation started.
  - Initialize output signals as in:
    {signal_initializations}
  - Start the timer with period {cycle_time} ms.
- In on message {input_message}:
  - Read ALL required input signals exactly as in:
    {input_signal_reads}
  - Update the corresponding output message signals based on the stored values.
  - DO NOT call output(...) in this handler.
- In on timer:
  - Call output(...) for the target message.
  - Restart the timer.

=============================================================================
DATA TYPES AND VALUES
=============================================================================
- Data types (MUST respect these): {signal_data_types}
- Signal value definitions (use numeric constants): 
{signal_value_defs}

=============================================================================
OUTPUT FORMAT
=============================================================================
Return ONLY valid CAPL code, no markdown fences and no explanations.
Ensure the code compiles and strictly follows the structure above.''',
)


CAPL_PROMPT_IMMEDIATE = PromptTemplate(
    input_variables=[
        "requirement",
        "target_message",
        "input_message",
        "input_signals",
        "output_signals",
        "variable_declarations",
        "signal_initializations",
        "signal_data_types",
        "signal_value_defs",
        "byte_packing_required",
        "capl_byte_access_style_hint",
        "dbc_bit_layout_text",
        "dbc_authority",
        "byte_packing_snippets",
        "capl_corrected_rules",
        "capl_examples",
    ],
    template='''You are generating a CAPL script for IMMEDIATE transmission.

=============================================================================
DBC AUTHORITY – USE ONLY THESE NAMES (from DBC; do not invent or substitute)
=============================================================================
{dbc_authority}

=============================================================================
CORRECTED CAPL PROMPT (Primary – Authoritative, Must Follow)
=============================================================================
{capl_corrected_rules}

=============================================================================
REQUIREMENT
=============================================================================
{requirement}

=============================================================================
DBC BIT/BYTE LAYOUT (Authoritative)
=============================================================================
{dbc_bit_layout_text}

=============================================================================
BYTE-LEVEL REQUIREMENT (Must Follow)
=============================================================================
- Byte-packing required: {byte_packing_required} (This will always be True for this project.)
- Byte access style hint: {capl_byte_access_style_hint}
- You MUST:
  - Clear the outgoing message payload using a bounded for-loop over DLC bytes before packing.
  - Pack each required output signal into payload bytes using shifts/masks per DBC and scale/offset.
  - Avoid infinite loops; only bounded loops allowed.
  - Treat DBC layout as authoritative; do NOT rely solely on msg.Signal = ... as the final payload definition.
  - A CAPL script that does NOT contain both (1) at least one bounded loop and (2) explicit byte-level access (e.g. msg.byte(i) or msg.data[i]) is INCORRECT. Do NOT output such a script.

=============================================================================
MANDATORY BYTE-LEVEL SNIPPETS (DO NOT CHANGE, JUST USE)
=============================================================================
{byte_packing_snippets}

=============================================================================
OPTIONAL REFERENCE (RAG – Lower Priority, Use Only If Helpful)
=============================================================================
{capl_examples}

=============================================================================
MANDATORY STRUCTURE (maps to B: REACTIVE + IMMEDIATE)
=============================================================================
- variables block:
  - Declare ONLY the output message variable (NO timer).
  - Use exactly this declaration set:
    {variable_declarations}
- on start:
  - Log that the simulation started.
  - Initialize output signals exactly as in:
    {signal_initializations}
- on message {input_message}:
  - Read the relevant input signal(s).
  - Update the corresponding output signal(s) in the output message.
  - Call output(...) IMMEDIATELY in this handler.

FORBIDDEN:
- Any msTimer declarations.
- Any on timer handlers.
- Any output(...) calls outside the on message handler.

=============================================================================
DATA TYPES AND VALUE DEFINITIONS
=============================================================================
- Data types (MUST respect these): {signal_data_types}
- Signal value definitions (use numeric values): 
{signal_value_defs}

Example: If a signal has "3=HAZARD", compare using "== 3" rather than named enums.

=============================================================================
OUTPUT FORMAT
=============================================================================
Return ONLY valid CAPL code, no markdown fences and no explanations.
Ensure the code compiles and strictly follows the structure above.''',
)


def _add_section_comments_to_capl(capl: str) -> str:
    """
    Add section comments before each major CAPL block (variables, on start, on timer, on message, on key).
    Makes the generated script self-documenting at every stage.
    """
    if not capl or not capl.strip():
        return capl
    pattern = re.compile(
        r"^(\s*)(variables\s*\{|on\s+start\s*\{|on\s+timer\s+\w+\s*\{|on\s+message\s+[^{]+\{|on\s+key\s+[^{]+\{)",
        re.IGNORECASE | re.MULTILINE,
    )
    def _section_comment(block_start: str) -> str:
        bl = block_start.strip().lower()
        if bl.startswith("variables"):
            return "// ========== Variable declarations =========="
        if bl.startswith("on start"):
            return "// ========== Initialization (on start) =========="
        if bl.startswith("on timer"):
            return "// ========== Cyclic transmission (timer) =========="
        if bl.startswith("on message"):
            return "// ========== Reactive message handler =========="
        if bl.startswith("on key"):
            return "// ========== Key event handler =========="
        return "// ========== Block =========="

    def _repl(match):
        indent = match.group(1)
        block_start = match.group(2)
        comment = _section_comment(block_start)
        # Avoid duplicate if there is already a section comment on the previous line
        return indent + comment + "\n" + indent + block_start

    return pattern.sub(_repl, capl)


def clean_capl_output(raw_output: str) -> str:
    """Clean LLM CAPL output."""
    output = raw_output.strip()
    output = re.sub(r"```capl\s*\n?", "", output)
    output = re.sub(r"```c\s*\n?", "", output)
    output = re.sub(r"```\s*\n?", "", output)

    lines = output.split("\n")
    start_idx = 0
    for i, line in enumerate(lines):
        if (
            line.strip().startswith("/*")
            or line.strip().startswith("//")
            or "variables" in line.lower()
        ):
            start_idx = i
            break

    if start_idx > 0:
        output = "\n".join(lines[start_idx:])

    lines = output.split("\n")
    last_brace_idx = len(lines) - 1
    for i in range(len(lines) - 1, -1, -1):
        if "}" in lines[i]:
            last_brace_idx = i
            break

    output = "\n".join(lines[: last_brace_idx + 1])
    return output.strip()


def verify_capl_structure(
    capl_script: str, analysis: SimulationAnalysis, dbc_ctx: DBCContext
) -> Tuple[bool, List[str]]:
    """Verify generated CAPL matches expected structure."""
    issues: List[str] = []

    # Check ECU ownership
    for msg_name in analysis.target_messages:
        msg = dbc_ctx.message_info_map.get(msg_name)
        if msg and analysis.target_ecu:
            if msg.transmitter != analysis.target_ecu:
                issues.append(
                    f"ECU MISMATCH: {msg_name} is transmitted by {msg.transmitter}, not {analysis.target_ecu}"
                )

    # Check handler structure
    has_on_timer = bool(re.search(r"on\s+timer\s+\w+", capl_script))
    has_on_message = bool(re.search(r"on\s+message\s+\w+", capl_script))
    output_in_timer = bool(
        re.search(r"on\s+timer[^}]*output\s*\([^}]*\}", capl_script, re.DOTALL)
    )
    output_in_message = bool(
        re.search(r"on\s+message[^}]*output\s*\([^}]*\}", capl_script, re.DOTALL)
    )

    if analysis.simulation_type == "SINGLE_ECU_TRANSMIT":
        if has_on_message:
            issues.append(
                "STRUCTURE ERROR: SINGLE_ECU_TRANSMIT should NOT have on message handler"
            )
        if not has_on_timer:
            issues.append(
                "STRUCTURE ERROR: SINGLE_ECU_TRANSMIT should have on timer handler"
            )
        if not output_in_timer:
            issues.append(
                "STRUCTURE ERROR: output() should be in on timer handler"
            )

    elif analysis.transmission_mode == "IMMEDIATE":
        if has_on_timer:
            issues.append(
                "STRUCTURE ERROR: IMMEDIATE mode should NOT have on timer handler"
            )
        if not output_in_message:
            issues.append(
                "STRUCTURE ERROR: IMMEDIATE mode must have output() in on message handler"
            )

    elif analysis.transmission_mode == "CYCLIC":
        if output_in_message:
            issues.append(
                "STRUCTURE ERROR: CYCLIC mode should NOT have output() in on message handler"
            )
        if not output_in_timer:
            issues.append(
                "STRUCTURE ERROR: CYCLIC mode must have output() in on timer handler"
            )

    # Check data types
    for sig_name, expected_type in analysis.signal_data_types.items():
        if expected_type == "float":
            pattern = rf"\bint\s+\w*{sig_name}\w*\s*[=;]"
            if re.search(pattern, capl_script, re.IGNORECASE):
                issues.append(
                    f"DATA TYPE ERROR: {sig_name} should use float (scale != 1), not int"
                )

    # Byte/loop checks (warnings only, but surfaced to user)
    if analysis.byte_packing_required:
        # Look for any byte-level access patterns (common CAPL styles)
        has_byte_ops = bool(
            re.search(r"\b(?:msg|this)\s*\.\s*(?:byte|data)\s*\(", capl_script, re.IGNORECASE)
            or re.search(r"\b(?:msg|this)\s*\.\s*data\s*\[", capl_script, re.IGNORECASE)
            or re.search(r"\bbyte\s*\(", capl_script, re.IGNORECASE)
        )
        if not has_byte_ops:
            issues.append(
                "BYTE PACKING WARNING: Byte-level packing was required by DBC/requirement, but no byte() / data[] access was found."
            )

        has_bounded_loop = bool(re.search(r"\bfor\s*\(", capl_script) or re.search(r"\bwhile\s*\(", capl_script))
        if not has_bounded_loop:
            issues.append(
                "LOOP WARNING: Byte-level packing typically needs a bounded loop (e.g., clearing DLC bytes). No loop was found."
            )

        if re.search(r"\bwhile\s*\(\s*1\s*\)", capl_script):
            issues.append("FORBIDDEN LOOP: Found while(1) which can block CAPL execution.")

    # Check message names match DBC
    for msg_name in analysis.target_messages:
        if msg_name not in capl_script:
            issues.append(f"MESSAGE ERROR: {msg_name} not found in generated code")

    # Check for message/signal names in script that are not in DBC (alignment)
    allowed_messages = set(analysis.target_messages or []) | set(analysis.input_messages or [])
    allowed_signals = set(analysis.output_signals or []) | set(analysis.input_signals or [])
    # Common CAPL patterns: on message MsgName, message MsgName var, var.SignalName, this.SignalName
    for msg_name in allowed_messages:
        if msg_name not in dbc_ctx.message_info_map:
            continue
        for sig in dbc_ctx.message_info_map[msg_name].signals:
            allowed_signals.add(sig.name)
    # Detect message names in script that look like CAN messages (e.g. GW_Response, EngineSpeed)
    on_message_match = re.findall(r"on\s+message\s+(\w+)", capl_script, re.IGNORECASE)
    message_decl_match = re.findall(r"message\s+(\w+)\s+\w+", capl_script)
    script_messages = set(on_message_match) | set(message_decl_match)
    for name in script_messages:
        # Normalize: CAPL may use without underscores
        if name not in allowed_messages and not any(
            name.replace("_", "") == m.replace("_", "") for m in allowed_messages
        ):
            if name not in ("this",):
                issues.append(
                    f"DBC ALIGNMENT: Message '{name}' in script is not in DBC authority (input/output messages). Use only: {', '.join(sorted(allowed_messages))}."
                )
    # Signal references: msgVar.SignalName or this.SignalName
    signal_refs = re.findall(r"(?:this|msg\w+)\.(\w+)\b", capl_script)
    for sig in signal_refs:
        if sig not in allowed_signals and not any(
            sig.replace("_", "") == s.replace("_", "") for s in allowed_signals
        ):
            if sig.lower() not in ("byte", "data", "dlc"):
                issues.append(
                    f"DBC ALIGNMENT: Signal '{sig}' in script may not be in DBC. Allowed output/input signals: {', '.join(sorted(allowed_signals))}."
                )

    return len(issues) == 0, issues


def _stream_or_invoke_chain(
    chain: Any,
    inputs: Dict,
    stream_container: Optional[DeltaGenerator],
    stream_content_type: Optional[str] = None,  # "capl" or "python" to strip preamble/suffix during streaming
) -> str:
    """Run chain with streaming to container if provided, else invoke. Returns full output.
    When streaming, strips preamble/suffix if stream_content_type is set, and clears placeholder when done."""
    if stream_container is not None:
        placeholder = stream_container.empty()
        accumulated: List[str] = []
        for chunk in chain.stream(inputs):
            accumulated.append(chunk)
            display_text = "".join(accumulated)
            if stream_content_type:
                display_text = _strip_streaming_preview(display_text, stream_content_type)
            placeholder.code(display_text, language="c" if stream_content_type == "capl" else "python")
        output = "".join(accumulated)
        placeholder.empty()  # Clear streamed content so _render shows only final output
        return output
    return chain.invoke(inputs)


def generate_simulation_capl(
    dbc_ctx: DBCContext,
    analysis: SimulationAnalysis,
    model_choice: str = "Ollama",
    rag_store: Optional[RAGVectorStore] = None,
    capl_examples: Optional[str] = None,  # RAG-retrieved examples; secondary/supplementary to Corrected prompt
    stream_container: Optional[DeltaGenerator] = None,
) -> str:
    """Generate CAPL using Corrected CAPL Prompt as primary rules, RAG examples as optional reference. Optionally stream output."""

    cycle_time = 100
    if analysis.target_messages:
        msg = dbc_ctx.message_info_map.get(analysis.target_messages[0])
        if msg and msg.cycle_time:
            cycle_time = msg.cycle_time

    # Use Ollama LLM
    llm = get_ollama_llm(temperature=0.05, max_tokens=3000, task_type="capl")

    # Build signal value definitions string
    signal_value_defs = ""
    for sig_name, values in analysis.signal_value_definitions.items():
        if values:
            val_str = ", ".join([f"{k}={v}" for k, v in values.items()])
            signal_value_defs += f"{sig_name}: {val_str}\n"
    if not signal_value_defs:
        signal_value_defs = "No predefined values"

    # Format RAG examples (optional reference, lower priority than Corrected prompt)
    if not capl_examples or not capl_examples.strip():
        capl_examples = "(No RAG examples – follow Corrected prompt rules only.)"
    else:
        capl_examples = f"""Reference only; Corrected prompt rules take precedence.
{capl_examples}"""

    # Load Corrected CAPL Prompt (primary rules)
    capl_corrected_rules = _load_corrected_capl_prompt()
    if capl_corrected_rules.strip():
        capl_corrected_rules = _truncate_to_chars(capl_corrected_rules.strip(), _TRUNCATE_CAPL_RULES_CHARS)
    else:
        capl_corrected_rules = "(Corrected CAPL Prompt file not found – follow standard CAPL conventions.)"

    target_msg_clean = (
        analysis.target_messages[0].replace("_", "")
        if analysis.target_messages
        else ""
    )
    input_msg = analysis.input_messages[0] if analysis.input_messages else ""
    requirement_for_prompt = _truncate_to_chars(analysis.raw_requirement, _TRUNCATE_REQUIREMENT_CHARS)
    dbc_authority = _build_dbc_authority_text(dbc_ctx, analysis)

    if analysis.simulation_type == "SINGLE_ECU_TRANSMIT":
        prompt = CAPL_PROMPT_SINGLE_ECU
        chain = prompt | llm | StrOutputParser()
        raw_output = _stream_or_invoke_chain(
            chain,
            {
                "requirement": requirement_for_prompt,
                "target_ecu": analysis.target_ecu,
                "target_message": target_msg_clean,
                "output_signals": ", ".join(analysis.output_signals),
                "variable_declarations": analysis.variable_declarations,
                "signal_initializations": analysis.signal_initializations,
                "cycle_time": cycle_time,
                "signal_value_defs": signal_value_defs,
                "byte_packing_required": str(bool(analysis.byte_packing_required)),
                "capl_byte_access_style_hint": analysis.capl_byte_access_style_hint,
                "dbc_bit_layout_text": analysis.dbc_bit_layout_text,
                "dbc_authority": dbc_authority,
                "byte_packing_snippets": analysis.byte_packing_snippets,
                "capl_corrected_rules": capl_corrected_rules,
                "capl_examples": capl_examples,
            },
            stream_container,
            stream_content_type="capl",
        )

    elif analysis.transmission_mode == "IMMEDIATE":
        prompt = CAPL_PROMPT_IMMEDIATE
        chain = prompt | llm | StrOutputParser()
        raw_output = _stream_or_invoke_chain(
            chain,
            {
                "requirement": requirement_for_prompt,
                "target_message": target_msg_clean,
                "input_message": input_msg,
                "input_signals": ", ".join(analysis.input_signals),
                "output_signals": ", ".join(analysis.output_signals),
                "variable_declarations": analysis.variable_declarations,
                "signal_initializations": analysis.signal_initializations,
                "signal_data_types": json.dumps(analysis.signal_data_types),
                "signal_value_defs": signal_value_defs,
                "byte_packing_required": str(bool(analysis.byte_packing_required)),
                "capl_byte_access_style_hint": analysis.capl_byte_access_style_hint,
                "dbc_bit_layout_text": analysis.dbc_bit_layout_text,
                "dbc_authority": dbc_authority,
                "byte_packing_snippets": analysis.byte_packing_snippets,
                "capl_corrected_rules": capl_corrected_rules,
                "capl_examples": capl_examples,
            },
            stream_container,
            stream_content_type="capl",
        )

    else:  # GATEWAY
        prompt = CAPL_PROMPT_GATEWAY
        chain = prompt | llm | StrOutputParser()
        raw_output = _stream_or_invoke_chain(
            chain,
            {
                "requirement": requirement_for_prompt,
                "target_message": target_msg_clean,
                "input_message": input_msg,
                "input_signals": ", ".join(analysis.input_signals),
                "output_signals": ", ".join(analysis.output_signals),
                "variable_declarations": analysis.variable_declarations,
                "signal_initializations": analysis.signal_initializations,
                "input_signal_reads": analysis.input_signal_reads,
                "cycle_time": cycle_time,
                "signal_data_types": json.dumps(analysis.signal_data_types),
                "signal_value_defs": signal_value_defs,
                "byte_packing_required": str(bool(analysis.byte_packing_required)),
                "capl_byte_access_style_hint": analysis.capl_byte_access_style_hint,
                "dbc_bit_layout_text": analysis.dbc_bit_layout_text,
                "dbc_authority": dbc_authority,
                "byte_packing_snippets": analysis.byte_packing_snippets,
                "capl_corrected_rules": capl_corrected_rules,
                "capl_examples": capl_examples,
            },
            stream_container,
            stream_content_type="capl",
        )

    cleaned = clean_capl_output(raw_output)

    # Add section comments before each major block (variables, on start, on timer, on message, etc.)
    cleaned = _add_section_comments_to_capl(cleaned)

    # Add a descriptive header comment to make the CAPL easier to understand
    try:
        req_preview = analysis.raw_requirement.strip().replace("\n", " ")
        if len(req_preview) > 200:
            req_preview = req_preview[:197] + "..."
        header_lines = [
            "/*",
            "  Auto-generated CAPL simulation script",
            f"  Requirement: {req_preview}",
            f"  Target ECU: {analysis.target_ecu or 'N/A'}",
            f"  Target messages: {', '.join(analysis.target_messages) or 'N/A'}",
            f"  Simulation type: {analysis.simulation_type} | Mode: {analysis.transmission_mode}",
            "*/",
            "",
        ]
        cleaned_with_header = "\n".join(header_lines) + cleaned.lstrip()
    except Exception:
        cleaned_with_header = cleaned

    # Post-generation verification
    _, issues = verify_capl_structure(cleaned_with_header, analysis, dbc_ctx)

    # If byte-level handling is required but missing, fall back to deterministic generator
    if analysis.byte_packing_required:
        has_byte_warning = any("BYTE PACKING WARNING" in msg for msg in issues)
        has_loop_warning = any("LOOP WARNING" in msg for msg in issues)
        if has_byte_warning or has_loop_warning:
            print(
                "[DEBUG] generate_simulation_capl: Falling back to deterministic "
                "byte-level CAPL generator due to missing byte()/loop in LLM output."
            )
            fallback_script = build_deterministic_capl_script(dbc_ctx, analysis)
            return fallback_script

    # For other issues, surface as warnings but still return LLM-based script
    if issues:
        st.warning(f"CAPL verification issues: {', '.join(issues)}")

    return cleaned_with_header


# =============================================================================
# GENERATION FUNCTIONS (TEST CASES + PYTHON)
# =============================================================================

def generate_test_cases(
    requirement: str,
    dbc_ctx: DBCContext,
    rag_store: Optional[ExtendedRAGVectorStore] = None,
    requirement_id: Optional[str] = None,
    retrieved_log: Optional[List[str]] = None,
    stream_container: Optional[DeltaGenerator] = None,
) -> Dict:
    """Generate up to 5 test cases from requirement using a structured prompt (no RAG dependency).
    Covers positive, negative, and edge scenarios. Optionally stream raw LLM output to stream_container.
    """
    dbc_summary = _truncate_to_chars(dbc_ctx.raw_dbc_summary, _TRUNCATE_DBC_SUMMARY_CHARS)
    requirement_for_prompt = _truncate_to_chars(requirement, _TRUNCATE_REQUIREMENT_CHARS)
    llm = get_ollama_llm(temperature=0.1, max_tokens=3000, stop=['"test_id": "TC_006"'])
    chain = TEST_CASE_GENERATION_PROMPT | llm | StrOutputParser()

    stream_placeholder = None
    if stream_container is not None:
        print("[DEBUG] generate_test_cases: Streaming LLM output...")
        stream_placeholder = stream_container.empty()
        accumulated: List[str] = []
        for chunk in chain.stream({
            "requirement": requirement_for_prompt,
            "dbc_summary": dbc_summary,
        }):
            accumulated.append(chunk)
            stream_placeholder.code("".join(accumulated), language="json")
        raw_output = "".join(accumulated)
    else:
        print("[DEBUG] generate_test_cases: Invoking LLM...")
        raw_output = chain.invoke({
            "requirement": requirement_for_prompt,
            "dbc_summary": dbc_summary,
        })
    print(f"[DEBUG] generate_test_cases: LLM returned {len(raw_output)} chars")
    
    # Parse JSON from output
    try:
        # First, try to parse the entire raw_output as JSON (supports both array and object forms)
        json_str = raw_output.strip()
        # Remove only control chars invalid in JSON; keep tab (0x09), newline (0x0a), carriage return (0x0d)
        json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', json_str)
        # Remove JavaScript-style comments FIRST (invalid in JSON; LLM often adds // or /* */)
        json_str = re.sub(r'//[^\n]*', '', json_str)
        json_str = re.sub(r'/\*[\s\S]*?\*/', '', json_str)
        # Remove trailing commas before ] or } (invalid in strict JSON)
        json_str = re.sub(r',(\s*])', r'\1', json_str)
        json_str = re.sub(r',(\s*})', r'\1', json_str)

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            # Fallback to legacy object-based extraction if full parse fails
            json_match = re.search(r'\{[\s\S]*\}', json_str)
            if not json_match:
                print("[DEBUG] generate_test_cases: No JSON block found in output")
                st.error("No JSON found in test case generation response")
                if stream_placeholder is not None:
                    stream_placeholder.empty()
                return {"test_suite": "", "test_cases": []}
            json_obj_str = json_match.group()
            test_cases = json.loads(json_obj_str)
        else:
            # parsed successfully
            if isinstance(parsed, list):
                # New format: array of test case objects
                test_cases = {"test_suite": "", "test_cases": parsed}
            elif isinstance(parsed, dict):
                test_cases = parsed
            else:
                print("[DEBUG] generate_test_cases: Parsed JSON is neither list nor object")
                st.error("Unexpected JSON structure in test case generation response")
                if stream_placeholder is not None:
                    stream_placeholder.empty()
                return {"test_suite": "", "test_cases": []}

        # ------------------------------------------------------------------
        # Post-process: de-duplicate then cap at 5.
        # ------------------------------------------------------------------
        cases = test_cases.get("test_cases", []) or test_cases or []
        MAX_CASES = 5
        print(f"[DEBUG] generate_test_cases: Parsed {len(cases)} test cases, capping at {MAX_CASES}")
        if len(cases) > MAX_CASES:
            cases = cases[:MAX_CASES]
        seen_keys = set()
        deduped: List[Dict] = []
        for tc in cases:
            # Support both old and new schemas
            name = str(
                tc.get("name")
                or tc.get("test_objective", "")
            ).strip().lower()
            ttype = str(
                tc.get("type")
                or tc.get("test_case_type", "")
            ).strip().lower()
            exp_val = tc.get("expected_result")
            if not exp_val:
                # expected_output may be a list of strings in new schema
                exp_list = tc.get("expected_output") or []
                if isinstance(exp_list, list):
                    exp_val = " ".join(str(e) for e in exp_list)
                else:
                    exp_val = str(exp_list or "")
            exp = str(exp_val).strip().lower()
            key = (name, ttype, exp)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(tc)
        deduped = deduped[:MAX_CASES]

        test_cases["test_cases"] = deduped
        print(f"[DEBUG] generate_test_cases: Success, returning {len(deduped)} test cases")
        # Clear streamed content so _render shows only final output
        if stream_placeholder is not None:
            stream_placeholder.empty()
        return test_cases

    except Exception as e:
        print(f"[DEBUG] generate_test_cases: PARSE FAILED - {type(e).__name__}: {e}")
        st.error(f"Error parsing test cases: {str(e)}")
        st.text("Raw output:")
        st.text(raw_output)
        if stream_placeholder is not None:
            stream_placeholder.empty()
        return {"test_suite": "", "test_cases": []}


def generate_python_script(
    requirement: str,
    test_cases: Dict,
    rag_store: ExtendedRAGVectorStore,
    requirement_id: str,
    retrieved_log: Optional[List[str]] = None,
    dbc_ctx: Optional[DBCContext] = None,
    analysis: Optional[SimulationAnalysis] = None,
    capl_script: Optional[str] = None,
    stream_container: Optional[DeltaGenerator] = None,
) -> str:
    """Generate Python test script from test cases (and optionally CAPL). Optionally stream output to stream_container."""
    llm = get_ollama_llm(temperature=0.1, max_tokens=3000)

    # Build DBC-derived setup info (message IDs, decode functions) - DISABLED
    python_setup_info = "(Python script generation disabled - setup info not available)"
    # python_setup_info = _build_python_test_setup_info(dbc_ctx, analysis)
    if not python_setup_info:
        python_setup_info = "(No DBC/analysis available - derive message IDs and decode logic from test cases and CAPL)"
    python_setup_info = _truncate_to_chars(python_setup_info, _TRUNCATE_PYTHON_SETUP_CHARS)

    # Retrieve Python examples (RAG – secondary to Corrected prompt)
    try:
        _, linked_req_id, _ = get_similar_requirement_and_linked_ids(rag_store, requirement, req_k=1)
        python_results = RetrievalResult(chunks=[], scores=[], context_text="")
        if linked_req_id:
            try:
                python_results = rag_store.retrieve(
                    requirement, top_k=2, source_filter="python",
                    metadata_filter={"requirement_id": linked_req_id},
                )
            except TypeError:
                pass
        if python_results.chunks:
            rag_context_raw = "\n\n".join([
                f"--- Example {i+1} ---\n{chunk.content}"
                for i, chunk in enumerate(python_results.chunks)
            ])
        else:
            rag_context_raw = ""
    except Exception:
        rag_context_raw = ""
    rag_context_raw = _truncate_to_chars(rag_context_raw, _TRUNCATE_RAG_CONTEXT_CHARS)
    if retrieved_log is not None:
        retrieved_log.append(f"[python] RAG context:\n{rag_context_raw}")

    # Format RAG as optional/secondary (Corrected prompt is primary)
    if not rag_context_raw or not rag_context_raw.strip():
        rag_context = "(No RAG examples – follow Corrected prompt rules only.)"
    else:
        rag_context = f"Reference only; Corrected prompt rules take precedence.\n{rag_context_raw}"

    capl_script_truncated = (
        _truncate_to_chars(capl_script, _TRUNCATE_CAPL_SCRIPT_CHARS)
        if capl_script
        else "(Not available - generate based on requirement, test cases, DBC setup, and python_setup_info above.)"
    )
    requirement_for_prompt = _truncate_to_chars(requirement, _TRUNCATE_REQUIREMENT_CHARS)
    
    # Count test cases and ensure all are included (no truncation should cut them off)
    test_cases_list = test_cases.get("test_cases", []) or []
    num_test_cases = len(test_cases_list)
    test_cases_json = json.dumps(test_cases, indent=2)
    # Only truncate if absolutely necessary (shouldn't happen with 5 test cases)
    if len(test_cases_json) > _TRUNCATE_TEST_CASES_JSON_CHARS:
        test_cases_json = _truncate_to_chars(test_cases_json, _TRUNCATE_TEST_CASES_JSON_CHARS)
        st.warning(f"Test cases JSON truncated - some test cases may be missing. Original length: {len(json.dumps(test_cases, indent=2))} chars")
    else:
        # All test cases are included
        pass

    # PYTHON SCRIPT GENERATION DISABLED - Return empty string
    return ""


# PYTHON SCRIPT GENERATION DISABLED - generate_python_script function ended

def _build_test_scenario_markdown(test_cases: Dict) -> str:
    """Convert generated test_cases dict into a simple markdown test scenario for evaluation logging."""
    cases = test_cases.get("test_cases", []) or test_cases or []
    if not cases:
        return ""
    lines: List[str] = []
    lines.append("# Generated Test Scenarios")
    for i, tc in enumerate(cases, start=1):
        tc_id = tc.get("test_case_id") or tc.get("test_id") or f"TC_{i:03d}"
        name = tc.get("name") or tc.get("test_objective", "")
        lines.append(f"\n## {tc_id}: {name}")
        desc = tc.get("description") or tc.get("summary") or tc.get("test_objective") or ""
        if desc:
            lines.append(f"\n**Description**: {desc}")
        pre = tc.get("precondition") or tc.get("preconditions") or tc.get("pre_conditions")
        if pre:
            lines.append(f"\n**Preconditions**: {pre}")
        steps = tc.get("steps") or tc.get("test_steps") or []
        if steps:
            lines.append("\n**Steps:**")
            for step in steps:
                lines.append(f"- {step}")
        exp = tc.get("expected_result") or tc.get("expected") or tc.get("expected_output")
        if exp:
            lines.append(f"\n**Expected Result**: {exp}")
    return "\n".join(lines)


def _record_evaluation_run(
    requirement: str,
    dbc_ctx: DBCContext,
    analysis: SimulationAnalysis,
    test_cases: Dict,
    capl_script: str,
    python_script: str,
    retrieved_contexts: List[str],
) -> None:
    """
    Build one evaluation entry in the Validation_dataset_format style and append it.
    Ground truth fields are left empty for later human/automatic evaluation.
    """
    try:
        test_case_id = _next_eval_test_case_id()
        # Map simulation_type to category
        sim_type = (analysis.simulation_type or "").upper()
        transmission_mode = (analysis.transmission_mode or "").upper()
        if sim_type == "SINGLE_ECU_TRANSMIT":
            category = "single_ecu_transmit"
        elif sim_type == "GATEWAY":
            category = "gateway_transform"
        elif sim_type == "REACTIVE":
            category = "reactive_behavior"
        else:
            # Fallback based on mode
            category = "reactive_behavior" if transmission_mode == "IMMEDIATE" else "single_ecu_transmit"

        description = (requirement or "").strip()
        if len(description) > 160:
            description = description[:157] + "..."

        entry = {
            "test_case_id": test_case_id,
            "category": category,
            "description": description,
            "difficulty": "medium",
            "user_input": {
                "requirement": requirement,
                "dbc_content": dbc_ctx.raw_dbc_summary,
            },
            # Ground truth intentionally left empty – to be filled later
            "ground_truth": {
                "capl_code": "",
                "python_code": "",
                "test_scenario": "",
            },
            "retrieved_contexts": retrieved_contexts or [],
            "expected_answer_type": {
                "capl_code": "capl_simulation_script",
                "python_code": "pytest_test_module",
                "test_scenario": "markdown_scenario",
            },
            "response": {
                "capl_code": capl_script,
                "python_code": python_script,
                "test_scenario": _build_test_scenario_markdown(test_cases),
            },
        }
        _append_eval_entry(entry)
    except Exception as e:
        _log_debug(f"Failed to record evaluation run: {e}")

# =============================================================================
# STORAGE FUNCTIONS
# =============================================================================

def generate_requirement_id(requirement: str) -> str:
    """Generate unique requirement ID."""
    req_hash = hashlib.md5(requirement.encode()).hexdigest()[:8]
    return f"req_{req_hash}_{int(time.time())}"

def generate_dbc_id(dbc_summary: str) -> str:
    """Generate unique DBC ID."""
    dbc_hash = hashlib.md5(dbc_summary.encode()).hexdigest()[:8]
    return f"dbc_{dbc_hash}"


def load_data_v1_into_rag(rag_store: ExtendedRAGVectorStore, data_v1_path: Optional[str] = None) -> int:
    """
    Clear the vector DB and cache, then load Data_v* folders (CAPL_script + Python_Script) into RAG in one go.
    - If data_v1_path is None: discover all Data_v1, Data_v2, Data_v3, ... under the app directory and load each (one combined load).
    - If data_v1_path is provided: load only that single folder (e.g. "Data_v1" for backward compatibility).
    Returns the total number of chunks loaded.
    """
    parent_dir = Path(__file__).parent
    if data_v1_path is not None:
        folders_to_load = [data_v1_path]
    else:
        folders_to_load = _discover_data_v_folders(parent_dir)
        if not folders_to_load:
            folders_to_load = [DATA_V1_PATH]

    rag_store.clear()
    total_count = 0
    print(f"[DEBUG] load_data_v1_into_rag: folders_to_load={folders_to_load}")
    for folder_name in folders_to_load:
        base = parent_dir / folder_name
        if base.is_dir():
            n = _load_one_data_folder_into_rag(rag_store, base)
            print(f"[DEBUG] load_data_v1_into_rag: {folder_name} -> {n} chunks")
            total_count += n
        else:
            print(f"[DEBUG] load_data_v1_into_rag: {folder_name} NOT FOUND or not a dir")
    print(f"[DEBUG] load_data_v1_into_rag: total loaded {total_count} chunks")
    return total_count


# =============================================================================
# STREAMLIT APP
# =============================================================================

# Process-global RAG store cache so we reuse the same store across session reruns
# (e.g. after browser refresh session_state is cleared but process is the same).
# This avoids creating a second Qdrant client/store in the same process.
_global_rag_store: Optional[ExtendedRAGVectorStore] = None
_global_rag_loaded_count: int = 0


def main():
    global _global_rag_store, _global_rag_loaded_count

    st.set_page_config(page_title="AI Powered ECU Testing", layout="wide")
    
    st.title("AI Powered ECU Testing System")
    st.markdown("**Input DBC + Requirement → Generate Test Cases → CAPL**")

    # Initialize RAG vector store: reuse from session, or from process cache (after refresh), or create once
    if "rag_store" in st.session_state:
        rag_store = st.session_state["rag_store"]
        n_loaded = st.session_state.get("rag_data_v1_loaded", 0)
    elif _global_rag_store is not None:
        # Reuse store from previous session (e.g. browser refresh cleared session_state)
        rag_store = _global_rag_store
        n_loaded = _global_rag_loaded_count
        st.session_state["rag_store"] = rag_store
        st.session_state["rag_data_v1_loaded"] = n_loaded
    else:
        with st.spinner("Loading..."):
            rag_store = ExtendedRAGVectorStore(cache_dir="./rag_cache", path="./qdrant_data")
            stats = rag_store.get_stats()
            n_loaded = stats["total_documents"]
            if n_loaded == 0:
                n_loaded = load_data_v1_into_rag(rag_store)
            st.session_state["rag_store"] = rag_store
            st.session_state["rag_data_v1_loaded"] = n_loaded
            _global_rag_store = rag_store
            _global_rag_loaded_count = n_loaded

    rag_store = st.session_state["rag_store"]
    for _key in ("test_cases_result", "capl_script_result", "python_script_result"):
        if _key not in st.session_state:
            st.session_state[_key] = None

    # Ensure Ollama is configured and accessible
    base_url = _get_ollama_base_url()
    model = _get_ollama_model(base_url)
    
    _log_debug(f"main: Using Ollama at {base_url} with model {model}")
    print(f"[DEBUG] main: Using Ollama at {base_url} with model {model}")
    
    is_gpu = base_url == OLLAMA_BASE_URL_GPU
    st.session_state["llm_provider"] = "ollama_gpu" if is_gpu else "ollama_local"
    st.session_state["llm_model"] = model
    st.session_state["ollama_base_url"] = base_url

    st.markdown("---")
    
    # Input Section: DBC and Requirement upload side by side
    st.subheader("Inputs")
    col1, col2 = st.columns(2)
    
    with col1:
        dbc_file = st.file_uploader("Upload DBC File", type=["dbc"])
    
    with col2:
        req_file = st.file_uploader("Upload Requirement", type=["pdf", "txt", "xlsx", "xls"])
    
    dbc_ctx = None
    if dbc_file:
        try:
            dbc_ctx = parse_dbc_file(dbc_file.read())
        except Exception as e:
            st.error(f"DBC Error: {str(e)}")
    
    st.markdown("---")

    # Resolve raw requirement text (from uploaded file only)
    raw_requirement_text: str = ""
    if req_file is not None:
        try:
            if req_file.name.endswith('.txt'):
                # For .txt files, treat each line as a separate requirement
                raw_requirement_text = req_file.read().decode('utf-8')
            else:
                raw_requirement_text = parse_requirement_file(req_file.read(), req_file.name)
        except Exception as e:
            st.error(f"Requirement file error: {str(e)}")

    # Parse into individual requirements
    requirements_list: List[str] = []
    if raw_requirement_text.strip():
        if req_file and req_file.name.endswith('.txt'):
            # For .txt files, split by lines and filter out empty lines
            requirements_list = [line.strip() for line in raw_requirement_text.split('\n') if line.strip()]
        else:
            # For other file types, use the existing parsing logic
            requirements_list = _parse_requirements_from_text(raw_requirement_text)

    # Requirement selection - simplified checkbox approach
    selected_requirements: List[str] = []
    selected_indices: List[int] = []

    if len(requirements_list) == 1:
        selected_requirements = requirements_list
        selected_indices = [0]
    elif len(requirements_list) > 1:
        st.subheader("📌 Select Requirements to Process")

        # Initialize selection state
        if "req_selected_indices" not in st.session_state or st.session_state.get("req_selection_list_len") != len(requirements_list):
            st.session_state["req_selected_indices"] = set()  # Start with none selected
            st.session_state["req_selection_list_len"] = len(requirements_list)

        selected_set = st.session_state["req_selected_indices"].copy()

        # Selection controls
        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            if st.button("✅ Select All"):
                selected_set = set(range(len(requirements_list)))
        with col2:
            if st.button("❌ Clear All"):
                selected_set = set()
        with col3:
            st.write(f"**{len(selected_set)} of {len(requirements_list)} selected**")

        st.session_state["req_selected_indices"] = selected_set

        # Display requirements with checkboxes in a scrollable container
        with st.container(height=400):
            for i, req in enumerate(requirements_list):
                cols = st.columns([1, 20])
                with cols[0]:
                    is_checked = st.checkbox(
                        f"req_{i}",
                        value=(i in selected_set),
                        key=f"req_sel_{i}",
                        label_visibility="collapsed"
                    )
                    if is_checked:
                        selected_set.add(i)
                    else:
                        selected_set.discard(i)
                with cols[1]:
                    # Display requirement with numbering
                    st.markdown(f"**{i+1}.** {req}")

        st.session_state["req_selected_indices"] = selected_set
        selected_indices = sorted(selected_set)
        selected_requirements = [requirements_list[i] for i in selected_indices]

    st.markdown("---")
    st.subheader("📋 Generated Artifacts")

    # Single requirement: current structure (Test Cases | CAPL tabs) with streaming
    if len(requirements_list) == 1 or (len(requirements_list) > 1 and len(selected_requirements) == 1):
        tab_tc, tab_capl = st.tabs(["1️⃣ Test Cases", "2️⃣ CAPL Script"])
        with tab_tc:
            test_cases_container = st.container()
            _render_test_cases_section(test_cases_container, st.session_state.get("test_cases_result"))
        with tab_capl:
            capl_container = st.container()
            _render_capl_section(capl_container, st.session_state.get("capl_script_result"))
        # with tab_python:
        #     python_container = st.container()
        #     _render_python_section(python_container, st.session_state.get("python_script_result"))

        run_clicked = False
        if selected_requirements and dbc_ctx:
            if st.button("🚀 Generate Complete Test Suite", type="primary"):
                run_clicked = True

        if run_clicked and selected_requirements and dbc_ctx:
            requirement = selected_requirements[0]
            st.session_state["test_cases_result"] = None
            st.session_state["capl_script_result"] = None
            # st.session_state["python_script_result"] = None
            st.session_state["debug_logs"] = []
            try:
                _run_generation_workflow(
                    requirement,
                    dbc_ctx,
                    rag_store,
                    test_cases_container=test_cases_container,
                    capl_container=capl_container,
                    python_container=None,
                )
            except Exception as e:
                err_msg = str(e).lower()
                if "connection" in err_msg or "refused" in err_msg or "connect" in err_msg:
                    st.error(
                        "**Ollama connection failed.** The GPU server or local Ollama may be down or unreachable. "
                        "Check that Ollama is running and the port is open."
                    )
                else:
                    st.error(f"Error: {e}")
                st.stop()

    # Multiple requirements: tabs per requirement (sequential processing, no streaming)
    elif len(requirements_list) > 1 and len(selected_requirements) >= 2 and dbc_ctx:
        run_clicked = False
        button_text = f"🚀 Generate Test Suite for {len(selected_requirements)} Requirements (Sequential)"
        if st.button(button_text, type="primary"):
            run_clicked = True

        if run_clicked:
            st.session_state["multi_req_results"] = []
            st.session_state["debug_logs"] = []
            with st.spinner(f"Processing {len(selected_requirements)} requirements sequentially..."):
                results: List[Optional[Dict[str, Any]]] = []
                for i, req in enumerate(selected_requirements):
                    try:
                        res = _run_generation_workflow(req, dbc_ctx, rag_store, None, None, None)
                        if res:
                            results.append(res)
                        else:
                            results.append({"requirement": req, "error": "Generation failed"})
                    except Exception as e:
                        results.append({"requirement": req, "error": str(e)})
                st.session_state["multi_req_results"] = results

        if "multi_req_results" in st.session_state and st.session_state["multi_req_results"]:
            results = st.session_state["multi_req_results"]
            tab_names = [f"Req {i + 1}" for i in range(len(results))]
            req_tabs = st.tabs(tab_names)
            for i, (tab, res) in enumerate(zip(req_tabs, results)):
                with tab:
                    if res and "error" in res:
                        st.error(res["error"])
                    elif res:
                        c1 = st.container()
                        _render_test_cases_section(c1, res.get("test_cases"))
                        st.markdown("---")
                        c2 = st.container()
                        _render_capl_section(c2, res.get("capl_script"), key_suffix=str(i))
                        st.markdown("---")
                        # c3 = st.container()
                        # _render_python_section(c3, res.get("python_script"), key_suffix=str(i))
                    else:
                        st.info("No results.")

    else:
        # Multiple reqs: 0 selected, or 2+ selected but no DBC
        tab_tc, tab_capl = st.tabs(["1️⃣ Test Cases", "2️⃣ CAPL Script"])
        with tab_tc:
            _render_test_cases_section(st.container(), st.session_state.get("test_cases_result"))
        with tab_capl:
            _render_capl_section(st.container(), st.session_state.get("capl_script_result"))
        # with tab_python:
        #     _render_python_section(st.container(), st.session_state.get("python_script_result"))
        if len(requirements_list) > 1:
            if len(selected_requirements) == 0:
                st.info("Select at least one requirement to process.")
            elif len(selected_requirements) >= 2 and not dbc_ctx:
                st.info("Upload DBC file to process multiple requirements sequentially.")

def _run_generation_workflow(
    requirement: str,
    dbc_ctx: DBCContext,
    rag_store: ExtendedRAGVectorStore,
    test_cases_container: Optional[DeltaGenerator] = None,
    capl_container: Optional[DeltaGenerator] = None,
    python_container: Optional[DeltaGenerator] = None,
) -> Optional[Dict[str, Any]]:
    """Run the full generate flow (test cases → analysis → CAPL → Python).
    When containers are None (headless/parallel mode): no streaming, no UI, returns dict or None on failure.
    When containers are provided: streams and renders to UI, returns None."""
    headless = test_cases_container is None
    print(f"[DEBUG] _run_generation_workflow: Starting... (headless={headless})")
    retrieved_contexts: List[str] = []
    # Step 1: Generate IDs (for display only; nothing stored in RAG)
    requirement_id = generate_requirement_id(requirement)
    dbc_id = generate_dbc_id(dbc_ctx.raw_dbc_summary)

    # Step 2: Generate Test Cases (prompt-based, max 5; positive/negative/edge; stream to UI when not headless)
    def _step2():
        return generate_test_cases(
            requirement, dbc_ctx, rag_store=rag_store, requirement_id=requirement_id,
            retrieved_log=retrieved_contexts, stream_container=test_cases_container,
        )
    if headless:
        test_cases = _step2()
    else:
        with st.spinner("Generating test cases (streaming)..."):
            _log_debug("Starting test case generation")
            test_cases = _step2()
    if not test_cases.get("test_cases"):
        print("[DEBUG] _run_generation_workflow: FAILED at Step 2 - no test cases (parse error or empty)")
        if not headless:
            st.error("Failed to generate test cases")
        return None
    if not headless:
        st.session_state["test_cases_result"] = test_cases
        if test_cases_container is not None:
            _render_test_cases_section(test_cases_container, test_cases)
    # Support both new and old schemas for test case IDs
    tc_list = test_cases.get("test_cases", []) or test_cases or []
    test_case_ids = [
        tc.get("test_case_id") or tc.get("test_id") or f"TC_{i:03d}"
        for i, tc in enumerate(tc_list, start=1)
    ]

    # Step 3: Requirement analysis (needed by both CAPL and Python)
    if headless:
        _log_debug("Starting requirement analysis")
        analysis = analyze_requirement_for_simulation(
            requirement, dbc_ctx, model_choice="Ollama", rag_store=rag_store, retrieved_log=retrieved_contexts,
        )
    else:
        with st.spinner("Analyzing requirement..."):
            _log_debug("Starting requirement analysis")
            analysis = analyze_requirement_for_simulation(
                requirement, dbc_ctx, model_choice="Ollama", rag_store=rag_store, retrieved_log=retrieved_contexts,
            )

    # Step 4: Generate CAPL (Corrected prompt primary; RAG examples secondary)
    def _step4():
        capl_examples_text = ""
        try:
            capl_pattern = _analysis_to_capl_pattern(analysis)
            _, _, linked_dbc_id = get_similar_requirement_and_linked_ids(rag_store, requirement, req_k=1)
            capl_results = RetrievalResult(chunks=[], scores=[], context_text="")
            if linked_dbc_id:
                try:
                    if capl_pattern:
                        capl_results = rag_store.retrieve(
                            requirement, top_k=2, source_filter="capl",
                            metadata_filter={"dbc_id": linked_dbc_id, "capl_pattern": capl_pattern},
                        )
                    if not capl_results.chunks:
                        capl_results = rag_store.retrieve(
                            requirement, top_k=2, source_filter="capl",
                            metadata_filter={"dbc_id": linked_dbc_id},
                        )
                except TypeError:
                    pass
            if capl_results.chunks:
                capl_examples_text = _truncate_to_chars(
                    "\n\n".join(
                        f"--- Example {i+1} ---\n{chunk.content}"
                        for i, chunk in enumerate(capl_results.chunks)
                    ),
                    _TRUNCATE_CAPL_EXAMPLES_CHARS,
                )
                retrieved_contexts.append(f"[capl_examples] Retrieved CAPL examples (dbc_id={linked_dbc_id}):\n{capl_examples_text}")
        except Exception as e:
            print(f"[DEBUG] CAPL examples retrieval: {e}")
        return generate_simulation_capl(
            dbc_ctx, analysis, model_choice="Ollama", rag_store=rag_store,
            capl_examples=capl_examples_text, stream_container=capl_container,
        )
    if headless:
        _log_debug("Starting CAPL generation")
        capl_script = _step4()
    else:
        with st.spinner("Generating CAPL (streaming)..."):
            _log_debug("Starting CAPL generation")
            capl_script = _step4()
    if not capl_script:
        print("[DEBUG] _run_generation_workflow: FAILED at Step 4 - no CAPL script")
        if not headless:
            st.error("Failed to generate CAPL script")
        return None
    if not headless:
        st.session_state["capl_script_result"] = capl_script
        if capl_container is not None:
            _render_capl_section(capl_container, capl_script)

    # Step 5: Generate Python
    # COMMENTED OUT: Python script generation disabled
    # def _step5():
    #     return generate_python_script(
    #         requirement, test_cases, rag_store, requirement_id,
    #         retrieved_log=retrieved_contexts, dbc_ctx=dbc_ctx, analysis=analysis,
    #         capl_script=capl_script, stream_container=python_container,
    #     )
    # if headless:
    #     _log_debug("Starting Python generation")
    #     python_script = _step5()
    # else:
    #     with st.spinner("Generating Python script (streaming)..."):
    #         _log_debug("Starting Python generation")
    #         python_script = _step5()
    # if not python_script:
    #     print("[DEBUG] _run_generation_workflow: FAILED at Step 5 - no Python script")
    #     if not headless:
    #         st.error("Failed to generate Python script")
    #     return None
    # if not headless:
    #     st.session_state["python_script_result"] = python_script
    #     if python_container is not None:
    #         _render_python_section(python_container, python_script)
    
    python_script = None  # Placeholder for commented section

    # Record evaluation entry for RAGAS-style analysis (skip in headless to avoid thread issues)
    if not headless:
        _record_evaluation_run(
            requirement=requirement,
            dbc_ctx=dbc_ctx,
            analysis=analysis,
            test_cases=test_cases,
            capl_script=capl_script,
            python_script=python_script if python_script else None,
            retrieved_contexts=retrieved_contexts,
        )
        st.session_state["last_requirement_id"] = requirement_id
        st.session_state["last_test_cases"] = test_cases
        st.session_state["last_capl"] = capl_script
        # st.session_state["last_python"] = python_script  # Python disabled

    if headless:
        return {
            "requirement": requirement,
            "test_cases": test_cases,
            "capl_script": capl_script,
            # "python_script": python_script,  # Python script generation disabled
            "requirement_id": requirement_id,
        }
    return None

if __name__ == "__main__":
    main()
