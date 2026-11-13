FROM python:3.12-slim
WORKDIR /app
COPY eor ./eor
ENV EOR_HOST=0.0.0.0 EOR_PORT=8080 EOR_DB=/data/eor.db
RUN mkdir /data && chown 1000 /data
VOLUME /data
EXPOSE 8080
USER 1000
CMD ["python", "-m", "eor", "serve"]
