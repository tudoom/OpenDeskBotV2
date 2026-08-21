#ifndef DESKBOT_RUNTIME_SUPERVISOR_H
#define DESKBOT_RUNTIME_SUPERVISOR_H

#include <Arduino.h>

/** Start the independent runtime liveness task after peripheral setup. */
bool runtime_supervisor_start();

/** Progress marker owned by the USB parser. */
void runtime_supervisor_mark_usb_poll();

/** Retained recovery telemetry included in the next USB hello_ack. */
const char* runtime_supervisor_last_reason();
uint32_t runtime_supervisor_recovery_count();
uint32_t runtime_supervisor_boot_count();
uint32_t runtime_supervisor_last_restart_uptime_ms();

/** Hardware/ROM reason for the current boot, independent of retained state. */
const char* runtime_supervisor_hardware_reset_reason();
uint32_t runtime_supervisor_hardware_reset_code();
bool runtime_supervisor_last_reset_was_panic();

#endif
