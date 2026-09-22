#!/bin/bash
cd /home/georg/Dokumente/Development-Privat/swarmv
export SWARMQ_BROKER_URL="amqp://guest:guest@localhost:5699/"
export SWARMQ_BACKEND_URL="valkey://localhost:6399/0"
exec uv run swarmq-dashboard --data-dir ./sqd-data serve --insecure-http --host 127.0.0.1 --port 8080
