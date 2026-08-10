FROM python:3.11

WORKDIR /app

# Install deps in a layer-cached step
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy controller source
COPY controller/ ./controller/

# Ensure the /app directory is on sys.path so `from controller.xxx` imports work
# when kopf loads main.py as a standalone script (not via -m)
ENV PYTHONPATH=/app

# Run the Kopf operator
CMD ["kopf", "run", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz", "controller/main.py"]
