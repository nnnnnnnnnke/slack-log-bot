"""Deprecated: use setup.py instead.

Google authentication is now one step of the full setup wizard, which also
checks the Slack tokens and creates the spreadsheet and Drive folder.
Kept so existing instructions and scripts keep working.
"""

import sys

from setup import run_google_auth

if __name__ == "__main__":
    print("setup_drive_auth.py は setup.py に統合されました。", file=sys.stderr)
    print("次回からは `python setup.py` を実行してください。\n", file=sys.stderr)
    run_google_auth(reauth="--reauth" in sys.argv)
