import { type HttpClient, readJson } from './http.js';

/** A slider captcha that Whitebird may ask for when logging in. */
export interface Captcha {
  /** ID of this captcha, sent back with the answer. */
  requestId: string;
  /** PNG image with the cut-out that the piece must fill. */
  backgroundImage: Uint8Array;
  /** PNG image of the piece to slide. */
  puzzlePiece: Uint8Array;
  /** Vertical offset of the piece in background pixels. */
  positionY: number;
}

/**
 * Solves a login captcha: returns the horizontal position, in background pixels, at which the
 * piece fits the cut-out. The SDK never solves captchas itself; plug in your own image matching
 * or a human in the loop.
 *
 * @example
 * ```ts
 * import { writeFile } from 'node:fs/promises';
 * import { createInterface } from 'node:readline/promises';
 *
 * const askHuman: CaptchaSolver = async (captcha) => {
 *   await writeFile('captcha.png', captcha.backgroundImage);
 *   const prompt = createInterface({ input: process.stdin, output: process.stdout });
 *   const answer = await prompt.question('Open captcha.png and enter the slider position: ');
 *   prompt.close();
 *   return Number(answer);
 * };
 * const client = new Whitebird({ username, password, captcha: askHuman });
 * ```
 */
export type CaptchaSolver = (captcha: Captcha) => number | Promise<number>;

interface CaptchaImages {
  backgroundImage?: string;
  puzzlePiece?: string;
  positionY?: number;
}

/** Fetches a fresh captcha, has it solved and returns the value of the `captcha` header. */
export async function solveCaptcha(
  http: HttpClient,
  url: string,
  headers: Record<string, string>,
  solve: CaptchaSolver,
): Promise<string> {
  const requestId = crypto.randomUUID();
  const images = (await http.send(
    `${url}?${new URLSearchParams({ requestId })}`,
    { method: 'GET', headers },
    {},
    readJson,
  )) as CaptchaImages;
  const position = await solve({
    requestId,
    backgroundImage: decodeImage(images.backgroundImage),
    puzzlePiece: decodeImage(images.puzzlePiece),
    positionY: images.positionY ?? 0,
  });
  // The web app sends the value of its range input, which is a string.
  return btoa(JSON.stringify({ sliderPosition: String(Math.round(position)), requestId }));
}

function decodeImage(base64 = ''): Uint8Array {
  const data = base64.replace(/^data:[^,]*,/, '');
  return Uint8Array.from(atob(data), (char) => char.charCodeAt(0));
}
