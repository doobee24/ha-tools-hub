FROM python:3-alpine
WORKDIR /app
COPY app/server.py /app/server.py
RUN pip install pyyaml --break-system-packages
CMD ["python3", "-u", "/app/server.py"]
