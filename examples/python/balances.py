"""Print the balances of the Whitebird wallet with their USD value."""

from account import connect
from whitebird.models import AccountingAccountType


def main() -> None:
    with connect() as client:
        wallet = client.accounting.accounts.get_enhanced(type=AccountingAccountType.USER_BALANCE)
        for balance in wallet.balances or []:
            if balance.amount:
                print(f"{balance.currency:>6} {balance.amount:>24} ~ {balance.usd_amount} USD")


if __name__ == "__main__":
    main()
