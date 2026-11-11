# Podman: podman build -t registry.example.com/meridian:1.0 -f Containerfile .
FROM docker.io/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOME=/tmp NLTK_DATA=/usr/local/share/nltk_data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && python -m nltk.downloader -d "$NLTK_DATA" vader_lexicon stopwords
COPY *.py entrypoint.sh ./
# OpenShift-safe: arbitrary non-root UID in group 0
RUN chmod +x entrypoint.sh && chgrp -R 0 /app && chmod -R g=u /app
USER 10001
EXPOSE 8501
ENTRYPOINT ["./entrypoint.sh"]
