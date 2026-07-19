FROM python:3.12-alpine

WORKDIR /app
COPY worker.py /app/worker.py

USER 65532:65532
CMD ["python", "/app/worker.py"]

