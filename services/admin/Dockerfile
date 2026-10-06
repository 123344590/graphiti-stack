FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "aiohttp>=3.10,<4"

COPY admin.py .

EXPOSE 8090
CMD ["python", "admin.py"]
