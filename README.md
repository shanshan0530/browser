# Private Browser MCP

这是一个可视化 Playwright 浏览器 MCP，默认以只读模式运行，并通过两套独立凭据保护：

- `/mcp`：Bearer Token
- noVNC 浏览器界面：用户名和密码

## 必填环境变量

```env
MCP_TOKEN=至少32位，仅使用字母数字._~-
VNC_USERNAME=shanshan
VNC_PASSWORD=至少12位
DATA_DIR=/data
READ_ONLY_MODE=true
```

可选：

```env
ALLOWED_DOMAINS=xiaohongshu.com,github.com
```

设置 `ALLOWED_DOMAINS` 后，通用 `navigate` 只能打开白名单域名及其子域名。留空则允许所有公网 HTTP/HTTPS 域名，但仍阻止 localhost、局域网和非公网 IP。

## Zeabur

1. 部署本仓库。
2. 给服务挂载持久卷到 `/data`。
3. 填写上述环境变量。
4. 打开部署域名，通过 noVNC 登录目标网站。
5. MCP 地址为：

```text
https://你的域名/mcp
```

请求头：

```text
Authorization: Bearer 你的MCP_TOKEN
```

## 只读模式

`READ_ONLY_MODE=true` 时，以下工具会拒绝执行：

- `execute_js`
- `click`
- `type_text`
- `like_xhs_note`
- `comment_xhs_note`

读取、截图、导航、滚动以及小红书内容读取仍可使用。

在自由活动或无人值守场景中，建议始终保持只读模式；需要点赞、评论或输入时，再由上层网关做确认与权限裁决。
