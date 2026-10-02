// The `fake` provider entry the plugin adds to opencode's config.

import { execFile } from "node:child_process"
import { promisify } from "node:util"

type Json = Record<string, unknown>

export type Address = { host: string; port: number }

export type ProviderParams = {
  python: string
  script: string
  providerID: string
  host: string
  port: number
  mimic?: string
}

const cache = new Map<string, Promise<Json>>()

/**
 * Build the provider entry from the script's `opencode-config` output.
 *
 * Only the provider is taken: that output also sets `model` and
 * `small_model`, which would send every real session to the fake provider.
 * The result is cached, since opencode calls the config hook once per
 * project directory and the script takes about 0.1 s.
 */
export function providerEntry(params: ProviderParams): Promise<Json> {
  const key = JSON.stringify(params)
  let entry = cache.get(key)
  if (entry === undefined) {
    entry = buildEntry(params)
    cache.set(key, entry)
    entry.catch(() => cache.delete(key))
  }
  return entry
}

async function buildEntry({ python, script, providerID, host, port, mimic }: ProviderParams): Promise<Json> {
  const args = [script, "opencode-config", "--provider-id", providerID, "--host", host, "--port", String(port)]
  if (mimic) args.push("--mimic", mimic)
  let stdout: string
  try {
    ;({ stdout } = await promisify(execFile)(python, args, { timeout: 10_000 }))
  } catch (error) {
    throw new Error(`openai-fake-provider: could not build the ${providerID} provider with ${python}: ${error}`)
  }
  const entry = (JSON.parse(stdout) as { provider?: Record<string, Json> }).provider?.[providerID]
  if (!entry) throw new Error(`openai-fake-provider: opencode-config printed no ${providerID} provider`)
  return entry
}

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/** Merge `override` into `base`, recursing into objects; `override` wins everywhere else. */
export function deepMerge(base: Json, override: Json): Json {
  const merged: Json = { ...base }
  for (const [key, value] of Object.entries(override)) {
    const current = merged[key]
    merged[key] = isObject(current) && isObject(value) ? deepMerge(current, value) : value
  }
  return merged
}

export function isLoopback(host: string): boolean {
  const bare = host.replace(/^\[(.*)\]$/, "$1").toLowerCase()
  return bare === "localhost" || bare === "::1" || /^127(\.\d{1,3}){3}$/.test(bare)
}

/** Host and port a base URL points to, or `undefined` if it isn't an `http://` URL. */
export function addressOf(baseURL: unknown): Address | undefined {
  if (typeof baseURL !== "string") return undefined
  let url: URL
  try {
    url = new URL(baseURL)
  } catch {
    return undefined
  }
  if (url.protocol !== "http:") return undefined
  return { host: url.hostname.replace(/^\[(.*)\]$/, "$1"), port: Number(url.port || 80) }
}

/**
 * The server a provider entry points to, if the plugin may start it there.
 *
 * Only loopback addresses qualify: a server anywhere else isn't one this
 * plugin could start.
 */
export function managedAddress(provider: Json | undefined): Address | undefined {
  const options = isObject(provider?.options) ? provider.options : undefined
  const address = addressOf(options?.baseURL)
  return address && isLoopback(address.host) ? address : undefined
}

export function sameAddress(a: Address | undefined, b: Address | undefined): boolean {
  return a !== undefined && b !== undefined && a.host === b.host && a.port === b.port
}
