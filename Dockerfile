# pdf-translator 容器镜像——覆盖不会配 Python 环境的用户。
# 构建:  docker build -t pdf-translator .
# 运行:  docker compose up   （挂载 ./data，浏览器打开 http://localhost:8618）
#
# 翻译路径约定：输入/输出/缓存都放容器内 /data（compose 已挂载到宿主 ./data），
# UI 里输入填 /data/xxx.pdf、输出目录填 /data/out——项目级缓存库默认随输入
# 文件目录落盘，容器重建后缓存仍在。
# 注：镜像内置 Noto CJK 覆盖中日韩输出；其他文字系统（阿拉伯语等）请自行
#     追加字体包或用 fonts.cjk 配置指向挂载的字体文件。OCR 引擎未预装
#     （paddleocr 体积大），扫描件场景请在宿主机安装或扩展本镜像。
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY translator ./translator
COPY server ./server
COPY web ./web
COPY run_ui.py ./

RUN pip install --no-cache-dir .

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8618

# 容器内无需自动开浏览器，直接起服务（run_ui 的端口预探/开浏览器逻辑面向桌面）
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8618"]
