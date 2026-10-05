FROM python:3.10-slim

WORKDIR /app

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend application code
COPY backend ./backend

# Environment variables for HF Spaces & writable cache paths
ENV HF_HOME=/tmp/hf_home
ENV SENTENCE_TRANSFORMERS_HOME=/tmp/st_home
ENV PORT=7860

# Expose Hugging Face default port
EXPOSE 7860

# Start Uvicorn backend server
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7860"]
