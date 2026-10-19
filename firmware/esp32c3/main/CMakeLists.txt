idf_component_register(
    SRCS "main.c" "../../common/fw_bridge.c" "../../../core/sbcore.c"
    INCLUDE_DIRS "." "../../common" "../../../core"
    PRIV_REQUIRES esp_wifi esp_netif esp_event esp_http_client esp_timer nvs_flash driver lwip)
