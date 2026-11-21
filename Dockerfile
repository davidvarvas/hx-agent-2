# Mech Royale bot. The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB,
# as uid 65534, with the map mounted at /arena.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir --only-binary=:all: --no-deps numpy==2.1.3
WORKDIR /bot
COPY mechfight.py nav.py bot.py ./
RUN python3 -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
