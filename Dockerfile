FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=bind,source=.,target=/build \
    if [ -d /build/wheelhouse ]; then \
        pip install --no-cache-dir --no-index --find-links=/build/wheelhouse -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi
COPY relay.py protocols.py egress.py ./
COPY templates templates
ENV PYTHONUNBUFFERED=1 RELAY_STATE=/state
CMD ["gunicorn","--bind","0.0.0.0:8080","--workers","1","--threads","12","--timeout","330","relay:app"]
