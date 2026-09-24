/* User-timezone date helpers (V3 P28, V4 §25).
 *
 * Every date and time the app shows or queries with is expressed in the
 * USER's configured timezone (`state.me.settings.timezone`), never the
 * browser's: a user with a Moscow account on a device set to Amsterdam
 * must still see Moscow dates, Moscow day boundaries in the calendar, and
 * Moscow times in listings.
 *
 * Two distinct concepts are kept strictly separate:
 *
 *   1. an AWARE instant (backend ISO with an offset) — converted to the
 *      user's wall clock via isoToWall / instantIsoToUserWallBrowserDate;
 *   2. a NAIVE user-TZ wall-clock form value ("YYYY-MM-DD[THH:MM]") — the
 *      backend interprets it in the user's timezone. A naive wall value is
 *      NEVER an absolute instant, so it is never run through `new Date(...)`
 *      + timezone conversion (that would drift it by the browser/user
 *      offset); wallStringToBrowserDatePreservingFields builds the picker
 *      Date from its fields directly.
 */

import { state, localeCode } from "./state.js";

const pad2 = (n) => String(n).padStart(2, "0");

/** IANA timezone of the current user (falls back to the browser's zone). */
export function userTZ() {
  return (
    state.me?.settings?.timezone ||
    Intl.DateTimeFormat().resolvedOptions().timeZone ||
    "UTC"
  );
}

let _wallFmt;
let _wallFmtTZ;

function wallFmt() {
  const tz = userTZ();
  if (_wallFmt !== tz) {
    _wallFmt = tz;
    _wallFmtTZ = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }
  return _wallFmtTZ;
}

/**
 * Wall-clock components of an instant in the user's timezone:
 * { y, m (1-12), d, h, mi, s }.
 */
export function wallParts(date) {
  const p = {};
  for (const part of wallFmt().formatToParts(date)) {
    if (part.type !== "literal") p[part.type] = part.value;
  }
  // Some engines render midnight as hour "24".
  return {
    y: Number(p.year),
    m: Number(p.month),
    d: Number(p.day),
    h: p.hour === "24" ? 0 : Number(p.hour),
    mi: Number(p.minute),
    s: Number(p.second),
  };
}

/** "YYYY-MM-DD" of an instant in the user's timezone. */
export function dateKeyTZ(date) {
  const { y, m, d } = wallParts(date);
  return `${y}-${pad2(m)}-${pad2(d)}`;
}

/** Today ("YYYY-MM-DD") in the user's timezone. */
export function todayKey(now = new Date()) {
  return dateKeyTZ(now);
}

function tzOffsetMinutes(tz, instant) {
  const p = {};
  for (const part of new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(instant)) {
    if (part.type !== "literal") p[part.type] = part.value;
  }
  const asUTC = Date.UTC(
    Number(p.year),
    Number(p.month) - 1,
    Number(p.day),
    p.hour === "24" ? 0 : Number(p.hour),
    Number(p.minute),
    Number(p.second)
  );
  return (asUTC - instant.getTime()) / 60000;
}

/**
 * UTC instant of local midnight of the given "YYYY-MM-DD" day in the user's
 * timezone. Two-pass so DST transitions on that day resolve correctly.
 */
export function dayStartUTC(dateKey) {
  const [y, m, d] = dateKey.split("-").map(Number);
  const tz = userTZ();
  const utcMidnight = Date.UTC(y, m - 1, d);
  let instant = new Date(utcMidnight);
  // Two passes: the offset is evaluated at the corrected instant, so a DST
  // transition on that day resolves correctly.
  for (let i = 0; i < 2; i++) {
    instant = new Date(utcMidnight - tzOffsetMinutes(tz, instant) * 60000);
  }
  return instant;
}

/**
 * UTC instant of local midnight on the FIRST day of the month (m is
 * 0-based). Out-of-range m (e.g. 12) normalizes to January of the next
 * year, so `monthStartUTC(y, m + 1)` is exactly a month's end boundary.
 */
export function monthStartUTC(y, m) {
  const t = new Date(Date.UTC(y, m, 1));
  const key = `${t.getUTCFullYear()}-${pad2(t.getUTCMonth() + 1)}-01`;
  return dayStartUTC(key);
}

/** UTC instant when the given "YYYY-MM-DD" day ENDS (start of next day). */
export function dayEndUTC(dateKey) {
  const [y, m, d] = dateKey.split("-").map(Number);
  const next = new Date(Date.UTC(y, m - 1, d + 1));
  const key = `${next.getUTCFullYear()}-${pad2(next.getUTCMonth() + 1)}-${pad2(next.getUTCDate())}`;
  return dayStartUTC(key);
}

/**
 * Naive user-TZ wall clock "YYYY-MM-DD HH:MM" of an instant — the format
 * the backend accepts for create/update (naive == user's timezone).
 */
export function isoToWall(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const { y, m, d, h, mi } = wallParts(date);
  return `${y}-${pad2(m)}-${pad2(d)} ${pad2(h)}:${pad2(mi)}`;
}

/**
 * Parse a naive user-TZ wall-clock string ("YYYY-MM-DD[THH:MM]") into its
 * individual date/time fields. Returns null when the value carries no full
 * date. Time defaults to noon (flatpickr's `defaultHour`).
 */
function wallFields(wall) {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(
    String(wall || "")
  );
  if (!match) return null;
  return {
    y: Number(match[1]),
    m: Number(match[2]) - 1, // Date months are 0-based
    d: Number(match[3]),
    h: match[4] !== undefined ? Number(match[4]) : 12,
    mi: match[5] !== undefined ? Number(match[5]) : 0,
  };
}

/**
 * Build a browser-local Date from user-TZ wall-clock fields. flatpickr
 * renders in the BROWSER's local time, so a picker seeded with a Date whose
 * local fields equal the user's wall fields will display exactly that wall
 * clock (and its "Y-m-d H:i" output is exactly the naive user-TZ string the
 * backend expects).
 */
function _wallToBrowserDate(wall) {
  const f = wallFields(wall);
  if (!f) return null;
  const date = new Date(f.y, f.m, f.d, f.h, f.mi, 0, 0);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * AWARE-instant → user-wall browser Date. The only helper that performs an
 * instant→user-TZ conversion; feed it aware ISO strings from the backend.
 */
export function instantIsoToUserWallBrowserDate(instantIso) {
  return _wallToBrowserDate(isoToWall(instantIso));
}

/**
 * NAIVE user-TZ wall string → browser Date, preserving every field exactly.
 * A wall value is not an absolute instant, so it must NOT be run through
 * `new Date(wall)` + timezone conversion — that drifts the picker by the
 * browser/user offset (V4 §25).
 */
export function wallStringToBrowserDatePreservingFields(wall) {
  return _wallToBrowserDate(wall);
}

/**
 * The CURRENT instant rendered as a user-TZ wall browser Date — the correct
 * default for an empty picker (the user's "now", not the browser's).
 */
export function currentUserWallBrowserDate(now = new Date()) {
  return instantIsoToUserWallBrowserDate(now.toISOString());
}

/**
 * Locale-formatted "d MMM HH:mm" in the user's timezone, for aware ISO
 * instants coming from the API.
 */
export function fmtDT(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  try {
    return new Intl.DateTimeFormat(localeCode(), {
      timeZone: userTZ(),
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  } catch {
    return date.toLocaleString();
  }
}

/**
 * Locale-formatted "d MMM HH:mm" of a naive user-TZ wall-clock string
 * ("YYYY-MM-DD[THH:MM]") — picker values, never an instant.
 */
export function fmtWall(wall) {
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(wall || ""));
  if (!match) return "";
  const date = new Date(Date.UTC(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4]),
    Number(match[5])
  ));
  try {
    return new Intl.DateTimeFormat(localeCode(), {
      timeZone: "UTC",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  } catch {
    return String(wall);
  }
}
