// opencode plugin that adds the fake provider and starts its server when needed.
//
// opencode treats every export of this file as a plugin, so the default
// export is the only one.

import type { Hooks, Plugin } from "@opencode-ai/plugin"
import { fileURLToPath } from "node:url"

import { type Options, parseOptions } from "./options.ts"
import { type Address, addressOf, deepMerge, managedAddress, providerEntry, sameAddress } from "./provider.ts"
import { DUMP_DIR_HEADER, ensureServer, stopSpawned } from "./server.ts"

const SCRIPT = fileURLToPath(new URL("../openai_fake_provider.py", import.meta.url))

/** Plugin instances alive in this process; opencode makes one per project directory. */
let instances = 0

type RequestInput = Parameters<NonNullable<Hooks["chat.params"]>>[0]

const plugin: Plugin = async (_input, rawOptions) => {
  const options = parseOptions(rawOptions)
  let address: Address | undefined
  instances++

  const isFake = ({ model, provider }: RequestInput): boolean => {
    if (options.match === "model") return model.providerID === options.providerID
    return sameAddress(addressOf(provider.options?.baseURL ?? model.api.url), address)
  }
  const start = (at: Address) =>
    ensureServer({ ...at, python: options.python, script: SCRIPT, idleExit: options.idleExit, logFile: options.logFile })

  return {
    config: async (config) => {
      const providers = ((config as { provider?: Record<string, Record<string, unknown>> }).provider ??= {})
      const own = providers[options.providerID]
      let injected: Record<string, unknown> = {}
      let failure: unknown
      try {
        injected = await providerEntry({ ...providerParams(options), script: SCRIPT })
      } catch (error) {
        failure = error
      }
      const merged = deepMerge(injected, own ?? {})
      if (Object.keys(merged).length > 0) providers[options.providerID] = merged
      address = managedAddress(merged)
      if (options.autostart === "on-start" && address) {
        // Nothing waits for it; a failure shows up again on the first fake request.
        start(address).catch(() => {})
      }
      if (failure) throw failure
    },

    "chat.params": async (input) => {
      if (options.autostart === "off" || address === undefined || !isFake(input)) return
      await start(address)
    },

    "chat.headers": async (input, output) => {
      if (options.dumpDir && isFake(input)) output.headers[DUMP_DIR_HEADER] = options.dumpDir
    },

    dispose: async () => {
      if (--instances === 0) await stopSpawned()
    },
  }
}

function providerParams({ python, providerID, host, port, mimic }: Options) {
  return { python, providerID, host, port, mimic }
}

export default plugin
