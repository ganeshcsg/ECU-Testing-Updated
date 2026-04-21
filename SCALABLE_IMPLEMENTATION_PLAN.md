# Scalable Implementation Plan: ECU Testing AI System

## Overview
This document details the infrastructure and architectural improvements needed to scale the ECU Testing system from a single-machine Streamlit app to a production-grade, enterprise-ready platform supporting 100+ concurrent users.

---

## Part 1: Current System Analysis

### 1.1 Current State Assessment

| Metric | Current | Target |
|--------|---------|--------|
| **Deployment** | Single machine | Kubernetes cluster |
| **Throughput** | 5-10 requests/day | 200+ requests/day |
| **Concurrent Users** | 1-2 | 100+ |
| **Request Latency** | 45-60 sec | 8-12 sec |
| **Availability** | Manual restart | 99.5% SLA |
| **Data Storage** | JSON files + local Qdrant | PostgreSQL + distributed Qdrant |
| **Cache** | None | Redis (3-tier) |
| **Monitoring** | None | Prometheus + Grafana + ELK |

### 1.2 Bottlenecks Identified

1. **Streamlit overhead** - Python/Tornado event loop not optimized for concurrency
2. **Monolithic design** - Single process = no parallel scaling
3. **In-memory data loading** - Entire dataset loaded at startup (~2-3 GB)
4. **Sequential LLM calls** - Test cases, CAPL, Python generated one-at-a-time
5. **No caching** - Same query hits LLM/Qdrant repeatedly
6. **Local storage** - Single node failure = data loss
7. **No monitoring** - Unknown what's slow or failing

---

## Part 2: Architecture Evolution

### 2.1 Phase 1: From Monolith to Microservices (3-6 months)

#### 2.1.1 Service Decomposition

**Current (Monolithic):**
```
┌─────────────────────────────────────┐
│      Streamlit Application          │
│  ┌─────────────────────────────────┐│
│  │ UI Layer                        ││
│  ├─ File upload (DBC, Req)         ││
│  ├─ Result display                 ││
│  └─ Cache viewer                   ││
│  ├─────────────────────────────────┤│
│  │ LLM Integration                 ││
│  ├─ Ollama connection              ││
│  ├─ Prompt templating              ││
│  └─ Output parsing                 ││
│  ├─────────────────────────────────┤│
│  │ RAG Layer                       ││
│  ├─ Qdrant vector store            ││
│  ├─ Document embedding             ││
│  └─ Similarity search              ││
│  ├─────────────────────────────────┤│
│  │ Data Processing                 ││
│  ├─ JSON parsing                   ││
│  ├─ DBC parsing                    ││
│  └─ Dataset loading                ││
│  └─────────────────────────────────┘│
└─────────────────────────────────────┘
         (Python Streamlit)
```

**Target (Microservices):**
```
                    ┌──────────────────┐
                    │   Load Balancer  │
                    └─────────┬────────┘
                              │
            ┌─────────────────┼─────────────────┐
            │                 │                 │
      ┌─────▼─────┐   ┌──────▼──────┐  ┌──────▼──────┐
      │   API     │   │   API       │  │   API       │
      │ Gateway 1 │   │ Gateway 2   │  │ Gateway N   │
      └─────┬─────┘   └──────┬──────┘  └──────┬──────┘
            │                 │                 │
    ┌───────┴─────────────────┼─────────────────┴───────┐
    │                         │                         │
┌───▼────┐  ┌──────────┐  ┌──▼──────┐  ┌──────────┐   │
│ RAG    │  │   LLM    │  │  Data   │  │ Frontend │   │
│Service │  │ Service  │  │Pipeline │  │ (React)  │   │
│(3 Pod) │  │(2 Pod)   │  │(1 Pod)  │  │          │   │
└───┬────┘  └─────┬────┘  └─────┬───┘  └──────────┘   │
    │             │              │                      │
    │      ┌──────┴──────┐      │        ┌────────────┐│
    │      │  Redis      │      │        │ Nginx/TLS ││
    │      │  Cache      │      │        └────────────┘│
    │      └─────────────┘      │                       │
    │                            │                       │
┌───▼──────────┐  ┌─────────────▼──┐  ┌──────────────┐ │
│ Qdrant Clust.│  │  PostgreSQL    │  │ Prometheus   │ │
│  (3 nodes)   │  │  (Replica)     │  │ Monitoring   │ │
└──────────────┘  └────────────────┘  └──────────────┘ │
                                              │
                                      ┌───────▼───────┐
                                      │ Grafana/ELK   │
                                      │ Dashboards    │
                                      └───────────────┘
```

#### 2.1.2 Service Descriptions

**API Gateway (FastAPI/Kong)**
```
Role: Entry point, routing, rate limiting, auth
Replicas: 3 (for HA)
Features:
  - Request validation
  - Rate limiting (10/min per user)
  - Request ID tracking
  - Response caching headers
  - SSL/TLS termination
```

**RAG Service (Python)**
```
Role: Vector search & semantic retrieval
Replicas: 3 (load-balanced)
Ports: 8001
Dependencies: Qdrant, cache layer
Endpoints:
  - POST /retrieve (query text + filters)
  - POST /index (add documents)
  - GET /health
```

**LLM Service (Python)**
```
Role: Generate CAPL & Python code
Replicas: 2 (GPU-backed)
Ports: 8002
Dependencies: Ollama/vLLM, cache layer
Batch Processing: Yes
Timeout: 60 sec per request
Endpoints:
  - POST /generate/capl
  - POST /generate/python
  - POST /generate/test-cases
  - GET /health
```

**Data Pipeline Service (Python Celery)**
```
Role: Ingest Data_Vx folders, sync to DB
Replicas: 1 (ensures consistency)
Ports: 8003
Dependencies: PostgreSQL, Qdrant
Features:
  - Async ingestion with Celery
  - Version tracking
  - Incremental updates
  - Validation & error handling
Endpoints:
  - POST /ingest/data-version
  - GET /ingest/status/{job_id}
  - GET /datasets (list all)
```

**Frontend (React)**
```
Role: User interface
Deployment: CDN or Nginx
Features:
  - Real-time progress updates (WebSocket)
  - File upload with drag-drop
  - Results display with syntax highlighting
  - Cache/history viewer
```

---

## Part 3: Database Layer

### 3.1 SQL Schema (PostgreSQL)

```sql
-- Core tables for structured data, preserving versioning & auditability

CREATE TABLE dataset_versions (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,  -- Data_V1, Data_V2, ...
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    version_number INT,
    status VARCHAR(20) DEFAULT 'active'  -- active, deprecated, testing
);

CREATE TABLE capl_documents (
    id SERIAL PRIMARY KEY,
    dataset_version_id INT NOT NULL REFERENCES dataset_versions(id),
    file_name VARCHAR(255) NOT NULL,
    parsed_version TEXT,
    raw_json JSONB,  -- Full original JSON for reconstruction
    file_hash VARCHAR(64),  -- For dedup
    file_modified_at TIMESTAMP,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dataset_version_id, file_hash)
);

CREATE TABLE can_messages (
    id SERIAL PRIMARY KEY,
    capl_document_id INT NOT NULL REFERENCES capl_documents(id),
    frame_id INTEGER,
    name VARCHAR(255),
    dlc INTEGER,
    length INTEGER,
    senders TEXT,  -- Comma-separated ECU names
    receivers TEXT,
    cycle_time_ms INTEGER,
    raw_payload JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_can_msg_frame_id ON can_messages(frame_id);
CREATE INDEX idx_can_msg_name ON can_messages(name);

CREATE TABLE can_signals (
    id SERIAL PRIMARY KEY,
    message_id INT NOT NULL REFERENCES can_messages(id),
    name VARCHAR(255),
    start_bit INTEGER,
    length INTEGER,
    byte_order VARCHAR(20),  -- little_endian, big_endian
    is_signed BOOLEAN,
    scale REAL,
    offset REAL,
    minimum REAL,
    maximum REAL,
    unit VARCHAR(50),
    receivers TEXT,
    raw_payload JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_can_sig_name ON can_signals(name);

CREATE TABLE requirements (
    id SERIAL PRIMARY KEY,
    dataset_version_id INT NOT NULL REFERENCES dataset_versions(id),
    requirement_id VARCHAR(100),
    title VARCHAR(255),
    description TEXT,
    raw_json JSONB,
    file_name VARCHAR(255),
    file_modified_at TIMESTAMP,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dataset_version_id, requirement_id)
);

CREATE TABLE test_cases (
    id SERIAL PRIMARY KEY,
    requirement_id INT NOT NULL REFERENCES requirements(id),
    test_case_id VARCHAR(100),
    title VARCHAR(255),
    precondition TEXT,
    steps TEXT,
    expected_result TEXT,
    python_test_script TEXT,
    test_type VARCHAR(50),  -- unit, integration, e2e, embedded
    raw_json JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_test_case_req ON test_cases(requirement_id);

CREATE TABLE python_test_scripts (
    id SERIAL PRIMARY KEY,
    requirement_id INT NOT NULL REFERENCES requirements(id),
    setup_code TEXT,
    script_text TEXT,
    dependencies TEXT,  -- JSON list of imports
    raw_json JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE generated_artifacts (
    id SERIAL PRIMARY KEY,
    requirement_id INT NOT NULL REFERENCES requirements(id),
    generation_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    capl_code TEXT,
    python_code TEXT,
    test_scenario TEXT,
    llm_model VARCHAR(100),
    generation_time_seconds FLOAT,
    user_id VARCHAR(100),
    status VARCHAR(20),  -- success, partial, failed
    feedback_score INT,  -- 1-5 user rating
    feedback_text TEXT
);

CREATE TABLE import_log (
    id SERIAL PRIMARY KEY,
    dataset_version_id INT REFERENCES dataset_versions(id),
    operation_type VARCHAR(50),  -- full_refresh, incremental, validation
    files_processed INT,
    records_created INT,
    records_updated INT,
    errors TEXT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    duration_seconds FLOAT,
    status VARCHAR(20)
);

-- Materialized view for fast aggregations
CREATE MATERIALIZED VIEW dataset_stats AS
SELECT 
    dv.id,
    dv.name,
    COUNT(DISTINCT cm.id) as message_count,
    COUNT(DISTINCT cs.id) as signal_count,
    COUNT(DISTINCT r.id) as requirement_count,
    COUNT(DISTINCT tc.id) as test_case_count
FROM dataset_versions dv
LEFT JOIN capl_documents cd ON cd.dataset_version_id = dv.id
LEFT JOIN can_messages cm ON cm.capl_document_id = cd.id
LEFT JOIN can_signals cs ON cs.message_id = cm.id
LEFT JOIN requirements r ON r.dataset_version_id = dv.id
LEFT JOIN test_cases tc ON tc.requirement_id = r.id
GROUP BY dv.id;

CREATE INDEX idx_dataset_stats_name ON dataset_stats(name);
```

### 3.2 Database Optimization

**Replication & Backup:**
```yaml
PostgreSQL Setup:
  - Primary (write): 1 node
  - Read Replicas: 2-3 nodes
  - Streaming replication (synchronous)
  - Connection pooling: pgBouncer (500 connections)
  - Backup: WAL archiving + daily snapshots to S3
  - Recovery time objective: 15 minutes
```

**Query Optimization:**
```sql
-- Indexes for frequently accessed queries
CREATE INDEX idx_req_by_version ON requirements(dataset_version_id, created_at DESC);
CREATE INDEX idx_test_by_type ON test_cases(test_type, requirement_id);
CREATE INDEX idx_artifacts_by_time ON generated_artifacts(generation_timestamp DESC);

-- Partitioning large tables by date
CREATE TABLE generated_artifacts_2026_q1 PARTITION OF generated_artifacts
    FOR VALUES FROM ('2026-01-01') TO ('2026-04-01');
```

---

## Part 4: Caching Strategy

### 4.1 Three-Tier Cache Architecture

```
┌──────────────────────────────────────┐
│   Request from Client                │
└──────────────┬───────────────────────┘
               │
        ┌──────▼─────────────────────┐
        │  L1: In-Memory Cache       │  ← In API Gateway process
        │  (LRU, 100 MB)             │     TTL: 5 min
        │  - Query embeddings        │
        │  - Test case generations   │
        │  - DBC parsed structs      │
        └──────┬─────────────────────┘
               │ MISS
        ┌──────▼─────────────────────┐
        │  L2: Redis Cache           │  ← Distributed
        │  (10 GB, 30 min TTL)       │
        │  - RAG results             │
        │  - LLM outputs             │
        │  - Session data            │
        └──────┬─────────────────────┘
               │ MISS
        ┌──────▼─────────────────────┐
        │  L3: Database              │  ← Source of truth
        │  (PostgreSQL + Qdrant)     │
        └────────────────────────────┘
```

### 4.2 Cache Keys Design

```python
# Semantic caching - key by content hash, not rigid paths
CACHE_KEYS = {
    # RAG retrieval results
    "rag::{content_hash}::{source}": {
        "ttl": 1800,  # 30 min
        "size": "1 MB",
        "pattern": "query_embedding + filters"
    },
    
    # LLM generations
    "llm::capl::{req_hash}::{dbc_hash}": {
        "ttl": 3600,  # 1 hour
        "size": "500 KB",
        "pattern": "requirement + DBC context"
    },
    
    "llm::python::{req_hash}::{test_count}": {
        "ttl": 3600,
        "size": "300 KB"
    },
    
    # Dataset info
    "dataset::metadata::{version}": {
        "ttl": 86400,  # 1 day
        "size": "50 KB"
    },
    
    # Session/user data
    "session::{user_id}": {
        "ttl": 3600,
        "size": "100 KB"
    }
}
```

### 4.3 Cache Invalidation Policy

```python
class CacheInvalidation:
    """Smart cache invalidation rules"""
    
    def on_new_data_version(self, version_name: str):
        """New Data_Vx uploaded - invalidate derived caches"""
        # Invalidate:
        # - RAG results (old examples irrelevant)
        # - Generated artifacts suggestions
        # Keep: LLM model weights, embeddings
        redis.delete(f"rag::*")  # Pattern delete
        redis.delete(f"llm::*::suggestions")
    
    def on_llm_model_change(self):
        """LLM model updated - invalidate outputs"""
        redis.delete(f"llm::*")
        # Keep: RAG results, embeddings
    
    def on_requirement_update(self, req_id: str):
        """Single requirement updated"""
        redis.delete(f"llm::*::{req_id}:*")
        redis.delete(f"artifacts::{req_id}::*")
```

---

## Part 5: Performance Optimization

### 5.1 Parallel Request Processing

**Current (Sequential):**
```
Input DBC + Requirement
    ↓ (15 sec) Generate test cases
    ↓ (15 sec) Generate CAPL
    ↓ (15 sec) Generate Python
Total: ~45-60 seconds
```

**Optimized (Parallel):**
```
Input DBC + Requirement
    ├─ (15 sec) Generate test cases  ──┐
    ├─ (15 sec) Generate CAPL         ├─ Total: ~20 sec
    └─ (15 sec) Generate Python       ┘

ThreadPoolExecutor(max_workers=3):
    - Concurrent calls to LLM service
    - Aggregated results
    - Track partial failures
```

### 5.2 Batch Processing & Queuing

```python
# Celery + Redis for async task queues
from celery import Celery

app = Celery('ecu_tasks', broker='redis://localhost:6379')

@app.task(queue='high_priority', time_limit=120)
def generate_capl_task(requirement_id: str, dbc_content: str):
    """Long-running LLM task"""
    result = llm_service.generate_capl(requirement_id, dbc_content)
    return result

@app.task(queue='data_import', time_limit=3600)
def ingest_data_version_task(version: str, data_path: str):
    """Background data ingestion - 1 hour timeout"""
    pipeline.ingest(version, data_path)

# Monitoring task queue
@app.task
def monitor_queue_health():
    """Alert if queue backing up (>100 pending)"""
    pending = len(app.tasks)
    if pending > 100:
        alert(f"Queue backup: {pending} tasks pending")
```

### 5.3 Database Query Optimization

```python
# N+1 query avoidance
# Bad: O(n) database hits
for req in requirements:
    test_cases = db.query(TestCase).filter(req_id=req.id)  # ← N queries

# Good: Join + preload
requirements_with_tests = (
    db.query(Requirement)
      .join(TestCase)
      .options(selectinload(Requirement.test_cases))
      .all()  # ← 1-2 queries
)

# Batch inserts
# Bad: O(n) inserts
for item in items:
    db.add(item)
db.commit()

# Good: Batch insert
db.execute(insert(Table).values([...items...]))
db.commit()  # ← 1 query, 1000x signals
```

---

## Part 6: Containerization & Orchestration

### 6.1 Docker Compose (Local Development)

```yaml
version: '3.9'

services:
  # Database
  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_USER: ecu_user
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ecu_testdb
    ports:
      - "5432:5432"
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./init_db.sql:/docker-entrypoint-initdb.d/01-init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ecu_user"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Distributed cache
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --appendonly yes
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  # Vector database
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
    volumes:
      - qdrant-data:/qdrant/storage
    environment:
      QDRANT_API_KEY: ${QDRANT_API_KEY}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  # LLM service (requires GPU)
  llm-service:
    build:
      context: ./services/llm
      dockerfile: Dockerfile
    ports:
      - "8002:8002"
    environment:
      - OLLAMA_BASE_URL=${OLLAMA_BASE_URL}
      - MODEL_NAME=${LLM_MODEL}
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    volumes:
      - llm-cache:/root/.cache
    depends_on:
      - redis
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/health"]
      interval: 15s
      timeout: 10s
      retries: 3

  # RAG service
  rag-service:
    build:
      context: ./services/rag
      dockerfile: Dockerfile
    ports:
      - "8001:8001"
    environment:
      - QDRANT_HOST=qdrant
      - QDRANT_PORT=6333
      - EMBEDDING_MODEL=${EMBEDDING_MODEL}
    depends_on:
      - qdrant
      - redis
    volumes:
      - embeddings-cache:/root/.cache
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Data pipeline service
  data-pipeline:
    build:
      context: ./services/data_pipeline
      dockerfile: Dockerfile
    ports:
      - "8003:8003"
    environment:
      - DATABASE_URL=postgresql://ecu_user:${DB_PASSWORD}@postgres:5432/ecu_testdb
      - QDRANT_HOST=qdrant
      - REDIS_URL=redis://redis:6379
    depends_on:
      - postgres
      - qdrant
      - redis
    volumes:
      - ./Data_V*:/data:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8003/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  # API Gateway
  api-gateway:
    build:
      context: ./services/api
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    environment:
      - RAG_SERVICE_URL=http://rag-service:8001
      - LLM_SERVICE_URL=http://llm-service:8002
      - DATA_PIPELINE_URL=http://data-pipeline:8003
      - DATABASE_URL=postgresql://ecu_user:${DB_PASSWORD}@postgres:5432/ecu_testdb
      - REDIS_URL=redis://redis:6379
    depends_on:
      - rag-service
      - llm-service
      - data-pipeline
      - postgres
      - redis
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Frontend
  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "3000:3000"
    environment:
      - REACT_APP_API_URL=http://localhost:8000/api
    depends_on:
      - api-gateway

  # Monitoring
  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus-data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3001:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD}
    depends_on:
      - prometheus
    volumes:
      - grafana-data:/var/lib/grafana
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards

volumes:
  postgres-data:
  redis-data:
  qdrant-data:
  llm-cache:
  embeddings-cache:
  prometheus-data:
  grafana-data:
```

### 6.2 Kubernetes Deployment

```yaml
# k8s/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: ecu-testing

---
# k8s/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ecu-config
  namespace: ecu-testing
data:
  RAG_SERVICE_URL: "http://rag-service:8001"
  LLM_SERVICE_URL: "http://llm-service:8002"
  EMBEDDING_MODEL: "BAAI/bge-code-v1"

---
# k8s/secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: ecu-secrets
  namespace: ecu-testing
type: Opaque
stringData:
  DB_PASSWORD: "secure_password_here"
  QDRANT_API_KEY: "secure_key_here"

---
# k8s/api-gateway-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-gateway
  namespace: ecu-testing
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app: api-gateway
  template:
    metadata:
      labels:
        app: api-gateway
    spec:
      containers:
      - name: api
        image: ecu-api:v1.0.0
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 8000
          name: http
        envFrom:
        - configMapRef:
            name: ecu-config
        - secretRef:
            name: ecu-secrets
        resources:
          requests:
            cpu: "1000m"
            memory: "2Gi"
          limits:
            cpu: "2000m"
            memory: "4Gi"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 5
          failureThreshold: 2

---
# k8s/api-gateway-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: api-gateway
  namespace: ecu-testing
spec:
  type: ClusterIP
  selector:
    app: api-gateway
  ports:
  - port: 8000
    targetPort: 8000
    protocol: TCP
    name: http

---
# k8s/api-gateway-hpa.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-gateway-hpa
  namespace: ecu-testing
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-gateway
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 50
        periodSeconds: 15
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
      - type: Percent
        value: 100
        periodSeconds: 15
      - type: Pods
        value: 2
        periodSeconds: 15
      selectPolicy: Max

---
# k8s/llm-service-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llm-service
  namespace: ecu-testing
spec:
  replicas: 2
  selector:
    matchLabels:
      app: llm-service
  template:
    metadata:
      labels:
        app: llm-service
    spec:
      # GPU node selector
      nodeSelector:
        accelerator: nvidia-gpu
      containers:
      - name: llm
        image: ecu-llm:v1.0.0
        ports:
        - containerPort: 8002
        env:
        - name: CUDA_VISIBLE_DEVICES
          value: "0"
        resources:
          requests:
            nvidia.com/gpu: 1
            memory: "8Gi"
            cpu: "4"
          limits:
            nvidia.com/gpu: 1
            memory: "16Gi"
            cpu: "8"
        livenessProbe:
          httpGet:
            path: /health
            port: 8002
          initialDelaySeconds: 60
          periodSeconds: 30

---
# k8s/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ecu-ingress
  namespace: ecu-testing
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - ecu-testing.example.com
    secretName: ecu-tls-cert
  rules:
  - host: ecu-testing.example.com
    http:
      paths:
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: api-gateway
            port:
              number: 8000
      - path: /
        pathType: Prefix
        backend:
          service:
            name: frontend
            port:
              number: 3000
```

---

## Part 7: Monitoring & Observability

### 7.1 Metrics Collection

```python
# Prometheus metrics
from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry

registry = CollectorRegistry()

# Request metrics
http_requests_total = Counter(
    'http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status'],
    registry=registry
)

http_request_duration = Histogram(
    'http_request_duration_seconds',
    'HTTP request latency',
    ['method', 'endpoint'],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0),
    registry=registry
)

# Business metrics
llm_generations_total = Counter(
    'llm_generations_total',
    'Total LLM generations',
    ['model', 'type', 'status'],
    registry=registry
)

llm_generation_duration = Histogram(
    'llm_generation_duration_seconds',
    'LLM generation latency',
    ['model', 'type'],
    registry=registry
)

rag_queries_total = Counter(
    'rag_queries_total',
    'Total RAG queries',
    ['source_type'],
    registry=registry
)

rag_retrieval_duration = Histogram(
    'rag_retrieval_duration_seconds',
    'RAG retrieval latency',
    ['source_type'],
    registry=registry
)

# Cache metrics
cache_hits_total = Counter(
    'cache_hits_total',
    'Total cache hits',
    ['cache_level', 'cache_type'],
    registry=registry
)

cache_misses_total = Counter(
    'cache_misses_total',
    'Total cache misses',
    ['cache_level', 'cache_type'],
    registry=registry
)

# System metrics
active_connections = Gauge(
    'active_connections',
    'Active database connections',
    ['service'],
    registry=registry
)

database_query_duration = Histogram(
    'database_query_duration_seconds',
    'Database query latency',
    ['query_type', 'table'],
    registry=registry
)
```

### 7.2 Logging Strategy

```python
# Structured logging with JSON output
import structlog

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Example logs
logger.info(
    "request_received",
    user_id="user123",
    endpoint="/api/generate",
    method="POST"
)

logger.info(
    "generation_completed",
    requirement_id="req_abc123",
    model="llama3.3:70b",
    duration_seconds=15.3,
    tokens_generated=512,
    status="success"
)

logger.error(
    "generation_failed",
    requirement_id="req_xyz789",
    error="timeout",
    duration_seconds=60.0,
    model="llama3.3:70b"
)
```

### 7.3 Dashboards (Grafana)

**Dashboard 1: System Health**
- API Gateway: CPU, Memory, Request Rate, Error Rate
- Database: Connection Pool, Query Latency, Slow Queries
- Cache: Hit Rate, Memory Usage, Evictions
- Services: Uptime, Pod Restarts, Readiness Status

**Dashboard 2: Application Performance**
- Generation Pipeline: Success Rate, Average Latency, Throughput
- RAG Retrieval: Query Count, Top Queries, Hit Rate by Type
- LLM Service: GPU Utilization, Model Load Time, Queue Length
- Data Ingestion: Last Successful Import, Records Processed, Errors

**Dashboard 3: Business Metrics**
- Total Generations per Day/Week/Month
- User Feedback Scores (1-5 ratings)
- Common Error Patterns
- Data Version Usage Distribution

---

## Part 8: API Design

### 8.1 RESTful Endpoints

```python
from fastapi import FastAPI, UploadFile, BackgroundTasks
from slowapi import Limiter

app = FastAPI(title="ECU Testing API", version="1.0.0")
limiter = Limiter(key_func=get_remote_address)

# Generation endpoints
@app.post("/api/v1/generate/test-cases")
@limiter.limit("10/minute")
async def generate_test_cases(
    requirement: str,
    dbc_file: UploadFile,
    background_tasks: BackgroundTasks
) -> GenerateResponse:
    """Generate test cases from requirement + DBC."""

@app.post("/api/v1/generate/capl")
@limiter.limit("10/minute")
async def generate_capl(
    requirement: str,
    test_cases: List[str],
    dbc_context: str,
    background_tasks: BackgroundTasks
) -> GenerateResponse:
    """Generate CAPL code from test cases."""

@app.get("/api/v1/job/{job_id}")
async def get_job_status(job_id: str) -> JobStatus:
    """Get async job status and results."""

# RAG endpoints
@app.post("/api/v1/rag/retrieve")
@limiter.limit("30/minute")
async def rag_retrieve(
    query: str,
    source_type: Optional[str] = None,
    top_k: int = 5
) -> RetrievalResult:
    """Semantic search over test repository."""

# Data management endpoints
@app.post("/api/v1/data/ingest")
@limiter.limit("5/minute")
async def ingest_data_version(
    version: str,
    data_folder: UploadFile
) -> DataIngestionResponse:
    """Ingest new Data_Vx folder."""

@app.get("/api/v1/data/versions")
async def list_data_versions() -> List[DataVersionInfo]:
    """List all available data versions."""

@app.get("/api/v1/data/stats/{version}")
async def get_data_stats(version: str) -> DataStats:
    """Get statistics for a data version."""

# Health & monitoring
@app.get("/health")
async def health_check() -> HealthStatus:
    """Basic health check."""

@app.get("/ready")
async def readiness_check() -> ReadinessStatus:
    """Readiness probe for K8s."""

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
```

### 8.2 Request/Response Models

```python
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

class GenerateRequest(BaseModel):
    requirement: str
    dbc_file: bytes
    optional_context: Optional[str] = None

class GenerateResponse(BaseModel):
    job_id: str
    status: str  # queued, processing, completed, failed
    estimated_time_sec: float

class JobStatus(BaseModel):
    job_id: str
    status: str
    progress: float  # 0-1
    capl_code: Optional[str] = None
    python_code: Optional[str] = None
    test_cases: Optional[List[str]] = None
    error: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
```

---

## Part 9: Deployment Roadmap

### Phase 1: Monolith → API Layer (1-2 months)
- [ ] Create FastAPI wrapper around Streamlit
- [ ] Add PostgreSQL schema
- [ ] Implement Redis cache
- [ ] Deploy Docker Compose locally
- **Deliverable:** Containerized single-machine deployment

### Phase 2: Service Extraction (2-3 months)
- [ ] Extract RAG service
- [ ] Extract LLM service
- [ ] Extract Data Pipeline service
- [ ] Implement inter-service communication
- [ ] Add Kubernetes manifests
- **Deliverable:** Multi-service K8s deployment (local k3s)

### Phase 3: Production Hardening (1-2 months)
- [ ] Add comprehensive monitoring
- [ ] Security audit & TLS/SSL
- [ ] Disaster recovery setup
- [ ] Load testing & optimization
- **Deliverable:** Production-ready on cloud (EKS/GKE)

---

## Part 10: Cost Estimation

### Development Infrastructure
- **Local:** $0 (developer laptops)
- **CI/CD:** $100-200/mo (GitHub Actions, Docker Registry)

### Staging (Single Kubernetes Cluster)
- **Compute:** 3-4x m5.2xlarge instances = $250/mo
- **Database:** RDS PostgreSQL (db.t3.small) = $30/mo
- **Cache:** ElastiCache Redis (cache.t3.micro) = $15/mo
- **Vector DB:** Qdrant managed = $100/mo
- **Monitoring:** Prometheus + Grafana (self-hosted) = $0
- **Total:** ~$400-500/mo

### Production (Multi-AZ High Availability)
- **Compute:** 6x c5.2xlarge + 2x GPU nodes = $1,500/mo
- **Database:** RDS PostgreSQL multi-AZ = $200/mo
- **Cache:** ElastiCache Redis cluster mode = $150/mo
- **Vector DB:** Qdrant managed enterprise = $500/mo
- **CDN:** CloudFront for frontend = $50/mo
- **Monitoring:** Datadog/New Relic = $300/mo
- **Storage:** S3 backups = $20/mo
- **Total:** ~$2,720/mo

---

## Part 11: Success Metrics

- ✅ **Latency:** Reduce from 60s → 15s (75% improvement)
- ✅ **Throughput:** Increase from 10 req/day → 200 req/day (20x)
- ✅ **Availability:** Achieve 99.5% uptime SLA
- ✅ **Scalability:** Support 100+ concurrent users
- ✅ **Cost Effectiveness:** Cloud costs < hardware ROI in 9-12 months

---

**Document Version:** 1.0  
**Last Updated:** 2026-04-16  
**Review Frequency:** Quarterly
