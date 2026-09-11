from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.email_intake.gmail_oauth import GMAIL_MODIFY_SCOPE, GmailOAuthBootstrap, GmailOAuthError


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize one controlled Gmail mailbox for FlowOps Pilot v2.")
    parser.add_argument("--client-json", required=True, help="Path to the Desktop OAuth client JSON outside the repo.")
    parser.add_argument("--expected-email", required=True, help="Expected controlled Gmail mailbox.")
    args = parser.parse_args()

    client_path = Path(args.client_json)
    try:
        result = GmailOAuthBootstrap(
            client_json_path=client_path,
            expected_email=args.expected_email,
            scopes=(GMAIL_MODIFY_SCOPE,),
        ).authorize_and_verify()
    except GmailOAuthError as exc:
        print(f"ERROR: {exc}")
        return 1

    print("GMAIL_OAUTH_AUTHORIZATION=PASS")
    print(f"AUTHORIZED_MAILBOX={result.authorized_email}")
    print("OAUTH_SCOPE=https://www.googleapis.com/auth/gmail.modify")
    print("TOKEN_STORAGE=Windows Credential Manager")
    print("INBOX_LISTED=NO")
    print("MESSAGES_FETCHED=0")
    print("ATTACHMENTS_DOWNLOADED=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
