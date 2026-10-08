FROM node:22-alpine

RUN apk add --no-cache python3 py3-pip \
 && npm install -g --omit=dev supergateway@4.0.0 @modelcontextprotocol/server-filesystem@2026.8.31 \
 && npm cache clean --force \
 && pip install --no-cache-dir --break-system-packages "aiohttp>=3.10,<4"

WORKDIR /app
COPY supervisor.py .

EXPOSE 8080
CMD ["python3", "supervisor.py"]
