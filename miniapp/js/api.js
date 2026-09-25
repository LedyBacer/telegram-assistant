/* Minimal same-origin API client. Every request carries the raw Telegram
 * initData in the X-Telegram-Init-Data header; the backend verifies its HMAC
 * before trusting the identity. (In Playwright the backend runs with the
 * test-only auth override, so the header content is irrelevant there.)
 */

import { INIT_DATA } from "./telegram.js";

export class ApiError extends Error {
  /**
   * @param {string} message  user-facing safe message (not raw internals)
   * @param {number} status   HTTP status code
   * @param {any} [detail]    parsed detail from the response body (string or
   *   Pydantic 422 array) — callers may inspect it for specific field errors
   */
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function authHeaders() {
  const headers = { "X-Telegram-Init-Data": INIT_DATA };
  return headers;
}

/**
 * JSON request helper.
 * @param {string} path  e.g. "/api/v1/items"
 * @param {"GET"|"POST"|"PATCH"|"PUT"|"DELETE"} [method]
 * @param {object} [body]   JSON body (POST/PATCH)
 * @param {AbortSignal} [signal] cancels the request (stale renders)
 * @returns {Promise<any|null>} parsed JSON, or null on 204
 */
export async function api(path, method = "GET", body, signal) {
  const options = {
    method,
    headers: authHeaders(),
  };
  if (signal) options.signal = signal;
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const res = await fetch(path, options);
  if (res.status === 401) {
    throw new ApiError("auth", 401);
  }
  if (!res.ok) {
    let detail = null;
    try {
      const data = await res.json();
      if (data && "detail" in data) detail = data.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(`HTTP ${res.status}`, res.status, detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

/**
 * Multipart upload helper (Files screen).
 * @param {string} path
 * @param {File} file
 * @param {AbortSignal} [signal]
 */
export async function apiUpload(path, file, signal) {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(path, {
    method: "POST",
    headers: authHeaders(),
    body: form,
    ...(signal ? { signal } : {}),
  });
  if (res.status === 401) throw new ApiError("auth", 401);
  if (!res.ok) {
    let detail = null;
    try {
      const data = await res.json();
      if (data && "detail" in data) detail = data.detail;
    } catch {
      /* non-JSON */
    }
    throw new ApiError(`HTTP ${res.status}`, res.status, detail);
  }
  if (res.status === 204) return null;
  return res.json();
}
