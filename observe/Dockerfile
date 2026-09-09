FROM python:3.12-alpine
WORKDIR /app
COPY nomad_exporter.py feishu_webhook.py /app/
RUN chmod +x /app/nomad_exporter.py /app/feishu_webhook.py
USER nobody
ENTRYPOINT ["python3"]
