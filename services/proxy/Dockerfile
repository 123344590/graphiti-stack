FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "aiohttp>=3.10,<4"

COPY proxy.py .

EXPOSE 8080
CMD ["python", "proxy.py"]
