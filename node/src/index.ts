/**
 * Unofficial client for the Whitebird crypto exchange API.
 *
 * Start with {@link Whitebird}. TOTP codes for two-factor login come from the separate
 * `whitebird-api/otp` entry point.
 *
 * @packageDocumentation
 */

export type { CodeProvider } from './auth.js';
export { type RawRequest, Whitebird, type WhitebirdOptions } from './client.js';
export type { ApiRequest } from './core.js';
export {
  APIError,
  type APIErrorDetails,
  AuthenticationError,
  BadRequestError,
  ConflictError,
  MFARequiredError,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  ServerError,
  TransportError,
  VerificationRequiredError,
  WhitebirdError,
} from './errors.js';
export * from './generated/index.js';
export type { RequestOptions } from './http.js';
export { type Page, Paginator } from './pagination.js';
export { APIResource } from './resource.js';
export type { ServerSentEvent, StreamOptions } from './sse.js';
export { MemoryTokenStore, type TokenStore, type Tokens } from './tokens.js';
export { VERSION } from './version.js';
