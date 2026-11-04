FROM python:3.12-slim

WORKDIR /app

# Install the package in editable mode so the CLI's relative default paths
# (src/market_watcher/research/target_stocks.yaml, src/market_watcher/state.json)
# resolve against /app exactly as they do in a local checkout.
# Configuration (.env) is deliberately excluded via .dockerignore and must be
# supplied at runtime (see env_file in docker-compose.yaml).
COPY . .
RUN pip install --no-cache-dir -e .

CMD ["bash"]
