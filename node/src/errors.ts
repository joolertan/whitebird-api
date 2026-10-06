/** Base class of every error thrown by this package. */
export class WhitebirdError extends Error {
  override name = 'WhitebirdError';
}

/** The request got no HTTP response: the connection failed or the request timed out. */
export class TransportError extends WhitebirdError {
  override name = 'TransportError';
}

/** What is known about a failed API call. */
export interface APIErrorDetails {
  status?: number;
  code?: number;
  errorCode?: string;
  subStatus?: string;
  fields?: unknown[];
  body?: unknown;
  headers?: Headers;
}

/** The API rejected a request. */
export class APIError extends WhitebirdError {
  override name = 'APIError';
  /** HTTP status, or `undefined` when the client detected the error without an error response. */
  readonly status: number | undefined;
  /** Numeric code from the body: `code` of service errors, `retCode` of enveloped responses. */
  readonly code: number | undefined;
  /** Machine-readable code: `status` of service errors (`SMS_VERIFICATION_REQUIRED`, ...), `error` of OAuth errors. */
  readonly errorCode: string | undefined;
  /** Finer-grained machine-readable code that some service errors add (`subStatus`). */
  readonly subStatus: string | undefined;
  /** Field validation errors, as sent by the server. */
  readonly fields: unknown[];
  /** Parsed response body, or its text when it is not JSON. */
  readonly body: unknown;
  /** Response headers. */
  readonly headers: Headers | undefined;

  constructor(message: string, details: APIErrorDetails = {}) {
    super(message);
    this.status = details.status;
    this.code = details.code;
    this.errorCode = details.errorCode;
    this.subStatus = details.subStatus;
    this.fields = details.fields ?? [];
    this.body = details.body;
    this.headers = details.headers;
  }
}

/** HTTP 400: the request is invalid. */
export class BadRequestError extends APIError {
  override name = 'BadRequestError';
}

/** The operation needs an SMS or liveness verification completed in the Whitebird app first. */
export class VerificationRequiredError extends BadRequestError {
  override name = 'VerificationRequiredError';
  /** Which verification is pending. */
  readonly kind: 'sms' | 'liveness';

  constructor(message: string, kind: 'sms' | 'liveness', details: APIErrorDetails = {}) {
    super(message, details);
    this.kind = kind;
  }
}

/** HTTP 401 or a rejected login: missing, expired or invalid credentials. */
export class AuthenticationError extends APIError {
  override name = 'AuthenticationError';
}

/**
 * A two-factor code is needed: either to log in (configure `otp`), or as an operation MFA token
 * for a sensitive operation (configure `otp`, or call `client.confirmOperation`).
 */
export class MFARequiredError extends AuthenticationError {
  override name = 'MFARequiredError';
}

/**
 * Logging in needs a solved slider captcha: no `captcha` solver is configured, or the captchas
 * it solved were rejected.
 */
export class CaptchaRequiredError extends AuthenticationError {
  override name = 'CaptchaRequiredError';
}

/** HTTP 403: the account may not perform the operation. */
export class PermissionDeniedError extends APIError {
  override name = 'PermissionDeniedError';
}

/** HTTP 404. */
export class NotFoundError extends APIError {
  override name = 'NotFoundError';
}

/** HTTP 409: the request conflicts with the current state of the resource. */
export class ConflictError extends APIError {
  override name = 'ConflictError';
}

/** HTTP 429: too many requests. */
export class RateLimitError extends APIError {
  override name = 'RateLimitError';
  /** Seconds to wait before retrying, from the `Retry-After` header. */
  readonly retryAfter: number | undefined;

  constructor(message: string, details: APIErrorDetails = {}) {
    super(message, details);
    this.retryAfter = details.headers && parseRetryAfter(details.headers);
  }
}

/** HTTP 5xx: the server failed. */
export class ServerError extends APIError {
  override name = 'ServerError';
}

const VERIFICATIONS: Record<string, 'sms' | 'liveness'> = {
  SMS_VERIFICATION_REQUIRED: 'sms',
  LIVENESS_VERIFICATION_REQUIRED: 'liveness',
};

const STATUS_ERRORS: Record<number, typeof APIError> = {
  400: BadRequestError,
  401: AuthenticationError,
  403: PermissionDeniedError,
  404: NotFoundError,
  409: ConflictError,
  429: RateLimitError,
};

/**
 * Builds the error for an error response. Understands service errors
 * (`{code, status, subStatus, message, fields}`), OAuth errors (`{error, error_description}`)
 * and `{retCode, retMsg}` envelopes.
 */
export function errorFromResponse(status: number, body: unknown, headers?: Headers): APIError {
  const data = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  const errorCode = text(data.status) ?? text(data.error);
  const message =
    text(data.message) ?? text(data.error_description) ?? text(data.retMsg) ?? errorCode;
  const details: APIErrorDetails = {
    status,
    code: integer(data.code) ?? integer(data.retCode),
    errorCode,
    subStatus: text(data.subStatus),
    fields: Array.isArray(data.fields) ? data.fields : [],
    body,
    headers,
  };
  const summary = message ?? `HTTP ${status}`;

  const verification = errorCode === undefined ? undefined : VERIFICATIONS[errorCode];
  if (verification) return new VerificationRequiredError(summary, verification, details);
  if (errorCode === 'MFA_CHECKING_REQUIRED' || /requires mfa checking/i.test(summary)) {
    return new MFARequiredError(summary, details);
  }
  if (errorCode === 'invalid_grant') return new AuthenticationError(summary, details);
  const ErrorClass = STATUS_ERRORS[status] ?? (status >= 500 ? ServerError : APIError);
  return new ErrorClass(summary, details);
}

/** Reads `Retry-After` (seconds or an HTTP date) as seconds from now. */
export function parseRetryAfter(headers: Headers): number | undefined {
  const value = headers.get('retry-after');
  if (!value) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds);
  const date = Date.parse(value);
  return Number.isNaN(date) ? undefined : Math.max(0, (date - Date.now()) / 1000);
}

function text(value: unknown): string | undefined {
  return typeof value === 'string' && value !== '' ? value : undefined;
}

function integer(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) ? value : undefined;
}
