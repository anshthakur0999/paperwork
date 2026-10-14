FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPERWORK_MODE=hosted \
    PAPERWORK_MAX_UPLOAD_MB=25 \
    PAPERWORK_MAX_OUTPUT_MB=64 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 paperwork
COPY app.py pdf_engine.py serve.py LICENSE ./
COPY static ./static
USER paperwork
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','10000')+'/api/health',timeout=3)"
CMD ["python", "serve.py"]
