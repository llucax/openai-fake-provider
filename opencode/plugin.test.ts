import assert from "node:assert/strict"
import { mkdtempSync, rmSync } from "node:fs"
import { type AddressInfo, createServer } from "node:net"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { after, describe, it } from "node:test"

import type { Hooks, PluginInput } from "@opencode-ai/plugin"

import plugin from "./plugin.ts"
import { DUMP_DIR_HEADER, probe, spawnedCount } from "./server.ts"

const scratch = mkdtempSync(join(tmpdir(), "openai-fake-provider-test-"))
after(() => rmSync(scratch, { recursive: true, force: true }))

async function freePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve))
  const { port } = server.address() as AddressInfo
  await new Promise((resolve) => server.close(resolve))
  return port
}

async function load(options: Record<string, unknown>): Promise<Required<Hooks>> {
  return (await plugin({} as PluginInput, { logFile: join(scratch, "server.log"), ...options })) as Required<Hooks>
}

function request(providerID: string, baseURL: string) {
  return {
    sessionID: "s",
    agent: "build",
    model: { providerID, api: { url: "" } },
    provider: { options: { baseURL } },
    message: {},
  } as unknown as Parameters<Required<Hooks>["chat.params"]>[0]
}

const PARAMS = { temperature: 0, topP: 1, topK: 0, maxOutputTokens: undefined, options: {} }

describe("config hook", () => {
  it("adds the provider and leaves the default models alone", async () => {
    const hooks = await load({ port: 4199 })
    const config: Record<string, any> = { model: "anthropic/claude", provider: { other: { name: "Other" } } }
    await hooks.config(config as any)
    assert.equal(config.model, "anthropic/claude")
    assert.equal("small_model" in config, false)
    assert.deepEqual(config.provider.other, { name: "Other" })
    assert.equal(config.provider.fake.name, "FakeAI")
    assert.equal(config.provider.fake.options.baseURL, "http://127.0.0.1:4199/v1")
    await hooks.dispose()
  })

  it("merges a hand-written provider block over the defaults", async () => {
    const hooks = await load({ providerID: "fk" })
    const config: Record<string, any> = {
      provider: { fk: { name: "Mine", options: { baseURL: "http://127.0.0.1:4300/v1" }, models: { ok: { name: "Fine" } } } },
    }
    await hooks.config(config as any)
    const fk = config.provider.fk
    assert.equal(fk.name, "Mine")
    assert.deepEqual(fk.options, { baseURL: "http://127.0.0.1:4300/v1", apiKey: "fake" })
    assert.equal(fk.models.ok.name, "Fine")
    assert.equal(fk.models.ok.tool_call, true)
    assert.equal(fk.models.stats.name, "Stats")
    await hooks.dispose()
  })
})

describe("requests", () => {
  it("starts the server for a request to the provider's address, and stops it on dispose", async () => {
    const port = await freePort()
    const hooks = await load({ port, idleExit: 1 })
    await hooks.config({} as any)

    await hooks["chat.params"](request("elsewhere", "http://127.0.0.1:1/v1"), PARAMS)
    assert.equal(spawnedCount(), 0)

    await hooks["chat.params"](request("renamed", `http://127.0.0.1:${port}/v1`), PARAMS)
    assert.deepEqual(await probe({ host: "127.0.0.1", port }), { state: "fake" })
    assert.equal(spawnedCount(), 1)

    await hooks.dispose()
    assert.equal(spawnedCount(), 0)
  })

  it("can match by model instead", async () => {
    const port = await freePort()
    const hooks = await load({ port, match: "model", autostart: "off", dumpDir: "/tmp/dumps" })
    await hooks.config({} as any)
    const headers = async (input: ReturnType<typeof request>) => {
      const output = { headers: {} as Record<string, string> }
      await hooks["chat.headers"](input, output)
      return output.headers
    }
    assert.deepEqual(await headers(request("fake", "http://example.com/v1")), { [DUMP_DIR_HEADER]: "/tmp/dumps" })
    assert.deepEqual(await headers(request("other", `http://127.0.0.1:${port}/v1`)), {})

    // autostart off: nothing is started even for a fake request.
    await hooks["chat.params"](request("fake", `http://127.0.0.1:${port}/v1`), PARAMS)
    assert.equal(spawnedCount(), 0)
    await hooks.dispose()
  })

  it("adds no dump header without a dump directory", async () => {
    const hooks = await load({})
    await hooks.config({} as any)
    const output = { headers: {} as Record<string, string> }
    await hooks["chat.headers"](request("fake", "http://127.0.0.1:4141/v1"), output)
    assert.deepEqual(output.headers, {})
    await hooks.dispose()
  })
})
