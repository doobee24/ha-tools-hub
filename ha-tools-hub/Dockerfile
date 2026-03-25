ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3

# Force rebuild of app layer — version 0.02d
RUN echo '0.02d' > /version.txt

COPY app/ /app/
WORKDIR /app
CMD ["python3", "-u", "server.py"]
