#include "logger.h"
#include "usb_transport.h"
#include <HWCDC.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

// Default log level is INFO
uint8_t global_log_level = LOG_LEVEL_INFO;

// Set the global log level
void log_set_level(uint8_t level) {
    global_log_level = level;
}

namespace {

void emit_bytes(const char* text, size_t length, bool newline) {
    if (text == nullptr) {
        return;
    }
    /*
     * Once a host has completed DBOT hello, never fall back to raw HWCDC:
     * even one printf would make PB/audio framing ambiguous.  During early
     * boot (before the first hello) plain logs remain useful for flashing and
     * bring-up; the host parser deliberately resynchronises past them.
     */
    if (usb_transport_framed_mode()) {
        char framed[384];
        size_t n = length;
        if (n > sizeof(framed) - (newline ? 2u : 1u)) {
            n = sizeof(framed) - (newline ? 2u : 1u);
        }
        memcpy(framed, text, n);
        if (newline) {
            framed[n++] = '\n';
        }
        framed[n] = '\0';
        (void)usb_transport_send_log(framed, n);
        return;
    }
    HWCDCSerial.write(reinterpret_cast<const uint8_t*>(text), length);
    if (newline) {
        HWCDCSerial.write('\n');
    }
}

void emit_log(const char* level, const char* format, va_list args) {
    char line[352];
    const unsigned long ms = millis();
    const unsigned long seconds = ms / 1000;
    const unsigned long minutes = seconds / 60;
    const unsigned long hours = minutes / 60;
    const int prefix_len =
        snprintf(line, sizeof(line), "[%02lu:%02lu:%02lu.%03lu] [%s] ",
                 hours % 24, minutes % 60, seconds % 60, ms % 1000, level);
    if (prefix_len < 0) {
        return;
    }
    const size_t offset =
        static_cast<size_t>(prefix_len) < sizeof(line)
            ? static_cast<size_t>(prefix_len)
            : sizeof(line) - 1u;
    vsnprintf(line + offset, sizeof(line) - offset, format, args);
    emit_bytes(line, strnlen(line, sizeof(line)), true);
}

}  // namespace

// Log functions implementation
void log_error(const char* format, ...) {
    if (global_log_level >= LOG_LEVEL_ERROR) {
        va_list args;
        va_start(args, format);
        emit_log("ERROR", format, args);
        va_end(args);
    }
}

void log_warn(const char* format, ...) {
    if (global_log_level >= LOG_LEVEL_WARN) {
        va_list args;
        va_start(args, format);
        emit_log("WARN ", format, args);
        va_end(args);
    }
}

void log_info(const char* format, ...) {
    if (global_log_level >= LOG_LEVEL_INFO) {
        va_list args;
        va_start(args, format);
        emit_log("INFO ", format, args);
        va_end(args);
    }
}

void log_debug(const char* format, ...) {
    if (global_log_level >= LOG_LEVEL_DEBUG) {
        va_list args;
        va_start(args, format);
        emit_log("DEBUG", format, args);
        va_end(args);
    }
}

void log_trace(const char* format, ...) {
    if (global_log_level >= LOG_LEVEL_TRACE) {
        va_list args;
        va_start(args, format);
        emit_log("TRACE", format, args);
        va_end(args);
    }
}

// Raw print functions without prefix
void log_print(const char* format, ...) {
    va_list args;
    va_start(args, format);
    char buf[256];
    vsnprintf(buf, sizeof(buf), format, args);
    emit_bytes(buf, strnlen(buf, sizeof(buf)), false);
    va_end(args);
}

void log_println(const char* format, ...) {
    va_list args;
    va_start(args, format);
    char buf[256];
    vsnprintf(buf, sizeof(buf), format, args);
    emit_bytes(buf, strnlen(buf, sizeof(buf)), true);
    va_end(args);
}
