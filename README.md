# LunaRelay

可自托管的多协议 AI API 网关，支持模型路由、代理订阅、邀请注册和按密钥配额管理。

## 部署

在 Ubuntu 24.04 上下载源代码后执行：

```sh
sudo bash install.sh --domain relay.example.com
```

如需指定管理端口，追加 `--admin-port 25443`。无域名时使用 `--ip 203.0.113.10`，并在客户端信任安装器给出的 Caddy CA。域名安装需将公网 80、443 和管理端口转发至部署服务器。配置保存在 `.env`，数据、证书和备份保存在 `state/`。首次登录使用面板用户 `root`，初始密码由安装器生成并以仅限服务器管理员读取的方式存放在 `state/relay/bootstrap-password`。

网络受限的服务器可以先用 `docker load` 导入 Compose 和 Dockerfile 所用的官方基础镜像，再将适用于目标 Linux 架构及 CPython 3.13 的二进制 Python 依赖包放进 `wheelhouse/`。Docker 构建会从该目录离线安装。

1. 登录管理面板并更换初始密码。
2. 在「出站代理」中导入 HTTPS 节点订阅。支持 Clash `proxies` YAML、逐行节点 URI 和常见 v2rayN/Xray Base64 节点订阅。
3. 新建上游，选择适合该服务的协议和代理节点。上游地址填写 API 根地址，省略由网关添加的版本路径，例如 `https://api.openai.com`。同步模型或手工填写模型 ID，再创建公开模型别名并测试调用。
4. 配置 SMTP 和可选的 Cloudflare Turnstile，再创建注册授权码。SMTP 配置前可手工开户。
5. 用户在服务台领取 Key 后，在「API Key 审批」中为其授权模型、配额和有效期。

## 接口

| 入口 | 操作 |
| --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| Gemini | `POST /v1beta/models/{model}:generateContent` 和 `:streamGenerateContent?alt=sse` |
| 授权模型目录 | `GET /v1/models` 或 Gemini `GET /v1beta/models` |

根据客户端使用 Bearer、`x-api-key` 或 `x-goog-api-key`。Codex 选择 Responses，Claude Code 选择 Anthropic，Gemini CLI 选择 Gemini，Grok Build 和 Kimi CLI 可通过自定义兼容提供商接入。工具调用使用客户端与上游协议一致的路由。跨协议路由支持文本和文本流。缺少上游 Token 用量时，账本按预留的保守估算量结算。

管理后台和用户服务台使用独立的 HTTPS 入口。每个上游可选代理或 HTTPS 直连。DeepSeek 和 Kimi 预设使用直连，其他预设使用代理。两种方式都校验模型厂商的 HTTPS 证书，代理故障时该上游保持代理模式。订阅可通过当前代理组下载和定时刷新。默认组优先选择健康的 UDP 节点，同组按探测延迟选择，UDP 组不可用时切换到其他健康节点；显式指定的节点列表按配置顺序切换。

## 运维

```sh
sudo docker compose ps
sudo docker compose logs --tail 100
sudo bash install.sh
```

重复安装保留现有配置，并在更新前备份数据库和主密钥。恢复时停止网关，把同一次备份的 SQLite 文件和主密钥恢复到 `state/relay/`，移走恢复前的 SQLite WAL 和 SHM 文件，再启动网关。若仅迁移数据库，请同时迁移 `.env`，并重新导入代理订阅或迁移 Mihomo 数据。备份与状态目录包含敏感信息，应限制读取权限。
