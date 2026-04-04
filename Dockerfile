FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps svglib==1.6.0

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY scripts ./scripts

ENV BSENTINEL_DATA_DIR=/data \
    BSENTINEL_REPORT_DIR=/reports \
    PYTHONUNBUFFERED=1

EXPOSE 80

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
