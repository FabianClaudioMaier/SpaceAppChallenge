FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=5173

WORKDIR /app

COPY python-backend/requirements.txt python-backend/setup_venv.sh ./python-backend/
RUN ./python-backend/setup_venv.sh

COPY . .
RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 5173
CMD ["./start.sh"]
