#ifndef DESKBOT_TASK_H
#define DESKBOT_TASK_H

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

/**
 * 创建 pinned 任务，栈优先放 PSRAM（移植自早期分支的 utils_task_create_pinned）。
 *
 * 成功路径：xTaskCreateStaticPinnedToCore + heap_caps_malloc(SPIRAM) 栈 +
 * heap_caps_malloc(INTERNAL) TCB（TCB 必须在内部 RAM）；任一分配/创建失败
 * 则释放已分配部分并回落 xTaskCreatePinnedToCore（内部堆）。stack_bytes
 * 单位是字节（ESP-IDF 约定，StackType_t 为 1 字节）。
 *
 * 依赖 CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY（见 firmware/sdkconfig.defaults；
 * 预编译 Arduino SDK 已开启 CONFIG_FREERTOS_TASK_CREATE_ALLOW_EXT_MEM）。
 *
 * 适用前提（ESP-IDF 对 PSRAM 栈的限制）：该任务**绝不能写 flash/NVS**
 * （Preferences / nvs_set / esp_partition_write / esp_ota_*）——flash 写入
 * 期间 cache 关闭，执行写入的任务栈必须在内部 RAM。只读 flash（mmap 的
 * ESP-SR 模型、常量表）不受影响。loopTask 写 NVS（黑匣子、pb 存储、配网），
 * 所以永远不走这里。
 *
 * 这样创建的任务视为常驻：vTaskDelete 之后静态栈/TCB 不会被内核回收
 * （仅在一次性的失败清理路径上出现，调用点有注释）。
 *
 * @param out_stack_in_psram 可选：报告栈最终落在 PSRAM（true）还是内部堆。
 * @return pdPASS / pdFAIL，与 xTaskCreatePinnedToCore 一致。
 */
BaseType_t deskbot_task_create_pinned(TaskFunction_t fn, const char* name,
                                      uint32_t stack_bytes, void* arg,
                                      UBaseType_t priority,
                                      TaskHandle_t* out_handle,
                                      BaseType_t core_id,
                                      bool* out_stack_in_psram = nullptr);

#endif
