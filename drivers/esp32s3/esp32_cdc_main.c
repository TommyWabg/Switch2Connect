#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_system.h"
#include "tinyusb.h"
#include "tusb_cdc_acm.h"
#include "sdkconfig.h"

static const char *TAG = "ESP32S3_CDC";

#define APP_FIRMWARE_VERSION "1.0.0"
#define EXPECTED_FIRMWARE_PROFILE "tinyusb_direct"
#define EXPECTED_FIRMWARE_BUILD "cdc_bridge_1"
#define NINTENDO_REPORT_SIZE 64

// Buffer for receiving text commands from host
static char rx_buf[256];
static int rx_len = 0;

// Simple state
static bool stream_mode = false;

// Task for streaming at 800Hz (1.25ms)
void cdc_stream_task(void *arg) {
    uint8_t packet[3 + NINTENDO_REPORT_SIZE];
    packet[0] = 0xAA;
    packet[1] = 0x55;
    packet[2] = NINTENDO_REPORT_SIZE; // length byte (could be report_id + payload)
    
    // packet[3] = Report ID (e.g., channel + 1)
    // packet[4...] = payload
    
    memset(&packet[3], 0, NINTENDO_REPORT_SIZE);
    packet[3] = 1; // Channel 1 (Report ID = 1)
    
    TickType_t xLastWakeTime = xTaskGetTickCount();
    const TickType_t xFrequency = pdMS_TO_TICKS(1); // 1ms timer for roughly 800Hz-1000Hz
    
    while (1) {
        vTaskDelayUntil(&xLastWakeTime, xFrequency);
        
        if (stream_mode && tud_cdc_connected()) {
            // Check if host asserted DTR (Data Terminal Ready)
            // TinyUSB CDC requires DTR to be high to send data properly in most cases.
            if (tud_cdc_n_get_line_state(0) & 0x01) {
                // Generate dummy data or read from BLE queue
                packet[4]++; // dummy counter
                
                // Write to CDC
                tinyusb_cdcacm_write_queue(0, packet, sizeof(packet));
                tinyusb_cdcacm_write_flush(0);
            }
        }
    }
}

// Callback for receiving CDC data
void tinyusb_cdc_rx_callback(int itf, cdcacm_event_t *event) {
    if (event->type == CDC_ACM_DATA_RX) {
        size_t rx_size = 0;
        esp_err_t ret = tinyusb_cdcacm_read(itf, (uint8_t*)&rx_buf[rx_len], sizeof(rx_buf) - rx_len - 1, &rx_size);
        if (ret == ESP_OK && rx_size > 0) {
            rx_len += rx_size;
            rx_buf[rx_len] = '\0';
            
            // Check for newline
            char *newline = strchr(rx_buf, '\n');
            if (newline) {
                *newline = '\0';
                ESP_LOGI(TAG, "Received command: %s", rx_buf);
                
                if (strncmp(rx_buf, "status lite", 11) == 0) {
                    char response[256];
                    snprintf(response, sizeof(response), 
                        "{\"ok\":true, \"cmd\":\"status lite\", \"version\":\"%s\", \"mode\":\"app\", \"profile\":\"%s\", \"build\":\"%s\", \"ble_channels\": 1, \"usb\": true}\n",
                        APP_FIRMWARE_VERSION, EXPECTED_FIRMWARE_PROFILE, EXPECTED_FIRMWARE_BUILD);
                    tinyusb_cdcacm_write_queue(itf, (uint8_t*)response, strlen(response));
                    tinyusb_cdcacm_write_flush(itf);
                }
                else if (strncmp(rx_buf, "ble auto on", 11) == 0) {
                    stream_mode = true;
                }
                else if (strncmp(rx_buf, "ble auto off", 12) == 0) {
                    stream_mode = false;
                }
                else if (strncmp(rx_buf, "ble disconnect", 14) == 0) {
                    stream_mode = false;
                }
                
                // Shift buffer
                int remaining = rx_len - (newline - rx_buf + 1);
                if (remaining > 0) {
                    memmove(rx_buf, newline + 1, remaining);
                }
                rx_len = remaining;
            }
        }
    }
}

void app_main(void) {
    ESP_LOGI(TAG, "Starting ESP32-S3 USB CDC Bridge");

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
        .callback_rx = &tinyusb_cdc_rx_callback, // the callback
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL
    };

    ESP_ERROR_CHECK(tusb_cdc_acm_init(&amc_cfg));
    ESP_LOGI(TAG, "USB CDC initialized");

    // Create 800Hz high-frequency stream task
    xTaskCreatePinnedToCore(cdc_stream_task, "cdc_stream_task", 4096, NULL, 10, NULL, 1);

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
