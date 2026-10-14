FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -m -u 1000 -s /usr/sbin/nologin appuser \
    && mkdir -p /app/data && chown -R appuser:appuser /app
COPY --chown=appuser:appuser . .
USER appuser
EXPOSE 8765
CMD ["python", "run.py", "server", "--port", "8765", "--host", "0.0.0.0"]
