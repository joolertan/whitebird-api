"""Client of the examples that sign in, built from environment variables."""

import base64
import os
import tempfile
from pathlib import Path
from string import Template

from whitebird import Captcha, CodeProvider, FileTokenStore, Whitebird

SESSION = Path.home() / ".config" / "whitebird" / "session.json"

CAPTCHA_PAGE = Template("""<!doctype html>
<meta charset="utf-8">
<title>Whitebird captcha</title>
<div style="position: relative; width: 290px; height: 192px">
  <img src="data:image/png;base64,$background">
  <img id="piece" src="data:image/png;base64,$piece"
       style="position: absolute; left: 0; top: ${y}px">
</div>
<input id="slider" type="range" min="0" max="250" value="0" style="width: 290px">
<p>Slider position: <b id="value">0</b></p>
<script>
  slider.oninput = () => {
    piece.style.left = slider.value + "px";
    value.textContent = slider.value;
  };
</script>
""")


def two_factor_codes() -> CodeProvider:
    """Asks for each two-factor code, or computes it from WHITEBIRD_TOTP_SECRET when that is set."""
    secret = os.environ.get("WHITEBIRD_TOTP_SECRET")
    if not secret:
        return lambda: input("Two-factor code: ").strip()
    from whitebird.otp import TOTP  # noqa: PLC0415 - the otp extra is only needed here

    return TOTP(secret)


def solve_captcha(captcha: Captcha) -> int:
    """Shows the sign-in captcha of accounts without 2FA on a local page; a person solves it."""
    path = Path(tempfile.gettempdir()) / "whitebird-captcha.html"
    page = CAPTCHA_PAGE.substitute(
        background=base64.b64encode(captcha.background_image).decode(),
        piece=base64.b64encode(captcha.puzzle_piece).decode(),
        y=captcha.position_y,
    )
    path.write_text(page, encoding="utf-8")
    prompt = f"Open {path} in a browser, slide the piece into the gap and enter the number shown: "
    return int(input(prompt))


def connect() -> Whitebird:
    """Client of WHITEBIRD_USERNAME and WHITEBIRD_PASSWORD; the session is kept in SESSION."""
    return Whitebird(
        os.environ["WHITEBIRD_USERNAME"],
        os.environ["WHITEBIRD_PASSWORD"],
        otp=two_factor_codes(),
        captcha=solve_captcha,
        token_store=FileTokenStore(SESSION),
    )
