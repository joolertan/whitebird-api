"""Download the statement of exchange operations of the last days as a PDF file."""

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path

from whitebird import FileTokenStore, Whitebird
from whitebird.models import ReportDateFrame
from whitebird.otp import TOTP

SESSION = Path.home() / ".config" / "whitebird" / "session.json"
# The format the web app sends, e.g. 2026-11-07T00:00:00+0300.
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("exchange-statement.pdf"))
    args = parser.parse_args()

    end = datetime.now().astimezone()
    period = ReportDateFrame(
        start=(end - timedelta(days=args.days)).strftime(DATE_FORMAT),
        end=end.strftime(DATE_FORMAT),
    )
    with Whitebird(
        os.environ["WHITEBIRD_USERNAME"],
        os.environ["WHITEBIRD_PASSWORD"],
        otp=TOTP(os.environ["WHITEBIRD_TOTP_SECRET"]),
        token_store=FileTokenStore(SESSION),
    ) as client:
        statement = client.reports.download_exchange(completion_date_frame=period, type="PDF")
        args.output.write_bytes(statement)
        print(f"Saved {len(statement)} bytes to {args.output}")


if __name__ == "__main__":
    main()
