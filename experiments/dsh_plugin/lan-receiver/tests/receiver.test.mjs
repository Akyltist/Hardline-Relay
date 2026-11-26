import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import net from 'node:net'
import { apply } from '../index.mjs'

const token = 'f'.repeat(64)
async function freePort() {
  const s = net.createServer(); await new Promise(resolve => s.listen(0, '127.0.0.1', resolve))
  const { port } = s.address(); await new Promise(resolve => s.close(resolve)); return port
}
async function runReceiver(followup, action) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-receiver-test-'))
  const tokenFile = path.join(dir, 'token'); fs.writeFileSync(tokenFile, token)
  const port = await freePort(); let cleanup
  const logPath = path.join(dir, 'receiver.log')
  const ctx = { agents: new Map([['test-session', { followup }]]), effect(fn) { cleanup = fn() } }
  apply(ctx, {port, sessionId:'test-session', tokenFile, logPath, noticeText:'fixed notice'})
  async function send(nonce) {
    return fetch(`http://127.0.0.1:${port}/notice`, {method:'POST', headers:{authorization:`Bearer ${token}`, 'content-type':'application/json'}, body:JSON.stringify({nonce})})
  }
  try { await action(send); assert.ok(!fs.readFileSync(logPath, 'utf8').includes('sensitive')) } finally {
    await cleanup()
    for (const file of [tokenFile, logPath]) {
      if (fs.existsSync(file)) fs.unlinkSync(file)
    }
    fs.rmdirSync(dir)
  }
}

test('successful followup returns 202 and deduplicates', async () => {
  let calls = 0
  await runReceiver(() => { calls++ }, async send => {
    assert.equal((await send('1234567890abcdef')).status, 202)
    assert.equal((await send('1234567890abcdef')).status, 202)
    assert.equal(calls, 1)
  })
})
test('asynchronous rejection is unknown and does not log arbitrary error', async () => {
  await runReceiver(() => Promise.reject(new Error('sensitive')), async send => {
    assert.equal((await send('1234567890abcdef')).status, 500)
    assert.equal((await send('1234567890abcdef')).status, 500)
  })
})
test('timeout reports unknown while concurrent same nonce does not duplicate', async () => {
  let calls = 0
  await runReceiver(() => { calls++; return new Promise(() => {}) }, async send => {
    const first = send('1234567890abcdef')
    await new Promise(resolve => setTimeout(resolve, 100))
    assert.equal((await send('1234567890abcdef')).status, 500)
    assert.equal((await first).status, 500)
    assert.equal(calls, 1)
  })
})
