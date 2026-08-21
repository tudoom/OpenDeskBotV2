# 本地维护工具

设备业务链路仅使用 USB CDC。工具目录不再提供模拟设备接入的
`/asr_chat`、`/camera_uplink` 或麦克风 WebSocket 客户端；音频、相机和控制能力
应由真实 USB 设备完成验收。

本机控制台直接使用唯一的 `data/local/`；真机编号只用于选择 USB 路由。

## USB 服务诊断

`network_connectivity_test.py` 为兼容旧运维命令保留文件名，但现在只访问 PC 服务
HTTP API，不会打开串口，也不会模拟设备 WebSocket。目标必须是 Core 已完成 hello
的 live USB session。

```bash
source .venv/bin/activate
# 默认只读：检查服务健康及 live usb_cdc 会话
python tools/network_connectivity_test.py \
  --device-id deskbot_e8f60a8cf9b0

# 可选：发送一次极小舵机动作，并等待设备 terminal played ACK
python tools/network_connectivity_test.py \
  --device-id deskbot_e8f60a8cf9b0 \
  --control-rounds 1
```

这里的 `device_id` 只选择真机路由，不选择数据空间。诊断接口只允许 loopback；
当前产品不支持远端诊断入口。

`fetch_test_assets.py` 只下载 PC 侧测试资源，不参与设备连接。
