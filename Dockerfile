# CPU image: runs the serving layer against the mock backend.
# Useful for CI, integration tests and load-testing the HTTP path without a GPU.
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LLMSERVE_BACKEND=mock

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY llmserve ./llmserve
COPY loadtest ./loadtest
COPY pyproject.toml ./

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status==200 else 1)"

CMD ["python", "-m", "llmserve"]
