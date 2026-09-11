#include "deskbot_task.h"

#include <esp_heap_caps.h>

BaseType_t deskbot_task_create_pinned(TaskFunction_t fn, const char* name,
                                      uint32_t stack_bytes, void* arg,
                                      UBaseType_t priority,
                                      TaskHandle_t* out_handle,
                                      BaseType_t core_id,
                                      bool* out_stack_in_psram) {
  if (out_stack_in_psram != nullptr) {
    *out_stack_in_psram = false;
  }
  if (fn == nullptr || name == nullptr || stack_bytes < 1024u) {
    return pdFAIL;
  }
  /* 栈放 PSRAM，TCB 留内部 RAM（FreeRTOS 要求 TCB 内部可访问）。 */
  StackType_t* stack = static_cast<StackType_t*>(
      heap_caps_malloc(stack_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  StaticTask_t* tcb = static_cast<StaticTask_t*>(heap_caps_malloc(
      sizeof(StaticTask_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (stack != nullptr && tcb != nullptr) {
    const TaskHandle_t handle = xTaskCreateStaticPinnedToCore(
        fn, name, stack_bytes / sizeof(StackType_t), arg, priority, stack, tcb,
        core_id);
    if (handle != nullptr) {
      if (out_handle != nullptr) {
        *out_handle = handle;
      }
      if (out_stack_in_psram != nullptr) {
        *out_stack_in_psram = true;
      }
      return pdPASS;
    }
  }
  /* 无 PSRAM / 分配失败 / 静态创建失败：不留半截分配，回落动态创建。 */
  heap_caps_free(stack);
  heap_caps_free(tcb);
  return xTaskCreatePinnedToCore(fn, name, stack_bytes, arg, priority,
                                 out_handle, core_id);
}
