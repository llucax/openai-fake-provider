import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { describe, it } from "node:test"

import { addressOf, deepMerge, isLoopback, managedAddress, providerEntry } from "./provider.ts"

const SCRIPT = fileURLToPath(new URL("../openai_fake_provider.py", import.meta.url))

describe("providerEntry", () => {
  it("builds the provider from the script, pointing at the given address", async () => {
    const entry = await providerEntry({ python: "python3", script: SCRIPT, providerID: "fk", host: "127.0.0.1", port: 4199 })
    assert.equal(entry.name, "FakeAI")
    assert.deepEqual(entry.options, { baseURL: "http://127.0.0.1:4199/v1", apiKey: "fake" })
    assert.deepEqual(Object.keys(entry.models as object), ["echo", "render", "stats", "ok"])
    assert.equal("model" in entry, false)
    assert.equal("small_model" in entry, false)
  })

  it("passes mimic through", async () => {
    const entry = await providerEntry({
      python: "python3",
      script: SCRIPT,
      providerID: "fake",
      host: "127.0.0.1",
      port: 4141,
      mimic: "claude-opus-4-5",
    })
    assert.equal((entry.models as Record<string, { id: string }>).stats.id, "claude-opus-4-5-stats")
  })

  it("explains a failure", async () => {
    await assert.rejects(
      providerEntry({ python: "no-such-python", script: SCRIPT, providerID: "x", host: "127.0.0.1", port: 1 }),
      /could not build the x provider with no-such-python/,
    )
  })
})

describe("deepMerge", () => {
  it("lets the override win, field by field", () => {
    const base = { name: "FakeAI", options: { baseURL: "http://a/v1", apiKey: "fake" }, models: { ok: { name: "OK" } } }
    const override = { options: { baseURL: "http://b/v1" }, models: { ok: { name: "Fine" }, x: { name: "X" } } }
    assert.deepEqual(deepMerge(base, override), {
      name: "FakeAI",
      options: { baseURL: "http://b/v1", apiKey: "fake" },
      models: { ok: { name: "Fine" }, x: { name: "X" } },
    })
  })

  it("replaces arrays instead of merging them", () => {
    assert.deepEqual(deepMerge({ a: [1, 2] }, { a: [3] }), { a: [3] })
  })
})

describe("addresses", () => {
  it("reads host and port from a base URL", () => {
    assert.deepEqual(addressOf("http://127.0.0.1:4141/v1"), { host: "127.0.0.1", port: 4141 })
    assert.deepEqual(addressOf("http://[::1]:4141/v1"), { host: "::1", port: 4141 })
    assert.deepEqual(addressOf("http://localhost/v1"), { host: "localhost", port: 80 })
    assert.equal(addressOf("https://api.example.com/v1"), undefined)
    assert.equal(addressOf("not a url"), undefined)
  })

  it("only manages loopback addresses", () => {
    assert.ok(isLoopback("127.0.0.1") && isLoopback("localhost") && isLoopback("::1") && isLoopback("127.1.2.3"))
    assert.equal(isLoopback("10.0.0.1"), false)
    assert.deepEqual(managedAddress({ options: { baseURL: "http://127.0.0.1:4141/v1" } }), {
      host: "127.0.0.1",
      port: 4141,
    })
    assert.equal(managedAddress({ options: { baseURL: "http://10.0.0.1:4141/v1" } }), undefined)
    assert.equal(managedAddress(undefined), undefined)
  })
})
