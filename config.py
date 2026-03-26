import os
from dotenv import load_dotenv

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
# APP_TOKEN is only required for Socket Mode (main.py), not for weekly collection
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")

GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_SPREADSHEET_ID = os.environ["GOOGLE_SPREADSHEET_ID"]
GOOGLE_DRIVE_FOLDER_ID = os.environ["GOOGLE_DRIVE_FOLDER_ID"]

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tokyo")

# File download size limit (50MB)
MAX_FILE_SIZE = 50 * 1024 * 1024
