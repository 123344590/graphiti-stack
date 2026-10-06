FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "aiohttp>=3.10,<4"

COPY vault_service.py .

EXPOSE 8070
CMD ["python", "vault_service.py"]
