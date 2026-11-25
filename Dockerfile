FROM python:3.12-slim

RUN pip install --no-cache-dir flask==3.1.3

WORKDIR /app
COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV LOG_VOLUME=/logs \
    PORT=8080
EXPOSE 8080

# 需要读取容器内所有进程的 /proc/<pid>/fd，保持 root 运行
ENTRYPOINT ["/entrypoint.sh"]
