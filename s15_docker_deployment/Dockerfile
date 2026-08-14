FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8015
EXPOSE 8015
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; print(httpx.get('http://localhost:8015/healthz').status_code)" || exit 1
CMD ["python", "s15_docker_deployment/code.py"]
