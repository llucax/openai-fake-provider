// Plugin options, from the `plugin` array entry and the environment.

import { homedir } from "node:os"
import { isAbsolute, join } from "node:path"

export const AUTOSTART = ["on-use", "on-start", "off"] as const
export type Autostart = (typeof AUTOSTART)[number]

export const MATCH = ["baseURL", "model"] as const
export type Match = (typeof MATCH)[number]

export type Options = {
  /** When to start the server: before the first fake request, when opencode starts, or never. */
  autostart: Autostart
  /** How a request is recognized as fake: by where it goes, or by its model's provider ID. */
  match: Match
  providerID: string
  /** Where the injected provider points; a `baseURL` in the user's own provider block wins. */
  host: string
  port: number
  /** Prefix for model IDs, so opencode uses that model's system prompt (`--mimic`). */
  mimic?: string
  /** Minutes without requests before the server exits by itself; 0 means never. */
  idleExit: number
  /** Directory each fake request is saved to, as an absolute path. */
  dumpDir?: string
  /** Server log; by default `$TMPDIR/openai-fake-provider/server-<port>.log`. */
  logFile?: string
  python: string
}

export const DEFAULTS: Options = {
  autostart: "on-use",
  match: "baseURL",
  providerID: "fake",
  host: "127.0.0.1",
  port: 4141,
  idleExit: 60,
  python: "python3",
}

export const ENV_PREFIX = "OPENAI_FAKE_PROVIDER_"

const KEYS = [
  "autostart",
  "match",
  "providerID",
  "host",
  "port",
  "mimic",
  "idleExit",
  "dumpDir",
  "logFile",
  "python",
] as const satisfies readonly (keyof Options)[]

type Key = (typeof KEYS)[number]

/** `providerID` becomes `OPENAI_FAKE_PROVIDER_PROVIDER_ID`, `dumpDir` `..._DUMP_DIR`. */
export function envName(key: Key): string {
  return ENV_PREFIX + key.replace(/ID$/, "Id").replace(/[A-Z]/g, (c) => "_" + c).toUpperCase()
}

/**
 * Build the options from the plugin's `plugin` array entry and the environment.
 *
 * Environment variables win over the array entry, so one command can change
 * a setting without editing the config. An empty variable counts as unset.
 * Throws on unknown keys and invalid values, which makes opencode report the
 * plugin as failed to load, with the message.
 */
export function parseOptions(
  raw: Record<string, unknown> | undefined,
  env: Record<string, string | undefined> = process.env,
): Options {
  const given = { ...raw }
  const unknown = Object.keys(given).filter((key) => !(KEYS as readonly string[]).includes(key))
  if (unknown.length > 0) {
    throw new Error(`openai-fake-provider: unknown options ${unknown.join(", ")}; known: ${KEYS.join(", ")}`)
  }
  for (const key of KEYS) {
    const value = env[envName(key)]
    if (value !== undefined && value !== "") given[key] = value
  }

  const where = (key: Key) =>
    env[envName(key)] ? `${envName(key)}=${JSON.stringify(env[envName(key)])}` : `option ${key}`
  const fail = (key: Key, expected: string): never => {
    throw new Error(`openai-fake-provider: ${where(key)} must be ${expected}`)
  }
  const oneOf = <T extends string>(key: Key, allowed: readonly T[]): T | undefined => {
    const value = given[key]
    if (value === undefined) return undefined
    if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
      fail(key, `one of ${allowed.join(", ")}`)
    }
    return value as T
  }
  const text = (key: Key): string | undefined => {
    const value = given[key]
    if (value === undefined) return undefined
    if (typeof value !== "string" || value === "") fail(key, "a non-empty string")
    return value as string
  }
  const number = (key: Key, check: (n: number) => boolean, expected: string): number | undefined => {
    const value = given[key]
    if (value === undefined) return undefined
    const n = typeof value === "string" && value.trim() !== "" ? Number(value) : value
    if (typeof n !== "number" || !check(n)) fail(key, expected)
    return n as number
  }
  const path = (key: Key): string | undefined => {
    const value = text(key)
    if (value === undefined) return undefined
    const expanded = value === "~" ? homedir() : value.startsWith("~/") ? join(homedir(), value.slice(2)) : value
    if (!isAbsolute(expanded)) fail(key, "an absolute path (or start with ~/)")
    return expanded
  }

  return {
    autostart: oneOf("autostart", AUTOSTART) ?? DEFAULTS.autostart,
    match: oneOf("match", MATCH) ?? DEFAULTS.match,
    providerID: text("providerID") ?? DEFAULTS.providerID,
    host: text("host") ?? DEFAULTS.host,
    port: number("port", (n) => Number.isInteger(n) && n > 0 && n < 65536, "a port number") ?? DEFAULTS.port,
    mimic: text("mimic"),
    idleExit: number("idleExit", (n) => Number.isFinite(n) && n >= 0, "a number of minutes, 0 for never") ??
      DEFAULTS.idleExit,
    dumpDir: path("dumpDir"),
    logFile: path("logFile"),
    python: text("python") ?? DEFAULTS.python,
  }
}
