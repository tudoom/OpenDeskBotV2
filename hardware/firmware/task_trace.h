#ifndef TASK_TRACE_H
#define TASK_TRACE_H

#include <Arduino.h>

#ifndef DESKBOT_TASK_STALL_MS
#define DESKBOT_TASK_STALL_MS 8000
#endif
#ifndef DESKBOT_TASK_HEARTBEAT_MS
#define DESKBOT_TASK_HEARTBEAT_MS 5000
#endif

/** 开始顶层任务（会覆盖上一任务状态）。 */
void log_task_begin(const char* task, const char* detail = nullptr);
/** 结束当前任务并打印总耗时。result 可为 nullptr。 */
void log_task_end(const char* result = nullptr);
/** 进入子阶段；重置该阶段的 stall 计时。 */
void log_task_phase(const char* phase, const char* detail = nullptr);
/** 长循环内调用：更新 detail，并按间隔打印 ALIVE 心跳。 */
void log_task_pump(const char* detail = nullptr);
/** 主 loop 调用：阶段超过 DESKBOT_TASK_STALL_MS 时打印 STALL 告警。 */
void log_task_tick();
/** 串口 task 命令：打印当前任务快照。 */
void log_task_dump();

#ifndef DESKBOT_STACK_HEAP_INFO_MS
#define DESKBOT_STACK_HEAP_INFO_MS 5000
#endif
#ifndef DESKBOT_STACK_HEAP_WARN_MS
#define DESKBOT_STACK_HEAP_WARN_MS 30000
#endif

/**
 * 主 loop 调用：栈/堆快照（移植自早期分支的 task_trace.cpp log_stack_heap_tick）。
 * 每 DESKBOT_STACK_HEAP_INFO_MS 打一遍各应用任务的 alloc/used_peak/free_min
 * （log_info，只上串口监视器），每 DESKBOT_STACK_HEAP_WARN_MS 打一行紧凑的
 * log_warn（会经 LOG 通道转发到 PC）：全部任务里最紧的栈 + 内部/PSRAM 堆
 * free/min/largest。tick 内不做任何动态分配。
 */
void log_stack_heap_tick();

/**
 * 启动 CPU 统计：双核 tick hook 采样 + 每 5s 打印各任务 CPU 占比。
 * （Arduino-ESP32 2.x 预编译库无 vTaskGetRunTimeStats，用等价采样实现。）
 */
void task_setup_cpu_runtime_stats();

class LogTaskScope {
 public:
  LogTaskScope(const char* task, const char* detail = nullptr);
  ~LogTaskScope();
  void phase(const char* phase, const char* detail = nullptr) { log_task_phase(phase, detail); }
  void pump(const char* detail = nullptr) { log_task_pump(detail); }
  LogTaskScope(const LogTaskScope&) = delete;
  LogTaskScope& operator=(const LogTaskScope&) = delete;
};

#define LOG_TASK_SCOPE(task) LogTaskScope _log_task_scope_##__LINE__(task)
#define LOG_TASK_SCOPE_D(task, detail) LogTaskScope _log_task_scope_##__LINE__(task, detail)

#endif
