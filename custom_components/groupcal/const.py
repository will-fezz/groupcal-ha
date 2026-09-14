"""Constants for the GroupCal integration."""

from datetime import timedelta

DOMAIN = "groupcal"

CONF_PHONE_NUMBER = "phone_number"
CONF_DEVICE_ID = "device_id"
CONF_CODE_PROVIDER = "code_provider"
CONF_CM_TOKEN = "cm_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_USER_ID = "user_id"
CONF_GROUPS = "groups"

DEFAULT_CODE_PROVIDER = "cm"

SCAN_INTERVAL = timedelta(minutes=5)
LOOKAHEAD = timedelta(days=30)
