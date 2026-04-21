# Scalability & Model Fine-Tuning Roadmap: ECU Testing AI System

## Executive Summary
This document outlines infrastructure improvements for production scalability and a comprehensive strategy for fine-tuning both the LLM (test generation) and embedding models (RAG retrieval) specific to ECU/automotive testing domain.

---

## Part 1: Scalable Implementation Strategy

### 1.1 Architecture Evolution

#### Current State
- **Monolithic Streamlit app** with embedded RAG store
- **Local Qdrant vector store** (persistent at `./qdrant_data`)
- **Ollama integration** (GPU/Local fallback)
- **In-memory data loading** at startup
- **Sequential generation** of test cases, CAPL, Python scripts

#### Phase 1: Microservices Decomposition (3-6 months)

**1.1.1 Service Separation**
```
📦 ECU Testing Platform
├── 🔵 API Gateway (FastAPI/Django)
│   ├── Authentication & Rate limiting
│   ├── Request routing
│   └── Response caching
├── 🟢 RAG Service (Microservice)
│   ├── Qdrant vector store (persistent/distributed)
│   ├── Document ingestion pipeline
│   └── Semantic search API
├── 🟡 LLM Generation Service (Microservice)
│   ├── Multi-model support (Ollama, vLLM, LM Studio)
│   ├── Prompt templating
│   ├── Output validation
│   └── Streaming support
├── 🟠 Data Pipeline Service (Microservice)
│   ├── JSON data validation
│   ├── SQL database sync
│   ├── Version management
│   └── Incremental ingestion
└── 🔴 UI Layer (Streamlit or React)
    └── Async client for API calls
```

**1.1.2 Deployment Architecture**
```
┌─────────────────────────────────────────────┐
│        Kubernetes Cluster (Prod)             │
├─────────────────────────────────────────────┤
│  ┌──────────────┐      ┌──────────────────┐ │
│  │ Ingress/LB   │──────│ Prometheus/ELK   │ │
│  └──────────────┘      └──────────────────┘ │
│                                              │
│  ┌────────────────────────────────────────┐ │
│  │ API Gateway (FastAPI, 3 replicas)      │ │
│  └────────────────────────────────────────┘ │
│          ↓        ↓          ↓               │
│  ┌──────────┐ ┌────────┐ ┌──────────────┐  │
│  │RAG Svc   │ │LLM Svc │ │Data Pipeline │  │
│  │(3 pods)  │ │(2 pods)│ │  (1 pod)    │  │
│  └──────────┘ └────────┘ └──────────────┘  │
│         ↓         ↓              ↓           │
│  ┌────────────┐ ┌─────┐ ┌──────────────┐  │
│  │Qdrant Cls. │ │vLLM │ │PostgreSQL DB │  │
│  │(3 nodes)   │ │     │ │  (+ backups) │  │
│  └────────────┘ └─────┘ └──────────────┘  │
│                                      │      │
│            Redis Cache ─────────────┘      │
└─────────────────────────────────────────────┘
```

### 1.2 Database Implementation (SQL Layer)

**1.2.1 Recommended Setup** (see Database_Implementation_Plan.md for schema)
```sql
-- Production Stack
Database: PostgreSQL 15+
Read Replicas: 2-3
Backup Strategy: WAL archiving + daily snapshots
Connection Pooling: pgBouncer (500+ connections)

-- Key tables for scalable ingestion
- dataset_versions (versioning & metadata)
- capl_documents (CAPL specs)
- can_messages & can_signals (DBC structure)
- requirements & test_cases (test data)
- python_test_scripts (test code)
- import_log (audit trail)
```

**1.2.2 Data Ingestion Pipeline**
```python
# Pseudo-code: Async data ingestion
class DataIngestionPipeline:
    async def ingest_data_v_folder(self, version: str):
        """
        1. Validate JSON structure
        2. Transform to relational schema
        3. Bulk insert via COPY command (PostgreSQL)
        4. Index and optimize
        5. Sync to Qdrant for semantic search
        6. Log operation with timestamps
        """
        
    async def incremental_update(self, data_v_path: str):
        """
        1. Hash comparison (content hasn't changed)
        2. Skip if identical
        3. Else: soft-delete old, insert new
        4. Update search indices
        """
```

### 1.3 Performance Optimization

#### 1.3.1 Caching Strategy (3-tier)
```
┌─────────────────────────────────────┐
│ L1: In-Memory Cache (LRU)            │  ← Frequently accessed queries
│ TTL: 5 min | Size: 100 MB           │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ L2: Redis Cache (Distributed)       │  ← Shared across services
│ TTL: 30 min | Size: 5-10 GB         │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ L3: PostgreSQL (Source of Truth)    │  ← Persistent storage
└─────────────────────────────────────┘

Cache Keys:
- capl::<version>::<message_id>       (CAPL snippets)
- test_case::<req_id>::<test_name>    (Test cases)
- embedding::<doc_hash>               (Vector embeddings)
```

#### 1.3.2 Parallel Processing
```python
# Current: Sequential generation (3 operations = 3 LLM calls)
# Time: ~45-60 seconds

# Improved: Parallel with thread pool
ExecutorService(max_workers=3):
  - Generate test cases (15 sec)
  - Generate CAPL code  (15 sec)  
  - Generate Python     (15 sec)
  
# New Time: ~20 seconds (+ overhead)
```

#### 1.3.3 Streaming Optimization
```python
# Implement Server-Sent Events (SSE) for streaming responses
@app.post("/api/generate/capl")
async def stream_capl(requirement: str, dbc: str):
    async def event_generator():
        llm = get_ollama_llm()
        async for chunk in llm.stream(prompt):
            yield f"data: {json.dumps({'token': chunk})}\n\n"
    
    return StreamingResponse(event_generator(), 
                           media_type="text/event-stream")
```

### 1.4 Containerization & Orchestration

#### 1.4.1 Docker Compose (Local Development)
```yaml
version: '3.9'
services:
  api-gateway:
    image: ecu-api:latest
    ports: ["8000:8000"]
    depends_on: [rag-service, llm-service, postgres]
    
  rag-service:
    image: ecu-rag:latest
    ports: ["8001:8001"]
    environment:
      - QDRANT_PATH=/data/qdrant
    volumes:
      - qdrant-data:/data/qdrant
    
  llm-service:
    image: ecu-llm:latest
    ports: ["8002:8002"]
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    
  postgres:
    image: postgres:15
    environment:
      POSTGRES_PASSWORD: secure_pwd
    volumes:
      - pg-data:/var/lib/postgresql/data
    
  qdrant:
    image: qdrant/qdrant:latest
    volumes:
      - qdrant-data:/qdrant/storage
      
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

volumes:
  qdrant-data:
  pg-data:
```

#### 1.4.2 Kubernetes Deployment (Production)
```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ecu-api-gateway
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    spec:
      containers:
      - name: api
        image: ecu-api:v1.2.0
        resources:
          requests:
            cpu: "2"
            memory: "4Gi"
          limits:
            cpu: "4"
            memory: "8Gi"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
---
# hpa.yaml (Horizontal Pod Autoscaling)
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ecu-api-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ecu-api-gateway
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

### 1.5 Monitoring, Logging & Observability

#### 1.5.1 Metrics to Track
```python
# Key metrics (send to Prometheus)
from prometheus_client import Counter, Histogram, Gauge

# Throughput
requests_total = Counter(
    'ecu_requests_total', 
    'Total API requests',
    ['endpoint', 'status']
)

# Latency
request_latency = Histogram(
    'ecu_request_duration_seconds',
    'Request latency',
    ['endpoint'],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0)
)

# Cache efficiency
cache_hits = Counter('ecu_cache_hits_total', 'Cache hit count')
cache_misses = Counter('ecu_cache_misses_total', 'Cache miss count')

# Model availability
llm_availability = Gauge('ecu_llm_available', 'LLM service status (1=up, 0=down)')
rag_query_time = Histogram('ecu_rag_query_duration_seconds', 'RAG retrieval latency')
```

#### 1.5.2 Logging Strategy
```python
# Structured logging (JSON to ELK/Datadog)
import structlog

logger = structlog.get_logger()

logger.info(
    "generation_completed",
    requirement_id=req_id,
    duration_seconds=elapsed,
    tokens_generated=out_tokens,
    llm_model=model_name,
    status="success"
)
```

#### 1.5.3 Distributed Tracing
```python
# Use OpenTelemetry for request tracing
from opentelemetry import trace, metrics
from opentelemetry.exporter.jaeger.thrift import JaegerExporter

tracer = trace.get_tracer(__name__)

@app.post("/generate")
async def generate(req: GenerateRequest):
    with tracer.start_as_current_span("generate_test_cases") as span:
        span.set_attribute("requirement_id", req.requirement_id)
        
        with tracer.start_as_current_span("rag_retrieve"):
            retrieved = await rag_svc.retrieve(req.requirement)
            span.set_attribute("rag_hits", len(retrieved))
        
        with tracer.start_as_current_span("llm_generate"):
            result = await llm_svc.generate(retrieved)
```

### 1.6 API Design & Rate Limiting

#### 1.6.1 RESTful API Specification
```python
from fastapi import FastAPI, BackgroundTasks
from slowapi import Limiter
from slowapi.util import get_remote_address

app = FastAPI()
limiter = Limiter(key_func=get_remote_address)

@app.post("/api/v1/generate/test-cases")
@limiter.limit("10/minute")  # Rate limit: 10 requests/min per IP
async def generate_test_cases(
    requirement: str,
    dbc_file: UploadFile,
    background_tasks: BackgroundTasks
):
    """
    Generate test cases from requirement + DBC.
    Returns: job_id (async) or inline (if cached)
    """
    
@app.get("/api/v1/job/{job_id}")
async def get_job_status(job_id: str):
    """Check generation status and retrieve results."""
    
@app.post("/api/v1/ingest/data-version")
@limiter.limit("5/minute")
async def ingest_data_version(
    version: str,
    data_folder: UploadFile
):
    """Ingest new Data_Vx folder."""
```

---

## Part 2: Model Fine-Tuning Strategy

### 2.1 Current Model Stack

| Component | Model | Size | Purpose |
|-----------|-------|------|---------|
| **LLM** | Llama 3.3 70B | 70B params | CAPL/Python generation |
| **LLM Alt** | Llama 3.1 8B | 8B params | Lightweight fallback |
| **Embeddings** | BAAI/bge-code-v1 | ~110M params | Code snippet retrieval |
| **Infrastructure** | Ollama | - | Model serving |

### 2.2 LLM Fine-Tuning (CAPL & Python Generation)

#### 2.2.1 Data Preparation

**Goal:** Fine-tune on automotive test generation tasks

```python
# Dataset structure (JSONL format)
{
  "instruction": "Generate CAPL test code for the following requirement",
  "input": {
    "requirement": "BCM should transmit hazard status message (ID: 0x100) every 10ms",
    "dbc_context": "Message: BCM_STATUS...",
    "example_tests": "[retrieved RAG examples]"
  },
  "output": "on key 'x' {\n  msgSend(BCM_STATUS, ...)\n}",
  "metadata": {
    "data_version": "V1",
    "test_type": "message_generation",
    "source": "automotive_ecu"
  }
}
```

**Data Collection Strategy:**
1. **Extract from existing Data_V1-V7** (current dataset)
   - 1000+ requirement → test case pairs
   - Clean & validate
   - ~70% train, 15% val, 15% test split

2. **Synthetic data augmentation**
   ```python
   # Use LLM to generate variations of existing examples
   template = """
   Given this ECU requirement:
   {original_requirement}
   
   Create 3 variations (different CAN IDs, signal ranges, etc.):
   """
   ```

3. **Manual annotation** (high-quality tests)
   - Have automotive engineers review & annotate 500 best examples
   - Use as "gold standard" eval set

**Data Cleaning Pipeline:**
```python
def prepare_fine_tune_dataset(raw_json_path: str):
    """
    1. Extract requirement & corresponding test case
    2. Normalize CAPL syntax (fix formatting)
    3. Validate DBC references
    4. Remove duplicates (semantic deduplication)
    5. Trim long sequences (max 2048 tokens)
    6. Balance by test type
    7. Export JSONL
    """
```

#### 2.2.2 Fine-Tuning Configurations

**Option A: QLoRA (Quantized LoRA) - Recommended for resource constraints**
```python
from transformers import AutoModelForCausalLM, BitsAndBytesConfig
from peft import LoraConfig, get_peft_model

# Quantization config (4-bit)
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16
)

model = AutoModelForCausalLM.from_pretrained(
    "meta-llama/Llama-2-70b-hf",
    quantization_config=bnb_config,
    device_map="auto"
)

# LoRA config (adapt last 8 layers)
lora_config = LoraConfig(
    r=16,  # LoRA rank
    lora_alpha=32,
    target_modules=["q_proj", "v_proj"],
    lora_dropout=0.05,
    bias="none",
    task_type="CAUSAL_LM"
)

model = get_peft_model(model, lora_config)

# Training args
training_args = TrainingArguments(
    output_dir="./fine_tuned_llm",
    num_train_epochs=3,
    per_device_train_batch_size=4,
    per_device_eval_batch_size=4,
    gradient_accumulation_steps=4,
    learning_rate=2e-4,
    logging_steps=10,
    eval_strategy="steps",
    eval_steps=100,
    save_strategy="steps",
    save_steps=100,
    warmup_steps=100,
    bf16=True,  # bfloat16 precision
    use_flash_attention_2=True,  # FlashAttention v2
)

trainer = SFTTrainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=eval_dataset,
    packing=True,  # Pack sequences efficiently
)

trainer.train()
```

**Option B: Full Fine-Tuning (if resources available)**
- Requires: 4x A100 GPUs (320GB VRAM total)
- Time: 3-5 days
- Better quality but more expensive
- Use for final production model

**Option C: Parameter-Efficient Fine-Tuning (PEFTs)**
```python
# Other efficient methods:
# 1. Adapter modules (smaller, faster training)
# 2. Prefix tuning (good for domain-specific terms)
# 3. Prompt tuning (minimal parameters)
```

#### 2.2.3 Training Infrastructure

**Recommended Setup:**
```
Local Dev:    1x GPU (8GB+) - Quick experiments
Dev/Test:     2x A100 (80GB each) - Full runs
Production:   4x H100 (240GB total) - Final model training
```

**Training Pipeline (on-premise or cloud):**
```bash
# Step 1: Data preparation
python prepare_dataset.py --input raw_data/ --output train_data.jsonl

# Step 2: Fine-tuning
python fine_tune_llm.py \
  --model meta-llama/Llama-2-70b-hf \
  --train_data train_data.jsonl \
  --method qlo ra \
  --num_epochs 3 \
  --batch_size 4

# Step 3: Evaluation
python evaluate_finetuned.py \
  --model checkpoints/final \
  --test_data eval_data.jsonl \
  --metrics bleu,rouge,exact_match
```

#### 2.2.4 Evaluation Metrics

```python
class FinetuneEvaluator:
    def evaluate_capl_generation(self, predictions, groundtruth):
        metrics = {}
        
        # 1. Syntactic validity
        metrics['capl_syntax_valid'] = sum(
            self.is_valid_capl(pred) for pred in predictions
        ) / len(predictions)
        
        # 2. Semantic similarity (BERTScore with automotive vocab)
        metrics['bertscore'] = compute_bertscore(
            predictions, groundtruth,
            model_type="microsoft/deberta-large"
        )
        
        # 3. CAPL-specific checks
        metrics['message_id_accuracy'] = self.check_correct_msg_ids(
            predictions, groundtruth
        )
        metrics['signal_mapping_accuracy'] = self.check_signal_maps(
            predictions, groundtruth
        )
        
        # 4. Test coverage (line-by-line syntax check)
        metrics['line_coverage'] = self.measure_test_coverage(predictions)
        
        # 5. Execution simulation
        metrics['executable_without_errors'] = sum(
            self.can_compile_capl(pred) for pred in predictions
        ) / len(predictions)
        
        # 6. Human evaluation (sample-based)
        metrics['human_rating'] = self.get_human_scores(
            sample_predictions, evaluators=3
        )
        
        return metrics
```

### 2.3 Embedding Model Fine-Tuning

#### 2.3.1 Why Fine-tune Embeddings?

Current model (BAAI/bge-code-v1) is general-purpose. ECU domain has:
- Specialized terminology (CAN, DBC, CAPL, signal mapping)
- Unique code patterns
- Automotive-specific test structures

**Expected improvement:** 15-25% better RAG recall on in-domain queries

#### 2.3.2 Domain Adaptation Data

```python
# Pair data for contrastive learning
{
    "query": "Generate test for hazard light signal",
    "positive": [
        "Test case for BCM_HAZARD_STATUS message transmission",
        "CAPL code: on key 'x' { msgSend(BCM_HAZARD, ...); }"
    ],
    "negative": [
        "Test for engine temperature sensor",
        "Door lock signal verification"
    ]
}
```

**Data Collection:**
1. Use existing CAPL/Python examples as positives
2. Sample random negatives from other domains
3. Manual review of top 100 hard negatives (similar but wrong)
4. ~5,000 pairs minimum

#### 2.3.3 Fine-Tuning Code

```python
from sentence_transformers import SentenceTransformer, InputExample
from sentence_transformers.losses import TripletLoss
from torch.utils.data import DataLoader

# Load model
model = SentenceTransformer('BAAI/bge-code-v1')

# Prepare training data
train_examples = [
    InputExample(
        texts=[query, positive_doc1, negative_doc1],
        label=1
    )
    for query, positive_doc1, negative_doc1 in paired_data
]

train_dataloader = DataLoader(train_examples, shuffle=True, batch_size=16)

# Fine-tune with triplet loss
train_loss = TripletLoss(model)

model.fit(
    train_objectives=[(train_dataloader, train_loss)],
    epochs=3,
    warmup_steps=500,
    output_path="./fine_tuned_embeddings",
)

# Evaluate
from sentence_transformers.evaluation import InformationRetrievalEvaluator

evaluator = InformationRetrievalEvaluator(
    queries=eval_queries,
    corpus=eval_corpus,
    relevant_docs=eval_relevant_docs,
    show_progress_bar=True
)

model.evaluate(evaluator)
```

#### 2.3.4 Integration into RAG

```python
# Use fine-tuned embedding in Qdrant
from rag_vector_store_qdrant import ExtendedRAGVectorStore

rag_store = ExtendedRAGVectorStore(
    path="./qdrant_data",
    embedding_model="./fine_tuned_embeddings"  # ← Use fine-tuned model
)

# All queries now use domain-adapted embeddings
result = rag_store.retrieve(
    "Generate test for CAN message with cyclic scheduler",
    top_k=5
)
```

---

## Part 3: Implementation Roadmap (Timeline)

### Phase 1: Quick Wins (1-2 months) 🚀
- [ ] Implement caching layer (Redis) - 2 weeks
- [ ] Add API ratelimiting - 1 week
- [ ] Docker Compose setup - 2 weeks
- [ ] Basic monitoring (Prometheus) - 1 week
- [ ] **Impact:** 30-40% latency reduction

### Phase 2: Automation & Scalability (2-3 months) 🔄
- [ ] Microservices: Split RAG from LLM service - 3 weeks
- [ ] PostgreSQL implementation + data pipeline - 4 weeks
- [ ] Kubernetes deployment (local k3s first) - 2 weeks
- [ ] Data ingestion framework - 2 weeks
- [ ] **Impact:** 5x parallel request capacity

### Phase 3: AI/ML Optimization (3-6 months) 🤖
- [ ] Collect & prepare fine-tuning dataset - 3 weeks
- [ ] Fine-tune embedding model (BAAI/bge-code-v1) - 2 weeks
- [ ] QLoRA fine-tune on LLM (Llama 70B) - 4 weeks
- [ ] Evaluation & iteration - 2 weeks
- [ ] Deploy fine-tuned models - 1 week
- [ ] **Impact:** 20-30% better quality, 15-25% faster with smaller models

### Phase 4: Production Hardening (1-2 months) 🔒
- [ ] End-to-end testing & performance benchmarking - 2 weeks
- [ ] Security audit & credentials management - 1 week
- [ ] Disaster recovery & backups - 1 week
- [ ] Documentation & runbooks - 1 week
- [ ] **Impact:** Production-ready system

---

## Part 4: Return on Investment (ROI)

### Current System (Single machine)
- **Throughput:** 5-10 requests/day (Ollama local)
- **Latency:** 45-60 sec per request
- **Cost:** Hardware amortized ($5K), no cloud

### Post-Scalability (Phase 2)
- **Throughput:** 100+ requests/day
- **Latency:** 15-20 sec per request (parallel generation)
- **Cost:** Kubernetes cluster ($2-3K/month) vs local ($0)

### Post-Fine-tuning (Phase 3)
- **Throughput:** 200+ requests/day (cheaper models)
- **Latency:** 8-12 sec per request (smaller models still accurate)
- **Quality:** +20% CAPL correctness, +25% test case coverage
- **Cost:** Same Kubernetes cluster, cheaper model inference

**Break-even:** 6-9 months (CloudOps savings + reduced manual testing)

---

## Part 5: Technical Dependencies & Tools

### Infrastructure
```
✅ Kubernetes (local: k3s or Minikube; cloud: EKS/GKE)
✅ PostgreSQL 15+
✅ Redis 7+
✅ Qdrant 1.6+
✅ Prometheus + Grafana (monitoring)
✅ ELK / Datadog (logging)
```

### ML/AI
```
✅ Hugging Face Transformers
✅ sentence-transformers
✅ PEFT (Parameter-Efficient Fine-Tuning)
✅ LLaMA 2/3
✅ OpenTelemetry (tracing)
```

### Development
```
✅ FastAPI (API framework)
✅ Pydantic (data validation)
✅ Docker & docker-compose
✅ Helm (Kubernetes package manager)
✅ pytest (testing)
✅ pre-commit hooks (code quality)
```

---

## Part 6: Risk Mitigation

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|-----------|
| Fine-tuned model overfits on limited data | Medium | High | Early stopping, cross-validation, synthetic data augmentation |
| Kubernetes complexity | High | Medium | Start with Docker Compose, graduate to k3s first |
| Model inference latency increases | Low | Medium | Use quantization (INT8), model distillation, batching |
| Data drift (new ECU types break model) | Medium | High | Monitor prediction confidence, implement feedback loop for retraining |
| GPU availability/cost | High | Medium | Use QLoRA/LoRA instead of full fine-tune, consider CPU-optimized inference |

---

## Part 7: Success Criteria

- ✅ **Scalability:** Support 100+ concurrent users
- ✅ **Quality:** 25%+ improvement in CAPL/Python generation correctness
- ✅ **Latency:** <15 sec end-to-end (currently 45-60 sec)
- ✅ **Cost:** $2-3K/month cloud vs. $5K one-time hardware
- ✅ **Availability:** 99.5% uptime SLA
- ✅ **Documentation:** Full runbooks for deployment, monitoring, scaling

---

## Appendix: Quick Reference Commands

### Fine-tuning Preparation
```bash
# Dataset preparation
python prepare_finetuning_data.py \
  --input-folder Data_V* \
  --output-format jsonl \
  --train-split 0.7

# Embedding fine-tune
python finetune_embeddings.py \
  --model BAAI/bge-code-v1 \
  --train-data paired_examples.jsonl \
  --epochs 3

# LLM fine-tune (QLoRA)
python finetune_llm_qlora.py \
  --model meta-llama/Llama-2-70b-hf \
  --train-data train.jsonl \
  --output ./checkpoints
```

### Deployment
```bash
# Local development
docker-compose up -d

# Kubernetes (development)
kubectl create namespace ecu-testing
helm install ecu-app ./helm-chart -n ecu-testing

# Scale API service
kubectl scale deployment ecu-api-gateway --replicas=5 -n ecu-testing
```

---

**Document Version:** 1.0  
**Author:** AI Architecture Team  
**Date:** 2026-04-16  
**Next Review:** Q3 2026
