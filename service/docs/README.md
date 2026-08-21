# 文档目录

当前架构以 USB CDC 作为唯一设备业务链路。机器人不连接 Wi-Fi 或云服务器；
PC 负责联网、ASR、LLM、TTS、资源和时间。
一台 PC 只有一个 `data/local/` 可写数据空间；`data/global/` 仅提供只读模板，
硬件 `device_id` 只用于 USB/RTC/ACK 的运行时路由。

## 协议与接口

| 文档 | 内容 |
|------|------|
| [esp32_pb_protocol.md](./esp32_pb_protocol.md) | DBOT v1 USB CDC 传输、设备上下行与 PB v2.1 |
| [api_interfaces.md](./api_interfaces.md) | `:5050` Web、`:9000` Core、`:18790` RTC Agent 与浏览器调试订阅 |

## 服务

| 文档 | 内容 |
|------|------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 单机数据空间、进程边界、串口路由、对话与定时任务链路 |
| [SERVO_ACTION_ARCHITECTURE.md](./SERVO_ACTION_ARCHITECTURE.md) | 舵机/动作 catalog、PB 原子事务、固件 motor actor、ACK 闭环和验证边界 |
| [SERVER.md](./SERVER.md) | 启动、USB 自动发现、配置、部署与运维 |

## 协作与安全

| 文档 | 内容 |
|------|------|
| [../tools/README.md](../tools/README.md) | USB 服务诊断与维护工具 |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 贡献流程与测试 |
| [../SECURITY.md](../SECURITY.md) | 本机端口、供应商凭据和网络边界 |
