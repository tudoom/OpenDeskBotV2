#include "wifi_link.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>

#include <esp_event.h>
#include <esp_heap_caps.h>
#include <esp_netif.h>
#include <esp_netif_net_stack.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <fcntl.h>
#include <lwip/netif.h>
#include <lwip/sockets.h>
#include <mdns.h>
#include <stdarg.h>
#include <string.h>

#include "common.h"
#include "runtime_supervisor.h"
#include "usb_transport.h"

/*
 * 按名字找 Core（0.0.49）：PC 的 DHCP 地址会变（2026-09-08 实机从 .163 变
 * .63，机器人插电源后再也找不到 Core）。现在 Core 有永久 core_id，并以
 * Bonjour 服务 _deskbot-core._tcp（TXT core_id=…）+ UDP 广播应答两种方式
 * 报名。设备拿到 IP 后：mDNS 查询 → UDP 广播（9021）→ 保存的 host，找到
 * 且 core_id 与配对的一致才连；找到的地址写回 NVS。USB 会话里 Core 每次
 * 都会用 host-only 的 wifi_config 刷新 host/port/core_id（不带 ssid、不
 * 重启）。所有步骤都是非阻塞的：loopTask 上不能等超过一个 tick。
 *
 * 直接驱动 esp_wifi 而不是 Arduino WiFi 类：默认初始化配置要吃约 44 KiB
 * 内部堆，与 ESP-SR/相机/语音链路同机放不下。static_rx 降到 6（每个约
 * 1.6 KiB 预分配），dynamic 上限保持 24（按需分配，压太低会栓死 TX）。
 * TCP 直接走 lwIP BSD socket。
 */

namespace {

constexpr const char* kPrefsNamespace = "wifilink";
constexpr uint32_t kStaRetryMs = 15000u;
constexpr uint32_t kTcpRetryMinMs = 5000u;
constexpr uint32_t kTcpRetryMaxMs = 30000u;
constexpr size_t kMaxSsidBytes = 33;
constexpr size_t kMaxPasswordBytes = 65;
constexpr size_t kMaxHostBytes = 48;
constexpr size_t kMaxTokenBytes = 64;
constexpr size_t kMaxCoreIdBytes = 40;

/* 与 PC 侧 infrastructure/net/core_discovery.py 逐字一致（契约测试 grep）。 */
constexpr const char* kDiscoveryService = "_deskbot-core";
constexpr const char* kDiscoveryProto = "_tcp";
constexpr uint16_t kDiscoveryUdpPort = 9021;
constexpr const char* kDiscoverRequestType = "deskbot_discover";
constexpr const char* kDiscoverReplyType = "deskbot_core";
constexpr uint32_t kMdnsQueryMs = 1500u;
constexpr uint32_t kUdpDiscoverWaitMs = 1500u;
constexpr uint32_t kUdpResendMs = 600u;
constexpr uint32_t kDiscoveryRetryMs = 30000u;
constexpr uint32_t kConnectFailsBeforeRediscover = 2u;

char s_ssid[kMaxSsidBytes] = "";
char s_password[kMaxPasswordBytes] = "";
char s_host[kMaxHostBytes] = "";
uint16_t s_port = 0;
char s_token[kMaxTokenBytes] = "";
char s_core_id[kMaxCoreIdBytes] = "";

enum class HostSource : uint8_t { kNone, kStored, kMdns, kUdp };
HostSource s_host_source = HostSource::kNone;
enum class DiscoveryPhase : uint8_t { kIdle, kMdns, kUdp };
DiscoveryPhase s_discovery = DiscoveryPhase::kIdle;
mdns_search_once_t* s_mdns_search = nullptr;
bool s_mdns_inited = false;
int s_udp_fd = -1;
uint32_t s_discovery_started_ms = 0;
uint32_t s_udp_last_send_ms = 0;
uint8_t s_udp_sends = 0;
uint32_t s_discovery_last_done_ms = 0;
bool s_discovery_wanted = false;
bool s_ip_seen = false;
uint32_t s_connect_failures = 0;

bool s_wifi_inited = false;
bool s_sta_connect_sent = false;
/* USB 主机会话在线期间射频整体下电（stop + deinit）：WiFi 只是无 USB 时的
 * 备用链路，而它的收发缓冲占走的几十 KB 内部 RAM 正是相机驱动重建所缺的。
 * USB 会话结束后重新拉起。 */
bool s_radio_suspended = false;
volatile bool s_got_ip = false;
volatile bool s_sta_link_up = false;
esp_netif_t* s_netif = nullptr;
uint32_t s_last_sta_attempt_ms = 0;
uint32_t s_last_tcp_attempt_ms = 0;
uint32_t s_tcp_retry_ms = kTcpRetryMinMs;
uint32_t s_restart_at_ms = 0;
bool s_boot_init_blocked = false;
bool s_mtu_clamped = false;
/* 拿到 IP 后即使 USB 在线也做一次 TCP 试连（成功即断开）：把通路问题
 * 第一时间暴露在 USB 日志里，而不是等拔线后才发现连不上。 */
bool s_path_validated = false;

void copy_bounded(char* dst, size_t dst_size, const char* src) {
  if (src == nullptr) {
    dst[0] = '\0';
    return;
  }
  snprintf(dst, dst_size, "%s", src);
}

/* 取证事件环存储：定义见文件尾的 wifi_link_forensic()。
 *
 * 放在 RTC_NOINIT 里：WiFi 会话跑在电池供电下，USB 重插触发的是软复位而非
 * 掉电，RTC 慢速内存能挺过软复位，于是 WiFi 会话期间记录的事件在设备复位
 * 回到 USB 后仍可经 wifi_status 读出。magic 用来识别掉电后的脏内存。 */
constexpr size_t kForensicSlots = 16;
constexpr size_t kForensicTextLen = 48;
constexpr uint32_t kForensicMagic = 0x0DE58B01u;
struct ForensicEvent {
  uint32_t ms;
  char text[kForensicTextLen];
};
RTC_NOINIT_ATTR ForensicEvent s_forensic[kForensicSlots];
RTC_NOINIT_ATTR uint32_t s_forensic_count;
RTC_NOINIT_ATTR uint32_t s_forensic_magic;
/* USB 重插实测是上电复位（reset_reason=1），RTC 内存也保不住；离线期间的
 * 事件另外落到 NVS（仅在没有 USB 会话时写，每条几毫秒，事件本就稀少），
 * 下次任一会话建立时以 [BB-prev] 倒出后清掉。 */
constexpr const char* kForensicNvsNs = "deskbot_bb";
ForensicEvent s_forensic_prev[kForensicSlots];
uint32_t s_forensic_prev_count = 0;
constexpr uint32_t kForensicNvsMinIntervalMs = 2000u;
uint32_t s_forensic_nvs_last_ms = 0;

void forensic_persist_to_nvs() {
  Preferences prefs;
  if (!prefs.begin(kForensicNvsNs, /*readOnly=*/false)) {
    return;
  }
  prefs.putBytes("ring", s_forensic, sizeof(s_forensic));
  prefs.putUInt("count", s_forensic_count);
  prefs.end();
}

void forensic_load_prev_from_nvs() {
  Preferences prefs;
  if (!prefs.begin(kForensicNvsNs, /*readOnly=*/true)) {
    return;
  }
  const size_t len = prefs.getBytesLength("ring");
  if (len == sizeof(s_forensic_prev)) {
    prefs.getBytes("ring", s_forensic_prev, len);
    s_forensic_prev_count = prefs.getUInt("count", 0);
  }
  prefs.end();
}

void forensic_clear_nvs() {
  Preferences prefs;
  if (prefs.begin(kForensicNvsNs, /*readOnly=*/false)) {
    prefs.clear();
    prefs.end();
  }
}

/* usb_transport 的活动链路以 Stream 为口径；这里给 BSD socket 包一层。 */
class TcpSocketStream : public Stream {
 public:
  int fd = -1;

  /* 并发安全的两段式关闭：其它任务（相机/音频写手）可能正拿着 fd 在
   * send()。立刻 close 是 lwIP 上的 use-after-free；先 shutdown 让并发
   * send 安全失败，fd 留在 s_dead_fd 里等 500ms 宽限期后再真正 close。 */
  void closeSocket() {
    if (fd >= 0) {
      /* 死亡现场快照：断开瞬间冻结本会话的 ack 序号与收发计数。
       * PC 报"seq=N ACK 超时"后新会话秒速重建会覆盖活值，事后只能
       * 从这份快照对账：prev_ack_rx_seq < N 即 PC→设备丢帧。 */
      prev_ack_rx_seq = usb_transport_last_ack_required_rx_seq();
      prev_ack_tx_seq = usb_transport_last_ack_tx_seq();
      prev_rx_bytes = rx_bytes;
      prev_tx_ok = tx_ok_count;
      const int dying = fd;
      fd = -1;
      shutdown(dying, SHUT_RDWR);
      if (dead_fd >= 0) {
        ::close(dead_fd);
      }
      dead_fd = dying;
      dead_since_ms = millis();
    }
  }

  void reapDeadSocket() {
    if (dead_fd >= 0 &&
        static_cast<uint32_t>(millis() - dead_since_ms) >= 500u) {
      ::close(dead_fd);
      dead_fd = -1;
    }
  }

  int dead_fd = -1;
  uint32_t dead_since_ms = 0;
  /* 每个套接字只取证一次的一次性标志；try_tcp_connect 换新 fd 时复位。 */
  bool fionread_fault_noted = false;
  bool first_rx_noted = false;

  bool connected() const { return fd >= 0; }

  int available() override {
    if (fd < 0) {
      return 0;
    }
    int pending = 0;
    if (ioctl(fd, FIONREAD, &pending) < 0) {
      if (!fionread_fault_noted) {
        fionread_fault_noted = true;
        wifi_link_forensic("fionread err=%d", errno);
      }
      return 0;
    }
    return pending;
  }

  int read() override {
    if (fd < 0) {
      return -1;
    }
    uint8_t value = 0;
    const int n = recv(fd, &value, 1, MSG_DONTWAIT);
    if (n == 1) {
      rx_bytes++;
      rx_last_ms = millis();
      if (!first_rx_noted) {
        first_rx_noted = true;
        wifi_link_forensic("first rx byte");
      }
      return value;
    }
    return -1;
  }

  int peek() override {
    if (fd < 0) {
      return -1;
    }
    uint8_t value = 0;
    const int n = recv(fd, &value, 1, MSG_DONTWAIT | MSG_PEEK);
    return n == 1 ? value : -1;
  }

  size_t write(uint8_t value) override { return write(&value, 1); }

  size_t write(const uint8_t* data, size_t length) override {
    if (fd < 0 || data == nullptr || length == 0) {
      return 0;
    }
    const int n = send(fd, data, length, 0);
    if (n > 0) {
      tx_ok_count++;
      return static_cast<size_t>(n);
    }
    /* 停发窗口取证：errno/时刻/计数留在内存，经 wifi_status 随时读出。 */
    tx_fail_count++;
    tx_last_errno = errno;
    tx_last_fail_ms = millis();
    return 0;
  }

  uint32_t tx_ok_count = 0;
  uint32_t tx_fail_count = 0;
  int tx_last_errno = 0;
  uint32_t tx_last_fail_ms = 0;
  uint32_t rx_bytes = 0;
  uint32_t rx_last_ms = 0;
  uint32_t prev_ack_rx_seq = 0;
  uint32_t prev_ack_tx_seq = 0;
  uint32_t prev_rx_bytes = 0;
  uint32_t prev_tx_ok = 0;

  void flush() override {}
};

TcpSocketStream s_stream;

volatile uint8_t s_last_disconnect_reason = 0;

void on_wifi_event(void*, esp_event_base_t base, int32_t id, void* data) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    if (data != nullptr) {
      s_last_disconnect_reason =
          static_cast<wifi_event_sta_disconnected_t*>(data)->reason;
    }
    s_sta_link_up = false;
    s_got_ip = false;
    s_sta_connect_sent = false;
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
    s_sta_link_up = true;
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    s_got_ip = true;
  }
}

bool load_config() {
  Preferences prefs;
  if (!prefs.begin(kPrefsNamespace, /*readOnly=*/true)) {
    return false;
  }
  String ssid = prefs.getString("ssid", "");
  String password = prefs.getString("pass", "");
  String host = prefs.getString("host", "");
  const uint16_t port = prefs.getUShort("port", 0);
  String token = prefs.getString("token", "");
  String core = prefs.getString("core", "");
  prefs.end();
  copy_bounded(s_ssid, sizeof(s_ssid), ssid.c_str());
  copy_bounded(s_password, sizeof(s_password), password.c_str());
  copy_bounded(s_host, sizeof(s_host), host.c_str());
  s_port = port;
  copy_bounded(s_token, sizeof(s_token), token.c_str());
  copy_bounded(s_core_id, sizeof(s_core_id), core.c_str());
  s_host_source = s_host[0] != '\0' ? HostSource::kStored : HostSource::kNone;
  return s_ssid[0] != '\0';
}

bool save_config(const char* ssid, const char* password, const char* host,
                 uint16_t port, const char* token, const char* core_id) {
  Preferences prefs;
  if (!prefs.begin(kPrefsNamespace, /*readOnly=*/false)) {
    return false;
  }
  prefs.putString("ssid", ssid != nullptr ? ssid : "");
  prefs.putString("pass", password != nullptr ? password : "");
  prefs.putString("host", host != nullptr ? host : "");
  prefs.putUShort("port", port);
  prefs.putString("token", token != nullptr ? token : "");
  prefs.putString("core", core_id != nullptr ? core_id : "");
  prefs.end();
  return true;
}

/* 只改 host/port（发现结果）：凭据与 core_id 原样保留。 */
void persist_host(const char* host, uint16_t port) {
  Preferences prefs;
  if (!prefs.begin(kPrefsNamespace, /*readOnly=*/false)) {
    return;
  }
  prefs.putString("host", host != nullptr ? host : "");
  prefs.putUShort("port", port);
  prefs.end();
}

const char* host_source_name(HostSource src) {
  switch (src) {
    case HostSource::kStored:
      return "stored";
    case HostSource::kMdns:
      return "mdns";
    case HostSource::kUdp:
      return "udp";
    default:
      return "none";
  }
}

bool lean_wifi_init() {
  if (s_wifi_inited) {
    return true;
  }
  esp_err_t err = esp_netif_init();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    log_error("[WIFI] esp_netif_init: 0x%x", err);
    return false;
  }
  err = esp_event_loop_create_default();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    log_error("[WIFI] event loop: 0x%x", err);
    return false;
  }
  if (s_netif == nullptr) {
    s_netif = esp_netif_create_default_wifi_sta();
  }
  if (s_netif == nullptr) {
    log_error("[WIFI] sta netif create failed");
    return false;
  }
  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  /* 只有 static_rx 预占内存（每个 ~1.6 KiB）；dynamic_* 是按需分配的上限，
   * 压得过低（实测 8）会在首个突发后把 TX 通道永久栓死——连重传单包都
   * 发不出去。static_rx=6 时代实测 PC→设备方向会单向黑洞 10s+（PC 内核
   * 重传收不到 ACK、设备应用层却完全健康）——下行音频突发耗尽接收缓冲、
   * 驱动丢收包的特征。AFE 出局后内存余量 ~110KB，加回到 16/40。 */
  cfg.static_rx_buf_num = 16;
  cfg.dynamic_rx_buf_num = 40;
  /* TX 缓冲静态化：开机初始化时一次预分配（此刻内存充裕），运行时发包
   * 不再依赖内部堆——整机满载后堆只剩几 KB，动态 TX 分配会静默失败，
   * 表现为发送方向整体冻结而接收正常。 */
  cfg.tx_buf_type = 0;
  cfg.static_tx_buf_num = 8;
  cfg.nvs_enable = 0;
  err = esp_wifi_init(&cfg);
  if (err != ESP_OK) {
    log_error("[WIFI] esp_wifi_init: 0x%x free_int=%u", err,
              static_cast<unsigned>(heap_caps_get_free_size(
                  MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
    return false;
  }
  esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &on_wifi_event,
                             nullptr);
  esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &on_wifi_event,
                             nullptr);
  esp_wifi_set_storage(WIFI_STORAGE_RAM);
  esp_wifi_set_mode(WIFI_MODE_STA);
  err = esp_wifi_start();
  if (err != ESP_OK) {
    log_error("[WIFI] esp_wifi_start: 0x%x", err);
    esp_wifi_deinit();
    return false;
  }
  /* 桌面供电设备：省电模式会给 TCP 握手引入 DTIM 延迟与丢包，直接关闭。 */
  esp_wifi_set_ps(WIFI_PS_NONE);
  /* 发射功率保持默认：供电假设已被 2A 供电对照否定，满功率的链路余量
   * 让空口重传更快收敛。 */
  s_wifi_inited = true;
  return true;
}

void start_sta() {
  if (s_ssid[0] == '\0' || !s_wifi_inited) {
    return;
  }
  s_last_sta_attempt_ms = millis();
  wifi_config_t sta{};
  copy_bounded(reinterpret_cast<char*>(sta.sta.ssid), sizeof(sta.sta.ssid),
               s_ssid);
  copy_bounded(reinterpret_cast<char*>(sta.sta.password),
               sizeof(sta.sta.password), s_password);
  sta.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
  sta.sta.threshold.authmode =
      s_password[0] != '\0' ? WIFI_AUTH_WPA_WPA2_PSK : WIFI_AUTH_OPEN;
  /* WPA3-SAE（手机热点常见默认）要求 PMF capable，否则关联直接被拒。 */
  sta.sta.pmf_cfg.capable = true;
  sta.sta.pmf_cfg.required = false;
  esp_wifi_set_config(WIFI_IF_STA, &sta);
  const esp_err_t err = esp_wifi_connect();
  s_sta_connect_sent = err == ESP_OK;
  log_warn("[WIFI] connecting to \"%s\" err=0x%x free_int=%u", s_ssid, err,
           static_cast<unsigned>(heap_caps_get_free_size(
               MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
}

void send_status_reply() {
  char json[1792];
  const size_t n = wifi_link_status_json(json, sizeof(json));
  if (n > 0) {
    (void)usb_transport_send_control_json(json);
  }
}

bool socket_alive(int fd) {
  uint8_t probe = 0;
  const int n = recv(fd, &probe, 1, MSG_DONTWAIT | MSG_PEEK);
  if (n > 0) {
    return true;
  }
  if (n == 0) {
    return false; /* orderly FIN */
  }
  return errno == EWOULDBLOCK || errno == EAGAIN;
}

void tcp_backoff() {
  s_tcp_retry_ms = s_tcp_retry_ms * 2;
  if (s_tcp_retry_ms > kTcpRetryMaxMs) {
    s_tcp_retry_ms = kTcpRetryMaxMs;
  }
}

/* 返回连上的 fd；失败返回 -1 并带原因日志（配网诊断的关键可见性）。 */
int open_tcp_to_host() {
  const int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (fd < 0) {
    log_warn("[WIFI] socket() failed errno=%d", errno);
    return -1;
  }
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(s_port);
  if (inet_pton(AF_INET, s_host, &addr.sin_addr) != 1) {
    ::close(fd);
    log_warn("[WIFI] invalid host \"%s\"", s_host);
    return -1;
  }
  /* 必须非阻塞：PC 侧防火墙静默丢包时，阻塞 connect 会挂起 loopTask 十几
   * 秒，直接触发 usb_poll_stall 看门狗重启。2s select 限时（覆盖首个 SYN
   * 重传窗口）。 */
  const int flags = fcntl(fd, F_GETFL, 0);
  fcntl(fd, F_SETFL, flags | O_NONBLOCK);
  const int rc =
      connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
  if (rc != 0 && errno != EINPROGRESS) {
    log_warn("[WIFI] connect(%s:%u) errno=%d", s_host,
             static_cast<unsigned>(s_port), errno);
    ::close(fd);
    return -1;
  }
  if (rc != 0) {
    fd_set wset;
    FD_ZERO(&wset);
    FD_SET(fd, &wset);
    timeval tv{};
    tv.tv_sec = 2;
    const int sel = select(fd + 1, nullptr, &wset, nullptr, &tv);
    if (sel <= 0) {
      log_warn("[WIFI] connect(%s:%u) timeout (sel=%d)", s_host,
               static_cast<unsigned>(s_port), sel);
      ::close(fd);
      return -1;
    }
    int soerr = 0;
    socklen_t len = sizeof(soerr);
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &len) != 0 ||
        soerr != 0) {
      log_warn("[WIFI] connect(%s:%u) soerr=%d", s_host,
               static_cast<unsigned>(s_port), soerr);
      ::close(fd);
      return -1;
    }
  }
  /* lwIP 的 F_SETFL 只接受纯 O_NONBLOCK 位；回填 F_GETFL 的原值（含
   * O_RDWR）会被整体拒绝，非阻塞位就永远清不掉，后续 send 全部
   * EWOULDBLOCK。显式写 0 恢复阻塞。 */
  (void)flags;
  (void)fcntl(fd, F_SETFL, 0);
  const int flag = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &flag, sizeof(flag));
  /* 单次 send 最多阻塞 250ms：写预算在 write_all 循环里、send 返回后才
   * 检查，1s 的 SNDTIMEO 会让一次拥塞把 loopTask 拖住一整秒（收帧/发确认
   * 全部停摆）。 */
  timeval io_tv{};
  io_tv.tv_usec = 250 * 1000;
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &io_tv, sizeof(io_tv));
  return fd;
}

/* ---- 按名字找 Core ------------------------------------------------------ */

void discovery_close_udp() {
  if (s_udp_fd >= 0) {
    ::close(s_udp_fd);
    s_udp_fd = -1;
  }
}

void discovery_finish() {
  if (s_mdns_search != nullptr) {
    mdns_query_async_delete(s_mdns_search);
    s_mdns_search = nullptr;
  }
  if (s_mdns_inited) {
    /* 查询结束就释放：mDNS 任务栈与收发缓冲占的是相机/音频争抢的内部 RAM。 */
    mdns_free();
    s_mdns_inited = false;
  }
  discovery_close_udp();
  s_discovery = DiscoveryPhase::kIdle;
  s_discovery_wanted = false;
  s_discovery_last_done_ms = millis();
  if (s_discovery_last_done_ms == 0) {
    s_discovery_last_done_ms = 1;
  }
}

/* 采用一条发现结果。core_id 不匹配的 Core 一律不认：同一局域网可能有别
 * 人的 Core。返回 true 表示 host 已确定（无论是否变化）。 */
bool discovery_adopt(const char* host, uint16_t port, const char* core_id,
                     HostSource src) {
  if (host == nullptr || host[0] == '\0' || port == 0) {
    return false;
  }
  if (s_core_id[0] != '\0' &&
      (core_id == nullptr || strcmp(core_id, s_core_id) != 0)) {
    log_warn("[WIFI] core %s at %s:%u is not mine (%s); ignored",
             core_id != nullptr ? core_id : "?", host,
             static_cast<unsigned>(port), s_core_id);
    return false;
  }
  const bool changed = strcmp(host, s_host) != 0 || port != s_port;
  if (changed) {
    copy_bounded(s_host, sizeof(s_host), host);
    s_port = port;
    persist_host(host, port);
    s_tcp_retry_ms = kTcpRetryMinMs;
    s_last_tcp_attempt_ms = 0;
    if (s_stream.connected()) {
      wifi_link_drop_connection("core moved");
    }
  }
  s_host_source = src;
  s_connect_failures = 0;
  wifi_link_forensic("core via %s %s:%u%s", host_source_name(src), host,
                     static_cast<unsigned>(port), changed ? " new" : "");
  log_warn("[WIFI] core found via %s: %s:%u id=%s%s", host_source_name(src),
           host, static_cast<unsigned>(port),
           core_id != nullptr ? core_id : "-", changed ? " (updated)" : "");
  return true;
}

bool discovery_mdns_start() {
  if (!s_mdns_inited) {
    if (mdns_init() != ESP_OK) {
      log_warn("[WIFI] mdns_init failed");
      return false;
    }
    s_mdns_inited = true;
    (void)mdns_hostname_set(get_device_id());
    if (s_netif != nullptr) {
      /* 接口早就 up 了；显式启用，别依赖 mdns 自己订阅的事件顺序。 */
      (void)mdns_netif_action(
          s_netif, static_cast<mdns_event_actions_t>(MDNS_EVENT_ENABLE_IP4 |
                                                     MDNS_EVENT_ANNOUNCE_IP4));
    }
  }
  s_mdns_search = mdns_query_async_new(nullptr, kDiscoveryService,
                                       kDiscoveryProto, MDNS_TYPE_PTR,
                                       kMdnsQueryMs, 4, nullptr);
  if (s_mdns_search == nullptr) {
    log_warn("[WIFI] mdns query alloc failed");
    return false;
  }
  return true;
}

/* 结果里挑 core_id 匹配的那条；没有配对 core_id（旧版 Core 配的网）时只在
 * 恰好一个候选时采用。 */
bool discovery_mdns_pick(mdns_result_t* results) {
  mdns_result_t* chosen = nullptr;
  unsigned candidates = 0;
  for (mdns_result_t* r = results; r != nullptr; r = r->next) {
    const char* core_id = nullptr;
    for (size_t i = 0; i < r->txt_count; ++i) {
      if (r->txt[i].key != nullptr && strcmp(r->txt[i].key, "core_id") == 0) {
        core_id = r->txt[i].value;
      }
    }
    bool has_v4 = false;
    for (mdns_ip_addr_t* a = r->addr; a != nullptr; a = a->next) {
      if (a->addr.type == ESP_IPADDR_TYPE_V4) {
        has_v4 = true;
      }
    }
    if (!has_v4 || r->port == 0) {
      continue;
    }
    candidates++;
    if (s_core_id[0] != '\0') {
      if (core_id != nullptr && strcmp(core_id, s_core_id) == 0) {
        chosen = r;
        break;
      }
    } else if (chosen == nullptr) {
      chosen = r;
    }
  }
  if (chosen == nullptr || (s_core_id[0] == '\0' && candidates != 1)) {
    return false;
  }
  char host[kMaxHostBytes] = "";
  for (mdns_ip_addr_t* a = chosen->addr; a != nullptr; a = a->next) {
    if (a->addr.type == ESP_IPADDR_TYPE_V4) {
      snprintf(host, sizeof(host), IPSTR, IP2STR(&a->addr.u_addr.ip4));
      break;
    }
  }
  const char* core_id = nullptr;
  for (size_t i = 0; i < chosen->txt_count; ++i) {
    if (chosen->txt[i].key != nullptr &&
        strcmp(chosen->txt[i].key, "core_id") == 0) {
      core_id = chosen->txt[i].value;
    }
  }
  return discovery_adopt(host, chosen->port, core_id, HostSource::kMdns);
}

void discovery_udp_send() {
  if (s_udp_fd < 0) {
    return;
  }
  char msg[192];
  const int n = snprintf(
      msg, sizeof(msg),
      "{\"type\":\"%s\",\"device_id\":\"%s\",\"core_id\":\"%s\",\"v\":1}",
      kDiscoverRequestType, get_device_id(), s_core_id);
  if (n <= 0 || static_cast<size_t>(n) >= sizeof(msg)) {
    return;
  }
  sockaddr_in to{};
  to.sin_family = AF_INET;
  to.sin_port = htons(kDiscoveryUdpPort);
  to.sin_addr.s_addr = htonl(INADDR_BROADCAST);
  (void)sendto(s_udp_fd, msg, static_cast<size_t>(n), 0,
               reinterpret_cast<sockaddr*>(&to), sizeof(to));
  /* 有的路由器不转 255.255.255.255；再发一份子网定向广播。 */
  esp_netif_ip_info_t info{};
  if (s_netif != nullptr && esp_netif_get_ip_info(s_netif, &info) == ESP_OK &&
      info.ip.addr != 0) {
    to.sin_addr.s_addr = info.ip.addr | ~info.netmask.addr;
    (void)sendto(s_udp_fd, msg, static_cast<size_t>(n), 0,
                 reinterpret_cast<sockaddr*>(&to), sizeof(to));
  }
  s_udp_last_send_ms = millis();
  s_udp_sends++;
}

bool discovery_udp_start() {
  s_udp_fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (s_udp_fd < 0) {
    log_warn("[WIFI] discover socket() errno=%d", errno);
    return false;
  }
  const int on = 1;
  setsockopt(s_udp_fd, SOL_SOCKET, SO_BROADCAST, &on, sizeof(on));
  (void)fcntl(s_udp_fd, F_SETFL, O_NONBLOCK);
  s_udp_sends = 0;
  discovery_udp_send();
  return true;
}

/* 非阻塞收一条应答；对端地址就是 Core 在本网段的地址。 */
bool discovery_udp_poll_reply() {
  if (s_udp_fd < 0) {
    return false;
  }
  char buf[320];
  sockaddr_in from{};
  socklen_t from_len = sizeof(from);
  const int n = recvfrom(s_udp_fd, buf, sizeof(buf) - 1, MSG_DONTWAIT,
                         reinterpret_cast<sockaddr*>(&from), &from_len);
  if (n <= 0) {
    return false;
  }
  buf[n] = '\0';
  StaticJsonDocument<384> doc;
  if (deserializeJson(doc, buf, static_cast<size_t>(n))) {
    return false;
  }
  const char* type = doc["type"] | "";
  if (strcmp(type, kDiscoverReplyType) != 0) {
    return false;
  }
  const char* core_id = doc["core_id"] | "";
  const uint16_t port = doc["port"] | 0;
  char host[kMaxHostBytes] = "";
  inet_ntoa_r(from.sin_addr, host, sizeof(host));
  return discovery_adopt(host, port, core_id, HostSource::kUdp);
}

void discovery_start() {
  s_discovery_started_ms = millis();
  s_connect_failures = 0;
  wifi_link_forensic("discover core id=%s", s_core_id[0] ? s_core_id : "-");
  if (discovery_mdns_start()) {
    s_discovery = DiscoveryPhase::kMdns;
    return;
  }
  if (discovery_udp_start()) {
    s_discovery = DiscoveryPhase::kUdp;
    s_discovery_started_ms = millis();
    return;
  }
  discovery_finish();
}

/* 每个 loop tick 推进一步；从不阻塞。 */
void discovery_poll() {
  const uint32_t now = millis();
  if (s_discovery == DiscoveryPhase::kMdns) {
    mdns_result_t* results = nullptr;
    uint8_t num = 0;
    if (s_mdns_search != nullptr &&
        !mdns_query_async_get_results(s_mdns_search, 0, &results, &num)) {
      if (static_cast<uint32_t>(now - s_discovery_started_ms) <
          kMdnsQueryMs + 500u) {
        return; /* 还在查 */
      }
    }
    const bool found = results != nullptr && discovery_mdns_pick(results);
    if (results != nullptr) {
      mdns_query_results_free(results);
    }
    if (s_mdns_search != nullptr) {
      mdns_query_async_delete(s_mdns_search);
      s_mdns_search = nullptr;
    }
    if (found) {
      discovery_finish();
      return;
    }
    log_warn("[WIFI] mdns: no matching core (%u result%s); trying udp",
             static_cast<unsigned>(num), num == 1 ? "" : "s");
    if (discovery_udp_start()) {
      s_discovery = DiscoveryPhase::kUdp;
      s_discovery_started_ms = now;
      return;
    }
    discovery_finish();
    return;
  }
  if (s_discovery == DiscoveryPhase::kUdp) {
    if (discovery_udp_poll_reply()) {
      discovery_finish();
      return;
    }
    if (s_udp_sends < 2 &&
        static_cast<uint32_t>(now - s_udp_last_send_ms) >= kUdpResendMs) {
      discovery_udp_send();
    }
    if (static_cast<uint32_t>(now - s_discovery_started_ms) >=
        kUdpDiscoverWaitMs) {
      log_warn("[WIFI] udp discover: no reply; using stored host %s:%u",
               s_host[0] ? s_host : "-", static_cast<unsigned>(s_port));
      wifi_link_forensic("discover none; stored %s", s_host[0] ? s_host : "-");
      discovery_finish();
    }
  }
}

void discovery_abort() {
  if (s_discovery != DiscoveryPhase::kIdle) {
    discovery_finish();
    s_discovery_last_done_ms = 0;
  }
}

void try_tcp_connect() {
  const int fd = open_tcp_to_host();
  if (fd < 0) {
    tcp_backoff();
    /* 连不上就怀疑地址过期：连续失败两次后重新找一遍（有 30s 节流）。 */
    if (++s_connect_failures >= kConnectFailsBeforeRediscover) {
      s_discovery_wanted = true;
    }
    return;
  }
  s_connect_failures = 0;
  s_stream.fd = fd;
  s_stream.fionread_fault_noted = false;
  s_stream.first_rx_noted = false;
  s_tcp_retry_ms = kTcpRetryMinMs;
  s_path_validated = true;
  wifi_link_forensic("tcp up %s:%u", s_host, static_cast<unsigned>(s_port));
  wifi_link_forensic_mem("mem@tcp");
  log_warn("[WIFI] link up to %s:%u", s_host, static_cast<unsigned>(s_port));
}

}  // namespace

void wifi_link_setup(void) {
  /* 上一次离线期间落在 NVS 里的事件先读进来，等会话建立时倒出。 */
  forensic_load_prev_from_nvs();
  /* 开机分隔符：黑匣子跨软复位续写，靠它区分是哪一次开机的事件。 */
  wifi_link_forensic("boot reset_reason=%d",
                     static_cast<int>(esp_reset_reason()));
  if (!load_config()) {
    log_info("[WIFI] no stored credentials; wireless link idle");
    return;
  }
  /* 防循环保险：上一次因启动期资源被挤而看门狗重启的话，本次不占 WiFi
   * 的内存，保住 USB 基本盘；下次干净启动再试。 */
  const char* prior = runtime_supervisor_last_reason();
  if (prior != nullptr && strstr(prior, "stall") != nullptr) {
    s_boot_init_blocked = true;
    log_warn("[WIFI] boot init skipped after restart reason=%s", prior);
    return;
  }
  if (!lean_wifi_init()) {
    return;
  }
  start_sta();
  log_warn("[WIFI] stack up free_int=%u",
           static_cast<unsigned>(heap_caps_get_free_size(
               MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
}

void wifi_link_poll(void) {
  const uint32_t now = millis();
  s_stream.reapDeadSocket();

  if (s_restart_at_ms != 0 &&
      static_cast<int32_t>(now - s_restart_at_ms) >= 0) {
    log_warn("[WIFI] restarting to apply provisioning");
    vTaskDelay(pdMS_TO_TICKS(50));
    ESP.restart();
  }

  if (s_ssid[0] == '\0') {
    return;
  }

  /* USB 主机在线：射频下电让出内部 RAM；USB 断开：重新拉起备用链路。 */
  const bool usb_host_active =
      usb_transport_is_active() && !usb_transport_active_link_is_wifi();
  if (usb_host_active) {
    /* USB 主机在线：只从 AP 断开关联（停止 TCP 与收发），**不再** deinit/stop
     * 整个 esp_wifi。反复 esp_wifi_deinit→esp_wifi_init 会让 PHY 射频层恢复出坏态
     * （nvs_enable=0，PHY 校准不持久化）——2026-09-04 台架实测：全程同一 USB 供电、
     * 仅让 Core 接管一次 USB 再放开，WiFi 就从 12ms/0% 变 100% 丢包且不自愈。保持
     * 协议栈常驻只多占 ~65KB 内部 RAM（USB 会话时 free_int 仍有 ~72KB，够用；当年
     * 为省内存关掉的 AFE 早已出局），射频状态始终健康，USB 离开即重连。 */
    if (s_wifi_inited && !s_radio_suspended) {
      discovery_abort();
      s_ip_seen = false;
      wifi_link_drop_connection("usb host active; radio parked");
      esp_wifi_disconnect();
      s_radio_suspended = true;
      s_sta_link_up = false;
      s_got_ip = false;
      s_sta_connect_sent = false;
      s_mtu_clamped = false;
      wifi_link_forensic("radio parked (usb host)");
      log_warn("[WIFI] radio parked (assoc dropped, stack resident) free_int=%u",
               static_cast<unsigned>(heap_caps_get_free_size(
                   MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
    }
    return;
  }
  if (s_radio_suspended) {
    /* USB 离开：协议栈一直在，只需重新关联；不做 esp_wifi_init（那正是坏态之源）。
     * 万一开机时因内存被挤没能 init（s_wifi_inited=false），才退回补做一次 init。 */
    if (static_cast<uint32_t>(now - s_last_sta_attempt_ms) < kStaRetryMs) {
      return;
    }
    s_last_sta_attempt_ms = now;
    if (!s_wifi_inited && !lean_wifi_init()) {
      wifi_link_forensic_mem("radio unpark init fail");
      return;
    }
    s_radio_suspended = false;
    start_sta();
    wifi_link_forensic("radio unpark");
    log_warn("[WIFI] radio unparked (reassociating) free_int=%u",
             static_cast<unsigned>(heap_caps_get_free_size(
                 MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
    return;
  }

  if (!s_wifi_inited) {
    return;
  }

  if (!s_got_ip) {
    s_mtu_clamped = false;
    if (s_ip_seen) {
      s_ip_seen = false;
      discovery_abort();
    }
    if (s_stream.connected()) {
      wifi_link_drop_connection("sta lost");
    }
    if (!s_sta_connect_sent &&
        static_cast<uint32_t>(now - s_last_sta_attempt_ms) >= kStaRetryMs) {
      start_sta();
    }
    return;
  }
  if (!s_ip_seen) {
    /* 每次拿到 IP 都先找一遍 Core：PC 换地址通常正是发生在设备离线期间。 */
    s_ip_seen = true;
    s_discovery_wanted = true;
    s_discovery_last_done_ms = 0;
  }
  if (s_discovery != DiscoveryPhase::kIdle) {
    discovery_poll();
    return;
  }
  if (s_discovery_wanted &&
      (s_discovery_last_done_ms == 0 ||
       static_cast<uint32_t>(now - s_discovery_last_done_ms) >=
           kDiscoveryRetryMs)) {
    discovery_start();
    return;
  }

  /* 历史注记：曾因“>576 段全丢”钳过 MTU——后证实那是设备内存饥荒时代
   * 动态收包缓冲分配失败的假象（AFE 让位修复后 1400B ping 畅通）。恢复
   * 默认 MTU：MSS 1440 比 536 少三倍报文数，双向语音时的空口占用决定性
   * 下降。 */
  (void)s_mtu_clamped;

  if (s_stream.connected()) {
    if (!socket_alive(s_stream.fd)) {
      wifi_link_drop_connection("peer closed");
    }
    return;
  }
  if (s_host[0] == '\0' || s_port == 0) {
    return;
  }
  if (static_cast<uint32_t>(now - s_last_tcp_attempt_ms) < s_tcp_retry_ms) {
    return;
  }
  /* USB 会话活着时 PC 会拒绝这条链路，正常不去打扰；但通路从未验证过时
   * 试连一次（成功即断开），把网络问题第一时间暴露到 USB 日志里。 */
  if (usb_transport_is_active() &&
      !usb_transport_active_link_is_wifi()) {
    if (s_path_validated) {
      return;
    }
    s_last_tcp_attempt_ms = now;
    const int fd = open_tcp_to_host();
    if (fd >= 0) {
      s_path_validated = true;
      shutdown(fd, SHUT_RDWR);
      ::close(fd);
      wifi_link_forensic("path probe ok");
      log_warn("[WIFI] path verified: %s:%u reachable", s_host,
               static_cast<unsigned>(s_port));
    } else {
      tcp_backoff();
    }
    return;
  }
  s_last_tcp_attempt_ms = now;
  try_tcp_connect();
}

Stream* wifi_link_stream(void) {
  return s_stream.connected() ? &s_stream : nullptr;
}

bool wifi_link_connected(void) {
  return s_stream.connected();
}

bool wifi_link_configured(void) {
  return s_ssid[0] != '\0';
}

bool wifi_link_boot_configured(void) {
  Preferences prefs;
  if (!prefs.begin(kPrefsNamespace, /*readOnly=*/true)) {
    return false;
  }
  const String ssid = prefs.getString("ssid", "");
  prefs.end();
  return ssid.length() > 0;
}

const char* wifi_link_token(void) {
  return s_token;
}

void wifi_link_drop_connection(const char* reason) {
  if (s_stream.connected()) {
    log_warn("[WIFI] link closed (%s)",
             reason != nullptr ? reason : "unspecified");
    wifi_link_forensic("tcp drop rx=%u: %s",
                       static_cast<unsigned>(s_stream.rx_bytes),
                       reason != nullptr ? reason : "unspecified");
  }
  s_stream.closeSocket();
}

size_t wifi_link_status_json(char* out, size_t out_size) {
  char ip[20] = "";
  if (s_got_ip && s_netif != nullptr) {
    esp_netif_ip_info_t info{};
    if (esp_netif_get_ip_info(s_netif, &info) == ESP_OK) {
      snprintf(ip, sizeof(ip), IPSTR, IP2STR(&info.ip));
    }
  }
  int rssi = 0;
  if (s_sta_link_up) {
    wifi_ap_record_t ap{};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
      rssi = ap.rssi;
    }
  }
  const int n = snprintf(
      out, out_size,
      "{\"type\":\"wifi_status\",\"configured\":%s,\"ssid\":\"%s\","
      "\"connected\":%s,\"ip\":\"%s\",\"rssi\":%d,\"link_up\":%s,"
      "\"host\":\"%s\",\"port\":%u,\"core_id\":\"%s\","
      "\"host_source\":\"%s\",\"sta_started\":%s,"
      "\"free_int\":%u,\"restart_pending\":%s,"
      "\"disconnect_reason\":%u,\"sw_restart_caller\":\"0x%08x\","
      "\"tx_ok\":%u,\"tx_fail\":%u,\"tx_last_errno\":%d,"
      "\"tx_fail_age_ms\":%u,\"poll_gap_ms\":%u,"
      "\"ack_rx_seq\":%u,\"ack_tx_seq\":%u,"
      "\"rx_bytes\":%u,\"rx_age_ms\":%u,"
      "\"prev_ack_rx_seq\":%u,\"prev_ack_tx_seq\":%u,"
      "\"prev_rx_bytes\":%u,\"prev_tx_ok\":%u,"
      "\"reset_reason\":%d,\"uptime_ms\":%u,"
      "\"transport\":\"%s\"}",
      s_ssid[0] != '\0' ? "true" : "false", s_ssid,
      s_got_ip ? "true" : "false", ip, rssi,
      s_stream.connected() ? "true" : "false", s_host,
      static_cast<unsigned>(s_port), s_core_id,
      host_source_name(s_host_source),
      s_wifi_inited ? "true" : "false",
      static_cast<unsigned>(heap_caps_get_free_size(
          MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
      s_restart_at_ms != 0 ? "true" : "false",
      static_cast<unsigned>(s_last_disconnect_reason),
      static_cast<unsigned>(runtime_supervisor_sw_restart_caller()),
      static_cast<unsigned>(s_stream.tx_ok_count),
      static_cast<unsigned>(s_stream.tx_fail_count),
      s_stream.tx_last_errno,
      static_cast<unsigned>(
          s_stream.tx_last_fail_ms == 0
              ? 0
              : millis() - s_stream.tx_last_fail_ms),
      static_cast<unsigned>(usb_transport_poll_max_gap_ms()),
      static_cast<unsigned>(usb_transport_last_ack_required_rx_seq()),
      static_cast<unsigned>(usb_transport_last_ack_tx_seq()),
      static_cast<unsigned>(s_stream.rx_bytes),
      static_cast<unsigned>(
          s_stream.rx_last_ms == 0 ? 0 : millis() - s_stream.rx_last_ms),
      static_cast<unsigned>(s_stream.prev_ack_rx_seq),
      static_cast<unsigned>(s_stream.prev_ack_tx_seq),
      static_cast<unsigned>(s_stream.prev_rx_bytes),
      static_cast<unsigned>(s_stream.prev_tx_ok),
      // 复位归因：esp_restart 走 sw_restart_caller，panic/看门狗/掉电只能
      // 靠芯片记录的 reset reason 区分（1=上电 3=SW 4=PANIC 5=INT_WDT
      // 6=TASK_WDT 7=WDT 9=BROWNOUT 11=USB 烧录复位）。
      static_cast<int>(esp_reset_reason()),
      static_cast<unsigned>(millis()),
      usb_transport_active_link_is_wifi() ? "wifi_tcp" : "usb_cdc");
  if (n <= 0 || static_cast<size_t>(n) >= out_size) {
    return 0;
  }
  /* 事件环以 "ev":["<ms> <text>",...] 追加在对象尾部；放不下就保持基础
   * JSON 原样返回（事件是取证增强，不能反过来弄丢基础状态）。 */
  size_t used = static_cast<size_t>(n) - 1;  // 吃掉收尾的 '}'
  char tail[kForensicSlots * (kForensicTextLen + 16) + 16];
  size_t tail_used = 0;
  int m = snprintf(tail, sizeof(tail), ",\"ev\":[");
  if (m > 0) {
    tail_used = static_cast<size_t>(m);
    const uint32_t total =
        s_forensic_magic == kForensicMagic ? s_forensic_count : 0;
    const uint32_t first =
        total > kForensicSlots ? total - kForensicSlots : 0;
    for (uint32_t seq = first; seq < total; ++seq) {
      const ForensicEvent& ev = s_forensic[seq % kForensicSlots];
      m = snprintf(tail + tail_used, sizeof(tail) - tail_used,
                   "%s\"%u %s\"", seq == first ? "" : ",",
                   static_cast<unsigned>(ev.ms), ev.text);
      if (m <= 0 || tail_used + static_cast<size_t>(m) >= sizeof(tail)) {
        tail_used = 0;
        break;
      }
      tail_used += static_cast<size_t>(m);
    }
    if (tail_used > 0) {
      m = snprintf(tail + tail_used, sizeof(tail) - tail_used, "]}");
      if (m == 2 && used + tail_used + 2 < out_size) {
        memcpy(out + used, tail, tail_used + 2);
        out[used + tail_used + 2] = '\0';
        return used + tail_used + 2;
      }
    }
  }
  return static_cast<size_t>(n);
}

void wifi_link_forensic(const char* fmt, ...) {
  if (s_forensic_magic != kForensicMagic) {
    /* 掉电后的脏 RTC 内存：清零再用。软复位保留旧事件，跨复位续写。 */
    memset(s_forensic, 0, sizeof(s_forensic));
    s_forensic_count = 0;
    s_forensic_magic = kForensicMagic;
  }
  ForensicEvent& slot = s_forensic[s_forensic_count % kForensicSlots];
  slot.ms = millis();
  va_list args;
  va_start(args, fmt);
  vsnprintf(slot.text, sizeof(slot.text), fmt, args);
  va_end(args);
  s_forensic_count++;
  /* 会话活着时同步发一条 WARN（USB 或 WiFi 都走 LOG 通道）；没会话时
   * usb_transport_send_log 会静默丢弃，事件仍留在环里等 dump。 */
  log_warn("[BB] %s", slot.text);
  /* 只有彻底没有会话（日志无处可发）时才落 NVS，且至少间隔 2 秒：
   * 有会话时日志已经回传 PC，再写 flash 纯属磨损——每确认一帧写一次
   * 曾在 4 分钟内写掉上千次。 */
  if (!usb_transport_is_active()) {
    const uint32_t now_ms = millis();
    if (s_forensic_nvs_last_ms == 0 ||
        static_cast<uint32_t>(now_ms - s_forensic_nvs_last_ms) >=
            kForensicNvsMinIntervalMs) {
      s_forensic_nvs_last_ms = now_ms;
      forensic_persist_to_nvs();
    }
  }
}

void wifi_link_forensic_mem(const char* tag) {
  wifi_link_forensic(
      "%s free=%u largest=%u", tag != nullptr ? tag : "mem",
      static_cast<unsigned>(
          heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
      static_cast<unsigned>(heap_caps_get_largest_free_block(
          MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
}

void wifi_link_forensic_dump(void) {
  if (s_forensic_prev_count > 0) {
    const uint32_t ptotal = s_forensic_prev_count;
    const uint32_t pfirst =
        ptotal > kForensicSlots ? ptotal - kForensicSlots : 0;
    for (uint32_t seq = pfirst; seq < ptotal; ++seq) {
      const ForensicEvent& ev = s_forensic_prev[seq % kForensicSlots];
      log_warn("[BB-prev] %u %s", static_cast<unsigned>(ev.ms), ev.text);
    }
    s_forensic_prev_count = 0;
    forensic_clear_nvs();
  }
  if (s_forensic_magic != kForensicMagic) {
    return;
  }
  const uint32_t total = s_forensic_count;
  const uint32_t first = total > kForensicSlots ? total - kForensicSlots : 0;
  for (uint32_t seq = first; seq < total; ++seq) {
    const ForensicEvent& ev = s_forensic[seq % kForensicSlots];
    log_warn("[BB-hist] %u %s", static_cast<unsigned>(ev.ms), ev.text);
  }
}

bool wifi_link_handle_control_json(const uint8_t* payload, size_t length) {
  if (payload == nullptr || length == 0 || length > 2048) {
    return false;
  }
  StaticJsonDocument<1024> doc;
  if (deserializeJson(doc, payload, length)) {
    return false;
  }
  const char* type = doc["type"] | "";
  if (strcmp(type, "wifi_status_req") == 0) {
    send_status_reply();
    return true;
  }
  if (strcmp(type, "wifi_config") != 0) {
    return false;
  }
  const char* ssid = doc["ssid"] | "";
  const char* password = doc["password"] | "";
  const char* host = doc["host"] | "";
  const uint16_t port = doc["port"] | 0;
  const char* token = doc["token"] | "";
  const char* core_id = doc["core_id"] | "";
  if (ssid[0] == '\0') {
    /* host-only 刷新（0.0.49）：Core 每次 USB 会话都把自己现在的地址与
     * 身份告诉设备。凭据不动、不重启；只在地址真变了时断开当前 TCP。 */
    if (s_ssid[0] == '\0' || (host[0] == '\0' && core_id[0] == '\0')) {
      log_warn("[WIFI] wifi_config rejected: empty ssid");
      send_status_reply();
      return true;
    }
    const bool host_changed =
        host[0] != '\0' && (strcmp(host, s_host) != 0 || (port != 0 && port != s_port));
    if (host[0] != '\0') {
      copy_bounded(s_host, sizeof(s_host), host);
    }
    if (port != 0) {
      s_port = port;
    }
    if (token[0] != '\0') {
      copy_bounded(s_token, sizeof(s_token), token);
    }
    if (core_id[0] != '\0') {
      copy_bounded(s_core_id, sizeof(s_core_id), core_id);
    }
    s_host_source = HostSource::kStored;
    if (!save_config(s_ssid, s_password, s_host, s_port, s_token, s_core_id)) {
      log_error("[WIFI] core binding not persisted (NVS begin failed)");
    }
    if (host_changed && s_stream.connected()) {
      wifi_link_drop_connection("core address updated");
    }
    log_warn("[WIFI] core binding refreshed host=%s:%u core=%s%s", s_host,
             static_cast<unsigned>(s_port), s_core_id[0] ? s_core_id : "-",
             host_changed ? " (changed)" : "");
    send_status_reply();
    return true;
  }
  if (!save_config(ssid, password, host, port, token, core_id)) {
    log_error("[WIFI] wifi_config not persisted (NVS begin failed)");
    send_status_reply();
    return true;
  }
  log_warn("[WIFI] provisioned ssid=\"%s\" host=%s:%u core=%s; reboot scheduled",
           ssid, host[0] != '\0' ? host : "-", static_cast<unsigned>(port),
           core_id[0] != '\0' ? core_id : "-");
  /* WiFi 驱动必须在启动早期初始化（运行时会 NO_MEM），所以回执发出后
   * 延迟 1.5s 整机重启入网。 */
  copy_bounded(s_ssid, sizeof(s_ssid), ssid);
  copy_bounded(s_password, sizeof(s_password), password);
  copy_bounded(s_host, sizeof(s_host), host);
  s_port = port;
  copy_bounded(s_token, sizeof(s_token), token);
  copy_bounded(s_core_id, sizeof(s_core_id), core_id);
  s_restart_at_ms = millis() + 1500u;
  if (s_restart_at_ms == 0) {
    s_restart_at_ms = 1;
  }
  send_status_reply();
  return true;
}
