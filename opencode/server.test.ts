import assert from "node:assert/strict"
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { createServer } from "node:http"
import { type AddressInfo, createServer as createNetServer } from "node:net"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { after, afterEach, describe, it } from "node:test"
import { fileURLToPath } from "node:url"

import { ensureServer, probe, spawnedCount, stopSpawned } from "./server.ts"

const SCRIPT = fileURLToPath(new URL("../openai_fake_provider.py", import.meta.url))
const HOST = "127.0.0.1"
const scratch = mkdtempSync(join(tmpdir(), "openai-fake-provider-test-"))
after(() => rmSync(scratch, { recursive: true, force: true }))
afterEach(stopSpawned)

async function freePort(): Promise<number> {
  const server = createNetServer()
  await new Promise<void>((resolve) => server.listen(0, HOST, resolve))
  const { port } = server.address() as AddressInfo
  await new Promise((resolve) => server.close(resolve))
  return port
}

async function params(overrides: { script?: string } = {}) {
  const port = await freePort()
  return { host: HOST, port, python: "python3", script: SCRIPT, idleExit: 1, logFile: join(scratch, `${port}.log`), ...overrides }
}

describe("probe", () => {
  it("finds nothing on a closed port", async () => {
    assert.deepEqual(await probe({ host: HOST, port: await freePort() }), { state: "down" })
  })

  it("tells another program apart", async () => {
    const other = createServer((_req, res) => res.writeHead(200, { Server: "nginx" }).end("{}"))
    await new Promise<void>((resolve) => other.listen(0, HOST, resolve))
    try {
      const { port } = other.address() as AddressInfo
      assert.deepEqual(await probe({ host: HOST, port }), { state: "other", server: "nginx" })
      await assert.rejects(ensureServer({ ...(await params()), port }), /127\.0\.0\.1:\d+ is used by another program \(Server: nginx\)/)
    } finally {
      other.close()
    }
  })
})

describe("ensureServer", () => {
  it("starts the server once, and stopSpawned stops it", async () => {
    const p = await params()
    await Promise.all([ensureServer(p), ensureServer(p)])
    assert.deepEqual(await probe(p), { state: "fake" })
    assert.equal(spawnedCount(), 1)
    assert.match(readFileSync(p.logFile, "utf8"), /exiting after 1 idle minutes/)

    await ensureServer(p) // already up: nothing new
    assert.equal(spawnedCount(), 1)

    await stopSpawned()
    for (let i = 0; i < 40 && (await probe(p)).state !== "down"; i++) await new Promise((r) => setTimeout(r, 50))
    assert.deepEqual(await probe(p), { state: "down" })
  })

  it("waits for a start in progress before stopping", async () => {
    const p = await params()
    const started = ensureServer(p)
    await stopSpawned()
    await started
    assert.equal(spawnedCount(), 0)
    for (let i = 0; i < 40 && (await probe(p)).state !== "down"; i++) await new Promise((r) => setTimeout(r, 50))
    assert.deepEqual(await probe(p), { state: "down" })
  })

  it("quotes the log when the server fails to start", async () => {
    const broken = join(scratch, "broken.py")
    writeFileSync(broken, 'import sys\nprint("boom", file=sys.stderr)\nsys.exit(3)\n')
    const p = await params({ script: broken })
    await assert.rejects(ensureServer(p), (error: Error) => {
      assert.match(error.message, /could not start the server on 127\.0\.0\.1:\d+: the server exited with 3/)
      assert.match(error.message, new RegExp(`Last lines of ${p.logFile}:\nboom$`))
      return true
    })
  })

  it("explains a missing interpreter", async () => {
    await assert.rejects(ensureServer({ ...(await params()), python: "no-such-python" }), /could not run no-such-python/)
  })
})
