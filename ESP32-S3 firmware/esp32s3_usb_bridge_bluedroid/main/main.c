/*
 * Switch2Connect - A Python and ESP32-S3 bridge utility for Switch 2 controller inputs.
 * Copyright (C) 2026 TommyWabg
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 *
 * Contact Information:
 * Electronic Mail: tommyw9318@gmail.com
 */

// ESP32-S3 USB <-> BLE bridge for Switch 2 controllers — BLUEDROID variant.
//
// Goal: see whether Bluedroid's multi-task host (BTU/BTC) beats NimBLE's single-host-
// task ~33 rumble-writes/s/channel ceiling.  Same on-the-wire protocol & same USB-CDC
// command protocol as the NimBLE build, so the existing Python host drives it unchanged.
//
// MULTI-CONTROLLER MODEL (matches NimBLE's per-conn_handle isolation): one GATTC app
// interface (gattc_if) per channel.  Every GATTC event is tagged with its gattc_if, so
// connections with IDENTICAL GATT handle layouts (two Joy-Cons!) never get confused —
// the REG_FOR_NOTIFY event in particular carries no conn_id, so a single shared gattc_if
// could not tell the two apart (it reported the 2nd controller's subscribe against the
// 1st).  Per-channel gattc_if fixes that.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_ble_api.h"
#include "esp_timer.h"
#include "esp_gattc_api.h"
#include "esp_gatt_defs.h"
#include "esp_gatt_common_api.h"

#include "tinyusb.h"
#include "tusb_cdc_acm.h"

static const char *TAG = "S3_BLUEDROID";

#define APP_FIRMWARE_VERSION      "0.12.3"
#define EXPECTED_FIRMWARE_PROFILE "tinyusb_direct"
#define EXPECTED_FIRMWARE_BUILD   "cdc_bridge_1"
#define CDC_LINE_STATE_DTR        0x01
#define NINTENDO_COMPANY_ID       0x0553
#define MAX_CH                    8     // one GATTC app per channel
#define REPORT_SIZE               64


// --- Switch 2 GATT UUIDs (128-bit; little-endian in esp_bt_uuid_t = string reversed) ---
#define UUID128(b0,b1,b2,b3,b4,b5,b6,b7,b8,b9,b10,b11,b12,b13,b14,b15) \
    { .len = ESP_UUID_LEN_128, .uuid = { .uuid128 = { \
        b15,b14,b13,b12,b11,b10,b9,b8,b7,b6,b5,b4,b3,b2,b1,b0 } } }
static const esp_bt_uuid_t UUID_NOTIFY_FD2 =
    UUID128(0xab,0x7d,0xe9,0xbe,0x89,0xfe,0x49,0xad,0x82,0x8f,0x11,0x8f,0x09,0xdf,0x7f,0xd2);
static const esp_bt_uuid_t UUID_NOTIFY_LEGACY =
    UUID128(0x74,0x92,0x86,0x6c,0xec,0x3e,0x46,0x19,0x82,0x58,0x32,0x75,0x5f,0xfc,0xc0,0xf8);
static const esp_bt_uuid_t UUID_ACK =
    UUID128(0xc7,0x65,0xa9,0x61,0xd9,0xd8,0x4d,0x36,0xa2,0x0a,0x53,0x15,0xb1,0x11,0x83,0x6a);
static const esp_bt_uuid_t UUID_CMD =
    UUID128(0x64,0x9d,0x4a,0xc9,0x8e,0xb7,0x4e,0x6c,0xaf,0x44,0x1e,0xa5,0x4f,0xe5,0xf0,0x05);
static const esp_bt_uuid_t UUID_RUMBLE_PRO =
    UUID128(0xcc,0x48,0x3f,0x51,0x92,0x58,0x42,0x7d,0xa9,0x39,0x63,0x0c,0x31,0xf7,0x2b,0x05);
static const esp_bt_uuid_t UUID_RUMBLE_JOYCON_R =
    UUID128(0xfa,0x19,0xb0,0xfb,0xcd,0x1f,0x46,0xa7,0x84,0xa1,0xbb,0xb0,0x9e,0x00,0xc1,0x49);
static const esp_bt_uuid_t UUID_RUMBLE_JOYCON_L =
    UUID128(0x28,0x93,0x26,0xcb,0xa4,0x71,0x48,0x5d,0xa8,0xf4,0x24,0x0c,0x14,0xf1,0x82,0x41);
static const esp_bt_uuid_t UUID_CCCD =
    { .len = ESP_UUID_LEN_16, .uuid = { .uuid16 = ESP_GATT_UUID_CHAR_CLIENT_CONFIG } };

// --- per-controller channel: fixed slot, each owns one GATTC app interface ---
typedef struct {
    esp_gatt_if_t gattc_if;  // assigned once at REG_EVT (channel == app_id); permanent
    bool     used;           // a controller is connected on this slot
    bool     ready;          // discovered + input subscribed
    bool     connecting;
    uint16_t conn_id;
    esp_bd_addr_t bda;
    uint8_t  addr_type;
    uint16_t input_handle;
    uint16_t fd2_handle;     // canonical SW2 input notify (ab7de9be…), if present
    uint16_t legacy_handle;  // legacy input notify (74928 66c…), if present
    uint16_t ack_handle;
    uint16_t cmd_handle;
    uint16_t rumble_handle;
    uint16_t itvl;           // connection interval in 1.25 ms units (6=7.5ms, 12=15ms)
    uint8_t  input_src;      // which UUID set input_handle: 1=FD2, 2=legacy (diagnostic)
    bool     prefer_legacy;  // NSO GameCube: input is on the LEGACY char, not FD2
} channel_t;
static channel_t s_ch[MAX_CH];



static volatile bool s_scan_mode = false;
static int s_pending_conn = -1;   // channel waiting to open once the scan has stopped
// 3rd-controller establishment: two links pinned at 7.5ms saturate the radio so the
// controller cannot even schedule a 3rd connection's SETUP (reason 0x100 CONN_CANCEL,
// regardless of the 3rd's requested interval).  Workaround: temporarily widen the
// existing links to 15ms to free radio time, defer the 3rd open until that settles,
// then restore the widened links to 7.5ms once the 3rd link is established.
static volatile uint32_t s_conn_open_after = 0;  // tick deadline to open a deferred pending conn (0 = open immediately)
static volatile uint8_t  s_widened_mask  = 0;    // links temporarily widened to 15ms; restored after the 3rd connects
static char s_own_mac[18] = "00:00:00:00:00:00";

// Deferred scan resume.  Starting a scan from inside a GATTC callback while another
// GAP op (a 2nd disconnect, a connect) is still in flight collides on the HCI command
// path and silently drops that op (symptom: only one of a merged pair disconnects).
// So callbacks NEVER start scanning directly — they set s_resume_scan and bump a
// "GAP busy" deadline; cdc_task starts the scan only once the bus has been quiet.
//
// gap_busy() uses ONLY-EXTEND semantics: it never shortens a deadline already in the
// future.  kick_disc_queue sets a 400 ms guard before each gap_disconnect; a
// DISCONNECT_EVT must NOT shorten that window or the next queued disconnect fires
// before the HCI path has settled.
static volatile bool     s_resume_scan = false;
static volatile uint32_t s_gap_busy_until = 0;   // ms tick until GAP is considered busy
static inline uint32_t now_ms(void) { return (uint32_t)xTaskGetTickCount(); }  // FREERTOS_HZ=1000
static inline void gap_busy(uint32_t ms) {
    uint32_t t = now_ms() + ms;
    // Only extend the deadline, never shorten it (cast keeps wrap-around safe).
    if ((int32_t)(t - s_gap_busy_until) > 0) s_gap_busy_until = t;
}

// Sequential disconnect queue.
// Bluedroid's HCI path can safely process only ONE gap_disconnect at a time.  Issuing
// several in a tight loop (the old do_disc_all) corrupts the BLE controller's internal
// state → crash on the 3rd controller or after a disc_all during an active session.
// Solution: callers set bits in s_disc_mask; kick_disc_queue() issues ONE disconnect,
// sets s_disc_in_flight, and the DISCONNECT_EVT completion clears it and calls
// kick_disc_queue() again for the next pending channel.
static volatile uint8_t s_disc_mask      = 0;      // bitmask: channels queued for disconnect
static volatile bool    s_disc_in_flight = false;   // true while a gap_disconnect is pending
static portMUX_TYPE     s_disc_mux = portMUX_INITIALIZER_UNLOCKED;

// Issue the next queued disconnect (if none is currently in flight).
// Safe to call from cdc_task (core 1) AND from DISCONNECT_EVT (BTC task, core 0).
static void kick_disc_queue(void) {
    portENTER_CRITICAL(&s_disc_mux);
    if (s_disc_in_flight || s_disc_mask == 0) {
        portEXIT_CRITICAL(&s_disc_mux);
        return;
    }
    // Find the lowest-indexed pending channel that is still in use.
    int ch = -1;
    for (int i = 0; i < MAX_CH; i++) {
        if (s_disc_mask & (1u << i)) {
            if (s_ch[i].used) { ch = i; break; }
            s_disc_mask &= ~(1u << i);  // already gone, clear and skip
        }
    }
    if (ch < 0) {
        // Queue drained; all channels already released — safe to resume scanning.
        bool was_last = (s_disc_mask == 0);
        portEXIT_CRITICAL(&s_disc_mux);
        if (was_last && s_scan_mode) s_resume_scan = true;
        return;
    }
    s_disc_mask      &= ~(1u << ch);
    s_disc_in_flight  = true;
    portEXIT_CRITICAL(&s_disc_mux);
    gap_busy(400);                        // hold off scan until this disconnect settles
    esp_ble_gap_disconnect(s_ch[ch].bda);
}

static int ch_by_if(esp_gatt_if_t gif) {
    for (int i = 0; i < MAX_CH; i++) if (s_ch[i].gattc_if == gif) return i;
    return -1;
}

static int ch_by_bda(esp_bd_addr_t bda) {
    for (int i = 0; i < MAX_CH; i++) {
        if (s_ch[i].used && memcmp(s_ch[i].bda, bda, sizeof(esp_bd_addr_t)) == 0) return i;
    }
    return -1;
}
static int ch_alloc(void) {
    for (int i = 0; i < MAX_CH; i++)
        if (s_ch[i].gattc_if != ESP_GATT_IF_NONE && !s_ch[i].used) return i;
    return -1;
}
static uint8_t ch_active_mask(void) {
    uint8_t m = 0; for (int i = 0; i < MAX_CH; i++) if (s_ch[i].used && s_ch[i].ready) m |= (1u << i);
    return m;
}
// Count slots in use (reserved/connecting) and fully ready (input subscribed).
// Used by the connection diagnostics so a 3rd-controller failure log shows how many
// links were already live when it was attempted.
static void ch_count(int *used, int *ready) {
    int u = 0, r = 0;
    for (int i = 0; i < MAX_CH; i++) if (s_ch[i].used) { u++; if (s_ch[i].ready) r++; }
    if (used)  *used  = u;
    if (ready) *ready = r;
}

// --- USB-CDC transport ---
static QueueHandle_t s_cmd_queue;   // inbound command lines
static QueueHandle_t s_ack_queue;   // ack/cmd notifications (P0)
static QueueHandle_t s_out_queue;   // outbound JSON lines from BLE callbacks
static volatile bool s_request_status = false;
typedef struct { char text[256]; } line_t;
typedef struct { uint8_t ch; uint8_t len; uint8_t data[REPORT_SIZE]; } in_report_t;
static char s_rx_buf[512];
static int  s_rx_len = 0;
static in_report_t s_in_shadow[MAX_CH];
static volatile bool s_in_dirty[MAX_CH];
static portMUX_TYPE s_in_mux = portMUX_INITIALIZER_UNLOCKED;

// --- Jitter Buffer (FIFO) for Audio Haptics ---
#define RUMBLE_QUEUE_SIZE 5
typedef struct {
    int ch;
    uint8_t data[64];
    size_t len;
} rumble_pkt_t;

static QueueHandle_t s_rumble_queue;
static TaskHandle_t s_rumble_task_h;

static void rumble_playout_task(void *arg) {
    rumble_pkt_t pkt;
    while (1) {
        if (xQueueReceive(s_rumble_queue, &pkt, portMAX_DELAY)) {
            if (s_ch[pkt.ch].used && s_ch[pkt.ch].rumble_handle) {
                esp_ble_gattc_write_char(s_ch[pkt.ch].gattc_if, s_ch[pkt.ch].conn_id, s_ch[pkt.ch].rumble_handle, pkt.len, pkt.data,
                                         ESP_GATT_WRITE_TYPE_NO_RSP, ESP_GATT_AUTH_REQ_NONE);
            }
            // Strict 15ms minimum gap between packets as requested
            vTaskDelay(pdMS_TO_TICKS(15));
        }
    }
}

static bool cdc_host_ready(void) {
    return tud_cdc_connected() && (tud_cdc_get_line_state() & CDC_LINE_STATE_DTR);
}
static void safe_cdc_write(const uint8_t *data, uint32_t len) {
    uint32_t w = 0, t = 0;
    while (w < len && t < 100) {
        if (!cdc_host_ready()) { vTaskDelay(pdMS_TO_TICKS(1)); t++; continue; }
        uint32_t avail = tud_cdc_write_available();
        if (avail > 0) {
            uint32_t n = (len - w) > avail ? avail : (len - w);
            tud_cdc_write(data + w, n); tud_cdc_write_flush(); w += n; t = 0;
        } else { vTaskDelay(pdMS_TO_TICKS(1)); t++; }
    }
}
static void send_json(const char *s) { safe_cdc_write((const uint8_t *)s, strlen(s)); }  // cdc_task only
// out_json/out_debug: enqueue for cdc_task to send.  MUST be used from BLE callback
// (BTC task) context — calling safe_cdc_write there can block up to 100 ms and, under a
// flood of scan_results (a controller in pairing mode), stalls the BLE host -> crash.
static void out_json(const char *s) {
    if (!s_out_queue) return;
    line_t L; strncpy(L.text, s, sizeof(L.text) - 1); L.text[sizeof(L.text) - 1] = '\0';
    xQueueSend(s_out_queue, &L, 0);   // drop if full (back-pressure, never block)
}
static void out_debug(const char *msg) {
    char b[200]; snprintf(b, sizeof(b), "{\"cmd\":\"debug\",\"msg\":\"%s\"}\n", msg); out_json(b);
}
static void send_status_response(void) {
    char b[256];
    snprintf(b, sizeof(b),
        "{\"cmd\":\"status\",\"version\":\"%s\",\"profile\":\"%s\",\"build\":\"%s\","
        "\"ble_channels\":%u,\"mac\":\"%s\",\"features\":{\"wrpair\":1,\"shadow\":1}}\n",
        APP_FIRMWARE_VERSION, EXPECTED_FIRMWARE_PROFILE, EXPECTED_FIRMWARE_BUILD,
        (unsigned)ch_active_mask(), s_own_mac);
    send_json(b);
}
// CDC frame: 0xaa 0x55 <len=payload+1> <chan|0x80 if cmd> <payload...>
static void send_report_frame(uint8_t channel, const uint8_t *payload, uint8_t plen, bool is_cmd) {
    if (plen > REPORT_SIZE) plen = REPORT_SIZE;
    uint8_t hdr[4] = { 0xaa, 0x55, (uint8_t)(plen + 1),
                       (uint8_t)((channel + 1) | (is_cmd ? 0x80 : 0x00)) };
    safe_cdc_write(hdr, 4);
    safe_cdc_write(payload, plen);
}
static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static size_t parse_hex(const char *s, uint8_t *out, size_t max) {
    size_t n = 0;
    while (s[0] && s[1] && n < max) {
        int hi = hexval(s[0]), lo = hexval(s[1]);
        if (hi < 0 || lo < 0) break;
        out[n++] = (uint8_t)((hi << 4) | lo); s += 2;
    }
    return n;
}

static void write_cccd_value(int ch, uint16_t handle, bool enable) {
    if (ch < 0 || ch >= MAX_CH || !s_ch[ch].used || handle == 0) return;
    esp_gattc_descr_elem_t descr;
    uint16_t got = 1;
    if (esp_ble_gattc_get_descr_by_char_handle(s_ch[ch].gattc_if, s_ch[ch].conn_id,
                                               handle, UUID_CCCD, &descr, &got) == ESP_OK && got > 0) {
        uint8_t v[2] = { enable ? 0x01 : 0x00, 0x00 };
        esp_ble_gattc_write_char_descr(s_ch[ch].gattc_if, s_ch[ch].conn_id,
                                       descr.handle, sizeof(v), v,
                                       ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE);
    }
}

static uint16_t choose_input_handle(int ch, bool prefer_legacy, uint8_t *src) {
    if (src) *src = 0;
    if (ch < 0 || ch >= MAX_CH) return 0;
    if (prefer_legacy && s_ch[ch].legacy_handle) {
        if (src) *src = 2;
        return s_ch[ch].legacy_handle;
    }
    if (!prefer_legacy && s_ch[ch].fd2_handle) {
        if (src) *src = 1;
        return s_ch[ch].fd2_handle;
    }
    if (s_ch[ch].fd2_handle) {
        if (src) *src = 1;
        return s_ch[ch].fd2_handle;
    }
    if (s_ch[ch].legacy_handle) {
        if (src) *src = 2;
        return s_ch[ch].legacy_handle;
    }
    return 0;
}

// --- command handlers (cdc_task context) ---
static void do_conn(char *args) {
    char *save = NULL;
    char *type_s = strtok_r(args, " ", &save);
    char *mac_s  = strtok_r(NULL, " ", &save);
    if (!type_s || !mac_s) return;
    unsigned m[6];
    if (sscanf(mac_s, "%x:%x:%x:%x:%x:%x", &m[0],&m[1],&m[2],&m[3],&m[4],&m[5]) != 6) return;
    int ch = ch_alloc();
    if (ch < 0) { out_debug("conn req REJECTED: no free channel slot"); return; }
    int u0, r0; ch_count(&u0, &r0);
    s_ch[ch].used = true;          // reserve the slot
    s_ch[ch].ready = false;
    s_ch[ch].connecting = true;
    s_ch[ch].addr_type = (uint8_t)atoi(type_s);
    for (int i = 0; i < 6; i++) s_ch[ch].bda[i] = (uint8_t)m[i];
    s_ch[ch].prefer_legacy = false;
    {   // Diagnostic: which channel + how many links already live when this starts.
        char dbg[110];
        snprintf(dbg, sizeof(dbg),
            "conn req ch=%d mac=%02X:%02X:%02X:%02X:%02X:%02X type=%d (before: used=%d ready=%d)",
            ch, s_ch[ch].bda[0],s_ch[ch].bda[1],s_ch[ch].bda[2],
            s_ch[ch].bda[3],s_ch[ch].bda[4],s_ch[ch].bda[5], s_ch[ch].addr_type, u0, r0);
        out_debug(dbg);
    }
    // If two links are already live, the radio is saturated by their 7.5ms anchors and
    // the controller cannot schedule a 3rd connection's SETUP at all (CONN_CANCEL).
    // Temporarily widen those links to 15ms NOW to free radio time, and defer the 3rd
    // open until the widen has settled (the conn-param update takes effect a few
    // intervals later).  The widened links are restored to 7.5ms once the 3rd connects.
    // If two links are already live, the radio is saturated by their 7.5ms anchors and
    // the controller cannot schedule a 3rd connection's SETUP at all (CONN_CANCEL).
    // Temporarily widen those links to 15ms NOW to free radio time, and defer the 3rd
    // open until the widen has settled (the conn-param update takes effect a few
    // intervals later).  The widened links are restored once the 3rd connects.
    s_widened_mask = 0;
    s_conn_open_after = 0;
    if (r0 >= 2) {
        for (int i = 0; i < MAX_CH; i++) {
            if (i != ch && s_ch[i].used && s_ch[i].ready && s_ch[i].itvl != 12) {
                esp_ble_conn_update_params_t cp = {0};
                memcpy(cp.bda, s_ch[i].bda, sizeof(esp_bd_addr_t));
                cp.min_int = 12; cp.max_int = 12; cp.latency = 0; cp.timeout = 400;
                esp_ble_gap_update_conn_params(&cp);
                s_widened_mask |= (1u << i);
            }
        }
        s_conn_open_after = now_ms() + 250;   // let the widen settle before opening
        gap_busy(700);
        char dbg[80];
        snprintf(dbg, sizeof(dbg), "3rd link: widened mask=0x%02x to 15ms, defer open 250ms",
                 s_widened_mask);
        out_debug(dbg);
    } else {
        gap_busy(300);
    }
    // Can't initiate while the scanner runs (ESP_GATT_CONGESTED). Stop the scan; the open
    // happens in SCAN_STOP_COMPLETE_EVT (immediate) or, for the deferred 3rd-link case,
    // from cdc_task once s_conn_open_after elapses.
    s_pending_conn = ch;
    esp_ble_gap_stop_scanning();
}

static void do_inputsrc(char *args) {  // inputsrc <ch> <fd2|legacy>
    char *save = NULL;
    char *ch_s = strtok_r(args, " ", &save);
    char *mode_s = strtok_r(NULL, " ", &save);
    if (!ch_s || !mode_s) return;
    int ch = atoi(ch_s);
    if (ch < 0 || ch >= MAX_CH || !s_ch[ch].used) return;

    bool prefer_legacy;
    if (strcmp(mode_s, "legacy") == 0) {
        prefer_legacy = true;
    } else if (strcmp(mode_s, "fd2") == 0) {
        prefer_legacy = false;
    } else {
        return;
    }

    s_ch[ch].prefer_legacy = prefer_legacy;
    uint8_t new_src = 0;
    uint16_t new_handle = choose_input_handle(ch, prefer_legacy, &new_src);
    if (!new_handle) {
        char dbg[96];
        snprintf(dbg, sizeof(dbg), "inputsrc ch=%d mode=%s pending (fd2=0x%04x legacy=0x%04x)",
                 ch, mode_s, s_ch[ch].fd2_handle, s_ch[ch].legacy_handle);
        out_debug(dbg);
        return;
    }

    uint16_t old_handle = s_ch[ch].input_handle;
    if (old_handle && old_handle != new_handle) {
        write_cccd_value(ch, old_handle, false);
        esp_ble_gattc_unregister_for_notify(s_ch[ch].gattc_if, s_ch[ch].bda, old_handle);
    }

    s_ch[ch].input_handle = new_handle;
    s_ch[ch].input_src = new_src;
    if (!old_handle || old_handle != new_handle) {
        esp_ble_gattc_register_for_notify(s_ch[ch].gattc_if, s_ch[ch].bda, new_handle);
    }

    char dbg[128];
    snprintf(dbg, sizeof(dbg), "inputsrc ch=%d mode=%s input=0x%04x(src=%u prefer_legacy=%d)",
             ch, mode_s, s_ch[ch].input_handle, s_ch[ch].input_src, s_ch[ch].prefer_legacy);
    out_debug(dbg);
}
// Steady-state interval policy.  This controller CANNOT sustain two 7.5ms links plus a
// third, so: 3+ established links all run at 15ms (itvl=12); with <=2 links everyone runs
// at 7.5ms (itvl=6).  Called after a link becomes ready and after a disconnect.  Bails
// while a connection is being established (the temporary widen owns that window) so it
// never fights the setup.  Each link's itvl is updated BEFORE the request so a failed
// request is not retried (no loop).
static void reconcile_intervals(void) {
    if (s_pending_conn >= 0 || s_widened_mask) return;
    for (int i = 0; i < MAX_CH; i++) if (s_ch[i].connecting) return;
    int ready = 0;
    for (int i = 0; i < MAX_CH; i++) if (s_ch[i].used && s_ch[i].ready) ready++;
    uint16_t target = (ready >= 3) ? 12 : 6;
    for (int i = 0; i < MAX_CH; i++) {
        if (s_ch[i].used && s_ch[i].ready && s_ch[i].itvl != target) {
            s_ch[i].itvl = target;
            esp_ble_conn_update_params_t cp = {0};
            memcpy(cp.bda, s_ch[i].bda, sizeof(esp_bd_addr_t));
            cp.min_int = target; cp.max_int = target; cp.latency = 0; cp.timeout = 400;
            esp_ble_gap_update_conn_params(&cp);
        }
    }
    char d[48]; snprintf(d, sizeof(d), "reconcile: %d links -> itvl=%d", ready, target);
    out_debug(d);
}
static void restore_widened_links(void);   // forward decl (defined just below)
// Open the pending connection (s_pending_conn).  Scan must already be stopped.  Called
// from SCAN_STOP_COMPLETE_EVT (immediate, 1st/2nd link) and from cdc_task (deferred
// 3rd-link case, after the existing links were widened to 15ms and that has settled).
static void open_pending_conn(void) {
    int ch = s_pending_conn;
    if (ch < 0) return;
    s_pending_conn = -1;
    s_conn_open_after = 0;
    if (!s_ch[ch].used) return;   // slot was cleared meanwhile (e.g. ble disconnect)

    int other_ready = 0;
    for (int i = 0; i < MAX_CH; i++)
        if (i != ch && s_ch[i].used && s_ch[i].ready) other_ready++;

    // 3rd+ link runs at 15ms (itvl=12); first two stay 7.5ms for gap-free rumble.
    // (enh_open + ce_len does NOT work on this chip — see v0.12.16; plain gattc_open.)
    s_ch[ch].itvl = (other_ready >= 2) ? 12 : 6;
    esp_err_t pc = esp_ble_gap_set_prefer_conn_params(s_ch[ch].bda,
                        s_ch[ch].itvl, s_ch[ch].itvl, 0, 400);
    esp_err_t oc = esp_ble_gattc_open(s_ch[ch].gattc_if, s_ch[ch].bda,
                        s_ch[ch].addr_type, true);
    int u1, r1; ch_count(&u1, &r1);
    char dbg[150];
    snprintf(dbg, sizeof(dbg),
        "open ch=%d itvl=%d type=%d set_prefer=%s gattc_open=%s (used=%d ready=%d other_ready=%d widened=0x%02x)",
        ch, s_ch[ch].itvl, s_ch[ch].addr_type,
        esp_err_to_name(pc), esp_err_to_name(oc), u1, r1, other_ready, s_widened_mask);
    out_debug(dbg);

    // If the open failed to even start, there will be no OPEN/DISCONNECT event to
    // restore from — undo the widen now so the existing links aren't left at 15ms.
    if (oc != ESP_OK) {
        esp_gatt_if_t keep = s_ch[ch].gattc_if;
        memset(&s_ch[ch], 0, sizeof(s_ch[ch]));
        s_ch[ch].gattc_if = keep;
        restore_widened_links();
        if (s_scan_mode) s_resume_scan = true;
    }
}
// End the temporary widen (3rd-link setup window) and apply the steady-state interval
// policy.  On SUCCESS (now 3 links) reconcile keeps everyone at 15ms; on ABORT (back to
// <=2 links) reconcile restores everyone to 7.5ms.  Single source of truth = reconcile.
static void restore_widened_links(void) {
    s_widened_mask = 0;
    reconcile_intervals();
}
static void do_disc(char *args) {
    int ch = atoi(args);
    if (ch >= 0 && ch < MAX_CH && s_ch[ch].used) {
        portENTER_CRITICAL(&s_disc_mux);
        s_disc_mask |= (1u << ch);
        portEXIT_CRITICAL(&s_disc_mux);
        kick_disc_queue();
    }
}
static void do_disc_all(void) {  // "ble disconnect": drop every live link (clear stale state)
    // If the scan was stopped for a pending conn but gattc_open hasn't fired yet, cancel it
    // so SCAN_STOP_COMPLETE doesn't open a connection we're about to discard anyway.
    if (s_pending_conn >= 0) {
        int pc = s_pending_conn; s_pending_conn = -1;
        esp_gatt_if_t keep = s_ch[pc].gattc_if;
        memset(&s_ch[pc], 0, sizeof(s_ch[pc]));
        s_ch[pc].gattc_if = keep;
    }
    // Enqueue every live channel for sequential disconnection.
    // Do NOT call gap_disconnect here — parallel disconnects corrupt the BLE
    // controller's HCI state and cause crashes (status=133 flooding → assert).
    // Scanning will resume automatically once the queue drains.
    s_resume_scan = false;  // kick_disc_queue will set this when done
    portENTER_CRITICAL(&s_disc_mux);
    for (int i = 0; i < MAX_CH; i++)
        if (s_ch[i].used) s_disc_mask |= (1u << i);
    portEXIT_CRITICAL(&s_disc_mux);
    kick_disc_queue();  // start first disconnect; rest follow via DISCONNECT_EVT
}
static void do_wr(char *args) {  // wr <ch> <c|r> <hex>
    char *save = NULL;
    char *ch_s = strtok_r(args, " ", &save);
    char *k_s  = strtok_r(NULL, " ", &save);
    char *h_s  = strtok_r(NULL, " ", &save);
    if (!ch_s || !k_s || !h_s) return;
    int ch = atoi(ch_s);
    if (ch < 0 || ch >= MAX_CH || !s_ch[ch].used) return;
    uint8_t buf[96]; size_t len = parse_hex(h_s, buf, sizeof(buf));
    if (len == 0) return;
    uint16_t handle = (k_s[0] == 'c') ? s_ch[ch].cmd_handle : s_ch[ch].rumble_handle;
    if (handle == 0) return;
    esp_ble_gattc_write_char(s_ch[ch].gattc_if, s_ch[ch].conn_id, handle, len, buf,
                             ESP_GATT_WRITE_TYPE_NO_RSP, ESP_GATT_AUTH_REQ_NONE);
}
static void do_rs(char *args) {  // rs <ch> <hex>
    char *save = NULL;
    char *ch_s = strtok_r(args, " ", &save);
    char *h_s  = strtok_r(NULL, " ", &save);
    if (!ch_s || !h_s) return;
    int ch = atoi(ch_s);
    if (ch < 0 || ch >= MAX_CH || !s_ch[ch].used || s_ch[ch].rumble_handle == 0) return;
    
    rumble_pkt_t pkt;
    pkt.ch = ch;
    pkt.len = parse_hex(h_s, pkt.data, sizeof(pkt.data));
    if (pkt.len == 0) return;
    
    if (s_rumble_queue) {
        if (xQueueSend(s_rumble_queue, &pkt, 0) != pdTRUE) {
            rumble_pkt_t dummy;
            xQueueReceive(s_rumble_queue, &dummy, 0); // Drop oldest
            xQueueSend(s_rumble_queue, &pkt, 0);      // Push newest
        }
    }
}
static void wr_one(int ch, char kind, const uint8_t *buf, size_t len) {
    if (ch < 0 || ch >= MAX_CH || !s_ch[ch].used || len == 0) return;
    uint16_t handle = (kind == 'c') ? s_ch[ch].cmd_handle : s_ch[ch].rumble_handle;
    if (handle == 0) return;
    esp_ble_gattc_write_char(s_ch[ch].gattc_if, s_ch[ch].conn_id, handle, len, (uint8_t *)buf,
                             ESP_GATT_WRITE_TYPE_NO_RSP, ESP_GATT_AUTH_REQ_NONE);
}
static void do_wrpair(char *args) {  // wrpair <ch_l> <ch_r> <kind> <hex_l> <hex_r>
    char *s = NULL;
    char *cl = strtok_r(args, " ", &s), *cr = strtok_r(NULL, " ", &s);
    char *k  = strtok_r(NULL, " ", &s);
    char *hl = strtok_r(NULL, " ", &s), *hr = strtok_r(NULL, " ", &s);
    if (!cl || !cr || !k || !hl || !hr) return;
    uint8_t bl[96], br[96];
    wr_one(atoi(cl), k[0], bl, parse_hex(hl, bl, sizeof(bl)));
    wr_one(atoi(cr), k[0], br, parse_hex(hr, br, sizeof(br)));
}
static void handle_command(char *cmd) {
    if (strncmp(cmd, "status", 6) == 0)         { s_request_status = true; }
    else if (strncmp(cmd, "scan on", 7) == 0)   {
        // The host sends "scan on" right after "disc <ch>" to re-arm detection.  Never
        // start the scan synchronously here: a gap_disconnect issued by do_disc is still
        // in flight on the HCI path and starting a scan on top of it collides and
        // silently drops the disconnect (symptom: the controller you asked to disconnect
        // stays connected while an unrelated link drops).  Defer to cdc_task, which only
        // resumes once GAP is quiet and no disconnect is in flight.
        s_scan_mode = true;
        if (now_ms() >= s_gap_busy_until && !s_disc_in_flight && s_disc_mask == 0) {
            s_resume_scan = false;
            esp_ble_gap_start_scanning(0);
        } else {
            s_resume_scan = true;
        }
    }
    else if (strncmp(cmd, "scan off", 8) == 0)  { s_scan_mode = false; s_resume_scan = false; esp_ble_gap_stop_scanning(); }
    else if (strncmp(cmd, "ble disconnect", 14) == 0) { do_disc_all(); }
    else if (strncmp(cmd, "auto", 4) == 0)      { /* host-driven conn only */ }
    else if (strncmp(cmd, "conn ", 5) == 0)     { do_conn(cmd + 5); }
    else if (strncmp(cmd, "inputsrc ", 9) == 0) { do_inputsrc(cmd + 9); }
    else if (strncmp(cmd, "disc ", 5) == 0)     { do_disc(cmd + 5); }
    else if (strncmp(cmd, "wrpair ", 7) == 0)   { do_wrpair(cmd + 7); }
    else if (strncmp(cmd, "wr ", 3) == 0)       { do_wr(cmd + 3); }
    else if (strncmp(cmd, "rs ", 3) == 0)       { do_rs(cmd + 3); }
}

void tinyusb_cdc_rx_callback(int itf, cdcacm_event_t *event) {
    if (event->type != CDC_EVENT_RX) return;
    uint8_t tmp[128]; size_t n = 0;
    if (tinyusb_cdcacm_read(itf, tmp, sizeof(tmp), &n) != ESP_OK || n == 0) return;
    for (size_t i = 0; i < n; i++) {
        char c = (char)tmp[i];
        if (c == '\r') continue;
        if (c == '\n') {
            s_rx_buf[s_rx_len] = '\0';
            if (s_rx_len > 0 && s_cmd_queue) {
                line_t L; strncpy(L.text, s_rx_buf, sizeof(L.text) - 1); L.text[sizeof(L.text)-1] = '\0';
                xQueueSend(s_cmd_queue, &L, 0);
            }
            s_rx_len = 0;
        } else if (s_rx_len < (int)sizeof(s_rx_buf) - 1) {
            s_rx_buf[s_rx_len++] = c;
        }
    }
}
static void cdc_task(void *arg) {
    (void)arg;
    for (;;) {
        if (s_request_status) { s_request_status = false; send_status_response(); }
        // Deferred 3rd-link open: the existing links were widened to 15ms in do_conn;
        // open the 3rd once that has settled (scan is already stopped by then).
        if (s_pending_conn >= 0 && s_conn_open_after != 0 && now_ms() >= s_conn_open_after)
            open_pending_conn();
        // Deferred scan resume: only once the GAP bus is quiet, no open is pending, and
        // no channel is mid-connect — so it never pre-empts an in-flight disconnect/open.
        if (s_resume_scan && s_scan_mode && s_pending_conn < 0 && now_ms() >= s_gap_busy_until) {
            bool connecting = false;
            for (int i = 0; i < MAX_CH; i++) if (s_ch[i].connecting) connecting = true;
            if (!connecting) { s_resume_scan = false; esp_ble_gap_start_scanning(0); }
        }
        // Safety net: if a DISCONNECT_EVT was missed (very rare), retry the queue
        // so a stuck disconnect doesn't wedge the bridge until replug.
        if (!s_disc_in_flight && s_disc_mask && now_ms() >= s_gap_busy_until)
            kick_disc_queue();
        in_report_t ack;
        if (xQueueReceive(s_ack_queue, &ack, 0) == pdTRUE) send_report_frame(ack.ch, ack.data, ack.len, true);
        line_t out;
        if (xQueueReceive(s_out_queue, &out, 0) == pdTRUE) safe_cdc_write((const uint8_t *)out.text, strlen(out.text));
        line_t L;
        if (xQueueReceive(s_cmd_queue, &L, pdMS_TO_TICKS(2)) == pdTRUE) handle_command(L.text);
        for (int i = 0; i < MAX_CH; i++) {
            in_report_t r; bool dirty = false;
            portENTER_CRITICAL(&s_in_mux);
            if (s_in_dirty[i]) { r = s_in_shadow[i]; s_in_dirty[i] = false; dirty = true; }
            portEXIT_CRITICAL(&s_in_mux);
            if (dirty) send_report_frame(r.ch, r.data, r.len, false);
        }
    }
}

// --- GATT discovery helpers ---
static void match_and_store_char(int ch, const esp_bt_uuid_t *uuid, uint16_t val_handle) {
    // Record both input notify characteristics; the actual input_handle is chosen in
    // SEARCH_CMPL_EVT once discovery is complete (order-independent).
    // FD2 (ab7de9be…) is the canonical Switch 2 input stream for the Pro 2 / Joy-Cons.
    // The Pro 2 ALSO exposes the legacy notify characteristic with a DIFFERENT byte
    // layout — parsing that as an FD2 report gives random buttons/sticks — so for
    // those controllers FD2 wins.  The NSO GameCube controller is the exception: its
    // compact native report is on the LEGACY characteristic, so prefer_legacy flips
    // the choice for it.
    if (memcmp(uuid, &UUID_NOTIFY_FD2, sizeof(*uuid)) == 0)         s_ch[ch].fd2_handle = val_handle;
    else if (memcmp(uuid, &UUID_NOTIFY_LEGACY, sizeof(*uuid)) == 0) s_ch[ch].legacy_handle = val_handle;
    else if (memcmp(uuid, &UUID_ACK, sizeof(*uuid)) == 0)      s_ch[ch].ack_handle = val_handle;
    else if (memcmp(uuid, &UUID_CMD, sizeof(*uuid)) == 0)      s_ch[ch].cmd_handle = val_handle;
    else if (memcmp(uuid, &UUID_RUMBLE_PRO, sizeof(*uuid)) == 0 ||
             memcmp(uuid, &UUID_RUMBLE_JOYCON_R, sizeof(*uuid)) == 0 ||
             memcmp(uuid, &UUID_RUMBLE_JOYCON_L, sizeof(*uuid)) == 0) s_ch[ch].rumble_handle = val_handle;
}
static void scan_service_chars(int ch, uint16_t start, uint16_t end) {
    uint16_t count = 0;
    if (esp_ble_gattc_get_attr_count(s_ch[ch].gattc_if, s_ch[ch].conn_id, ESP_GATT_DB_CHARACTERISTIC,
                                     start, end, 0, &count) != ESP_OK || count == 0) return;
    esp_gattc_char_elem_t *elems = calloc(count, sizeof(esp_gattc_char_elem_t));
    if (!elems) return;
    uint16_t got = count;
    if (esp_ble_gattc_get_all_char(s_ch[ch].gattc_if, s_ch[ch].conn_id, start, end, elems, &got, 0) == ESP_OK)
        for (int i = 0; i < got; i++) match_and_store_char(ch, &elems[i].uuid, elems[i].char_handle);
    free(elems);
}
static void enable_notifications(int ch) {
    if (s_ch[ch].ack_handle)   esp_ble_gattc_register_for_notify(s_ch[ch].gattc_if, s_ch[ch].bda, s_ch[ch].ack_handle);
    if (s_ch[ch].input_handle) esp_ble_gattc_register_for_notify(s_ch[ch].gattc_if, s_ch[ch].bda, s_ch[ch].input_handle);
}

static void gattc_cb(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                     esp_ble_gattc_cb_param_t *param) {
    if (event == ESP_GATTC_REG_EVT) {
        int slot = param->reg.app_id;   // app_id == channel index
        if (param->reg.status == ESP_GATT_OK && slot >= 0 && slot < MAX_CH) {
            s_ch[slot].gattc_if = gattc_if;
            ESP_LOGI(TAG, "GATTC app %d registered (if=%d)", slot, gattc_if);
        }
        return;
    }

    // ---- DISCONNECT: route by remote_bda, NOT by gattc_if ----------------------
    // Bluedroid may deliver DISCONNECT_EVT with ESP_GATT_IF_NONE (broadcast to every
    // app) or with a *mismatched* gattc_if.  The esp-idf gattc_multi_connect example
    // proves this: it identifies every DISCONNECT_EVT by memcmp(remote_bda), never by
    // gattc_if.  Using ch_by_if here would clear the WRONG channel (e.g. disconnecting
    // controller B drops controller A's slot) and, worse, leave the real channel's
    // s_disc_in_flight stuck → the sequential disconnect queue wedges and only the
    // first controller ever disconnects.  So look the channel up by its bonded address.
    if (event == ESP_GATTC_DISCONNECT_EVT) {
        int dch = ch_by_bda(param->disconnect.remote_bda);
        {   // Diagnostic: the BLE disconnect reason is the KEY clue for a failed 3rd link.
            // Common reasons: 0x08 supervision timeout, 0x13 remote terminated,
            // 0x16 local terminated, 0x22 LL response timeout, 0x28 LL instant passed,
            // 0x3B unacceptable conn params, 0x3E connection failed to be established.
            const uint8_t *d = param->disconnect.remote_bda;
            int ud, rd; ch_count(&ud, &rd);
            char dbg[140];
            snprintf(dbg, sizeof(dbg),
                "DISCONNECT_EVT bda=%02X:%02X:%02X:%02X:%02X:%02X reason=0x%02x ch=%d ready=%d (used=%d ready_cnt=%d)",
                d[0],d[1],d[2],d[3],d[4],d[5], param->disconnect.reason, dch,
                (dch >= 0 ? s_ch[dch].ready : -1), ud, rd);
            out_debug(dbg);
        }
        // Always free the connection control block (conn_id) or it leaks → later opens
        // fail with 133 and the stack asserts.
        esp_ble_gattc_close(gattc_if, param->disconnect.conn_id);

        if (dch >= 0) {
            if (!s_ch[dch].ready) {
                char b[100];
                snprintf(b, sizeof(b),
                    "{\"cmd\":\"connect_fail\",\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\"}\n",
                    s_ch[dch].bda[0],s_ch[dch].bda[1],s_ch[dch].bda[2],
                    s_ch[dch].bda[3],s_ch[dch].bda[4],s_ch[dch].bda[5]);
                out_json(b);
            } else {
                char b[80]; snprintf(b, sizeof(b), "{\"cmd\":\"disconnected\",\"channel\":%d}\n", dch);
                out_json(b);
            }
            esp_gatt_if_t keep = s_ch[dch].gattc_if;
            memset(&s_ch[dch], 0, sizeof(s_ch[dch]));
            s_ch[dch].gattc_if = keep;
        }
        // If a 3rd-link attempt aborted (cancelled before becoming ready) and nothing is
        // still being established, stop the temporary widen.  Then reconcile intervals:
        // dropping from 3 links to 2 restores everyone to 7.5ms; an aborted 3rd also
        // restores the (temporarily widened) existing links to 7.5ms.  reconcile bails on
        // its own while a connection is still in progress.
        if (s_widened_mask) {
            bool connecting = false;
            for (int i = 0; i < MAX_CH; i++) if (s_ch[i].connecting) connecting = true;
            if (!connecting && s_pending_conn < 0) s_widened_mask = 0;
        }
        reconcile_intervals();
        // Advance the sequential disconnect queue regardless of whether a channel
        // matched — an unmatched event (already-cleared slot) must NOT leave the
        // in-flight flag set, or the queue wedges.
        portENTER_CRITICAL(&s_disc_mux);
        s_disc_in_flight = false;
        bool queue_empty = (s_disc_mask == 0);
        portEXIT_CRITICAL(&s_disc_mux);
        if (queue_empty && s_scan_mode) s_resume_scan = true;
        kick_disc_queue();
        return;
    }

    int ch = ch_by_if(gattc_if);
    if (ch < 0) return;

    switch (event) {
    case ESP_GATTC_OPEN_EVT:
        if (!s_ch[ch].used) break;

        if (param->open.status != ESP_GATT_OK) {
            // Emit connect_fail JSON (mirrors NimBLE's BLE_GAP_EVENT_CONNECT status!=0 path)
            // so the Python host clears its "connecting" state and stops retrying.
            // Without this the host retries endlessly → repeated open-fail cycles →
            // GATTC conn_id leak / BLE controller assert → crash.
            char b[100];
            snprintf(b, sizeof(b),
                "{\"cmd\":\"connect_fail\",\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\"}\n",
                s_ch[ch].bda[0],s_ch[ch].bda[1],s_ch[ch].bda[2],
                s_ch[ch].bda[3],s_ch[ch].bda[4],s_ch[ch].bda[5]);
            out_json(b);
            int uo, ro; ch_count(&uo, &ro);
            char dbg[110];
            snprintf(dbg, sizeof(dbg),
                "OPEN_EVT FAIL ch=%d status=%d conn_id=%d (used=%d ready=%d)",
                ch, param->open.status, param->open.conn_id, uo, ro);
            out_debug(dbg);
            // CRUCIAL: a failed open still allocates a GATTC connection control block.
            // Must esp_ble_gattc_close() to release the conn_id, or it leaks and after a
            // few attempts esp_ble_gattc_open returns 133 and the stack asserts -> crash.
            esp_ble_gattc_close(gattc_if, param->open.conn_id);
            esp_gatt_if_t keep = s_ch[ch].gattc_if;
            memset(&s_ch[ch], 0, sizeof(s_ch[ch]));
            s_ch[ch].gattc_if = keep;
            gap_busy(300);
            if (s_scan_mode) s_resume_scan = true;
            break;
        }
        s_ch[ch].conn_id = param->open.conn_id;
        s_ch[ch].connecting = false;
        {   char dbg[90]; int uo, ro; ch_count(&uo, &ro);
            snprintf(dbg, sizeof(dbg), "OPEN_EVT OK ch=%d conn_id=%d (used=%d ready=%d) -> discovering",
                     ch, param->open.conn_id, uo, ro);
            out_debug(dbg); }
        esp_ble_gattc_send_mtu_req(gattc_if, param->open.conn_id);
        esp_ble_gattc_search_service(gattc_if, param->open.conn_id, NULL);
        break;

    case ESP_GATTC_SEARCH_RES_EVT:
        scan_service_chars(ch, param->search_res.start_handle, param->search_res.end_handle);
        break;

    case ESP_GATTC_SEARCH_CMPL_EVT: {
        // Choose the input stream now that all characteristics are known.  The NSO
        // GameCube controller (prefer_legacy) uses the LEGACY characteristic; every
        // other SW2 controller uses FD2.  Fall back to whichever is present.
        s_ch[ch].input_handle = choose_input_handle(ch, s_ch[ch].prefer_legacy, &s_ch[ch].input_src);
        char b[160];
        snprintf(b, sizeof(b), "discovered ch=%d input=0x%04x(src=%u prefer_legacy=%d fd2=0x%04x legacy=0x%04x) ack=0x%04x cmd=0x%04x rumble=0x%04x",
                 ch, s_ch[ch].input_handle, s_ch[ch].input_src, s_ch[ch].prefer_legacy,
                 s_ch[ch].fd2_handle, s_ch[ch].legacy_handle,
                 s_ch[ch].ack_handle, s_ch[ch].cmd_handle, s_ch[ch].rumble_handle);
        out_debug(b);
        enable_notifications(ch);
        break;
    }

    case ESP_GATTC_REG_FOR_NOTIFY_EVT: {
        uint16_t h = param->reg_for_notify.handle;
        esp_gattc_descr_elem_t descr; uint16_t got = 1;
        if (esp_ble_gattc_get_descr_by_char_handle(gattc_if, s_ch[ch].conn_id, h, UUID_CCCD,
                                                   &descr, &got) == ESP_OK && got > 0) {
            uint8_t v[2] = {0x01, 0x00};
            esp_ble_gattc_write_char_descr(gattc_if, s_ch[ch].conn_id, descr.handle, sizeof(v), v,
                                           ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE);
        }
        if (h == s_ch[ch].input_handle && !s_ch[ch].ready) {
            s_ch[ch].ready = true;
            {   // Update interval AFTER GATT discovery, overriding controller's defaults.
                // Doing this too early (in OPEN_EVT) collides with the Nintendo
                // controller's own initial parameter update request.
                uint16_t itvl = s_ch[ch].itvl ? s_ch[ch].itvl : 6;
                esp_ble_conn_update_params_t cp = {0};
                memcpy(cp.bda, s_ch[ch].bda, sizeof(esp_bd_addr_t));
                cp.min_int = itvl; cp.max_int = itvl; cp.latency = 0; cp.timeout = 400;
                esp_ble_gap_update_conn_params(&cp);
            }
            char b[96]; snprintf(b, sizeof(b),
                "{\"cmd\":\"connected\",\"channel\":%d,\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\"}\n",
                ch, s_ch[ch].bda[0],s_ch[ch].bda[1],s_ch[ch].bda[2],
                    s_ch[ch].bda[3],s_ch[ch].bda[4],s_ch[ch].bda[5]);
            out_json(b);
            // The 3rd link is now established — restore any links we temporarily widened
            // to 15ms back to 7.5ms.  Maintaining 2x7.5ms + 1x15ms is feasible; it was
            // only ESTABLISHING the 3rd alongside two 7.5ms anchors that failed.
            restore_widened_links();
            if (s_scan_mode) s_resume_scan = true;  // resume scan (deferred) to find the next one
        }
        break;
    }

    case ESP_GATTC_NOTIFY_EVT: {
        uint8_t len = param->notify.value_len > REPORT_SIZE ? REPORT_SIZE : param->notify.value_len;
        if (param->notify.handle == s_ch[ch].input_handle) {
            portENTER_CRITICAL(&s_in_mux);
            s_in_shadow[ch].ch = ch; s_in_shadow[ch].len = len;
            memcpy(s_in_shadow[ch].data, param->notify.value, len);
            s_in_dirty[ch] = true;
            portEXIT_CRITICAL(&s_in_mux);
        } else if (param->notify.handle == s_ch[ch].ack_handle && s_ack_queue) {
            in_report_t a; a.ch = ch; a.len = len; memcpy(a.data, param->notify.value, len);
            xQueueSend(s_ack_queue, &a, 0);
        }
        break;
    }

    // ESP_GATTC_DISCONNECT_EVT is handled above (routed by remote_bda, not gattc_if).

    default: break;
    }
}

// --- GAP: scan + report Nintendo controllers ---
static esp_ble_scan_params_t s_scan_params = {
    .scan_type          = BLE_SCAN_TYPE_ACTIVE,
    .own_addr_type      = BLE_ADDR_TYPE_PUBLIC,
    .scan_filter_policy = BLE_SCAN_FILTER_ALLOW_ALL,
    // Aligned to the NimBLE build's general-scan params (ble_gap_disc_params):
    // itvl = window = 0x30 (30 ms, 100% duty cycle).  The previous 0x20/0x08
    // (20 ms / 5 ms = 25% duty) missed most advertisements.
    .scan_interval      = 0x30,
    .scan_window        = 0x30,
    // Report EVERY advertisement (no controller-side dedup): a controller that fails a
    // connect (e.g. bonded to another host, reconnecting without sync) must stay
    // re-discoverable.  Dedup cached its address and hid it forever.  The scan_result
    // flood is now harmless — it goes through the non-blocking out_queue.
    .scan_duplicate     = BLE_SCAN_DUPLICATE_DISABLE,
};
static bool adv_is_nintendo(uint8_t *adv) {
    uint8_t mlen = 0;
    uint8_t *mfg = esp_ble_resolve_adv_data(adv, ESP_BLE_AD_MANUFACTURER_SPECIFIC_TYPE, &mlen);
    return (mfg && mlen >= 2 && ((uint16_t)mfg[0] | ((uint16_t)mfg[1] << 8)) == NINTENDO_COMPANY_ID);
}
static void gap_cb(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param) {
    switch (event) {
    case ESP_GAP_BLE_UPDATE_CONN_PARAMS_EVT: {
        int ch = ch_by_bda(param->update_conn_params.bda);
        {   char dbg[110];
            snprintf(dbg, sizeof(dbg),
                "UPDATE_CONN_PARAMS ch=%d status=%d itvl=%d latency=%d timeout=%d",
                ch, param->update_conn_params.status, param->update_conn_params.conn_int,
                param->update_conn_params.latency, param->update_conn_params.timeout);
            out_debug(dbg); }
        // Only act on a SUCCESSFUL update (status==0).  A failed update (e.g. status 19
        // when the controller can't grant 7.5ms while 3 links are active) must NOT be
        // re-attempted here, or it loops forever re-requesting the same rejected value.
        // Also skip channels we INTENTIONALLY widened during a 3rd-link setup.
        if (ch >= 0 && s_ch[ch].used && param->update_conn_params.status == 0
                && !(s_widened_mask & (1u << ch))) {
            // Re-assert THIS channel's target interval if the negotiated one deviated
            // (e.g. a Nintendo L2CAP update request).  Use the per-channel target.
            uint16_t target = s_ch[ch].itvl ? s_ch[ch].itvl : 6;
            if (param->update_conn_params.conn_int != target) {
                esp_ble_conn_update_params_t cp = {0};
                memcpy(cp.bda, param->update_conn_params.bda, sizeof(esp_bd_addr_t));
                cp.min_int = target; cp.max_int = target; cp.latency = 0; cp.timeout = 400;
                esp_ble_gap_update_conn_params(&cp);
            }
        }
        break;
    }
    case ESP_GAP_BLE_SCAN_STOP_COMPLETE_EVT:
        // Immediate open for the 1st/2nd link.  The 3rd-link case (s_conn_open_after set)
        // is deferred: cdc_task opens it once the temporary widen of the existing links
        // has settled.
        if (s_pending_conn >= 0 && s_conn_open_after == 0)
            open_pending_conn();
        break;
    case ESP_GAP_BLE_SCAN_RESULT_EVT: {
        esp_ble_gap_cb_param_t *r = param;
        if (r->scan_rst.search_evt != ESP_GAP_SEARCH_INQ_RES_EVT) break;
        if (!s_scan_mode || !adv_is_nintendo(r->scan_rst.ble_adv)) break;
        const uint8_t *a = r->scan_rst.bda;

        char data_hex[63]; int dl = r->scan_rst.adv_data_len; if (dl > 31) dl = 31;
        for (int i = 0; i < dl; i++) sprintf(&data_hex[i*2], "%02X", r->scan_rst.ble_adv[i]);
        data_hex[dl*2] = '\0';
        char b[256];
        snprintf(b, sizeof(b),
            "{\"cmd\":\"scan_result\",\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"type\":%d,"
            "\"rssi\":%d,\"data\":\"%s\",\"directed\":0}\n",
            a[0],a[1],a[2],a[3],a[4],a[5], r->scan_rst.ble_addr_type, r->scan_rst.rssi, data_hex);
        out_json(b);
        break;
    }
    default: break;
    }
}

void app_main(void) {
    ESP_LOGI(TAG, "Bluedroid bridge %s", APP_FIRMWARE_VERSION);
    for (int i = 0; i < MAX_CH; i++) s_ch[i].gattc_if = ESP_GATT_IF_NONE;

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase()); ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    s_cmd_queue = xQueueCreate(16, sizeof(line_t));
    s_ack_queue = xQueueCreate(16, sizeof(in_report_t));
    s_out_queue = xQueueCreate(24, sizeof(line_t));

    tinyusb_config_t tusb_cfg = { 0 };
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));
    tinyusb_config_cdcacm_t acm = {
        .usb_dev = TINYUSB_USBDEV_0, .cdc_port = TINYUSB_CDC_ACM_0,
        .rx_unread_buf_sz = 1024, .callback_rx = &tinyusb_cdc_rx_callback,
    };
    ESP_ERROR_CHECK(tusb_cdc_acm_init(&acm));
    xTaskCreatePinnedToCore(cdc_task, "cdc_task", 4096, NULL, 10, NULL, 1);

    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT));
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BLE));
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    const uint8_t *mac = esp_bt_dev_get_address();
    if (mac) snprintf(s_own_mac, sizeof(s_own_mac), "%02X:%02X:%02X:%02X:%02X:%02X",
                      mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);

    ESP_ERROR_CHECK(esp_ble_gap_register_callback(gap_cb));
    ESP_ERROR_CHECK(esp_ble_gattc_register_callback(gattc_cb));
    // Register one GATTC app per channel.  Non-fatal: if the Bluedroid app table is
    // smaller than MAX_CH, the extra channels just stay unusable (gattc_if == NONE).
    for (int i = 0; i < MAX_CH; i++) {
        esp_err_t e = esp_ble_gattc_app_register(i);
        if (e != ESP_OK) ESP_LOGW(TAG, "gattc app %d register failed: %s", i, esp_err_to_name(e));
    }
    ESP_ERROR_CHECK(esp_ble_gatt_set_local_mtu(247));
    esp_ble_gap_set_scan_params(&s_scan_params);

    s_rumble_queue = xQueueCreate(RUMBLE_QUEUE_SIZE, sizeof(rumble_pkt_t));
    xTaskCreatePinnedToCore(rumble_playout_task, "rumble_task", 4096, NULL, 5, &s_rumble_task_h, 0);

    ESP_LOGI(TAG, "Bluedroid up, MAC=%s, %d GATTC apps. Waiting for host.", s_own_mac, MAX_CH);
}
