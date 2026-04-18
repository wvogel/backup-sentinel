FROM python:3.14-slim

WORKDIR /app

# Create non-root user
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --no-create-home app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps svglib==1.6.0

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY scripts ./scripts

# Data and report directories owned by non-root user
RUN mkdir -p /data /reports && chown -R app:app /data /reports /app

ENV BSENTINEL_DATA_DIR=/data \
    BSENTINEL_REPORT_DIR=/reports \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 80

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
