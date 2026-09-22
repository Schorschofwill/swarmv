#!/bin/bash
cd /home/georg/Dokumente/Development-Privat/swarmv
export SWARMQ_BROKER_URL="amqp://guest:guest@localhost:5699/"
export SWARMQ_BACKEND_URL="valkey://localhost:6399/0"
export PYTHONPATH="$PWD/demo"
export SWARMQ_WORKER_ID=io-host
export SQD_WORKER_GROUP=io-gruppe
exec uv run swarmq worker --module tasks \
  --configure swarmq_dashboard.hooks.worker:setup \
  --queues io --pool io --concurrency 4 --processes 2 \
  --max-processes 6 --group io-gruppe --log-format json
