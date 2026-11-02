# 基础运行镜像由部署环境提供，源码仓库不绑定具体镜像地址。
ARG CVTE_RAG_PLUS_BASE_IMAGE
FROM ${CVTE_RAG_PLUS_BASE_IMAGE}

ARG APP_ROOT=/app
ENV APP_ROOT=${APP_ROOT}
WORKDIR ${APP_ROOT}

# 复制 Python 相关代码目录
COPY api ./api
COPY conf ./conf
COPY rag ./rag
COPY graphrag ./graphrag
COPY agentic_reasoning ./agentic_reasoning
COPY management/server ./management/server

# 复制前端源代码目录
COPY web ./web

# 复制 Docker 相关文件
COPY docker/service_conf.yaml.template ./conf/service_conf.yaml.template
COPY docker/entrypoint.sh ./entrypoint.sh

RUN chmod +x ./entrypoint.sh

# 重新构建前端应用

RUN cd web && npm install && npm run build

# 安装 transformers / torch 到运行时虚拟环境 ${APP_ROOT}/.venv：
# ① build 时 PATH 默认系统 python，直接装会进系统 dist-packages，运行时 entrypoint 激活 .venv 后 import 不到
# ② 基础镜像 PIP_INDEX_URL 指向阿里云，uv 默认只认第一个 index 会忽略 cpu index 的 torch，
#    需用 UV_INDEX_STRATEGY=unsafe-best-match 综合所有 index 找到 2.7.0+cpu（CPU 版，避免 5GB CUDA 依赖）
RUN UV_INDEX_STRATEGY=unsafe-best-match uv pip install --python ${APP_ROOT}/.venv/bin/python --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple "torch==2.7.0+cpu" "torchvision==0.22.0+cpu" && \
    uv pip install --python ${APP_ROOT}/.venv/bin/python --index-url https://pypi.tuna.tsinghua.edu.cn/simple transformers==4.49.0 && \
    rm -rf /root/.cache/uv

