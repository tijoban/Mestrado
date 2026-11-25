FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY previsao ./previsao
COPY coleta ./coleta

ENV TZ=America/Recife \
    PYTHONUNBUFFERED=1 \
    TF_CPP_MIN_LOG_LEVEL=3 \
    TF_ENABLE_ONEDNN_OPTS=0

# Executado pelo CronJob (1x por mês). Os CronJobs de coleta usam a mesma imagem
# trocando o comando (ex.: python -m coleta.storage_collect).
CMD ["python", "-m", "previsao.main"]
