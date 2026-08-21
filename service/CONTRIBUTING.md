# Contributing

## 本地环境

1. 使用 Python 3.11，执行 `uv sync --locked --extra dev`。
2. `cp .env.example .env`，填写所选 ASR/LLM/TTS 供应商配置。
3. 在 `service/` 执行 `./start.sh`，浏览器打开
   `http://127.0.0.1:5050/`。
4. 真机测试时通过 USB 连接统一固件，等待控制台自动发现 live session。

设备端只有 USB CDC 生产链路。不要新增设备 Wi-Fi、SoftAP、设备 WebSocket、
配对码、设备 token 或每设备固件秘密。浏览器调试订阅和供应商 WSS 不受此限制。

## 提交前检查

```bash
source .venv/bin/activate
ruff check src tools tests
PYTHONPATH=src pytest tests -q
```

串口单元测试必须使用 FakeSerial，不应探测开发机的真实 COM/tty。真机验收应作为
明确的人工步骤，记录目标端口，避免操作无关串口。

依赖使用锁文件安装。修改 `pyproject.toml` 后必须运行 `uv lock`，并与
`pyproject.toml` 一起提交 `uv.lock`。

## 文档同步

修改协议、端口、本机访问边界、控制台流程或 LLM 工具时，至少同步：

- [README.md](README.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/SERVER.md](docs/SERVER.md)
- [docs/api_interfaces.md](docs/api_interfaces.md)
- [docs/esp32_pb_protocol.md](docs/esp32_pb_protocol.md)（设备协议变更）

勿提交 `.env`、模型权重、数据库、`data/local/`、测试临时目录或构建产物。

贡献采用 [GPL-3.0](LICENSE)。
