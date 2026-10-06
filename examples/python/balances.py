"""Print the balances of the Whitebird wallet with their USD value."""

import os
from pathlib import Path

from whitebird import FileTokenStore, Whitebird
from whitebird.models import AccountingAccountType
from whitebird.otp import TOTP

SESSION = Path.home() / ".config" / "whitebird" / "session.json"


def main() -> None:
    with Whitebird(
        os.environ["WHITEBIRD_USERNAME"],
        os.environ["WHITEBIRD_PASSWORD"],
        otp=TOTP(os.environ["WHITEBIRD_TOTP_SECRET"]),
        token_store=FileTokenStore(SESSION),
    ) as client:
        wallet = client.accounting.accounts.get_enhanced(type=AccountingAccountType.USER_BALANCE)
        for balance in wallet.balances or []:
            if balance.amount:
                print(f"{balance.currency:>6} {balance.amount:>24} ~ {balance.usd_amount} USD")


if __name__ == "__main__":
    main()
