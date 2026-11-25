# LunaRelay

可自托管的多协议 AI API 网关，支持模型路由、代理订阅、邀请注册和按密钥配额管理。

## 部署

在 Ubuntu 24.04 上下载源代码后执行：

```sh
sudo bash install.sh --domain relay.example.com
```

如需指定管理端口，追加 `--admin-port 25443`。无域名时使用 `--ip 203.0.113.10`，并在客户端信任安装器给出的 Caddy CA。域名安装需将公网 80、443 和管理端口转发至部署服务器。配置保存在 `.env`，数据、证书和备份保存在 `state/`。首次登录使用面板用户 `root`，初始密码由安装器生成并以仅限服务器管理员读取的方式存放在 `state/relay/bootstrap-password`。

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

管理后台和用户服务台使用独立入口。模型流量经节点代理发送，代理不可用时模型调用将失败。给应用配置允许此用途的上游 API 凭据。

## 运维

```sh
sudo docker compose ps
sudo docker compose logs --tail 100
sudo bash install.sh
```

重复安装保留现有配置，并在更新前备份数据库和主密钥。恢复时停止网关，把同一次备份的 SQLite 文件和主密钥恢复到 `state/relay/`，移走恢复前的 SQLite WAL 和 SHM 文件，再启动网关。若仅迁移数据库，请同时迁移 `.env`，并重新导入代理订阅或迁移 Mihomo 数据。备份与状态目录包含敏感信息，应限制读取权限。
