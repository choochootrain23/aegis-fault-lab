FROM python:3.14.4-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && mkdir /state && chown 10001:10001 /state
COPY aegis ./aegis
COPY web ./web
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["uvicorn", "aegis.controller:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
