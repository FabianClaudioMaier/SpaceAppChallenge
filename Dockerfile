FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=5173 \
    SPHEREX_DOWNLOAD_THREADS=8 \
    SPHEREX_WORKER_PROCESSES=1 \
    SPHEREX_QUEUE_AHEAD_BYTES=67108864 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

WORKDIR /app

COPY python-backend/requirements.txt python-backend/setup_venv.sh ./python-backend/
RUN ./python-backend/setup_venv.sh

COPY . .
RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 5173
CMD ["./start.sh"]
