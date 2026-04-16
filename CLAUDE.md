# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

AI-powered ECU (Electronic Control Unit) test automation tool. Given a DBC file (CAN bus database) and a natural-language requirement, it generates:
- Structured test cases (JSON)
- CAPL simulation scripts for CANoe (Vector Tools)
- (Disabled) Python pytest scripts using python-can

The system uses RAG (Retrieval-Augmented Generation) over a library of hand-crafted CAPL examples (`Data_V1/` through `Data_V7/`) and a local Ollama LLM.

## Running the Application

```bash
# Start the Streamlit web app (primary entry point)
streamlit run complete_rag_appV1.0.py

# Docker build and run
docker build -t ecu-testing:v1 .
docker run -p 8501:8501 ecu-testing:v1

# Clean Streamlit/Python cache
python clear_cache.py
```

## Key Dependencies (install manually — no requirements.txt is committed but the Dockerfile copies one)

```
streamlit
cantools
langchain_ollama
langchain_core
sentence_transformers
qdrant_client
python-dotenv
pypdf
pandas
python-can
```

## Configuration Constants (top of `complete_rag_appV1.0.py`, ~line 252)

| Constant | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL_GPU` | `172.16.117.136:11435` | Shared GPU server |
| `OLLAMA_BASE_URL_LOCAL` | `localhost:11434` | Local Ollama fallback |
| `OLLAMA_MODEL_GPU` | `llama3.3:70b` | Primary model |
| `OLLAMA_MODEL_LOCAL` | `llama3.1:8b` | Fallback model |
| `FORCE_GPU_FIRST` | `True` | Try GPU server first |
| `USE_LOCALHOST_FALLBACK` | `True` | Fall back to local Ollama |
| `CAPL_PREDEFINED_RULES_PATH` | `CAPL Simulation predefined rules.txt` | Authoritative CAPL rules injected into every prompt |

## Architecture

### Module Layout

| File | Role |
|---|---|
| `complete_rag_appV1.0.py` | Monolithic Streamlit app (~3,900 lines). All UI, orchestration, DBC parsing, LLM calls, code generation |
| `rag_vector_store.py` | In-memory RAG store using bag-of-words similarity (no external DB) |
| `rag_vector_store_qdrant.py` | Production RAG store using Qdrant + `BAAI/bge-code-v1` sentence-transformer embeddings |
| `Data_V1/` – `Data_V7/` | Training examples: `CAPL_Data_*.json` (DBC + CAPL blocks) and `Python_Script_data_*/` (pytest templates) |
| `evaluation_dataset.json` | Auto-accumulated evaluation log of 147+ generated test cases with ground truth |
| `qdrant_data/` | Persistent Qdrant vector DB on disk |

### Processing Pipeline (inside `complete_rag_appV1.0.py`)

1. **DBC Parsing** (`parse_dbc_file`, ~line 1112): `cantools` parses the uploaded `.dbc` → builds `DBCContext` with messages, signals, ECU ownership, cycle times, counter/CRC detection.

2. **Requirement Analysis** (`_analyze_requirement`, ~line 1700): LLM extracts target ECU, transmission mode (`CYCLIC`/`IMMEDIATE`), simulation type (`SINGLE_ECU_TRANSMIT`, `GATEWAY`, `REACTIVE`), relevant signals.

3. **Code Snippet Pre-computation** (~lines 2032–2192): Before calling the LLM for CAPL, the app pre-computes:
   - Variable declarations
   - Signal initializations
   - Input signal read operations
   - Byte-level packing snippets (injected into the prompt to ensure correct CAPL byte manipulation)

4. **RAG Retrieval**: Queries `ExtendedRAGVectorStore` (Qdrant) for CAPL blocks matching the detected pattern (`cyclic_timer`, `reactive_message`, `reactive_key`, `on_start`, `variables`).

5. **CAPL Generation** (`_generate_capl_script`, ~line 2195): LLM call with DBC constraints, RAG examples, pre-computed snippets, and rules from `CAPL Simulation predefined rules.txt`.

6. **Evaluation Logging**: Every generation is appended to `evaluation_dataset.json`.

### RAG Data Format

`CAPL_Data_*.json` files inside `Data_V*/` contain chunks with these CAPL pattern types:
- `variables` – variable declarations block
- `on_start` – initialization block
- `cyclic_timer` – periodic message transmission
- `reactive_message` – message triggered by received CAN message
- `reactive_key` – message triggered by key press

At startup, all `Data_V*/CAPL_Data_*.json` files are auto-discovered and ingested into the Qdrant store (or the in-memory store) keyed by requirement ID.

### Key Data Structures (all `@dataclass`, ~lines 720–767)

- `DBCContext`: Central DBC parse result — message map, signal map, ECU map, by-name lookups
- `MessageInfo` / `SignalInfo` / `ECUInfo`: Typed wrappers over cantools objects
- `SimulationAnalysis`: Output of requirement analysis step — target ECU, simulation type, pre-computed code snippets, signal lists
- `Chunk` / `RetrievalResult`: RAG primitives used by both vector store implementations

### LLM Initialization (~line 1013)

The app tries the GPU server first (30s timeout). If unavailable and `USE_LOCALHOST_FALLBACK=True`, it falls back to local Ollama. Temperature is set to `0.1` for deterministic code generation. The LLM object is cached in `st.session_state` to avoid re-initialization on every Streamlit rerun.

## Important Notes for Editing

- **Python script generation is disabled**: All code paths for pytest generation are commented out. Do not re-enable without reviewing `Corrected Python Pytest script Prompt.txt`.
- **Byte packing is hard-coded**: The CAPL byte-packing snippet generation (~lines 2109–2192) pre-computes CAPL code based on signal start bits and lengths from the DBC. Changes here affect all generated CAPL output.
- **Prompt files are critical**: `CAPL Simulation predefined rules.txt` and `Corrected CAPL Prompt.txt` are loaded at runtime and injected into every CAPL generation prompt. Edits to these files directly change LLM behavior without any code change.
- **Data versioning**: When adding new training examples, create a new `Data_V8/` folder following the same JSON schema as existing `Data_V*/CAPL_Data_*.json` files. The app auto-discovers numbered folders at startup.
- **Qdrant persistence**: The `qdrant_data/` folder is the live vector DB. Deleting it forces re-ingestion of all `Data_V*/` files on next startup (handled automatically).
