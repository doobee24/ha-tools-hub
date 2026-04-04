FROM python:3-alpine

# PyYAML: required for Cat 9 configuration analysis tests (parsing HA YAML config files).
# --break-system-packages required on Alpine Linux managed Python.
RUN pip install pyyaml --break-system-packages

WORKDIR /app
COPY app/server.py /app/server.py

CMD ["python3", "-u", "/app/server.py"]
