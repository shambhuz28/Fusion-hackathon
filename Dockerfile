# Container for hosting the simulation (e.g. a Hugging Face Docker Space, which expects port 7860).
#   docker build -t fusion . && docker run -p 7860:7860 fusion
FROM python:3.12-slim

# libgomp1: OpenMP runtime needed by lightgbm / xgboost (ML risk ranking).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces run the container as user 1000; the daily update writes into app/data and data/.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt requirements.txt
RUN pip install --no-cache-dir --user -r requirements.txt

# The ML step runs in its own environment (src/daily_update.py looks for ml/.venv).
COPY --chown=user ml/requirements.txt ml/requirements.txt
RUN python -m venv ml/.venv && ml/.venv/bin/pip install --no-cache-dir -r ml/requirements.txt

COPY --chown=user . .

EXPOSE 7860
CMD ["python", "src/server.py", "--host", "0.0.0.0", "--port", "7860", "--workers", "2"]
