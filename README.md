<p align="center"><img src="static/logo.png" alt="Castor" width="150"></p>

# Castor

[中文](README.md) | [English](README.eu.md)

可自行部署的 AI 助手，支持中文对话、工具调用、长期记忆、知识库和定时任务。项目维护与署名：**joker2still**。

支持终端、网页和 Telegram；可使用本地 Ollama、LM Studio，也可在设置中连接云端模型服务。聊天记录、配置和记忆保存在本机；使用云端模型或联网工具时，相关请求会发送到对应服务。

## 快速开始

需要 Python 3.11+、Git，以及正在运行的模型服务。Windows 建议安装 Git Bash，供助手执行 shell 命令。

### 1. 准备 Ollama

安装并启动 Ollama，然后下载代码默认使用的模型：

```bash
ollama pull qwen3:8b
```

如果 Ollama 尚未作为服务运行，可在另一个终端执行 `ollama serve`。默认接口为 `http://localhost:11434/v1`。

### 2. 安装 Castor

```bash
git clone https://github.com/joker2still/castor.git
cd castor
```

Windows：

```powershell
.\setup.bat
.\.venv\Scripts\Activate.ps1
```

Linux / macOS：

```bash
./setup.sh
source .venv/bin/activate
```

安装脚本会创建虚拟环境、安装依赖并预下载嵌入模型，首次安装需要联网。也可手动创建虚拟环境并执行 `python -m pip install -e .`。

### 3. 启动

```bash
castor --web
```

打开 `http://localhost:7860`。首次启动按引导设置名称、时区、语言和模型；中文使用可将助手语言设为中文。首次引导可能按硬件推荐其他模型，最终使用你选择并保存的配置。

```bash
castor              # 终端聊天
castor --doctor     # 检查依赖、数据库和模型连接
```

如果模型已下载却无法连接，先检查 Ollama 是否运行，再确认设置中的提供商为 `ollama`、模型为 `qwen3:8b`。

## 主要功能

- **工具调用**：文件读写、命令执行、网络请求，扩展工具按需加载。
- **长期记忆与知识库**：SQLite 保存会话，Qdrant 保存向量；支持文件、网页和常见办公文档检索。
- **浏览器与 MCP**：浏览器操作、外部工具服务和可扩展技能。
- **任务执行**：定时任务、持续目标、执行记录和中断恢复。
- **多种交互**：网页、终端、Telegram，以及可选的语音、相机和 HTML 画布。
- **个性与模型设置**：调整助手名称、语言、性格和模型服务。

浏览器操作需要额外安装 Playwright 和 Chromium：

```bash
python -m pip install playwright
python -m playwright install chromium
```

本地语音识别可通过 `python -m pip install -e '.[voice]'` 安装。相机和麦克风在远程浏览器中通常需要 HTTPS，可用 `castor --web --ssl --port 7861` 启动。

## 默认配置

以下是 `config.py` 的初始默认值；模型服务与模型选择还会读取数据库中的已保存配置。切换服务请使用网页设置或终端 `/model`，已有安装应检查保存的设置。

| 配置项 | 默认值 | 用途 |
|---|---|---|
| 提供商 | `ollama` | 未保存提供商时的默认选择 |
| `CASTOR_LLM_URL` | `http://localhost:11434/v1` | 模型接口初始地址 |
| `CASTOR_LLM_MODEL` | `qwen3:8b` | 初始聊天模型 |
| `CASTOR_LLM_KEY` | `ollama` | 本地 Ollama 的占位密钥 |
| `CASTOR_DATA_DIR` | `~/.castor` | 用户数据目录 |
| `CASTOR_DB_PATH` | 数据目录下的 `castor.db` | SQLite 数据库 |
| `CASTOR_QDRANT_MODE` | `disk` | 向量存储模式，另支持 `memory`、`server` |
| `CASTOR_QDRANT_PATH` | 数据目录下的 `memory/` | 磁盘向量存储目录 |
| `CASTOR_PASSWORD` | 空 | 网页访问密码，需自行设置 |
| 网页端口 | `7860` | 可用 `--port` 修改 |

修改数据目录的示例：

```powershell
# Windows PowerShell
$env:CASTOR_DATA_DIR = "D:\castor-data"
castor --web
```

```bash
# Linux / macOS
export CASTOR_DATA_DIR="$HOME/.castor"
castor --web
```

其他设置在网页设置界面中修改并持久化。原生启动不会自动读取项目中的 `.env`；该文件由 Docker Compose 加载。

匿名遥测默认关闭，且不附带遥测服务器或应用密钥。未自行配置接收端时，不会发送遥测网络请求。

## Docker 部署

复制 `.env.example` 为 `.env`，设置网页密码和模型接口：

```bash
cp .env.example .env
docker compose up -d
```

PowerShell 可用 `Copy-Item .env.example .env` 完成复制。

Compose 使用 `ghcr.io/joker2still/castor:latest`，需要先由仓库的 Docker 工作流发布。尚未发布镜像时，在 `docker-compose.yml` 中注释 `image:`，启用 `build: .`，再执行：

```bash
docker compose up -d --build
```

Docker 示例使用宿主机 Ollama：`http://host.docker.internal:11434/v1`，模型为 `qwen3:8b`。该地址需要容器能够访问宿主机服务；Linux 上应配置宿主机网关或改成可访问的实际地址。Ollama 也需监听容器可访问的接口。启动后，在网页模型设置中将 Ollama 地址设为上述地址；当前提供商预设或已保存的配置会覆盖环境变量中的初始接口地址。

持久化目录是 **`./castor-data`**，映射到容器内 `/data`。更新镜像不会删除该目录，备份时应复制整个目录。

## 更新与开发检查

激活虚拟环境后执行：

```bash
git pull
python -m pip install -e . --upgrade
```

开发检查：

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/ -v
python -m ruff check .
python scripts/check_js.py
```

JavaScript 语法检查需要 Node.js。

## 文档与许可证

功能使用说明集中在 [docs/](docs/README.md)；架构、开发和版本记录集中在 `project-notes/`，与用户使用文档分开。

安全问题请按 [安全报告说明](SECURITY.md) 私下提交。项目采用 [MIT 许可证](LICENSE)。
