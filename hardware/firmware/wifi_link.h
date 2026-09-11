#ifndef DESKBOT_WIFI_LINK_H
#define DESKBOT_WIFI_LINK_H

#include <stddef.h>
#include <stdint.h>

class Stream;

/*
 * WiFi fallback transport.
 *
 * The PC provisions {ssid, password, host, port, token} over the USB control
 * channel; this module keeps an STA association plus one outbound TCP
 * connection to the PC alive whenever credentials exist.  usb_transport picks
 * the TCP stream up as its byte link only while no USB host is talking —
 * USB always has priority (see usb_transport.cpp link arbitration).
 */

/* Load persisted credentials and start the STA association when configured.
 * Call once from setup() after usb_transport_begin(). */
void wifi_link_setup(void);

/* Drive STA/TCP reconnect state.  Call from loop(); never blocks longer than
 * one bounded TCP connect attempt, and attempts nothing while the USB session
 * is active. */
void wifi_link_poll(void);

/* The connected TCP byte stream to the PC, or nullptr. */
Stream* wifi_link_stream(void);

/* True when the TCP connection to the PC is established. */
bool wifi_link_connected(void);

/* True when WiFi credentials are stored (transport may come up later). */
bool wifi_link_configured(void);

/* NVS-only credentials probe, callable before wifi_link_setup().  Boot uses
 * it to decide the memory layout (WiFi data plane vs on-device AFE). */
bool wifi_link_boot_configured(void);

/* Per-device secret the PC minted at provisioning; "" when unset. */
const char* wifi_link_token(void);

/* Handle one CONTROL_JSON payload.  Returns true when it was a wifi_config /
 * wifi_status_req command (a wifi_status reply has then been queued). */
bool wifi_link_handle_control_json(const uint8_t* payload, size_t length);

/* Tear the TCP connection down (e.g. when the PC rejected the session);
 * the poll loop retries with backoff. */
void wifi_link_drop_connection(const char* reason);

/* Build the wifi_status JSON used both for replies and for logs. */
size_t wifi_link_status_json(char* out, size_t out_size);

/* 取证事件环（黑匣子）：WiFi 会话没建立时设备日志根本发不出来，链路上的
 * 关键事件（切链路、TCP 建立/断开、hello 收发、字节泵首读）以时间戳短句
 * 留在 RAM，经 wifi_status 的 ev 字段事后读出。仅限 loopTask 调用（切链
 * 路、帧分发、wifi_link_poll 都在 loopTask 上），无锁。文本禁用引号与
 * 反斜杠——它会被原样嵌进 JSON 字符串。 */
void wifi_link_forensic(const char* fmt, ...)
    __attribute__((format(printf, 1, 2)));

/* 把黑匣子里现有事件按顺序以 WARN 日志倒出（[BB-hist] 前缀）。在 WiFi
 * 上 hello_ack 发出后调用：会话刚建立，此前"切链路/建 TCP"等事件当时
 * 发不出去，现在补发到 PC 日志。 */
void wifi_link_forensic_dump(void);

/* 记一条带内存快照的事件：内部 RAM 总空闲 + 最大连续块。WiFi 动态收缓冲
 * 按需分配看的是连续块，满载时这两个数决定"是内存饿死还是驱动收窗不够"。 */
void wifi_link_forensic_mem(const char* tag);

#endif  // DESKBOT_WIFI_LINK_H
