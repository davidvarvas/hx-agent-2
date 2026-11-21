# Mech Royale bot. The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB,
# as uid 65534, with the map mounted at /arena.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir numpy==2.1.3
WORKDIR /bot
COPY mechfight.py nav.py bot.py ./
RUN python3 -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
