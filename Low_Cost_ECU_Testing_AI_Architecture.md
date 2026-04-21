# Ultra-Detailed Free / Low-Cost Architecture Plan: ECU Testing AI System

## Overview
This document provides a **deep, production-grade architecture blueprint** using **free and open-source tools**, closely mirroring enterprise systems while keeping costs near zero.

---

# 1. End-to-End Architecture (Detailed)

Client (Browser / API)
│
▼
┌──────────────────────────────┐
│ NGINX (Reverse Proxy) │
│ - SSL (Let's Encrypt) │
│ - Load balancing │
│ - Static frontend serving │
└──────────────┬───────────────┘
│
▼
┌──────────────────────────────┐
│ FastAPI Gateway │
│ - Auth (JWT optional) │
│ - Rate limiting │
│ - Request routing │
│ - Request logging │
└───────┬─────────┬────────────┘
│ │
▼ ▼
┌──────────┐ ┌──────────────┐
│ RAG │ │ LLM Service │
│ Service │ │ (Ollama) │
└────┬─────┘ └──────┬───────┘
│ │
▼ ▼
┌──────────┐ ┌──────────────┐
│ Qdrant │ │ Model Cache │
│ VectorDB │ │ (Local Disk) │
└──────────┘ └──────────────┘

    ▼

┌────────────────────┐
│ Data Pipeline │
│ (Async / Celery) │
└─────────┬──────────┘
▼
┌────────────────────┐
│ PostgreSQL │
│ (Primary DB) │
└────────────────────┘

Shared Services:

Redis (cache + queue)
Prometheus (metrics)
Grafana (dashboards)
Loki (logs)

---

# 2. Component Deep Dive

## 2.1 API Gateway (FastAPI)

### Responsibilities
- Central request entry point
- Input validation (Pydantic)
- Rate limiting (`slowapi`)
- Routing to internal services
- Aggregating responses

### Key Features
- Async endpoints for high concurrency
- Middleware for logging and metrics

### Example Middleware
```python
@app.middleware("http")
async def log_requests(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    print(f"{request.url} completed in {duration}s")
    return response
```

## 2.2 RAG Service (Retrieval-Augmented Generation)

### Responsibilities
- Convert queries into embeddings
- Retrieve relevant documents
- Provide context to LLM

### Pipeline
Query → Embedding → Qdrant Search → Ranking → Top-K Results

### Tools
- sentence-transformers
- Qdrant (self-hosted)

### Optimizations
- Cache embeddings (Redis)
- Pre-chunk documents
- Store metadata filters

## 2.3 LLM Service (Ollama-Based)

### Responsibilities
Generate:
- Test cases
- CAPL code
- Python scripts

### Flow
Request → Cache Check → Prompt Build → Ollama → Parse → Cache → Response

### Parallel Execution
```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=3) as executor:
    results = list(executor.map(run_llm_task, tasks))
```

### Models (Free)
- llama3
- mistral
- codellama

## 2.4 Data Pipeline

### Responsibilities
- Ingest Data_Vx folders
- Parse JSON / DBC
- Normalize schema
- Insert into DB
- Generate embeddings

### Processing Flow
Files → Parser → Validator → DB Insert → Embedding → Qdrant

### Options
- Simple: FastAPI background tasks
- Advanced: Celery + Redis queue

---

# 3. Database Design (PostgreSQL)

### Key Practices
- Use JSONB for flexible schema
- Normalize core entities
- Index frequently queried fields

### Index Examples
```sql
CREATE INDEX idx_req_version ON requirements(dataset_version_id);
CREATE INDEX idx_jsonb ON requirements USING GIN(raw_json);
```

### Optimization
- Avoid N+1 queries
- Use JOINs and batch operations
- Partition large tables (future)

---

# 4. Caching Strategy (Critical for Cost)

### Multi-Layer Cache
- L1: In-Memory (Python dict / LRU)
- L2: Redis (shared cache)
- L3: PostgreSQL (persistent)

### Cache Use Cases
- LLM outputs
- RAG results
- Embeddings
- Dataset metadata

### Cache Keys
- llm::{{hash}}
- rag::{{query_hash}}
- dataset::{{version}}
- session::{{user}}

---

# 5. Deployment Architecture (Single VM)

### Recommended Specs
- 8 GB RAM
- 4 vCPU
- 100 GB SSD

### Layout
VM
│
├── Docker
│   ├── postgres
│   ├── redis
│   ├── qdrant
│   ├── ollama
│   ├── api
│   ├── rag
│   ├── llm
│   ├── frontend
│   ├── prometheus
│   └── grafana

---

# 6. Docker Compose Strategy

### Core Services
- api-gateway
- rag-service
- llm-service
- postgres
- redis
- qdrant
- ollama
- frontend
- monitoring

### Benefits
- Easy local setup
- One-command deployment
- Portable

---

# 7. Performance Optimization

### Parallelization
- Run LLM tasks concurrently
- Async API endpoints

### Batching
- Batch DB inserts
- Batch embedding generation

### Memory Optimization
- Avoid full dataset loading
- Stream large files

---

# 8. Monitoring & Observability

### Metrics (Prometheus)
- Request latency
- LLM latency
- Cache hit/miss
- DB query time

### Dashboards (Grafana)
- System health
- API performance
- LLM usage

### Logging (Loki)
- Structured JSON logs
- Error tracking
- Request tracing

---

# 9. Security (Free Setup)

### HTTPS
- Let's Encrypt (free SSL)

### API Protection
- Rate limiting
- JWT authentication (optional)

### Internal Security
- Private Docker network
- No public DB exposure

---

# 10. Failure Handling

### Strategies
- Retry failed requests
- Timeout handling
- Graceful degradation

### Example
```python
try:
    result = call_llm()
except TimeoutError:
    return "Fallback response"
```

---

# 11. Scaling Strategy

### Stage 1 (Current)
- Single VM
- Docker Compose

### Stage 2
- Split services into multiple VMs

### Stage 3
- k3s (lightweight Kubernetes)

### Stage 4
- Full Kubernetes (EKS/GKE)

---

# 12. Cost Optimization

### Techniques
- Use local LLMs (Ollama)
- Cache aggressively
- Avoid duplicate processing
- Use small models first

---

# 13. Cost Breakdown

| Component | Cost |
|----------|------|
| VM | $15–25 |
| Storage | Included |
| Monitoring | $0 |
| LLM | $0 |
| Backup APIs | ~$10 |
| **Total** | **~$20/month** |

---

# 14. Final Stack Summary

| Layer | Tool |
|------|------|
| API | FastAPI |
| LLM | Ollama |
| Embeddings | Sentence Transformers |
| Vector DB | Qdrant |
| Database | PostgreSQL |
| Cache | Redis |
| Queue | Celery |
| Frontend | React / Streamlit |
| Monitoring | Prometheus + Grafana |
| Logging | Loki |
| Orchestration | Docker / k3s |
| Proxy | Nginx |

---

# 15. Key Benefits

- 90% cost reduction vs managed cloud
- Full control over system
- Scalable architecture
- Production-ready design
- Easy migration to Kubernetes later

---

## Version
- Version: 2.0 (Ultra Detailed)
- Date: 2026-04-16
