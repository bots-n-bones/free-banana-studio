FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir /data && chown 10001:10001 /data
COPY site/local_server.py site/index.html site/login.html ./site/

USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "local_server:app", "--app-dir", "site", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
