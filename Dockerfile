FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py protocols.py egress.py ./
COPY templates templates
ENV PYTHONUNBUFFERED=1 RELAY_STATE=/state
CMD ["gunicorn","--bind","0.0.0.0:8080","--workers","1","--threads","12","--timeout","330","relay:app"]
