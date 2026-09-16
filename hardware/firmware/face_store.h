#pragma once
/*
 * 待机卡通脸持久化（0.0.57）。
 *
 * PC 在待机（idle）位图表情的 PB 里带 face_keep=true / face_tag，固件把那条时间线留在
 * PSRAM（见 display_standby_face_*）；PC 随后发 CONTROL_JSON {"type":"face_persist"}，
 * 这里把它写进 FFat（/face/meta.json + anim.json + a<i>.jpg），开机时读回，
 * 断电、重启、换电脑都保持最后设定的表情。
 *
 * CONTROL_JSON：
 *   {"type":"face_persist"}    → {"type":"face_persist_ack","ok":bool,"tag":"…","bytes":N[,"already":true][,"error":"…"]}
 *                                （写 FFat 在独立任务里做，回执可能晚 1～2 s；同一标签已在盘上则直接 already）
 *   {"type":"face_clear"}      → {"type":"face_clear_ack","ok":true}（删文件 + 清 PSRAM，回矢量待机脸）
 *   {"type":"face_status_req"} → {"type":"face_status_ack","tag":"…","persisted":bool}
 * hello 里的 face_tag 就是当前待机脸的标签，PC 据此判断是否需要重推重存。
 */
#include <stddef.h>
#include <stdint.h>

/** 开机：FFat 里有待机脸就读回 display（返回 true）；没有 / 损坏返回 false。 */
bool face_store_load();
/** CONTROL_JSON 分发：处理了返回 true（含回执）。 */
bool face_store_handle_control_json(const uint8_t* payload, size_t length);
/** FFat 里保存的标签（空串 = 未保存）。 */
const char* face_store_persisted_tag();
