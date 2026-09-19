# 数字炸弹小游戏后端
#
# ⚠️ 游戏状态存于进程内存，容器必须以单 worker 运行（下方 CMD 已固定 --workers 1）。
#    多副本横向扩展需先引入 Redis 共享状态，属于二期改造。

FROM python:3.12-slim

# 避免生成 .pyc 文件，并让日志实时输出（便于 docker logs 观察）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先只复制依赖清单，利用镜像层缓存：依赖未变时不重复安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再复制应用代码与前端静态资源
COPY app ./app
COPY static ./static
COPY run.py .

# 以非 root 用户运行，降低容器内权限风险
RUN useradd --create-home --shell /usr/sbin/nologin bomb \
    && chown -R bomb:bomb /app
USER bomb

EXPOSE 8000

# 简易存活探针：/api/health 返回 200 即视为健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
