  All services are UP:                                                                                                                                                                                                                                                                                                                                                                                                                               
  ┌────────────────────┬────────────────────────────┬─────────────────────┐                                                                                                                                                  
  │      Service       │            URL             │       Status        │                                                                                                                                                  
  ├────────────────────┼────────────────────────────┼─────────────────────┤                                                                                                                                                  
  │ PostgreSQL         │ localhost:5432             │ ✅ Running (Docker) │
  ├────────────────────┼────────────────────────────┼─────────────────────┤                                                                                                                                                  
  │ FastAPI backend    │ http://localhost:8000      │ ✅ Running          │                                                                                                                                                  
  ├────────────────────┼────────────────────────────┼─────────────────────┤                                                                                                                                                  
  │ Simple HTML UI     │ http://localhost:8000      │ ✅ Serving          │                                                                                                                                                  
  ├────────────────────┼────────────────────────────┼─────────────────────┤                                                                                                                                                  
  │ API docs (Swagger) │ http://localhost:8000/docs │ ✅ Available        │                                                                                                                                                  
  └────────────────────┴────────────────────────────┴─────────────────────┘  

    Terminal 1 — Database (Docker)                                                                                                                                                                                             
  cd "C:\Users\ganesjad\Documents\ECU Testing POC\Final_Code_versions\ECU_V10"                                                                                                                                               
  docker compose up db -d
                                                                                                                                                                                                                             
  Terminal 2 — FastAPI + UI
  cd "C:\Users\ganesjad\Documents\ECU Testing POC\Final_Code_versions\ECU_V10"
  .venv\Scripts\uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
  Wait for: [API] RAG ready and Application startup complete. (~20–30s)

  Then open your browser:
  http://localhost:8000

  ---
  To stop:
  # Ctrl+C in Terminal 2 (stops FastAPI)
  docker compose down   # stops PostgreSQL