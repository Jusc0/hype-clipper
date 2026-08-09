FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface \
    HF_HUB_DISABLE_XET=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin hype

WORKDIR /app
COPY requirements.txt requirements-vps.txt ./
RUN pip install --no-cache-dir -r requirements-vps.txt

COPY twitch_reaction_probe.py vps_worker.py hype_web.py ./

RUN mkdir -p /data /auth /models/huggingface \
    && touch /data/.keep /auth/.keep /models/huggingface/.keep \
    && chown -R hype:hype /app /data /auth /models

USER hype

CMD ["python", "-u", "vps_worker.py"]
