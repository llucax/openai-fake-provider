import assert from "node:assert/strict"
import { homedir } from "node:os"
import { join } from "node:path"
import { describe, it } from "node:test"

import { DEFAULTS, envName, parseOptions } from "./options.ts"

describe("parseOptions", () => {
  it("uses the defaults with no options and no environment", () => {
    assert.deepEqual(parseOptions(undefined, {}), { ...DEFAULTS, mimic: undefined, dumpDir: undefined, logFile: undefined })
  })

  it("takes options from the plugin entry", () => {
    const options = parseOptions({ autostart: "off", match: "model", port: 4199, idleExit: 0 }, {})
    assert.equal(options.autostart, "off")
    assert.equal(options.match, "model")
    assert.equal(options.port, 4199)
    assert.equal(options.idleExit, 0)
  })

  it("lets the environment override the plugin entry", () => {
    const env = { OPENAI_FAKE_PROVIDER_PORT: "4200", OPENAI_FAKE_PROVIDER_DUMP_DIR: "/tmp/dumps" }
    const options = parseOptions({ port: 4199, dumpDir: "/elsewhere" }, env)
    assert.equal(options.port, 4200)
    assert.equal(options.dumpDir, "/tmp/dumps")
  })

  it("ignores empty environment variables", () => {
    assert.equal(parseOptions({ port: 4199 }, { OPENAI_FAKE_PROVIDER_PORT: "" }).port, 4199)
  })

  it("names environment variables after the options", () => {
    assert.equal(envName("providerID"), "OPENAI_FAKE_PROVIDER_PROVIDER_ID")
    assert.equal(envName("dumpDir"), "OPENAI_FAKE_PROVIDER_DUMP_DIR")
    assert.equal(envName("idleExit"), "OPENAI_FAKE_PROVIDER_IDLE_EXIT")
  })

  it("expands ~ in paths", () => {
    assert.equal(parseOptions({ dumpDir: "~/dumps" }, {}).dumpDir, join(homedir(), "dumps"))
  })

  it("rejects relative paths", () => {
    assert.throws(() => parseOptions({ dumpDir: "dumps" }, {}), /option dumpDir must be an absolute path/)
  })

  it("rejects unknown options", () => {
    assert.throws(() => parseOptions({ autoStart: "off" }, {}), /unknown options autoStart/)
  })

  it("rejects invalid values, saying where they came from", () => {
    assert.throws(() => parseOptions({ autostart: "always" }, {}), /option autostart must be one of on-use/)
    assert.throws(() => parseOptions({ match: "id" }, {}), /option match must be one of baseURL, model/)
    assert.throws(
      () => parseOptions({}, { OPENAI_FAKE_PROVIDER_PORT: "http" }),
      /OPENAI_FAKE_PROVIDER_PORT="http" must be a port number/,
    )
    assert.throws(() => parseOptions({ idleExit: -1 }, {}), /option idleExit must be a number of minutes/)
  })
})
