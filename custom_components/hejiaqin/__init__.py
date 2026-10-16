"""The HeJiaQin integration."""

import asyncio
import logging
import time
import requests
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import homeassistant.helpers.entity_registry as er
import voluptuous as vol
from aiohttp import web
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_PLATFORM,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_IP_ADDRESS,
    EVENT_HOMEASSISTANT_STOP,
    SERVICE_RELOAD,
    MAJOR_VERSION,
    MINOR_VERSION,    
)
from homeassistant.core import HomeAssistant
from homeassistant.components.http import HomeAssistantView
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_track_time_interval

from .hejiaqin import DNSUpdateManger, get_hejiaqin_device, device_filter, async_get_devices_list
from .hejiaqin_api import async_login_and_get_api_key
from .config_flow import ENTRIES_VERSION
from .const import (
    CONFIG,
    CONF_USER_INPUT,
    CONF_TOKEN,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_EXPIRE,
    CONF_CONFIGURATION_UPDATE,
    CONF_DNS_UPDATE,
    CONF_RELOAD_FLAG,
    CONF_DEVICE_ADDRESS,
    CONF_API_KEY,
    CONF_PHONE,
    CONF_PASSWORD,
    CONF_SMARTPLUG,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SN,
    CONF_ENTRY_ID,
    SL_DEVICES,
    CLOUD_DATA,
    DOMAIN,
    PLUG_DOMAIN,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "sensor"] # 统一管理的组件列表

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the HeJiaQin integration component."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][CONFIG] = {}
    hass.data[DOMAIN][CONF_RELOAD_FLAG] = []
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up HeJiaQin integration from a config entry."""
    _LOGGER.debug(entry.entry_id)
    _LOGGER.debug(entry.data)
    _LOGGER.debug(hass.data[DOMAIN][CONF_RELOAD_FLAG])
    ha_version = MAJOR_VERSION * 100 + MINOR_VERSION
    
    if entry.entry_id in hass.data[DOMAIN][CONF_RELOAD_FLAG]:
        await async_hejiaqin_reload_entry(hass, entry)

    config = {
        SL_DEVICES: list(),
    }
    hass.data[DOMAIN][CONFIG][entry.entry_id] = config
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_USER_INPUT, {}).get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    hass.data[DOMAIN][CONF_SCAN_INTERVAL] = scan_interval

    async def setup_entities(device_ids):
        for dev_id in device_ids:
            device_config = {**entry.data[CONF_DEVICES][dev_id], CONF_ENTRY_ID: entry.entry_id}
            device = await get_hejiaqin_device(hass, device_config)
            if device is None: continue
            await device.async_set_scan_interval(scan_interval)
            config[SL_DEVICES].append(device)

        if ha_version >= 202408:
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        else:
            await asyncio.gather(
                *[
                    hass.config_entries.async_forward_entry_setup(entry, platform)
                    for platform in PLATFORMS
                ]
            )

        for device in config[SL_DEVICES]:
            await device.async_setup()
    
    hass.async_create_task(setup_entities(entry.data[CONF_DEVICES].keys()))
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("async_unload_entry")
    _LOGGER.debug(entry.entry_id)
    
    config = hass.data[DOMAIN][CONFIG].pop(entry.entry_id, None)
    if config:
        for device in config.get(SL_DEVICES, []):
            await device.update_manager.coordinator.async_shutdown()
            
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if entry.entry_id not in hass.data[DOMAIN][CONF_RELOAD_FLAG]:
        hass.data[DOMAIN][CONF_RELOAD_FLAG].append(entry.entry_id)
    return unload_ok

async def async_hejiaqin_reload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Reload entry and refresh token if needed."""
    api_key = entry.data[CONF_USER_INPUT].get(CONF_API_KEY)
    phone = entry.data[CONF_USER_INPUT].get(CONF_PHONE)
    password = entry.data[CONF_USER_INPUT].get(CONF_PASSWORD)
    
    error, resp = await async_get_devices_list(hass, api_key)
    
    if error is not None and phone and password:
        _LOGGER.info(f"API key expired, attempting re-login for {phone}")
        login_error, new_api_key = await async_login_and_get_api_key(phone, password)
        
        if login_error is None and new_api_key:
            api_key = new_api_key
            new_data = {**entry.data}
            new_data[CONF_USER_INPUT] = {**entry.data[CONF_USER_INPUT]}
            new_data[CONF_USER_INPUT][CONF_API_KEY] = api_key
            
            new_devices = {}
            for dev_id, dev_config in new_data.get(CONF_DEVICES, {}).items():
                dev_config = {**dev_config}
                dev_config[CONF_API_KEY] = api_key
                new_devices[dev_id] = dev_config
            new_data[CONF_DEVICES] = new_devices
            
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info(f"Successfully refreshed API key for {phone}")
            error, resp = await async_get_devices_list(hass, api_key)
        else:
            _LOGGER.error(f"Re-login failed for {phone}: {login_error}")
    
    if error is not None:
        _LOGGER.warning(f"Failed to get devices list: {error}")
        if entry.entry_id in hass.data[DOMAIN][CONF_RELOAD_FLAG]:
            hass.data[DOMAIN][CONF_RELOAD_FLAG].remove(entry.entry_id)
        return
    
    update_flag = False
    r_json = resp.json()
    devices_list = r_json.get(CONF_DEVICES, list())
    devices = device_filter(devices_list, api_key)
    if len(devices) != len(entry.data.get(CONF_DEVICES, {})):
        update_flag = True

    if update_flag:
        devices.update(entry.data.get(CONF_DEVICES, {}))
        new_data = {**entry.data}
        new_data[CONF_DEVICES] = devices
        hass.config_entries.async_update_entry(entry, data=new_data)

    if entry.entry_id in hass.data[DOMAIN][CONF_RELOAD_FLAG]:
        hass.data[DOMAIN][CONF_RELOAD_FLAG].remove(entry.entry_id)
