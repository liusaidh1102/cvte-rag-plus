# 常见问题 (FAQ)

## 问题 1：如何部署 CVTE-RAG-Plus？

**回答：** 您可以通过 Docker Compose 或源码进行部署。

- **Docker Compose (推荐):**
  - GPU 版本: `docker compose -f docker/docker-compose_gpu.yml up -d`
  - CPU 版本: `docker compose -f docker/docker-compose.yml up -d`
- **源码运行:** 请参考“快速开始”部分。

## 问题 2：管理后台与知识库前台是什么关系？

**回答：** CVTE-RAG-Plus 由管理后台与知识库前台两部分组成，二者共用同一套数据库、对象存储和检索服务。部署时请使用同一版本的配置与镜像，避免不同版本混用导致接口或端口冲突。

## 问题 3：后台解析时报错：存储桶不存在:f62a03f61fdd11f0b1301a12a4193bf3。

**回答：** 该错误通常表示文件记录存在，但对象存储中缺少对应的桶或对象。请确认文件是通过当前版本的页面上传，并检查 MinIO 地址、桶初始化状态以及各服务的存储配置是否一致。迁移旧数据后，建议重新上传文件并重新触发解析。

## 问题 4：支持解析的文档类型？

**回答：** 常见的文档类型均可支持，包括：pdf、word、ppt、excel、txt、md、html、jpg、png、bmp。

## 问题 5：docker镜像支持arm平台吗？

**回答：** 当前发布流程主要面向 x86_64 平台验证，暂不提供 ARM 平台镜像。如确有 ARM 部署需求，可自行构建镜像并验证解析、检索与模型推理依赖的兼容性。

## 问题 6：可以用ollama部署模型吗？

**回答：** 可以，兼容ollama及在线api(硅基流动平台)。

## 问题 7：端口冲突报错如何解决？

```bash
(HTTP code 500) server error - Ports are not available: exposing port TCP 0.0.0.0:5455 -> 0.0.0.0:0: listen tcp 0.0.0.0:5455: bind: An attempt was made to access a socket in a way forbidden by its access permissions.s
```

**回答：** 该问题原因是 Windows 网络地址转换服务（WinNAT），该服务为 Hyper-V、WSL2 或 Docker 等虚拟化技术提供网络地址转换(NAT)功能 。WinNAT 在运行时会随机保留一部分 TCP/UDP 端口供虚拟网络使用。这些保留端口可能与应用所需端口冲突。

通过以下命令，可以停止服务，释放这些保留端口，允许用户临时使用它们。
```bash
net stop winnat
netsh int ipv4 add excludedportrange protocol=tcp startport=5455 numberofports=1
net start winnat
```

## 问题 8：MinerU GPU 加速似乎只调用了第一张显卡，如何指定其它显卡？

**回答：** MinerU 1.x 本身无法指定具体所用显卡，且不支持多显卡部署，可通过以下方式去限定后端容器所能利用的显卡id。

修改`docker\docker-compose_gpu.yml`：

```bash
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          capabilities: [gpu]
          device_ids: ["2"]  # 使用索引号指定id为2的显卡
```

## 问题 9：日志出现警告：RedisDB.queue_info cvte_rag_plus_svr_queue got exception:no such key

**回答：** 这是解析器心跳触发的告警，不影响正常使用，可以忽略。

## 问题 10：为什么添加ollama时，无法联通？

**回答：** ollama需要预先设置为对所有网络接口开放

修改配置文件：
```bash
vim /etc/systemd/system/ollama.service
```

[Service] 下添加：

```bash
Environment="OLLAMA_HOST=0.0.0.0"
```

重新载入配置文件，重启ollama。

```bash
systemctl daemon-reload
systemctl restart ollama
```

--- 



*如有更多问题，可在 GitHub 上提交 Issue。*
