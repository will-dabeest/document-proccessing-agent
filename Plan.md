# Project Goal

Build a local-first, production-style document processing platform that:

- Accepts file uploads through an API
- Stores files in S3
- Publishes processing jobs to SQS
- Uses a worker service to process documents reliably
- Runs an LLM workflow with Ollama + LangGraph
- Persists results to DynamoDB
- Exposes status in a React frontend
- Includes tracing, tests, and CI/CD

---

# Phase 0: Project Structure

```
project-root/
├── infra/
│   ├── docker-compose.yml
│   ├── main.tf
│   ├── outputs.tf
│   └── variables.tf
├── service-ingestion/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── aws_clients.py
│   │   └── publisher.py
│   └── tests/
├── service-worker/
│   ├── app/
│   │   ├── worker.py
│   │   ├── processor.py
│   │   ├── langgraph_flow.py
│   │   └── tracing.py
│   └── tests/
├── frontend/
├── docs/
└── .github/workflows/
```

---

# Sprint 1: Local Cloud Foundation

## Goal

Create a single-command local environment that behaves like AWS.

## Install

- Docker Desktop
- Terraform
- AWS CLI
- Python 3.11+
- `pip install terraform-local`

## `infra/docker-compose.yml`

```yaml
services:
  localstack:
    image: localstack/localstack:latest
    ports:
      - "4566:4566"
      - "4510-4559:4510-4559"
    environment:
      SERVICES: s3,dynamodb,sqs,secretsmanager
      DEBUG: 1
      DOCKER_HOST: unix:///var/run/docker.sock
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"
      - "4317:4317"
```

## `infra/main.tf`

```hcl
provider "aws" {
  access_key                  = "test"
  secret_key                  = "test"
  region                      = "us-east-1"
  s3_use_path_style           = true
  skip_credentials_validation = true
  skip_metadata_api_check     = true

  endpoints {
    s3             = "<http://localhost:4566>"
    sqs            = "<http://localhost:4566>"
    dynamodb       = "<http://localhost:4566>"
    secretsmanager = "<http://localhost:4566>"
  }
}

resource "aws_s3_bucket" "uploads" {
  bucket = "doc-storage"
}

resource "aws_sqs_queue" "jobs_dlq" {
  name = "jobs-dlq"
}

resource "aws_sqs_queue" "jobs" {
  name = "jobs"

  visibility_timeout_seconds = 30

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_dynamodb_table" "audit" {
  name         = "ProcessLog"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "MessageId"

  attribute {
    name = "MessageId"
    type = "S"
  }
}

resource "aws_secretsmanager_secret" "llm_config" {
  name = "llm-config"
}
```

## Apply

```bash
docker compose up -d
tflocal apply
```

## Verify

```bash
aws --endpoint-url=http://localhost:4566 s3 ls
aws --endpoint-url=http://localhost:4566 sqs list-queues
aws --endpoint-url=http://localhost:4566 dynamodb list-tables
```

---

# Sprint 2: Ingestion Service

## Goal

Create a FastAPI service that uploads files to S3.

## Install

```bash
pip install fastapi uvicorn boto3 pydantic python-multipart
```

## `config.py`

```python
from pydantic import BaseSettings

class Settings(BaseSettings):
    use_localstack: bool = True
    aws_endpoint: str = "<http://localhost:4566>"
    bucket_name: str = "doc-storage"
    queue_url: str = "<http://localhost:4566/000000000000/jobs>"

settings = Settings()
```

## `aws_clients.py`

```python
import boto3
from config import settings

def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.aws_endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )
```

## FastAPI Lifespan

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = {"model": "llama3"}
    yield

app = FastAPI(lifespan=lifespan)
```

## Upload Endpoint

```python
from fastapi import UploadFile

@app.post("/upload")
async def upload(file: UploadFile):
    s3 = get_s3_client()
    s3.upload_fileobj(file.file, settings.bucket_name, file.filename)
    return {"status": "uploaded", "file": file.filename}
```

## Run

```bash
uvicorn app.main:app --reload
```

## Test

```bash
curl -X POST <http://localhost:8000/upload>   -F "file=@example.txt"
```

---

# Sprint 3: Async Bridge with SQS

## Goal

Publish a processing job after upload.

## Message Contract

```json
{
  "s3_key": "example.pdf",
  "idempotency_key": "uuid",
  "uploaded_at": "timestamp"
}
```

## Publish to SQS

```python
import json
import uuid
from datetime import datetime

sqs.send_message(
    QueueUrl=settings.queue_url,
    MessageBody=json.dumps({
        "s3_key": file.filename,
        "idempotency_key": str(uuid.uuid4()),
        "uploaded_at": datetime.utcnow().isoformat()
    })
)
```

## Error Handling

```python
try:
    publish_job(file.filename)
except Exception as e:
    logger.error(
        "ERR_SQS_PUBLISH_FAILED",
        extra={"filename": file.filename, "error": str(e)}
    )
```

---

# Sprint 4: Reliable Worker Service

## Goal

Consume queue messages safely and only process them once.

## Install

```bash
pip install boto3 langgraph ollama pydantic
```

## Long Polling

```python
response = sqs.receive_message(
    QueueUrl=QUEUE_URL,
    MaxNumberOfMessages=1,
    WaitTimeSeconds=20,
    MessageAttributeNames=["All"]
)
```

## Idempotency

```python
try:
    table.put_item(
        Item={
            "MessageId": job_id,
            "Status": "Processing"
        },
        ConditionExpression="attribute_not_exists(MessageId)"
    )
except ClientError:
    return
```

## Delete Only After Success

```python
sqs.delete_message(
    QueueUrl=QUEUE_URL,
    ReceiptHandle=message["ReceiptHandle"]
)
```

## Failure Test

Throw an exception during processing and confirm:

- Message returns to the queue
- After 3 failures it reaches the DLQ

---

# Sprint 5: Agentic Workflow with Ollama + LangGraph

## Goal

Use an LLM to classify and summarize uploaded documents.

## Install

```bash
ollama pull llama3
```

## State

```python
from typing import TypedDict

class AgentState(TypedDict):
    document_text: str
    classification: str
    summary: str
    attempts: int
```

## Graph Nodes

1. Extract text from S3
2. Classify document
3. Generate summary
4. Save result to DynamoDB

## Self-Correction Loop

```python
prompt = f"""
You returned invalid JSON.
Return valid JSON with this schema:
{{
  "classification": string,
  "summary": string
}}

Previous output:
{bad_output}
"""
```

## Save Result Tool

```python
def save_result(job_id, classification, summary):
    table.put_item(
        Item={
            "MessageId": job_id,
            "Status": "Completed",
            "Classification": classification,
            "Summary": summary,
        }
    )
```

## Sample Test Case

Input document:

> Kubernetes uses a control plane and worker nodes to schedule containers.
> 

Expected output:

- Classification: Technical
- Summary: Introductory explanation of Kubernetes architecture.

---

# Sprint 6: Observability and Tracing

## Goal

Trace the entire request across services.

## Install

```bash
pip install   opentelemetry-sdk   opentelemetry-exporter-otlp   opentelemetry-instrumentation-fastapi
```

## Instrument FastAPI

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

FastAPIInstrumentor.instrument_app(app)
```

## Pass Trace Through SQS

```python
MessageAttributes={
    "traceparent": {
        "StringValue": traceparent,
        "DataType": "String"
    }
}
```

## Extract Trace in Worker

```python
from opentelemetry.propagate import extract

context = extract({
    "traceparent": message["MessageAttributes"]["traceparent"]["StringValue"]
})
```

Expected Jaeger trace:

- `/upload`
- `s3.upload_fileobj`
- `sqs.send_message`
- `worker-process`
- `dynamodb.put_item`

---

# Sprint 7: React Frontend

## Goal

Display document status in near real time.

## Install

```bash
npm create vite@latest frontend -- --template react
npm install @tanstack/react-query axios tailwindcss
```

## Polling

```jsx
const { data } = useQuery({
  queryKey: ['documents'],
  queryFn: fetchDocuments,
  refetchInterval: 3000,
})
```

## Status Badge

```jsx
function StatusBadge({ status }) {
  return (
    <span className={`px-2 py-1 rounded ${
      status === 'Completed'
        ? 'bg-green-100 text-green-800'
        : 'bg-yellow-100 text-yellow-800'
    }`}>
      {status}
    </span>
  )
}
```

---

# Sprint 8: CI/CD, Testing, and Documentation

## Goal

Automate testing and quality checks.

## Install

- pytest
- moto
- flake8
- black
- mypy
- tfsec

## GitHub Action

```yaml
name: Pipeline

on: [push]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - run: pip install -r requirements.txt
      - run: pytest
      - run: flake8 .
```

## Example Test

```python
def test_upload_returns_200(client):
    response = client.post(
        "/upload",
        files={"file": ("test.txt", b"hello")}
    )

    assert response.status_code == 200
```

## Testing Pyramid

- Unit tests: small functions and validation logic
- Integration tests: LocalStack + FastAPI + worker
- End-to-end tests: upload a file and verify the frontend shows completion

---

# Definition of Done for Every Sprint

- Feature works
- One failure case tested
- Notes written in engineering journal
- Code committed

# Recommended Order

1. LocalStack + Terraform
2. FastAPI upload
3. S3 + SQS integration
4. Worker without AI
5. Worker with AI
6. Tracing
7. Frontend
8. CI/CD

# Sprint 9: Semantic Search with Embeddings + Vector Database

## Goal

Extend the project into a retrieval-augmented system that can search documents by meaning instead of only by exact text.

## Skills Learned

- Embeddings
- Vector databases
- Semantic search
- Retrieval-augmented generation (RAG)
- Chunking strategies

## Deliverable

Users can ask a question and the system retrieves the most relevant uploaded document sections before generating an answer.

## Recommended Choice

For learning speed and industry relevance:

- Start with Chroma locally
- Later swap to Pinecone to learn a managed production option

## Step 1: Install Dependencies

```bash
pip install chromadb sentence-transformers
```

Optional later:

```bash
pip install pinecone-client
```

## Step 2: Chunk Documents Before Embedding

Instead of embedding an entire file, split it into chunks.

Recommended chunking:

- 500-1000 characters
- 100 character overlap

```python
def chunk_text(text, chunk_size=800, overlap=100):
    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap

    return chunks
```

Resources:

- Chunking strategies for RAG
- Why overlap matters in semantic search

## Step 3: Generate Embeddings

Use a small local embedding model.

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = model.encode(chunks)
```

Resources:

- Sentence Transformers documentation
- Embedding model comparison guides

## Step 4: Store Embeddings in the Vector Database

```python
import chromadb

client = chromadb.Client()
collection = client.create_collection("documents")

collection.add(
    documents=chunks,
    embeddings=embeddings.tolist(),
    ids=[f"{job_id}-{i}" for i in range(len(chunks))],
    metadatas=[{"file": filename} for _ in chunks]
)
```

If you later switch to Pinecone, keep the same chunking and embedding code and only replace the storage layer.

Resources:

- Chroma quickstart
- Pinecone getting started guide

## Step 5: Add a Question Endpoint

Create:

```
POST /ask
```

Request body:

```json
{
  "question": "What does this document say about Kubernetes scheduling?"
}
```

Example FastAPI route:

```python
@app.post("/ask")
async def ask(request: AskRequest):
    answer = answer_question(request.question)
    return {"answer": answer}
```

## Step 6: Retrieve Relevant Chunks

```python
query_embedding = model.encode([question])[0]

results = collection.query(
    query_embeddings=[query_embedding.tolist()],
    n_results=3
)

retrieved_chunks = results["documents"][0]
```

Use the top 3 chunks as context for the LLM.

## Step 7: Generate an Answer with Retrieved Context

```python
prompt = f"""
Answer the question using only the context below.

Context:
{retrieved_chunks}

Question:
{question}
"""
```

This is the core RAG pattern.

## Step 8: Add a Frontend Search Box

Add:

- Search input
- Submit button
- Display area for answer and retrieved snippets

Example component:

```jsx
function AskQuestion() {
  const [question, setQuestion] = useState("")
  const [answer, setAnswer] = useState("")

  async function submit() {
    const response = await axios.post("/ask", { question })
    setAnswer(response.data.answer)
  }

  return (
    <div className="space-y-4">
      <input
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        className="border p-2 w-full"
        placeholder="Ask about uploaded documents..."
      />
      <button onClick={submit} className="bg-blue-500 text-white px-4 py-2 rounded">
        Ask
      </button>

      {answer && (
        <div className="border rounded p-4">
          {answer}
        </div>
      )}
    </div>
  )
}
```

## Success Criteria

- Relevant document sections are retrieved
- Answers use the correct context
- Unrelated questions return "No relevant information found"

## Intentional Failure Test

Upload two unrelated documents:

- One about Kubernetes
- One about cooking

Ask:

- "How does Kubernetes schedule pods?"

Verify only the Kubernetes document is returned.

## Stretch Goals

- Replace local storage with Pinecone
- Add chunk citations
- Add reranking
- Add hybrid keyword + semantic search
- Add support for multiple users and per-user document filtering