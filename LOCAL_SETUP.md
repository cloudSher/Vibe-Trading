# 本地 A 股观察台

本分支在现有 Vibe-Trading 上增加沪深主板资讯观察、日期和版本统一计划，以及独立模拟组合。真实交易始终由使用者手动操作。

## 安装与启动

在仓库目录建立 Python 环境并安装项目依赖：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cd frontend
npm ci
npm run build
cd ..
./start-local.sh
```

Node.js 版本须满足 `frontend/package.json` 的 engines 要求。浏览器打开 <http://127.0.0.1:8899/ashare>，`Ctrl+C` 停止本地服务。

`vibe-local.sh` 将运行目录固定在仓库内的 `.runtime`，限制文件工具访问范围，关闭 shell 工具和消息渠道自动启动。监听地址固定为回环地址；不要将本地账户页面直接暴露到公网。运行记录、账户、认证缓存、模拟资产和已发布计划不会随代码分发。

首次使用需要在页面自行核对账户及风险设置，再创建或发布所需观察计划。新安装不会获得其他机器的真实持仓、净值基准、已核对公告或模拟成交。计划仅在指定交易日有效，不自动顺延。

## 可选模型登录

```bash
./vibe-local.sh provider login openai-codex
./vibe-local.sh provider doctor
```

请由本人完成官方授权。应用使用独立认证存储，不复制 Codex CLI 的凭据。公开报价、确定性风控和已发布计划的量价判断不要求持续打开 Codex 会话；新闻模型整理需要可用的模型配置。

使用方式、风险边界和提醒中继见 [A 股观察台说明](wiki/docs/ashare-monitor.md)。
