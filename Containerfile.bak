# Podman: podman build -t registry.example.com/meridian:1.0 -f Containerfile .
FROM docker.io/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOME=/tmp
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app.py .
# OpenShift-safe: arbitrary non-root UID in group 0
RUN chgrp -R 0 /app && chmod -R g=u /app
USER 10001
EXPOSE 8501
CMD ["streamlit","run","app.py","--server.port=8501","--server.address=0.0.0.0","--server.headless=true", \
     "--browser.gatherUsageStats=false","--server.enableXsrfProtection=true","--server.maxUploadSize=8"]
