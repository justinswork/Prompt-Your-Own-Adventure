# Single-container deployment for Cloud Run.
#
# The container runs run.py, which spawns:
#   - server.py    (MCP SSE on 127.0.0.1:8001, internal-only)
#   - uvicorn      (FastAPI on 0.0.0.0:$PORT, exposed)
#
# Cloud Run injects $PORT (typically 8080). MCP traffic stays on
# localhost so the protocol is never exposed to the public internet.

FROM python:3.11-slim

# Avoid Python writing .pyc files and buffering stdout (better logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first so layer caching survives source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app source.
COPY . .

# Cloud Run-friendly defaults. Override at deploy time as needed.
ENV HOST=0.0.0.0 \
    RELOAD=0 \
    MCP_SERVER_URL=http://127.0.0.1:8001/sse

# Cloud Run uses PORT (default 8080) and ignores EXPOSE, but documenting
# it for local `docker run` users.
EXPOSE 8080

CMD ["python", "run.py"]
