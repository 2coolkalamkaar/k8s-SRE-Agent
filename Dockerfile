FROM python:3.11

WORKDIR /app

# Install deps in a layer-cached step
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the local embedding model so no network access is needed
# at runtime (used for RAG semantic similarity search over past incidents).
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"

# Copy controller source
COPY controller/ ./controller/

# Ensure the /app directory is on sys.path so `from controller.xxx` imports work
# when kopf loads main.py as a standalone script (not via -m)
ENV PYTHONPATH=/app

# Run the Kopf operator
CMD ["kopf", "run", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz", "controller/main.py"]
