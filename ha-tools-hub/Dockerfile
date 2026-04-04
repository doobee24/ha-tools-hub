FROM python:3-alpine
WORKDIR /app
COPY app/server.py /app/server.py
COPY companions/ /app/companions/
CMD ["python3", "-u", "/app/server.py"]
