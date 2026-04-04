FROM python:3-alpine
RUN pip install pyyaml --break-system-packages
WORKDIR /app
COPY app/server.py /app/server.py
CMD ["python3", "-u", "/app/server.py"]
