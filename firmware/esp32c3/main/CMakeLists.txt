idf_component_register(
    SRCS "main.c" "../../common/fw_bridge.c" "../../../core/sbcore.c"
    INCLUDE_DIRS "." "../../common" "../../../core"
    PRIV_REQUIRES esp_wifi esp_netif esp_event esp_http_client esp_timer nvs_flash driver lwip)
# 256 targets (about 55 KB) instead of the 1024 (about 213 KB) other platforms use
target_compile_definitions(${COMPONENT_LIB} PRIVATE SB_MAX_TARGETS=256)
