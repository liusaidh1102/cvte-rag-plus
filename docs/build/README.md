# Docker镜像构建

本节介绍如何构建镜像，并在离线情况下实现镜像的导出和加载。

## 1. 构建镜像

构建前台镜像：

```bash
docker build -t cvte-rag-plus:v0.6.0 .
```

构建后台镜像：

构建后台镜像前，需先将 MinerU 模型文件放置到 `management` 文件夹中，具体获取方式见 MinerU 官方文档。

```bash
cd management
docker-compose build
```


## 2. 上传镜像

上传镜像到公共仓库，可供他人进行下载。

本地镜像默认不带账号前缀；推送到 Docker Hub 前需先打上自己的账号名（以下以 liusaidh1102 为例）：

上传前台镜像：

```bash
docker tag cvte-rag-plus:v0.6.0 liusaidh1102/cvte-rag-plus:v0.6.0
docker push liusaidh1102/cvte-rag-plus:v0.6.0
```

上传后台镜像：
```bash
docker tag cvte-rag-plus-management-web:v0.6.0 liusaidh1102/cvte-rag-plus-management-web:v0.6.0
docker tag cvte-rag-plus-management-server:v0.6.0 liusaidh1102/cvte-rag-plus-management-server:v0.6.0
docker push liusaidh1102/cvte-rag-plus-management-web:v0.6.0
docker push liusaidh1102/cvte-rag-plus-management-server:v0.6.0
```

## 3. 导出镜像

导出所有镜像文件，可实现离线情况下的镜像迁移安装。

```bash
docker save -o cvte-rag-plus-images.tar cvte-rag-plus-management-web:v0.6.0 cvte-rag-plus-management-server:v0.6.0 cvte-rag-plus:v0.6.0 valkey/valkey:8 quay.io/minio/minio:RELEASE.2023-12-20T01-00-02Z mysql:8.0.39 elasticsearch:8.11.3
```

## 4. 离线安装镜像

在离线服务器中，安装镜像

```bash
docker load -i cvte-rag-plus-images.tar
```
