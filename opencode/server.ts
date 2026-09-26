// Finding, starting and stopping the fake provider server. No opencode imports.

import { type ChildProcess, spawn } from "node:child_process"
import { closeSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"

import type { Address } from "./provider.ts"

export const SERVER_HEADER_PREFIX = "openai-fake-provider"

/** Request header naming the directory the server saves that request to. */
export const DUMP_DIR_HEADER = "X-Fake-Provider-Dump-Dir"

export type Probe = { state: "fake" } | { state: "other"; server: string } | { state: "down" }

export type StartParams = Address & {
  python: string
  script: string
  /** Minutes without requests before the server exits by itself; 0 means never. */
  idleExit: number
  logFile?: string
  /** How long to wait for a started server to answer. */
  timeoutMs?: number
}

const starting = new Map<string, Promise<void>>()
const spawned = new Map<string, ChildProcess>()

function key({ host, port }: Address): string {
  return `${host}:${port}`
}

function url({ host, port }: Address, path: string): string {
  return `http://${host.includes(":") ? `[${host}]` : host}:${port}${path}`
}

export function defaultLogFile(port: number): string {
  return join(tmpdir(), "openai-fake-provider", `server-${port}.log`)
}

/** Tell whether the fake provider, something else, or nothing answers at an address. */
export async function probe(address: Address, timeoutMs = 2000): Promise<Probe> {
  let response: Response
  try {
    response = await fetch(url(address, "/v1/models"), { signal: AbortSignal.timeout(timeoutMs) })
  } catch {
    return { state: "down" }
  }
  await response.body?.cancel()
  const server = response.headers.get("server") ?? ""
  return server.startsWith(SERVER_HEADER_PREFIX) ? { state: "fake" } : { state: "other", server }
}

/**
 * Make sure the fake provider answers at an address, starting it if nothing does.
 *
 * Concurrent calls in one process share a single start. When two processes
 * start it at once, one of them fails to bind the port, and both still
 * succeed against the other one's server.
 */
export function ensureServer(params: StartParams): Promise<void> {
  const k = key(params)
  let pending = starting.get(k)
  if (pending === undefined) {
    pending = (async () => {
      const found = await probe(params)
      if (found.state === "fake") return
      if (found.state === "other") throw usedByOther(params, found.server)
      await start(params)
    })().finally(() => starting.delete(k))
    starting.set(k, pending)
  }
  return pending
}

function usedByOther(address: Address, server: string): Error {
  return new Error(
    `openai-fake-provider: ${key(address)} is used by another program` + (server ? ` (Server: ${server})` : ""),
  )
}

async function start(params: StartParams): Promise<void> {
  const { host, port, python, script, idleExit } = params
  const logFile = params.logFile ?? defaultLogFile(port)
  mkdirSync(dirname(logFile), { recursive: true })
  // Truncated at each start: it's only useful right after a request. Appending
  // keeps two racing servers' lines from overwriting each other.
  writeFileSync(logFile, "")
  const log = openSync(logFile, "a")
  const args = [script, "serve", "--host", host, "--port", String(port)]
  if (idleExit > 0) args.push("--idle-exit", String(idleExit))
  let child: ChildProcess
  try {
    child = spawn(python, args, { detached: true, stdio: ["ignore", log, log] })
  } finally {
    closeSync(log)
  }
  child.unref()

  let failure: string | undefined
  const forget = () => {
    if (spawned.get(key(params)) === child) spawned.delete(key(params))
  }
  child.on("error", (error) => {
    forget()
    failure = `could not run ${python}: ${error.message}`
  })
  child.on("exit", (code, signal) => {
    forget()
    // Losing the race to another process is fine, as long as its server comes up.
    if (!tail(logFile).includes("already in use")) failure ??= `the server exited with ${signal ?? code}`
  })
  spawned.set(key(params), child)

  const deadline = Date.now() + (params.timeoutMs ?? 5000)
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 50))
    const found = await probe(params, 500)
    if (found.state === "fake") return
    if (found.state === "other") throw usedByOther(params, found.server)
    if (failure) break
  }
  const lines = tail(logFile)
  throw new Error(
    `openai-fake-provider: could not start the server on ${key(params)}: ${failure ?? "it did not answer in time"}. ` +
      (lines ? `Last lines of ${logFile}:\n${lines}` : `Its log, ${logFile}, is empty.`),
  )
}

function tail(file: string, lines = 10): string {
  try {
    return readFileSync(file, "utf8").trimEnd().split("\n").slice(-lines).join("\n")
  } catch {
    return ""
  }
}

/**
 * Stop every server this process started that is still running.
 *
 * Starts still in progress are waited for first, or one of them could
 * spawn a server right after this returned.
 */
export async function stopSpawned(): Promise<void> {
  await Promise.allSettled(starting.values())
  for (const child of spawned.values()) {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM")
  }
  spawned.clear()
}

/** How many servers this process started that it still considers running. For tests. */
export function spawnedCount(): number {
  return spawned.size
}
