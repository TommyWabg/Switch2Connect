#include <stdbool.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "tinyusb.h"
#include "tusb_cdc_acm.h"

#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/ble_hs_adv.h"
#include "host/ble_sm.h"
#include "host/ble_store.h"
#include "host/ble_uuid.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

void ble_store_config_init(void);

static const char *TAG = "ESP32S3_CDC";

#define APP_FIRMWARE_VERSION "0.11.17"
#define EXPECTED_FIRMWARE_PROFILE "tinyusb_direct"
#define EXPECTED_FIRMWARE_BUILD "cdc_bridge_1"
#define NINTENDO_REPORT_SIZE 64
#define CDC_LINE_STATE_DTR 0x01
#define NINTENDO_COMPANY_ID 0x0553
#define BLE_SCAN_DURATION_MS 30000
#define CDC_PACKET_MAX (2 + 1 + 1 + NINTENDO_REPORT_SIZE)
#define BLE_MAX_SERVICES 32
#define BLE_MAX_REPORT_CHARS 96
#define Q_CTRL_DEPTH    32   // P0: BLE ACK/command responses (never starved)
#define MAX_BLE_CHANNELS 8

#define SWITCH2_NOTIFY_FD2_UUID "ab7de9be-89fe-49ad-828f-118f09df7fd2"
#define SWITCH2_NOTIFY_LEGACY_UUID "7492866c-ec3e-4619-8258-32755ffcc0f8"
#define SWITCH2_ACK_UUID "c765a961-d9d8-4d36-a20a-5315b111836a"
#define SWITCH2_CMD_UUID "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"
#define SWITCH2_RUMBLE_PRO_UUID "cc483f51-9258-427d-a939-630c31f72b05"
#define SWITCH2_RUMBLE_JOYCON_R_UUID "fa19b0fb-cd1f-46a7-84a1-bbb09e00c149"
#define SWITCH2_RUMBLE_JOYCON_L_UUID "289326cb-a471-485d-a8f4-240c14f18241"

typedef struct {
    uint8_t channel;
    uint8_t length;
    uint8_t is_command;   // 0 = input report, 1 = command/ack response
    uint8_t payload[NINTENDO_REPORT_SIZE];
} controller_report_t;

typedef struct {
    uint16_t start_handle;
    uint16_t end_handle;
} discovered_service_t;

typedef struct {
    uint16_t def_handle;
    uint16_t val_handle;
    uint16_t end_handle;
    uint16_t service_end_handle;
    uint16_t cccd_handle;
    uint8_t properties;
    char uuid[BLE_UUID_STR_LEN];
    bool notify_target;
    bool ack_target;
    bool input_target;
    bool command_target;
    bool rumble_target;
} discovered_report_char_t;

typedef struct {
    bool used;
    bool ready;
    uint16_t conn_handle;
    ble_addr_t peer_addr;
    discovered_service_t services[BLE_MAX_SERVICES];
    size_t service_count;
    size_t service_discovery_index;
    discovered_report_char_t report_chars[BLE_MAX_REPORT_CHARS];
    size_t report_char_count;
    int desc_discovery_index;
    int subscribe_index;
    uint16_t command_value_handle;
    bool command_write_no_rsp;
    uint16_t rumble_value_handle;
    bool rumble_write_no_rsp;
    bool init_started;
    bool init_done;
    size_t init_index;
} controller_channel_t;

static QueueHandle_t q_ctrl;    // P0: BLE ACK/command responses
static SemaphoreHandle_t s_stream_wake; // wakes cdc_stream_task

// Per-channel latest input shadow buffer (P2: always keeps newest, old dropped)
static controller_report_t s_latest_input[MAX_BLE_CHANNELS];
static bool s_latest_input_dirty[MAX_BLE_CHANNELS];
static portMUX_TYPE s_input_shadow_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t s_ctrl_drop_count; // q_ctrl overflow counter

// Per-channel rumble write diagnostics (ok vs dropped). Reported once a second so we
// can see whether merge-mode rumble alternation is caused by the firmware DROPPING
// writes on one channel (fail > 0) or by the BLE link/controller (writes all ok).
static volatile uint32_t s_rumble_tx_ok[MAX_BLE_CHANNELS];
static volatile uint32_t s_rumble_tx_fail[MAX_BLE_CHANNELS];
// Of the writes, how many carried NON-ZERO amplitude. If ok counts are equal but
// 'active' alternates/differs between channels, the app is sending complementary
// (real vs zero) rumble; if both channels are mostly active yet motors alternate,
// the issue is the BLE link/controller, not the data.
static volatile uint32_t s_rumble_active[MAX_BLE_CHANNELS];
// wrpair commands successfully parsed per second (two BLE writes each).
static volatile uint32_t s_wrpair_cmd;
// 'rs' (rumble-shadow set) commands received per second.
static volatile uint32_t s_rumble_shadow_set;
// Gap-fill HOLD writes emitted per second (steady continuation between host packets).
static volatile uint32_t s_rumble_hold;
// Per-channel INPUT reports forwarded to the host per second. Compares against the app's
// INPUT-RATE log: if firmware fwd is ~60 but the app sees ~30, the shared serial read
// thread is the bottleneck; if firmware fwd is already ~30, the radio/firmware is.
static volatile uint32_t s_input_fwd[MAX_BLE_CHANNELS];

// --- Rumble relay ---------------------------------------------------------
// Each Switch 2 Joy-Con rumble packet carries THREE 5-byte frames that are three
// CONSECUTIVE time-samples of the waveform (the host builds them from a 144 Hz
// envelope, so one packet's 3 frames span ~20 ms of playback).  The Joy-Con plays
// frame1->2->3 in order; a NEW packet restarts that playback at frame 1.
//
// Therefore we must relay each "rs <ch> <hex>" packet to BLE EXACTLY ONCE and let
// all three frames play out — that naturally fills the inter-packet interval.
// (The previous design re-sent the latest packet on a timer; every re-send
// restarted playback at frame 1, so only ~7 ms of frame 1 ever played: that
// caused the equal-interval gaps, the left/right complementary alternation —
// ~7 ms playback is shorter than the 7.5 ms radio slot offset between the two
// connections — and stretched transient effects like the Ping double-tap into a
// continuous buzz.  Relaying once removes all of that.)
//
// BLE writes are driven by the dedicated rumble_driver_task; the 'rs' parser only
// stores the payload as pending.
typedef struct {
    uint8_t  payload[NINTENDO_REPORT_SIZE];
    uint16_t len;
    bool     pending;       // a freshly-received host packet awaits its next write
    bool     holding;       // rumble is ongoing; keep feeding between host packets
    uint8_t  tx_id;         // rolling 4-bit packet-id (byte[1] = 0x50 | tx_id)
    int64_t  last_packet_us;// time of the last real host packet (relay)
    int64_t  last_write_us; // time of the last BLE write (for rate limiting)
    int64_t  suppress_until_us; // ignore rumble until this time (post-connect grace)
} rumble_shadow_t;
static rumble_shadow_t s_rumble_shadow[MAX_BLE_CHANNELS];
static portMUX_TYPE s_rumble_shadow_mux = portMUX_INITIALIZER_UNLOCKED;

// After a channel becomes ready, ignore rumble for this long.  Kills any
// connect-time buzz (a controller/host pulse at connect) — the user isn't
// mid-action right at connect, so dropping it is invisible.
#define RUMBLE_CONNECT_GRACE_US 500000

// Minimum spacing between rumble writes on a channel (per-channel rate limit).
// A controller-paced burst (queuing several copies to the BLE TX so the LL drains
// them at the connection interval) was tried but overran the BLE stack and crashed
// the device, so we send exactly ONE write per drive call and cap the rate here.
#define RUMBLE_MIN_GAP_US 12000

// Steady output pacing.  The host only streams ~34 packets/s/channel (~29 ms
// apart) but each packet's 3 frames play ~20 ms, so relaying once leaves a ~9 ms
// gap (and the two channels' anti-phase gaps looked complementary).  A single
// high-res esp_timer is the SOLE driver of BLE writes at a fixed cadence (no
// coalescing with 'rs' posts): every tick each channel sends the freshly-arrived
// packet if there is one (faithful 3-frame envelope), otherwise a steady HOLD =
// the latest frame replicated across all 3 slots (a continuation of the current
// level, NEVER a stop, safe to repeat).  Sending every ~13 ms (< the ~20 ms
// playback) tiles the gap so rumble is continuous on both channels at once.
#define RUMBLE_TICK_US      13000  // sole output cadence (~77/s/channel)
#define RUMBLE_HOLD_IDLE_US 60000  // stop feeding this long after the last packet

// True if a Joy-Con rumble payload commands motor activity (non-zero amplitude).
// Layout: byte0=0x00, byte1=0x50|id, then 3x 5-byte frames at offsets 2/7/12 with
// lf_amp at bits 10-19 and hf_amp at bits 30-39.  Short/unknown => treat as active.
static bool rumble_payload_active(const uint8_t *data, size_t len)
{
    if (data == NULL) return false;
    if (len < 17) return len > 0;
    for (int fo = 2; fo + 5 <= (int)len && fo <= 12; fo += 5) {
        uint64_t v = 0;
        for (int b = 0; b < 5; b++) v |= ((uint64_t)data[fo + b]) << (8 * b);
        if (((v >> 10) & 0x3FF) || ((v >> 30) & 0x3FF)) return true;
    }
    return false;
}

static char rx_buf[256];
static int rx_len = 0;
static volatile bool request_status = false;
static volatile bool scan_mode = false;
static volatile uint8_t active_ble_channels = 0;
static volatile bool s_auto_connect_enabled = true;

static uint8_t ble_own_addr_type;
static controller_channel_t channels[MAX_BLE_CHANNELS];
static int connecting_channel = -1;
static ble_addr_t connecting_addr;
static TaskHandle_t s_deferred_ac_task = NULL;
static ble_addr_t s_deferred_ac_addr;

static int ble_gap_event(struct ble_gap_event *event, void *arg);
static void start_ble_scan(void);

static bool cdc_host_ready(void)
{
    if (!tud_cdc_connected()) {
        return false;
    }
    if ((tud_cdc_get_line_state() & CDC_LINE_STATE_DTR) == 0) {
        return false;
    }
    return true;
}

static void safe_cdc_write(const uint8_t *data, uint32_t len)
{
    uint32_t written = 0;
    uint32_t timeout_ms = 0;

    while (written < len && timeout_ms < 100) {
        if (!cdc_host_ready()) {
            vTaskDelay(pdMS_TO_TICKS(1));
            timeout_ms++;
            continue;
        }

        uint32_t avail = tud_cdc_write_available();
        if (avail > 0) {
            uint32_t remaining = len - written;
            uint32_t to_write = remaining > avail ? avail : remaining;
            tud_cdc_write(data + written, to_write);
            tud_cdc_write_flush();
            written += to_write;
            timeout_ms = 0;
        } else {
            vTaskDelay(pdMS_TO_TICKS(1));
            timeout_ms++;
        }
    }
}

static void wake_cdc_stream_task(void)
{
    if (s_stream_wake != NULL) {
        xSemaphoreGive(s_stream_wake);
    }
}

static void uuid_to_lower_string(const ble_uuid_t *uuid, char *out, size_t out_len)
{
    if (out == NULL || out_len == 0) {
        return;
    }
    out[0] = '\0';
    if (uuid == NULL) {
        return;
    }

    ble_uuid_to_str(uuid, out);
    out[out_len - 1] = '\0';
    for (char *p = out; *p; p++) {
        *p = (char)tolower((unsigned char)*p);
    }
}

static bool is_input_uuid(const char *uuid)
{
    return uuid &&
           (strcmp(uuid, SWITCH2_NOTIFY_FD2_UUID) == 0 ||
            strcmp(uuid, SWITCH2_NOTIFY_LEGACY_UUID) == 0);
}

static bool is_post_init_notify_uuid(const char *uuid)
{
    return uuid && strcmp(uuid, SWITCH2_NOTIFY_FD2_UUID) == 0;
}

static bool is_ack_uuid(const char *uuid)
{
    return uuid && strcmp(uuid, SWITCH2_ACK_UUID) == 0;
}

static bool is_command_uuid(const char *uuid)
{
    return uuid && strcmp(uuid, SWITCH2_CMD_UUID) == 0;
}

static bool is_rumble_uuid(const char *uuid)
{
    return uuid &&
           (strcmp(uuid, SWITCH2_RUMBLE_PRO_UUID) == 0 ||
            strcmp(uuid, SWITCH2_RUMBLE_JOYCON_R_UUID) == 0 ||
            strcmp(uuid, SWITCH2_RUMBLE_JOYCON_L_UUID) == 0);
}

static controller_channel_t *channel_for_conn(uint16_t conn_handle)
{
    for (size_t i = 0; i < MAX_BLE_CHANNELS; i++) {
        if (channels[i].used && channels[i].conn_handle == conn_handle) {
            return &channels[i];
        }
    }
    return NULL;
}

static int channel_index(const controller_channel_t *ctx)
{
    if (ctx == NULL) {
        return -1;
    }
    for (size_t i = 0; i < MAX_BLE_CHANNELS; i++) {
        if (&channels[i] == ctx) {
            return (int)i;
        }
    }
    return -1;
}

static bool has_free_channel(void)
{
    for (size_t i = 0; i < MAX_BLE_CHANNELS; i++) {
        if (!channels[i].used) {
            return true;
        }
    }
    return false;
}

static int alloc_channel(void)
{
    for (size_t i = 0; i < MAX_BLE_CHANNELS; i++) {
        if (!channels[i].used) {
            memset(&channels[i], 0, sizeof(channels[i]));
            channels[i].used = true;
            channels[i].conn_handle = BLE_HS_CONN_HANDLE_NONE;
            channels[i].desc_discovery_index = -1;
            channels[i].subscribe_index = -1;
            return (int)i;
        }
    }
    return -1;
}

static void refresh_active_ble_channels(void)
{
    uint8_t mask = 0;
    for (size_t i = 0; i < MAX_BLE_CHANNELS; i++) {
        if (channels[i].used && channels[i].ready) {
            mask |= (uint8_t)(1u << i);
        }
    }
    active_ble_channels = mask;
}

static void release_channel(controller_channel_t *ctx)
{
    int idx = channel_index(ctx);
    if (idx < 0) {
        return;
    }
    memset(&channels[idx], 0, sizeof(channels[idx]));
    channels[idx].conn_handle = BLE_HS_CONN_HANDLE_NONE;
    channels[idx].desc_discovery_index = -1;
    channels[idx].subscribe_index = -1;
    // Drop any stale rumble shadow so a controller that later reuses this slot
    // does not inherit the previous one's active rumble.
    portENTER_CRITICAL(&s_rumble_shadow_mux);
    memset(&s_rumble_shadow[idx], 0, sizeof(s_rumble_shadow[idx]));
    portEXIT_CRITICAL(&s_rumble_shadow_mux);
    refresh_active_ble_channels();
}

static discovered_report_char_t *find_char_by_value_handle(controller_channel_t *ctx, uint16_t value_handle)
{
    if (ctx == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < ctx->report_char_count; i++) {
        if (ctx->report_chars[i].val_handle == value_handle) {
            return &ctx->report_chars[i];
        }
    }
    return NULL;
}

static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static size_t parse_hex_bytes(const char *hex, uint8_t *out, size_t out_max)
{
    size_t len = 0;
    while (hex && hex[0] && hex[1] && len < out_max) {
        int hi = hex_nibble(hex[0]);
        int lo = hex_nibble(hex[1]);
        if (hi < 0 || lo < 0) {
            break;
        }
        out[len++] = (uint8_t)((hi << 4) | lo);
        hex += 2;
    }
    return len;
}

static esp_err_t write_ble_payload(controller_channel_t *ctx, char kind, const uint8_t *data, uint16_t len)
{
    if (ctx == NULL || data == NULL || len == 0 || !ctx->used ||
        ctx->conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        return ESP_ERR_INVALID_STATE;
    }

    uint16_t handle = 0;
    bool no_rsp = false;
    if (kind == 'c') {
        handle = ctx->command_value_handle;
        no_rsp = ctx->command_write_no_rsp;
    } else {
        handle = ctx->rumble_value_handle;
        no_rsp = ctx->rumble_write_no_rsp;
    }
    if (handle == 0) {
        return ESP_ERR_INVALID_STATE;
    }

    // Pure transport: issue the write once and report the result. No retry/blocking here
    // (a blocking retry under load pile-ups the mbuf pool and stalls the task). The app
    // owns rumble pacing/repetition.
    int rc = no_rsp
        ? ble_gattc_write_no_rsp_flat(ctx->conn_handle, handle, data, len)
        : ble_gattc_write_flat(ctx->conn_handle, handle, data, len, NULL, NULL);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

static void send_status_response(void)
{
    // Report our own BLE address so the host can run the Switch 2 application-level
    // pairing handshake (COMMAND_PAIR / SET_MAC) with THIS bridge's MAC. The
    // controller then bonds to the bridge and reconnects to it on a button press
    // (instead of to whatever host it was previously paired with, e.g. the PC).
    char own_mac[18] = "00:00:00:00:00:00";
    uint8_t own_addr[6] = {0};
    if (ble_hs_id_copy_addr(ble_own_addr_type, own_addr, NULL) == 0) {
        snprintf(own_mac, sizeof(own_mac), "%02X:%02X:%02X:%02X:%02X:%02X",
                 own_addr[5], own_addr[4], own_addr[3],
                 own_addr[2], own_addr[1], own_addr[0]);
    }

    char response[256];
    int len = snprintf(response,
                       sizeof(response),
                       "{\"cmd\":\"status\",\"version\":\"%s\",\"profile\":\"%s\",\"build\":\"%s\",\"ble_channels\":%u,\"mac\":\"%s\",\"features\":{\"wrpair\":1,\"shadow\":1}}\n",
                       APP_FIRMWARE_VERSION,
                       EXPECTED_FIRMWARE_PROFILE,
                       EXPECTED_FIRMWARE_BUILD,
                       (unsigned)active_ble_channels,
                       own_mac);
    if (len > 0) {
        uint32_t write_len = (uint32_t)len < sizeof(response) ? (uint32_t)len : sizeof(response) - 1;
        safe_cdc_write((const uint8_t *)response, write_len);
    }
    request_status = false;
}

static void handle_write_command(char *command)
{
    char *save = NULL;
    char *tok = strtok_r(command, " ", &save);
    if (tok == NULL || strcmp(tok, "wr") != 0) {
        return;
    }

    char *channel_text = strtok_r(NULL, " ", &save);
    char *kind_text = strtok_r(NULL, " ", &save);
    char *hex_text = strtok_r(NULL, " ", &save);
    if (channel_text == NULL || kind_text == NULL || hex_text == NULL) {
        ESP_LOGW(TAG, "Invalid BLE write command");
        return;
    }

    uint8_t payload[96];
    size_t len = parse_hex_bytes(hex_text, payload, sizeof(payload));
    if (len == 0) {
        ESP_LOGW(TAG, "Invalid BLE write payload");
        return;
    }

    // Compute "active" (non-zero amplitude) once — the same payload goes to every target
    // channel. Joy-Con vibration: 0x00, (0x50|id), then 3 x 5-byte frames at offsets
    // 2/7/12; each frame packs lf_amp at bits 10-19 and hf_amp at bits 30-39.
    bool active = (len < 17);  // unknown layout -> don't mislabel as silent
    for (int fo = 2; !active && fo + 5 <= (int)len && fo <= 12; fo += 5) {
        uint64_t v = 0;
        for (int b = 0; b < 5; b++) {
            v |= ((uint64_t)payload[fo + b]) << (8 * b);
        }
        uint16_t lf_amp = (uint16_t)((v >> 10) & 0x3FF);
        uint16_t hf_amp = (uint16_t)((v >> 30) & 0x3FF);
        if (lf_amp != 0 || hf_amp != 0) {
            active = true;
        }
    }

    // channel_text is a single channel ("3") OR a comma-separated list ("3,4") so the host
    // can fan ONE merged-Joy-Con rumble to both channels in a single command — both motors
    // get the identical frame at the same instant (in-phase, no L/R complementary drift).
    char *ch_save = NULL;
    for (char *ch_tok = strtok_r(channel_text, ",", &ch_save);
         ch_tok != NULL;
         ch_tok = strtok_r(NULL, ",", &ch_save)) {
        int channel = atoi(ch_tok);
        if (channel < 0 || channel >= MAX_BLE_CHANNELS) {
            ESP_LOGW(TAG, "Invalid BLE write channel=%d", channel);
            continue;
        }
        esp_err_t err = write_ble_payload(&channels[channel], kind_text[0], payload, (uint16_t)len);
        if (kind_text[0] == 'r') {
            if (err == ESP_OK) {
                s_rumble_tx_ok[channel]++;
            } else {
                s_rumble_tx_fail[channel]++;
            }
            if (active) {
                s_rumble_active[channel]++;
            }
        }
        if (err != ESP_OK) {
            ESP_LOGD(TAG, "BLE write failed channel=%d kind=%c len=%u err=%d",
                     channel, kind_text[0], (unsigned)len, (int)err);
        }
    }
}

// handle_wrpair_command — write different rumble payloads to left and right
// Joy-Con BLE channels in one command cycle.
//
// Format: "wrpair <ch_l> <ch_r> <kind> <hex_payload_l> <hex_payload_r>"
// Example: "wrpair 0 1 r 005011... 005011..."
//
// Purpose: the Python host sends this instead of two separate "wr" commands so
// that both BLE writes are queued in the same firmware scheduling cycle.  This
// avoids the ~30 Hz L/R alternation that occurs when the host dispatches two
// independent writes: the BLE radio round-robins the two connections and tends
// to serve only one per connection event when writes arrive separately.
// Each side keeps its own payload (left ≠ right) so games that produce
// different L/R rumble intensities are handled correctly.
static void handle_wrpair_command(char *command)
{
    char *save = NULL;
    char *tok = strtok_r(command, " ", &save);
    if (tok == NULL || strcmp(tok, "wrpair") != 0) return;

    char *ch_l_text  = strtok_r(NULL, " ", &save);
    char *ch_r_text  = strtok_r(NULL, " ", &save);
    char *kind_text  = strtok_r(NULL, " ", &save);
    char *hex_l_text = strtok_r(NULL, " ", &save);
    char *hex_r_text = strtok_r(NULL, " ", &save);

    if (!ch_l_text || !ch_r_text || !kind_text || !hex_l_text || !hex_r_text) {
        ESP_LOGW(TAG, "Invalid wrpair command (missing fields)");
        return;
    }

    int ch_l = atoi(ch_l_text);
    int ch_r = atoi(ch_r_text);
    if (ch_l < 0 || ch_l >= MAX_BLE_CHANNELS || ch_r < 0 || ch_r >= MAX_BLE_CHANNELS) {
        ESP_LOGW(TAG, "wrpair invalid channels l=%d r=%d", ch_l, ch_r);
        return;
    }

    uint8_t payload_l[96], payload_r[96];
    size_t len_l = parse_hex_bytes(hex_l_text, payload_l, sizeof(payload_l));
    size_t len_r = parse_hex_bytes(hex_r_text, payload_r, sizeof(payload_r));
    if (len_l == 0 || len_r == 0) {
        ESP_LOGW(TAG, "wrpair empty payload l=%u r=%u", (unsigned)len_l, (unsigned)len_r);
        return;
    }

    // Determine active (non-zero amplitude) flag for each side independently.
    // Joy-Con vibration: lf_amp at bits 10-19, hf_amp at bits 30-39 of each
    // 5-byte frame at offsets 2/7/12 within the payload.
    bool active_l = (len_l < 17);
    for (int fo = 2; !active_l && fo + 5 <= (int)len_l && fo <= 12; fo += 5) {
        uint64_t v = 0;
        for (int b = 0; b < 5; b++) v |= ((uint64_t)payload_l[fo + b]) << (8 * b);
        if (((v >> 10) & 0x3FF) || ((v >> 30) & 0x3FF)) active_l = true;
    }
    bool active_r = (len_r < 17);
    for (int fo = 2; !active_r && fo + 5 <= (int)len_r && fo <= 12; fo += 5) {
        uint64_t v = 0;
        for (int b = 0; b < 5; b++) v |= ((uint64_t)payload_r[fo + b]) << (8 * b);
        if (((v >> 10) & 0x3FF) || ((v >> 30) & 0x3FF)) active_r = true;
    }

    // Write to both channels sequentially in the same command cycle.
    // If one write fails, the other is still attempted (independent results).
    esp_err_t err_l = write_ble_payload(&channels[ch_l], kind_text[0], payload_l, (uint16_t)len_l);
    esp_err_t err_r = write_ble_payload(&channels[ch_r], kind_text[0], payload_r, (uint16_t)len_r);

    if (kind_text[0] == 'r') {
        if (err_l == ESP_OK) s_rumble_tx_ok[ch_l]++;   else s_rumble_tx_fail[ch_l]++;
        if (err_r == ESP_OK) s_rumble_tx_ok[ch_r]++;   else s_rumble_tx_fail[ch_r]++;
        if (active_l) s_rumble_active[ch_l]++;
        if (active_r) s_rumble_active[ch_r]++;
        s_wrpair_cmd++;
    }

    ESP_LOGD(TAG, "RUMBLE_PAIR chL=%d okL=%d activeL=%d chR=%d okR=%d activeR=%d",
             ch_l, err_l == ESP_OK, active_l, ch_r, err_r == ESP_OK, active_r);

    if (err_l != ESP_OK)
        ESP_LOGD(TAG, "wrpair BLE write failed chL=%d err=%d", ch_l, (int)err_l);
    if (err_r != ESP_OK)
        ESP_LOGD(TAG, "wrpair BLE write failed chR=%d err=%d", ch_r, (int)err_r);
}

// handle_rumble_shadow_command — accept ONE rumble packet for one channel.
// Format: "rs <ch> <hex_payload>".  Stores the payload as pending; rumble_driver_task
// sends it on its next tick.  The handler does not write BLE itself.
static void handle_rumble_shadow_command(char *command)
{
    char *save = NULL;
    char *tok = strtok_r(command, " ", &save);
    if (tok == NULL || strcmp(tok, "rs") != 0) return;

    char *ch_text  = strtok_r(NULL, " ", &save);
    char *hex_text = strtok_r(NULL, " ", &save);
    if (!ch_text || !hex_text) {
        ESP_LOGW(TAG, "Invalid rs command (missing fields)");
        return;
    }
    int ch = atoi(ch_text);
    if (ch < 0 || ch >= MAX_BLE_CHANNELS) {
        ESP_LOGW(TAG, "rs invalid channel %d", ch);
        return;
    }
    uint8_t payload[NINTENDO_REPORT_SIZE];
    size_t len = parse_hex_bytes(hex_text, payload, sizeof(payload));
    if (len < 2) {
        ESP_LOGW(TAG, "rs short payload ch=%d", ch);
        return;
    }

    int64_t now_us = esp_timer_get_time();
    portENTER_CRITICAL(&s_rumble_shadow_mux);
    if (now_us < s_rumble_shadow[ch].suppress_until_us) {
        // Connect grace window: drop rumble so a connect-time pulse can't buzz.
        portEXIT_CRITICAL(&s_rumble_shadow_mux);
        s_rumble_shadow_set++;
        return;
    }
    memcpy(s_rumble_shadow[ch].payload, payload, len);
    s_rumble_shadow[ch].len = (uint16_t)len;
    s_rumble_shadow[ch].pending = true;
    portEXIT_CRITICAL(&s_rumble_shadow_mux);
    s_rumble_shadow_set++;
}

// rumble_relay_ev — runs in the NIMBLE HOST-TASK context once per timer tick.
// For each ongoing channel it emits exactly ONE write this tick: the freshly
// arrived host packet if there is one (faithful 3-frame envelope), otherwise a
// steady HOLD (latest frame x3 = continuation of the current level, never a stop).
// Emit ONE write for this channel this tick (relay a fresh host packet, or a
// steady HOLD), advancing the rolling packet-id.  Called from rumble_driver_task.
static void rumble_drive_channel(int ch, int64_t now_us)
{
    if (!channels[ch].used ||
        channels[ch].conn_handle == BLE_HS_CONN_HANDLE_NONE ||
        !channels[ch].ready ||
        channels[ch].rumble_value_handle == 0) {
        return;
    }

    uint8_t buf[NINTENDO_REPORT_SIZE];
    uint16_t len = 0;
    uint8_t id = 0;
    bool send = false;
    bool is_hold = false;

    portENTER_CRITICAL(&s_rumble_shadow_mux);
    rumble_shadow_t *sh = &s_rumble_shadow[ch];

    // Rate limit: this is driven by the esp_timer event and by input notifications
    // re-queuing the event, so cap the per-channel write spacing.
    if (now_us - sh->last_write_us < RUMBLE_MIN_GAP_US) {
        portEXIT_CRITICAL(&s_rumble_shadow_mux);
        return;
    }

    if (sh->pending && sh->len >= 2) {
        // Relay the real host packet (full, faithful 3-frame envelope).
        len = sh->len;
        memcpy(buf, sh->payload, len);
        sh->pending = false;
        sh->holding = true;
        sh->last_packet_us = now_us;
        sh->tx_id = (sh->tx_id + 1) & 0x0F;
        id = sh->tx_id;
        send = true;
    } else if (sh->holding) {
        if (now_us - sh->last_packet_us >= RUMBLE_HOLD_IDLE_US) {
            // Host stopped streaming without further packets — stop feeding.
            // (A real stop arrives as a zero packet, which is relayed above.)
            sh->holding = false;
        } else if (sh->len >= 17) {
            // Fill with a steady HOLD = latest frame (bytes [12..17)) replicated
            // across all three slots: continuation of the current level, never a stop.
            buf[0] = 0x00;
            memcpy(&buf[2],  &sh->payload[12], 5);
            memcpy(&buf[7],  &sh->payload[12], 5);
            memcpy(&buf[12], &sh->payload[12], 5);
            len = 17;
            sh->tx_id = (sh->tx_id + 1) & 0x0F;
            id = sh->tx_id;
            send = true;
            is_hold = true;
        }
    }
    if (send) {
        sh->last_write_us = now_us;
    }
    portEXIT_CRITICAL(&s_rumble_shadow_mux);

    if (send) {
        buf[1] = 0x50 | (id & 0x0F);  // stamp rolling packet-id
        esp_err_t err = write_ble_payload(&channels[ch], 'r', buf, len);
        if (err == ESP_OK) s_rumble_tx_ok[ch]++; else s_rumble_tx_fail[ch]++;
        if (rumble_payload_active(buf, len)) s_rumble_active[ch]++;
        if (is_hold) s_rumble_hold++;
    }
}

// rumble_driver_task — the SOLE driver of rumble output, on its OWN task.
// The host-task-based event approach was capped at ~33 writes/s/channel because
// the busy NimBLE host task serviced the event only ~33/s (not enough to tile the
// ~20 ms 3-frame playback, so a ~10 ms gap remained every cycle).  write_ble_payload
// is already driven from a non-host task elsewhere (tinyusb_cdc_rx_callback handles
// wr/wrpair), so writing from this dedicated task is safe; an 8 KB stack avoids the
// overflow that crashed the first attempt.  vTaskDelayUntil gives a jitter-free
// cadence both channels share, keeping a merged L/R pair in phase.
// Driven by esp_timer in the NimBLE HOST-TASK context (via the event below).
// Writing rumble from the host task (rather than a separate task) avoids
// ble_hs_lock contention with the host's own input-notification processing, which
// is what let a dedicated task either starve input (same core) or get starved by
// the host (other core).  This caps rumble at the host's spare service rate
// (~33/s/ch) but keeps input at full rate — the best stable balance on this HW.
static struct ble_npl_event s_rumble_ev;

static void rumble_relay_ev(struct ble_npl_event *ev)
{
    (void)ev;
    int64_t now_us = esp_timer_get_time();
    for (int ch = 0; ch < MAX_BLE_CHANNELS; ch++) {
        rumble_drive_channel(ch, now_us);
    }
}

static esp_timer_handle_t s_rumble_timer;
static void rumble_tick_cb(void *arg)
{
    (void)arg;
    ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &s_rumble_ev);
}

static void handle_conn_command(char *command)
{
    char *save = NULL;
    char *tok = strtok_r(command, " ", &save);
    if (tok == NULL || strcmp(tok, "conn") != 0) return;

    char *type_str = strtok_r(NULL, " ", &save);
    char *mac_str = strtok_r(NULL, " ", &save);
    if (!type_str || !mac_str) return;

    ble_addr_t addr;
    addr.type = atoi(type_str);
    unsigned int mac[6];
    if (sscanf(mac_str, "%x:%x:%x:%x:%x:%x", &mac[5], &mac[4], &mac[3], &mac[2], &mac[1], &mac[0]) == 6) {
        for (int i=0; i<6; i++) addr.val[i] = (uint8_t)mac[i];

        if (connecting_channel >= 0) {
            char busy[64];
            int busy_len = snprintf(busy, sizeof(busy),
                "{\"cmd\":\"connect_busy\",\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\"}\n",
                addr.val[5], addr.val[4], addr.val[3],
                addr.val[2], addr.val[1], addr.val[0]);
            if (busy_len > 0) safe_cdc_write((const uint8_t *)busy, busy_len);
            return;
        }

        if (ble_gap_disc_active()) {
            int cancel_rc = ble_gap_disc_cancel();
            if (cancel_rc != 0) {
                ESP_LOGW(TAG, "BLE scan cancel before connect rc=%d", cancel_rc);
            }
        }

        int channel = alloc_channel();
        if (channel < 0) return;
        connecting_channel = channel;
        connecting_addr = addr;

        struct ble_gap_conn_params conn_params = {
            .scan_itvl = 0x0010,
            .scan_window = 0x0010,
            .itvl_min = 6,   // 7.5ms (BLE spec minimum; values below 6 are rejected)
            .itvl_max = 6,   // 7.5ms
            .latency = 0,
            .supervision_timeout = 400,
            // Bound each connection event to ~1.875ms (3 * 0.625ms). The Switch 2
            // input/vibration packets are tiny, so a short event is plenty.
            .min_ce_len = 0,
            .max_ce_len = 0x0003,
        };
        int rc = ble_gap_connect(ble_own_addr_type, &addr, 10000, &conn_params, ble_gap_event, NULL);
        if (rc != 0) {
            release_channel(&channels[channel]);
            connecting_channel = -1;
        }
    }
}

static void handle_disc_command(char *command)
{
    char *save = NULL;
    char *tok = strtok_r(command, " ", &save);
    if (tok == NULL || strcmp(tok, "disc") != 0) return;

    char *channel_str = strtok_r(NULL, " ", &save);
    if (!channel_str) return;

    int channel = atoi(channel_str);
    if (channel >= 0 && channel < MAX_BLE_CHANNELS && channels[channel].used) {
        ble_gap_terminate(channels[channel].conn_handle, BLE_ERR_REM_USER_CONN_TERM);
    }
}

static void cdc_stream_task(void *arg)
{
    (void)arg;
    int64_t last_rumble_report_us = 0;

    while (1) {
        // Once a second, dump per-channel rumble and drop diagnostics.
        int64_t now_us = esp_timer_get_time();
        if (now_us - last_rumble_report_us >= 1000000) {
            last_rumble_report_us = now_us;
            if (s_rumble_tx_ok[0] || s_rumble_tx_ok[1] || s_rumble_tx_fail[0] || s_rumble_tx_fail[1] || s_wrpair_cmd || s_rumble_shadow_set || s_ctrl_drop_count) {
                char rdbg[360];
                int rl = snprintf(rdbg, sizeof(rdbg),
                    "{\"cmd\":\"debug\",\"msg\":\"RUMBLE-FW rs=%u hold=%u ch0=%u/%u/%u ch1=%u/%u/%u (ok/fail/active) in ch0=%u ch1=%u (/s) ctrl_drop=%u\"}\n",
                    (unsigned)s_rumble_shadow_set,
                    (unsigned)s_rumble_hold,
                    (unsigned)s_rumble_tx_ok[0], (unsigned)s_rumble_tx_fail[0], (unsigned)s_rumble_active[0],
                    (unsigned)s_rumble_tx_ok[1], (unsigned)s_rumble_tx_fail[1], (unsigned)s_rumble_active[1],
                    (unsigned)s_input_fwd[0], (unsigned)s_input_fwd[1],
                    (unsigned)s_ctrl_drop_count);
                if (rl > 0) safe_cdc_write((const uint8_t *)rdbg, rl);
            }
            s_ctrl_drop_count = 0;
            s_wrpair_cmd = 0;
            s_rumble_shadow_set = 0;
            s_rumble_hold = 0;
            for (int i = 0; i < MAX_BLE_CHANNELS; i++) {
                s_rumble_tx_ok[i] = 0;
                s_rumble_tx_fail[i] = 0;
                s_rumble_active[i] = 0;
                s_input_fwd[i] = 0;
            }
        }

        if (request_status) {
            send_status_response();
            continue;
        }

        // P0: drain q_ctrl (ACK/command — prioritised, re-loop after each item)
        controller_report_t report;
        if (xQueueReceive(q_ctrl, &report, 0) == pdTRUE) {
            uint8_t payload_len = report.length > NINTENDO_REPORT_SIZE ? NINTENDO_REPORT_SIZE : report.length;
            uint8_t frame_len = payload_len + 1;
            // High bit (0x80) flags command/ack so the host routes it away from the input parser.
            uint8_t chan_byte = (uint8_t)(report.channel + 1) | 0x80;
            uint8_t header[4] = {0xaa, 0x55, frame_len, chan_byte};
            safe_cdc_write(header, sizeof(header));
            safe_cdc_write(report.payload, payload_len);
            continue;
        }

        // P2: send latest shadow input for each channel (drops superseded frames)
        bool sent_any = false;
        for (int i = 0; i < MAX_BLE_CHANNELS; i++) {
            controller_report_t shadow;
            bool dirty = false;
            portENTER_CRITICAL(&s_input_shadow_mux);
            if (s_latest_input_dirty[i]) {
                shadow = s_latest_input[i];
                s_latest_input_dirty[i] = false;
                dirty = true;
            }
            portEXIT_CRITICAL(&s_input_shadow_mux);
            if (!dirty) {
                continue;
            }
            uint8_t payload_len = shadow.length > NINTENDO_REPORT_SIZE ? NINTENDO_REPORT_SIZE : shadow.length;
            uint8_t frame_len = payload_len + 1;
            uint8_t chan_byte = (uint8_t)(shadow.channel + 1);  // no 0x80: input report
            uint8_t header[4] = {0xaa, 0x55, frame_len, chan_byte};
            safe_cdc_write(header, sizeof(header));
            safe_cdc_write(shadow.payload, payload_len);
            sent_any = true;
        }

        if (!sent_any) {
            // Nothing to send — sleep until BLE notify or status request wakes us
            xSemaphoreTake(s_stream_wake, pdMS_TO_TICKS(5));
        }
    }
}

static bool is_target_controller(const struct ble_gap_disc_desc *disc)
{
    const uint8_t *data = disc->data;
    uint8_t len = disc->length_data;
    uint8_t pos = 0;

    while (pos < len) {
        uint8_t ad_len = data[pos];
        if (ad_len == 0) {
            break;
        }
        // Protect against malformed structures
        if (pos + 1 + ad_len > len) {
            break;
        }
        uint8_t ad_type = data[pos + 1];
        if (ad_type == 0xFF && ad_len >= 3) {
            uint16_t company_id = (uint16_t)data[pos + 2] | ((uint16_t)data[pos + 3] << 8);
            if (company_id == NINTENDO_COMPANY_ID) {
                return true;
            }
        }
        pos += ad_len + 1;
    }

    return false;
}


static void deferred_auto_connect_task(void *arg)
{
    (void)arg;
    if (ble_gap_disc_active()) {
        ble_gap_disc_cancel();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    if (connecting_channel < 0 && has_free_channel()) {
        int ac_channel = alloc_channel();
        if (ac_channel >= 0) {
            connecting_channel = ac_channel;
            connecting_addr = s_deferred_ac_addr;
            struct ble_gap_conn_params ac_params = {
                .scan_itvl = 0x0010,
                .scan_window = 0x0010,
                .itvl_min = 6,   // 7.5ms (BLE spec minimum)
                .itvl_max = 6,   // 7.5ms
                .latency = 0,
                .supervision_timeout = 400,
                .min_ce_len = 0,
                .max_ce_len = 0x0003,
            };
            int ac_rc = ble_gap_connect(ble_own_addr_type, &s_deferred_ac_addr, 10000,
                                        &ac_params, ble_gap_event, NULL);
            if (ac_rc != 0) {
                ESP_LOGW(TAG, "BLE deferred auto-connect failed rc=%d", ac_rc);
                release_channel(&channels[ac_channel]);
                connecting_channel = -1;
                start_ble_scan();
            } else {
                ESP_LOGI(TAG, "BLE deferred auto-connect initiated channel=%d", ac_channel);
            }
        }
    }
    s_deferred_ac_task = NULL;
    vTaskDelete(NULL);
}

static void start_ble_scan(void)
{
    // Only scan when the host (main program) has explicitly asked us to via
    // "scan on". This ensures that after "scan off" (app shutdown) a disconnect
    // event cannot silently restart scanning, and that at boot the bridge idles
    // until the main program tells it to start working.
    if (!scan_mode) {
        return;
    }
    if (ble_gap_disc_active() || connecting_channel >= 0 || !has_free_channel()) {
        return;
    }

    struct ble_gap_disc_params params = {
        .itvl = 0x0030,
        .window = 0x0030,
        .filter_policy = 0,
        .limited = 0,
        .passive = 0,
        .filter_duplicates = 0,
        .disable_observer_mode = 0,
    };

    int rc = ble_gap_disc(ble_own_addr_type,
                          BLE_SCAN_DURATION_MS,
                          &params,
                          ble_gap_event,
                          NULL);
    if (rc != 0) {
        ESP_LOGW(TAG, "BLE scan start failed rc=%d", rc);
    } else {
        ESP_LOGI(TAG, "BLE scan started");
    }
}

static void reset_gatt_state(controller_channel_t *ctx)
{
    if (ctx == NULL) {
        return;
    }
    ctx->ready = false;
    ctx->service_count = 0;
    ctx->service_discovery_index = 0;
    ctx->report_char_count = 0;
    ctx->desc_discovery_index = -1;
    ctx->subscribe_index = -1;
    ctx->command_value_handle = 0;
    ctx->command_write_no_rsp = false;
    ctx->rumble_value_handle = 0;
    ctx->rumble_write_no_rsp = false;
    ctx->init_started = false;
    ctx->init_done = false;
    ctx->init_index = 0;
    memset(ctx->services, 0, sizeof(ctx->services));
    memset(ctx->report_chars, 0, sizeof(ctx->report_chars));
    refresh_active_ble_channels();
}

static int gatt_subscribe_cb(uint16_t conn_handle,
                             const struct ble_gatt_error *error,
                             struct ble_gatt_attr *attr,
                             void *arg);

// Removed auto-init sequence

static void subscribe_next_report(controller_channel_t *ctx)
{
    if (ctx == NULL) {
        return;
    }
    uint8_t enable_notify[2] = {0x01, 0x00};

    for (int i = ctx->subscribe_index + 1; i < (int)ctx->report_char_count; i++) {
        discovered_report_char_t *chr = &ctx->report_chars[i];
        if (!chr->notify_target) {
            continue;
        }

        uint16_t cccd = chr->cccd_handle ? chr->cccd_handle : chr->val_handle + 1;
        ctx->subscribe_index = i;
        int rc = ble_gattc_write_flat(ctx->conn_handle,
                                      cccd,
                                      enable_notify,
                                      sizeof(enable_notify),
                                      gatt_subscribe_cb,
                                      NULL);
        if (rc != 0) {
            ESP_LOGW(TAG, "BLE subscribe start failed value=0x%04x cccd=0x%04x rc=%d",
                     chr->val_handle,
                     cccd,
                     rc);
            continue;
        }
        ESP_LOGI(TAG, "BLE subscribe start value=0x%04x cccd=0x%04x",
                 chr->val_handle,
                 cccd);
        return;
    }

    if (ctx->report_char_count == 0) {
        ESP_LOGW(TAG, "BLE GATT ready but no characteristics found — disconnecting to release channel");
        ble_gap_terminate(ctx->conn_handle, BLE_ERR_REM_USER_CONN_TERM);
        return;
    }

    ESP_LOGI(TAG, "BLE GATT ready channel=%d chars=%u",
             channel_index(ctx),
             (unsigned)ctx->report_char_count);

    ctx->ready = true;
    refresh_active_ble_channels();

    // Start this channel's rumble shadow clean and silent, and apply a connect
    // grace window so any connect-time rumble pulse cannot buzz the motor.
    {
        int rdy_idx = channel_index(ctx);
        if (rdy_idx >= 0) {
            portENTER_CRITICAL(&s_rumble_shadow_mux);
            memset(&s_rumble_shadow[rdy_idx], 0, sizeof(s_rumble_shadow[rdy_idx]));
            s_rumble_shadow[rdy_idx].suppress_until_us =
                esp_timer_get_time() + RUMBLE_CONNECT_GRACE_US;
            portEXIT_CRITICAL(&s_rumble_shadow_mux);
        }
    }

    // Re-assert 7.5ms on this connection in case the controller negotiated something
    // slower during setup. NOTE: this pins the per-connection INTERVAL to 7.5ms, but the
    // ESP32-S3 BLE controller still services connections one-per-interval, so with N
    // controllers each is serviced every N*7.5ms — a controller scheduling limit, not
    // something these params can override (confirmed: stopping the scan does not help).
    {
        struct ble_gap_upd_params upd = {
            .itvl_min = 6,   // 7.5ms
            .itvl_max = 6,   // 7.5ms
            .latency = 0,
            .supervision_timeout = 400,
            .min_ce_len = 0,
            .max_ce_len = 0x0003,
        };
        int upd_rc = ble_gap_update_params(ctx->conn_handle, &upd);
        if (upd_rc != 0) {
            ESP_LOGW(TAG, "BLE conn param update request failed rc=%d", upd_rc);
        }
    }

    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
             ctx->peer_addr.val[5], ctx->peer_addr.val[4], ctx->peer_addr.val[3],
             ctx->peer_addr.val[2], ctx->peer_addr.val[1], ctx->peer_addr.val[0]);
             
    char response[128];
    int len = snprintf(response, sizeof(response),
                       "{\"cmd\":\"connected\",\"channel\":%d,\"mac\":\"%s\"}\n",
                       channel_index(ctx), mac_str);
    if (len > 0) {
        safe_cdc_write((const uint8_t *)response, len);
    }
    // Resume scanning for additional controllers now that this one is established.
    start_ble_scan();
}

static int gatt_subscribe_cb(uint16_t conn_handle,
                             const struct ble_gatt_error *error,
                             struct ble_gatt_attr *attr,
                             void *arg)
{
    (void)attr;
    (void)arg;

    controller_channel_t *ctx = channel_for_conn(conn_handle);
    if (ctx == NULL) {
        return 0;
    }

    if (ctx->subscribe_index >= 0 && ctx->subscribe_index < (int)ctx->report_char_count) {
        discovered_report_char_t *chr = &ctx->report_chars[ctx->subscribe_index];
        if (error->status == 0) {
            ESP_LOGI(TAG, "BLE subscribe ok value=0x%04x", chr->val_handle);
        } else {
            ESP_LOGW(TAG, "BLE subscribe failed value=0x%04x status=%d",
                     chr->val_handle,
                     error->status);
        }
    }

    subscribe_next_report(ctx);
    return 0;
}

static void discover_next_descriptor(controller_channel_t *ctx);

static int gatt_dsc_cb(uint16_t conn_handle,
                       const struct ble_gatt_error *error,
                       uint16_t chr_val_handle,
                       const struct ble_gatt_dsc *dsc,
                       void *arg)
{
    (void)chr_val_handle;
    (void)arg;

    controller_channel_t *ctx = channel_for_conn(conn_handle);
    if (ctx == NULL || ctx->desc_discovery_index < 0 ||
        ctx->desc_discovery_index >= (int)ctx->report_char_count) {
        return 0;
    }

    discovered_report_char_t *chr = &ctx->report_chars[ctx->desc_discovery_index];
    if (error->status == 0) {
        if (dsc->uuid.u.type == BLE_UUID_TYPE_16 &&
            ble_uuid_u16(&dsc->uuid.u) == BLE_GATT_DSC_CLT_CFG_UUID16) {
            chr->cccd_handle = dsc->handle;
        }
        return 0;
    }

    if (error->status == BLE_HS_EDONE) {
        discover_next_descriptor(ctx);
        return 0;
    }

    ESP_LOGW(TAG, "BLE descriptor discovery failed status=%d", error->status);
    discover_next_descriptor(ctx);
    return 0;
}

static void discover_next_descriptor(controller_channel_t *ctx)
{
    if (ctx == NULL) {
        return;
    }
    for (int i = ctx->desc_discovery_index + 1; i < (int)ctx->report_char_count; i++) {
        discovered_report_char_t *chr = &ctx->report_chars[i];
        if (!chr->notify_target || chr->end_handle <= chr->val_handle) {
            continue;
        }

        ctx->desc_discovery_index = i;
        int rc = ble_gattc_disc_all_dscs(ctx->conn_handle,
                                         chr->val_handle,
                                         chr->end_handle,
                                         gatt_dsc_cb,
                                         NULL);
        if (rc != 0) {
            ESP_LOGW(TAG, "BLE descriptor discovery start failed rc=%d", rc);
            continue;
        }
        return;
    }

    ctx->subscribe_index = -1;
    subscribe_next_report(ctx);
}

static void finalize_report_end_handles(controller_channel_t *ctx)
{
    if (ctx == NULL) {
        return;
    }
    for (size_t i = 0; i < ctx->report_char_count; i++) {
        uint16_t next_def = ctx->report_chars[i].service_end_handle + 1;
        for (size_t j = 0; j < ctx->report_char_count; j++) {
            if (ctx->report_chars[j].def_handle > ctx->report_chars[i].val_handle &&
                ctx->report_chars[j].def_handle <= ctx->report_chars[i].service_end_handle &&
                ctx->report_chars[j].def_handle < next_def) {
                next_def = ctx->report_chars[j].def_handle;
            }
        }
        ctx->report_chars[i].end_handle = next_def > 0 ? next_def - 1 : ctx->report_chars[i].val_handle;
    }
}

static bool is_report_characteristic(const struct ble_gatt_chr *chr)
{
    return (chr->properties & (BLE_GATT_CHR_F_NOTIFY |
                               BLE_GATT_CHR_F_WRITE |
                               BLE_GATT_CHR_F_WRITE_NO_RSP)) != 0;
}

static void discover_next_service_characteristics(controller_channel_t *ctx);

static int gatt_chr_cb(uint16_t conn_handle,
                       const struct ble_gatt_error *error,
                       const struct ble_gatt_chr *chr,
                       void *arg)
{
    (void)arg;
    controller_channel_t *ctx = channel_for_conn(conn_handle);
    if (ctx == NULL) {
        return 0;
    }

    if (error->status == 0) {
        if (is_report_characteristic(chr) &&
            ctx->report_char_count < (sizeof(ctx->report_chars) / sizeof(ctx->report_chars[0]))) {
            discovered_report_char_t *out = &ctx->report_chars[ctx->report_char_count++];
            memset(out, 0, sizeof(*out));
            out->def_handle = chr->def_handle;
            out->val_handle = chr->val_handle;
            out->service_end_handle = ctx->services[ctx->service_discovery_index].end_handle;
            out->properties = chr->properties;
            uuid_to_lower_string(&chr->uuid.u, out->uuid, sizeof(out->uuid));
            out->ack_target = is_ack_uuid(out->uuid);
            out->input_target = is_input_uuid(out->uuid);
            out->notify_target = out->ack_target || is_post_init_notify_uuid(out->uuid);
            out->command_target = is_command_uuid(out->uuid);
            if (out->command_target) {
                ctx->command_value_handle = out->val_handle;
                ctx->command_write_no_rsp = (out->properties & BLE_GATT_CHR_F_WRITE_NO_RSP) != 0;
            }
            out->rumble_target = is_rumble_uuid(out->uuid);
            if (out->rumble_target) {
                ctx->rumble_value_handle = out->val_handle;
                ctx->rumble_write_no_rsp = (out->properties & BLE_GATT_CHR_F_WRITE_NO_RSP) != 0;
            }
            ESP_LOGI(TAG, "BLE char channel=%d value=0x%04x props=0x%02x uuid=%s target=%s",
                     channel_index(ctx),
                     out->val_handle,
                     chr->properties,
                     out->uuid,
                     out->ack_target ? "ack" :
                     (out->input_target ? "input" :
                     (out->command_target ? "cmd" :
                     (out->rumble_target ? "rumble" : "no"))));
        }
        return 0;
    }

    if (error->status == BLE_HS_EDONE) {
        ctx->service_discovery_index++;
        discover_next_service_characteristics(ctx);
        return 0;
    }

    ESP_LOGW(TAG, "BLE characteristic discovery failed status=%d", error->status);
    ctx->service_discovery_index++;
    discover_next_service_characteristics(ctx);
    return 0;
}

static void discover_next_service_characteristics(controller_channel_t *ctx)
{
    if (ctx == NULL) {
        return;
    }
    while (ctx->service_discovery_index < ctx->service_count) {
        discovered_service_t *svc = &ctx->services[ctx->service_discovery_index];
        if (svc->end_handle <= svc->start_handle) {
            ctx->service_discovery_index++;
            continue;
        }

        int rc = ble_gattc_disc_all_chrs(ctx->conn_handle,
                                         svc->start_handle,
                                         svc->end_handle,
                                         gatt_chr_cb,
                                         NULL);
        if (rc != 0) {
            ESP_LOGW(TAG, "BLE characteristic discovery start failed rc=%d", rc);
            ctx->service_discovery_index++;
            continue;
        }
        return;
    }

    finalize_report_end_handles(ctx);
    ctx->desc_discovery_index = -1;
    discover_next_descriptor(ctx);
}

static int gatt_svc_cb(uint16_t conn_handle,
                       const struct ble_gatt_error *error,
                       const struct ble_gatt_svc *service,
                       void *arg)
{
    (void)arg;
    controller_channel_t *ctx = channel_for_conn(conn_handle);
    if (ctx == NULL) {
        return 0;
    }

    if (error->status == 0) {
        if (ctx->service_count < (sizeof(ctx->services) / sizeof(ctx->services[0]))) {
            ctx->services[ctx->service_count].start_handle = service->start_handle;
            ctx->services[ctx->service_count].end_handle = service->end_handle;
            ctx->service_count++;
            ESP_LOGI(TAG, "BLE service channel=%d start=0x%04x end=0x%04x",
                     channel_index(ctx),
                     service->start_handle,
                     service->end_handle);
        }
        return 0;
    }

    if (error->status == BLE_HS_EDONE) {
        if (ctx->service_count == 0) {
            ESP_LOGW(TAG, "BLE services not found");
            return 0;
        }
        ctx->service_discovery_index = 0;
        discover_next_service_characteristics(ctx);
        return 0;
    }

    ESP_LOGW(TAG, "BLE service discovery failed status=%d", error->status);
    return 0;
}

static int gatt_mtu_cb(uint16_t conn_handle,
                       const struct ble_gatt_error *error,
                       uint16_t mtu,
                       void *arg)
{
    (void)arg;
    controller_channel_t *ctx = channel_for_conn(conn_handle);
    if (ctx == NULL) {
        return 0;
    }
    if (error->status == 0) {
        ESP_LOGI(TAG, "BLE MTU exchange ok mtu=%u", mtu);
    } else {
        ESP_LOGW(TAG, "BLE MTU exchange failed status=%d", error->status);
    }

    int rc = ble_gattc_disc_all_svcs(conn_handle, gatt_svc_cb, NULL);
    if (rc != 0) {
        ESP_LOGW(TAG, "BLE service discovery start failed rc=%d", rc);
    }
    return 0;
}

static void start_gatt_discovery(uint16_t conn_handle)
{
    controller_channel_t *ctx = channel_for_conn(conn_handle);
    if (ctx == NULL) return;
    reset_gatt_state(ctx);
    ctx->init_started = true;
    int rc = ble_gattc_exchange_mtu(conn_handle, gatt_mtu_cb, NULL);
    if (rc != 0) {
        ESP_LOGW(TAG, "BLE MTU exchange start failed rc=%d", rc);
        (void)gatt_mtu_cb(conn_handle,
                          &(struct ble_gatt_error){.status = BLE_HS_EDONE},
                          0,
                          NULL);
    }
}

static void handle_notify_rx(const struct ble_gap_event *event)
{
    if (event->notify_rx.om == NULL || q_ctrl == NULL) {
        return;
    }

    controller_channel_t *ctx = channel_for_conn(event->notify_rx.conn_handle);
    int ch_index = channel_index(ctx);
    if (ctx == NULL || ch_index < 0) {
        ESP_LOGD(TAG, "BLE notify from unknown conn=%u len=%u",
                 event->notify_rx.conn_handle,
                 (unsigned)OS_MBUF_PKTLEN(event->notify_rx.om));
        return;
    }

    uint16_t len = OS_MBUF_PKTLEN(event->notify_rx.om);
    uint16_t copy_len = len > NINTENDO_REPORT_SIZE ? NINTENDO_REPORT_SIZE : len;
    controller_report_t report = {
        .channel = (uint8_t)ch_index,
        .length = (uint8_t)copy_len,
    };

    if (os_mbuf_copydata(event->notify_rx.om, 0, copy_len, report.payload) != 0) {
        ESP_LOGW(TAG, "BLE notify copy failed len=%u", (unsigned)len);
        return;
    }

    discovered_report_char_t *chr = find_char_by_value_handle(ctx, event->notify_rx.attr_handle);
    if (chr == NULL) {
        ESP_LOGD(TAG, "BLE notify from unknown attr=0x%04x len=%u",
                 event->notify_rx.attr_handle,
                 (unsigned)copy_len);
        return;
    }

    if (!chr->input_target && !chr->ack_target && !chr->command_target) {
        ESP_LOGD(TAG, "BLE side-channel notify ignored uuid=%s len=%u",
                 chr->uuid,
                 (unsigned)copy_len);
        return;
    }

    // Tag command/ack responses so the host can keep them out of the input parser.
    // Only the input characteristic carries real controller input; ack/command
    // notifications otherwise get misparsed as random buttons/sticks on connect.
    report.is_command = chr->input_target ? 0 : 1;

    if (report.is_command) {
        // P0: ACK/command response — never starve, but never block BLE host task
        if (xQueueSend(q_ctrl, &report, 0) != pdTRUE) {
            s_ctrl_drop_count++;  // q_ctrl full = CDC disconnected; dropping here is correct
        } else {
            xSemaphoreGive(s_stream_wake);
        }
    } else {
        // P2: input report — always keep latest, discard stale
        if (report.channel < MAX_BLE_CHANNELS) {
            s_input_fwd[report.channel]++;
        }
        portENTER_CRITICAL(&s_input_shadow_mux);
        s_latest_input[ch_index] = report;
        s_latest_input_dirty[ch_index] = true;
        portEXIT_CRITICAL(&s_input_shadow_mux);
        xSemaphoreGive(s_stream_wake);

        // Nudge the rumble relay event on every input notification.  We MUST NOT call
        // ble_gattc here: this GAP callback runs with ble_hs_lock held, so a direct
        // write deadlocks the host task (hangs everything).  ble_npl_eventq_put is
        // lock-free and safe; because input arrives ~130/s/ch it re-queues the event
        // far more often than the 13 ms esp_timer, so the host services it (and thus
        // writes rumble) much more frequently — lifting the rate past the ~33/s the
        // timer alone managed.  RUMBLE_MIN_GAP_US still caps the per-channel rate.
        ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &s_rumble_ev);
    }
}

static int ble_gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;

    switch (event->type) {
    case BLE_GAP_EVENT_DISC:
        {
        bool is_directed = (event->disc.event_type == BLE_HCI_ADV_RPT_EVTYPE_DIR_IND);
        if (!is_target_controller(&event->disc) && !is_directed) {
            return 0;
        }
        if (scan_mode) {
            char mac_str[18];
            snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
                     event->disc.addr.val[5], event->disc.addr.val[4], event->disc.addr.val[3],
                     event->disc.addr.val[2], event->disc.addr.val[1], event->disc.addr.val[0]);

            char data_hex[65];
            data_hex[0] = '\0';
            if (!is_directed) {
                int data_len = event->disc.length_data;
                if (data_len > 31) data_len = 31;
                for (int i = 0; i < data_len; i++) {
                    sprintf(&data_hex[i*2], "%02X", event->disc.data[i]);
                }
                data_hex[data_len*2] = '\0';
            }

            char response[256];
            int len = snprintf(response, sizeof(response),
                               "{\"cmd\":\"scan_result\",\"mac\":\"%s\",\"type\":%d,\"rssi\":%d,\"data\":\"%s\",\"directed\":%d}\n",
                               mac_str, event->disc.addr.type, event->disc.rssi, data_hex, is_directed ? 1 : 0);
            if (len > 0) {
                safe_cdc_write((const uint8_t *)response, len);
            }
        }
        // Defer auto-connect to a FreeRTOS task so we can safely cancel the scan
        // outside the disc callback context (calling ble_gap_disc_cancel() from
        // within BLE_GAP_EVENT_DISC is not safe in NimBLE).
        if (s_auto_connect_enabled && connecting_channel < 0 && has_free_channel() && s_deferred_ac_task == NULL) {
            s_deferred_ac_addr = event->disc.addr;
            xTaskCreate(deferred_auto_connect_task, "ble_defer_ac", 4096, NULL, 5, &s_deferred_ac_task);
        }
        return 0;
        }

    case BLE_GAP_EVENT_DISC_COMPLETE:
        ESP_LOGI(TAG, "BLE scan complete reason=%d", event->disc_complete.reason);
        if (has_free_channel()) {
            start_ble_scan();
        }
        return 0;
	
	case BLE_GAP_EVENT_PHY_UPDATE_COMPLETE:
        {
            char pdbg[96];
            int pl = snprintf(pdbg, sizeof(pdbg),
                "{\"cmd\":\"debug\",\"msg\":\"PHY Update TX=%d RX=%d status=%d\"}\n",
                event->phy_updated.tx_phy, 
                event->phy_updated.rx_phy,
                event->phy_updated.status);
            if (pl > 0) {
                safe_cdc_write((const uint8_t *)pdbg, pl);
            }
        }
        return 0;

    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            int channel = connecting_channel >= 0 ? connecting_channel : alloc_channel();
            if (channel < 0) {
                ESP_LOGW(TAG, "BLE connected handle=%u but no channel slot is free", event->connect.conn_handle);
                ble_gap_terminate(event->connect.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
                return 0;
            }
            connecting_channel = -1;
            channels[channel].used = true;
            channels[channel].conn_handle = event->connect.conn_handle;
            channels[channel].ready = false;
            struct ble_gap_conn_desc desc;
            if (ble_gap_conn_find(event->connect.conn_handle, &desc) == 0) {
                channels[channel].peer_addr = desc.peer_ota_addr;
            }
            refresh_active_ble_channels();
            ESP_LOGI(TAG, "BLE connected channel=%d handle=%u", channel, event->connect.conn_handle);
            {
                // Diagnostic: surface that the link established and with which peer
                // address type, so a reconnect that drops mid-GATT can be told apart
                // from one that never establishes.
                char cdbg[96];
                int cl = snprintf(cdbg, sizeof(cdbg),
                    "{\"cmd\":\"debug\",\"msg\":\"conn ok ch=%d type=%d\"}\n",
                    channel, (int)channels[channel].peer_addr.type);
                if (cl > 0) safe_cdc_write((const uint8_t *)cdbg, cl);
            }
			
			uint8_t tx_phys_mask = BLE_GAP_LE_PHY_2M_MASK;
            uint8_t rx_phys_mask = BLE_GAP_LE_PHY_2M_MASK;
            int phy_rc = ble_gap_set_prefered_le_phy(event->connect.conn_handle, 
                                                     tx_phys_mask, 
                                                     rx_phys_mask, 
                                                     0);
			
			char req_dbg[96];
            int req_l = snprintf(req_dbg, sizeof(req_dbg),
                "{\"cmd\":\"debug\",\"msg\":\"PHY Req called, rc=%d\"}\n", phy_rc);
            if (req_l > 0) {
                safe_cdc_write((const uint8_t *)req_dbg, req_l);
            }
			
            if (phy_rc != 0) {
                ESP_LOGW(TAG, "Failed to request 2M PHY, rc=%d", phy_rc);
            }
            // y700 5.9.2 approach: Switch 2 controllers communicate over a PLAIN,
            // unencrypted/unbonded link. Do NOT initiate security — calling
            // ble_gap_security_initiate() triggers an SMP pairing exchange the
            // controller does not expect and it drops the link with reason 0x3E
            // ("Connection Failed to be Established", reported here as disc 574).
            // Just run GATT discovery directly, exactly like the reference firmware.
            start_gatt_discovery(event->connect.conn_handle);
        } else {
            ESP_LOGW(TAG, "BLE connect failed status=%d", event->connect.status);
            if (connecting_channel >= 0) {
                release_channel(&channels[connecting_channel]);
            }
            connecting_channel = -1;
            refresh_active_ble_channels();
            char fail_buf[96];
            int fail_len = snprintf(fail_buf, sizeof(fail_buf),
                "{\"cmd\":\"connect_fail\",\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\"}\n",
                connecting_addr.val[5], connecting_addr.val[4], connecting_addr.val[3],
                connecting_addr.val[2], connecting_addr.val[1], connecting_addr.val[0]);
            if (fail_len > 0) safe_cdc_write((const uint8_t *)fail_buf, fail_len);
            start_ble_scan();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        {
            controller_channel_t *ctx = channel_for_conn(event->disconnect.conn.conn_handle);
            char dbg[96];
            int l = snprintf(dbg, sizeof(dbg),
                "{\"cmd\":\"debug\",\"msg\":\"disc %d ready=%d\"}\n",
                event->disconnect.reason, (ctx != NULL && ctx->ready) ? 1 : 0);
            safe_cdc_write((const uint8_t *)dbg, l);
            ESP_LOGW(TAG, "BLE disconnected reason=%d ready=%d",
                     event->disconnect.reason, (ctx != NULL && ctx->ready) ? 1 : 0);
            // If the link dropped before GATT setup completed (we never emitted
            // "connected"), report it as a failed connect so the host clears its
            // "connecting" state and retries on the next advertisement. Without this
            // the host stays stuck on the MAC for ~12s and filters out the
            // controller's repeated directed reconnect ads (reason 574 / 0x3E is a
            // common transient establishment failure that succeeds on retry).
            if (ctx != NULL && !ctx->ready) {
                char fail_buf[96];
                int fail_len = snprintf(fail_buf, sizeof(fail_buf),
                    "{\"cmd\":\"connect_fail\",\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\"}\n",
                    ctx->peer_addr.val[5], ctx->peer_addr.val[4], ctx->peer_addr.val[3],
                    ctx->peer_addr.val[2], ctx->peer_addr.val[1], ctx->peer_addr.val[0]);
                if (fail_len > 0) safe_cdc_write((const uint8_t *)fail_buf, fail_len);
            } else if (ctx == NULL && connecting_channel >= 0) {
                // A pending connect failed and was delivered as a disconnect for a
                // handle we never registered (no CONNECT status=0). Free the stuck
                // connecting channel and reset connecting_channel — otherwise
                // start_ble_scan() bails forever (connecting_channel >= 0) and the
                // bridge can't find any controller until it is replugged. Report the
                // failure so the host retries.
                char fail_buf[96];
                int fail_len = snprintf(fail_buf, sizeof(fail_buf),
                    "{\"cmd\":\"connect_fail\",\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\"}\n",
                    connecting_addr.val[5], connecting_addr.val[4], connecting_addr.val[3],
                    connecting_addr.val[2], connecting_addr.val[1], connecting_addr.val[0]);
                if (fail_len > 0) safe_cdc_write((const uint8_t *)fail_buf, fail_len);
                release_channel(&channels[connecting_channel]);
                connecting_channel = -1;
            }
            release_channel(ctx);
        }
        start_ble_scan();
        return 0;

    case BLE_GAP_EVENT_ENC_CHANGE:
        ESP_LOGI(TAG, "BLE encryption change handle=%u status=%d",
                 event->enc_change.conn_handle, event->enc_change.status);
        return 0;

    case BLE_GAP_EVENT_NOTIFY_RX:
        handle_notify_rx(event);
        return 0;

    case BLE_GAP_EVENT_REPEAT_PAIRING:
        // Controller has an existing bond but NimBLE initiated fresh pairing.
        // Keep the existing bond and ignore the repeat pairing request.
        ESP_LOGI(TAG, "BLE repeat pairing handle=%u; keeping existing bond",
                 event->repeat_pairing.conn_handle);
        return BLE_GAP_REPEAT_PAIRING_IGNORE;

    case BLE_GAP_EVENT_CONN_UPDATE_REQ:
        // Force our fast 7.5ms parameters instead of accepting the controller's
        // requested (usually slower) interval. If we accept a slower interval the
        // link no longer runs at 7.5ms, so input/vibration arrive less often — that
        // is the periodic merge-mode rumble gap, and it is independent of the rumble
        // payload (which is why it persists with the 3 frames intact). Overriding
        // self_params here keeps every connection pinned to 7.5ms. (Matches y700.)
        if (event->conn_update_req.self_params != NULL) {
            // Force our fast 7.5ms parameters instead of the controller's slower request.
            event->conn_update_req.self_params->itvl_min = 6;   // 7.5ms
            event->conn_update_req.self_params->itvl_max = 6;   // 7.5ms
            event->conn_update_req.self_params->latency = 0;
            event->conn_update_req.self_params->supervision_timeout = 400;
            event->conn_update_req.self_params->min_ce_len = 0;
            event->conn_update_req.self_params->max_ce_len = 0x0003;
        }
        return 0;

    case BLE_GAP_EVENT_CONN_UPDATE:
        // Report the ACTUAL negotiated connection interval so latency can be verified:
        // conn_itvl is in 1.25ms units (6 = 7.5ms, 12 = 15ms). If this shows 12 the
        // controller/radio forced 15ms despite our 7.5ms request.
        {
            struct ble_gap_conn_desc ud;
            if (ble_gap_conn_find(event->conn_update.conn_handle, &ud) == 0) {
                char idbg[80];
                int il = snprintf(idbg, sizeof(idbg),
                    "{\"cmd\":\"debug\",\"msg\":\"itvl %u status=%d\"}\n",
                    (unsigned)ud.conn_itvl, event->conn_update.status);
                if (il > 0) safe_cdc_write((const uint8_t *)idbg, il);
            }
        }
        return 0;

    default:
        return 0;
    }
}

static void ble_on_reset(int reason)
{
    ESP_LOGW(TAG, "BLE host reset reason=%d", reason);
}

static void ble_on_sync(void)
{
    int rc = ble_hs_id_infer_auto(0, &ble_own_addr_type);
    if (rc != 0) {
        ESP_LOGE(TAG, "BLE address infer failed rc=%d", rc);
        return;
    }
    start_ble_scan();
}

static void ble_host_task(void *param)
{
    (void)param;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void init_ble(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.reset_cb = ble_on_reset;
    ble_hs_cfg.sync_cb = ble_on_sync;
    // y700 5.9.2 approach: Switch 2 controllers use a PLAIN, unencrypted/unbonded
    // link. Do NOT configure the security manager or a bonding store — leaving
    // bonding enabled lets a stale peripheral-side bond trigger an SMP exchange
    // that drops the link with reason 0x3E (disc 574). The reference firmware
    // configures no SM at all and connects reliably to paired controllers.

    ble_svc_gap_init();
    ble_svc_gatt_init();
    ble_svc_gap_device_name_set("esp32s3-usb-direct");
    nimble_port_freertos_init(ble_host_task);
}

void tinyusb_cdc_rx_callback(int itf, cdcacm_event_t *event)
{
    if (event->type != CDC_EVENT_RX) {
        return;
    }

    uint8_t tmp[128];
    size_t rx_size = 0;
    esp_err_t ret = tinyusb_cdcacm_read(itf, tmp, sizeof(tmp), &rx_size);
    if (ret != ESP_OK || rx_size == 0) {
        return;
    }

    for (size_t i = 0; i < rx_size; i++) {
        uint8_t c = tmp[i];
        if (c == '\r') {
            continue;
        }
        if (c == '\n') {
            rx_buf[rx_len] = '\0';
            ESP_LOGI(TAG, "Received command: %s", rx_buf);

            if (strncmp(rx_buf, "rs ", 3) == 0) {
                handle_rumble_shadow_command(rx_buf);
            } else if (strncmp(rx_buf, "wrpair ", 7) == 0) {
                handle_wrpair_command(rx_buf);
            } else if (strncmp(rx_buf, "wr ", 3) == 0) {
                handle_write_command(rx_buf);
            } else if (strncmp(rx_buf, "conn ", 5) == 0) {
                handle_conn_command(rx_buf);
            } else if (strncmp(rx_buf, "disc ", 5) == 0) {
                handle_disc_command(rx_buf);
            } else if (strcmp(rx_buf, "scan on") == 0) {
                scan_mode = true;
                start_ble_scan();
            } else if (strcmp(rx_buf, "scan off") == 0) {
                scan_mode = false;
                ble_gap_disc_cancel();
            } else if (strcmp(rx_buf, "auto off") == 0) {
                s_auto_connect_enabled = false;
            } else if (strcmp(rx_buf, "auto on") == 0) {
                s_auto_connect_enabled = true;
            } else if (strcmp(rx_buf, "cancel") == 0) {
                // Abort a pending/stuck connect attempt WITHOUT touching channels
                // that are already connected, then resume scanning. The host calls
                // this when its connect watchdog fires, so a connect that never
                // completes can no longer wedge the scanner (which bails while
                // connecting_channel >= 0) and require a replug to recover.
                if (ble_gap_conn_active()) {
                    ble_gap_conn_cancel();
                }
                if (connecting_channel >= 0) {
                    release_channel(&channels[connecting_channel]);
                    connecting_channel = -1;
                }
                start_ble_scan();
            } else if (strstr(rx_buf, "status lite") != NULL) {
                request_status = true;
                wake_cdc_stream_task();
            } else if (strcmp(rx_buf, "ble disconnect") == 0) {
                // Cancel any in-progress connection attempt and free its channel.
                // A pending connect leaves connecting_channel >= 0, which makes
                // start_ble_scan() bail out — so without this a failed/aborted
                // connect would wedge scanning until the board is replugged. The
                // host sends "ble disconnect" at startup and shutdown to force a
                // clean, scannable state.
                if (ble_gap_conn_active()) {
                    ble_gap_conn_cancel();
                }
                if (connecting_channel >= 0) {
                    release_channel(&channels[connecting_channel]);
                    connecting_channel = -1;
                }
                for (int i = 0; i < MAX_BLE_CHANNELS; i++) {
                    if (channels[i].used && channels[i].conn_handle != BLE_HS_CONN_HANDLE_NONE) {
                        ble_gap_terminate(channels[i].conn_handle, BLE_ERR_REM_USER_CONN_TERM);
                    }
                }
            }

            rx_len = 0;
            continue;
        }

        if (c < 0x20 || c > 0x7e) {
            rx_len = 0;
            continue;
        }

        if (rx_len < (int)sizeof(rx_buf) - 1) {
            rx_buf[rx_len++] = (char)c;
        } else {
            rx_len = 0;
        }
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "Starting ESP32-S3 USB CDC BLE Bridge");

    q_ctrl = xQueueCreate(Q_CTRL_DEPTH, sizeof(controller_report_t));
    ESP_ERROR_CHECK(q_ctrl == NULL ? ESP_ERR_NO_MEM : ESP_OK);
    s_stream_wake = xSemaphoreCreateBinary();
    ESP_ERROR_CHECK(s_stream_wake == NULL ? ESP_ERR_NO_MEM : ESP_OK);

    tinyusb_config_t tusb_cfg = {
        .device_descriptor = NULL,
        .string_descriptor = NULL,
        .external_phy = false,
        .configuration_descriptor = NULL,
    };
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));

    tinyusb_config_cdcacm_t amc_cfg = {
        .usb_dev = TINYUSB_USBDEV_0,
        .cdc_port = TINYUSB_CDC_ACM_0,
        .rx_unread_buf_sz = 1024,
        .callback_rx = &tinyusb_cdc_rx_callback,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL,
    };
    ESP_ERROR_CHECK(tusb_cdc_acm_init(&amc_cfg));
    ESP_LOGI(TAG, "USB CDC initialized");

    init_ble();
    xTaskCreatePinnedToCore(cdc_stream_task, "cdc_stream_task", 4096, NULL, 10, NULL, 1);
    // Rumble: a steady esp_timer posts an event that runs the BLE writes in the
    // NimBLE host-task context (see rumble_relay_ev / rumble_tick_cb).  This keeps
    // input at full rate (a dedicated task contended and starved one or the other).
    ble_npl_event_init(&s_rumble_ev, rumble_relay_ev, NULL);
    const esp_timer_create_args_t rumble_timer_args = {
        .callback = &rumble_tick_cb,
        .name = "rumble_tick",
    };
    ESP_ERROR_CHECK(esp_timer_create(&rumble_timer_args, &s_rumble_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(s_rumble_timer, RUMBLE_TICK_US));

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
