import { randomUUID } from 'node:crypto';
import { readFile, rename, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline/promises';
import { type Captcha, type TokenStore, Whitebird } from 'whitebird-api';
import { totp } from 'whitebird-api/otp';

/**
 * Keeps the session in a JSON file readable only by its owner, so that only the first run logs
 * in. A missing or damaged file means no session; the file is replaced atomically.
 */
export function fileTokenStore(path: URL): TokenStore {
  return {
    load: () =>
      readFile(path, 'utf8')
        .then(JSON.parse)
        .catch(() => undefined),
    async save(tokens) {
      const temporary = new URL(`${path.href}.${randomUUID()}.tmp`);
      try {
        await writeFile(temporary, JSON.stringify(tokens), { mode: 0o600, flush: true });
        await rename(temporary, path);
      } finally {
        await rm(temporary, { force: true });
      }
    },
    clear: () => rm(path, { force: true }),
  };
}

/** Asks a question in the terminal. */
async function ask(question: string): Promise<string> {
  const terminal = createInterface({ input: process.stdin, output: process.stdout });
  try {
    return (await terminal.question(question)).trim();
  } finally {
    terminal.close();
  }
}

/** A page where the puzzle piece is moved over the background with a slider. */
function captchaPage({ backgroundImage, puzzlePiece, positionY }: Captcha): string {
  const png = (bytes: Uint8Array) =>
    `data:image/png;base64,${Buffer.from(bytes).toString('base64')}`;
  return `<!doctype html>
<meta charset="utf-8">
<title>Whitebird captcha</title>
<div style="position: relative; width: 290px; height: 192px">
  <img src="${png(backgroundImage)}">
  <img id="piece" src="${png(puzzlePiece)}" style="position: absolute; left: 0; top: ${positionY}px">
</div>
<input id="slider" type="range" min="0" max="250" value="0" style="width: 290px">
<p>Slider position: <b id="value">0</b></p>
<script>
  slider.oninput = () => {
    piece.style.left = slider.value + "px";
    value.textContent = slider.value;
  };
</script>
`;
}

/** Lets a person solve the sign-in captcha of accounts without two-factor authentication. */
async function solveCaptcha(captcha: Captcha): Promise<number> {
  const path = join(tmpdir(), 'whitebird-captcha.html');
  await writeFile(path, captchaPage(captcha));
  const answer = await ask(
    `Open ${path} in a browser, slide the piece into the gap and enter the number shown: `,
  );
  return Number(answer);
}

/**
 * Client of the account in WHITEBIRD_USERNAME and WHITEBIRD_PASSWORD. Two-factor codes are asked
 * for in the terminal, or computed from WHITEBIRD_TOTP_SECRET (the base32 secret behind the
 * authenticator app QR code) when it is set; accounts without two-factor authentication solve
 * the sign-in captcha in a browser.
 */
export function createClient(): Whitebird {
  const { WHITEBIRD_USERNAME, WHITEBIRD_PASSWORD, WHITEBIRD_TOTP_SECRET } = process.env;
  return new Whitebird({
    username: WHITEBIRD_USERNAME,
    password: WHITEBIRD_PASSWORD,
    otp: WHITEBIRD_TOTP_SECRET ? totp(WHITEBIRD_TOTP_SECRET) : () => ask('Two-factor code: '),
    captcha: solveCaptcha,
    tokenStore: fileTokenStore(new URL('.session.json', import.meta.url)),
  });
}
