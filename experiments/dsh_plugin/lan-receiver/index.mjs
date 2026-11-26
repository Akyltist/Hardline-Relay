/**
 * Opt-in local receiver for the network pilot (DeepSeek Harness side).
 *
 * Contract with the co-located network MCP process:
 *   POST http://127.0.0.1:<port>/notice
 *   Authorization: Bearer <token from a local file, never from chat/logs>
 *   Content-Type: application/json
 *   Body: {"nonce":"<16 hex>"}   — exactly this one field; no mail content.
 *
 * Behaviour:
 *   202 — the notice was queued into the pinned session (followup returned).
 *   400 — malformed body/field set; 401 — token; 404/405 — route/method;
 *   409 — the pinned session is not live (no other session is woken);
 *   413 — oversized body; 500 — followup threw (outcome unknown, never a 202).
 * The text is constant and never carries a mail body. `202` is not a receipt.
 */
import fs from 'node:fs'
import { createServer } from 'node:http'
import { timingSafeEqual } from 'node:crypto'
import { createUserMessage } from '@deepseek-ai/dsh-llm'

export const name = 'lan-receiver'
export const inject = ['agents']

const NONCE = /^[0-9a-f]{16}$/
const MAX_BODY = 128
const RETENTION_MS = 24 * 60 * 60 * 1000
const MAX_NONCES = 1024
const UNKNOWN_RETRY_MS = 60 * 1000
const FOLLOWUP_TIMEOUT_MS = 10 * 1000

export function apply(ctx, config) {
  const port = Number(config?.port ?? 0)
  const sessionId = String(config?.sessionId ?? '')
  const tokenFile = String(config?.tokenFile ?? '')
  const logPath = String(config?.logPath ?? '')
  const noticeText = String(config?.noticeText ?? '')
  if (!noticeText.trim()) throw new TypeError('lan-receiver: noticeText is required and must name the pilot lane to read')

  if (!sessionId) throw new TypeError('lan-receiver: sessionId is required')
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new TypeError('lan-receiver: port must be 1024..65535')
  if (!tokenFile) throw new TypeError('lan-receiver: tokenFile is required')

  let token
  try {
    token = fs.readFileSync(tokenFile, 'utf8').trim()
  } catch {
    throw new TypeError('lan-receiver: tokenFile is not readable')
  }
  if (!/^[a-f0-9]{64}$/.test(token)) throw new TypeError('lan-receiver: tokenFile must hold 64 lowercase hex characters')

  const expected = Buffer.from(`Bearer ${token}`, 'utf8')
  const seen = new Map()
  let stopping = false

  const log = (event) => {
    if (!logPath) return
    try {
      fs.appendFileSync(logPath, JSON.stringify({ at: new Date().toISOString(), ...event }) + '\n')
    } catch {
      // Logging must never break delivery.
    }
  }

  const server = createServer(async (req, res) => {
    const finish = (status) => {
      if (!res.headersSent && !res.destroyed) res.writeHead(status, { 'Cache-Control': 'no-store' }).end()
    }
    try {
      if (stopping) return finish(503)
      if (req.method !== 'POST' || req.url !== '/notice') return finish(404)
      const supplied = Buffer.from(String(req.headers.authorization ?? ''), 'utf8')
      if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) return finish(401)
      if (req.headers['content-type'] !== 'application/json') return finish(415)
      const chunks = []
      let length = 0
      for await (const chunk of req) {
        length += chunk.length
        if (length > MAX_BODY) return finish(413)
        chunks.push(chunk)
      }
      let payload
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      } catch {
        return finish(400)
      }
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return finish(400)
      const keys = Object.keys(payload)
      if (keys.length !== 1 || keys[0] !== 'nonce' || !NONCE.test(String(payload.nonce))) return finish(400)
      const nonce = String(payload.nonce)
      const prior = seen.get(nonce)
      if (prior && prior.status === 'queued') {
        log({ kind: 'duplicate', nonce })
        return finish(202)
      }
      if (prior && prior.status === 'pending') return finish(500)
      if (prior && prior.status === 'unknown' && Date.now() - prior.at < UNKNOWN_RETRY_MS) {
        // An indeterminate outcome is retried after a wait, never immediately:
        // an immediate retry could wake the pinned chat twice.
        log({ kind: 'unknown-retry-too-soon', nonce })
        return finish(500)
      }
      const agent = ctx.agents.get(sessionId)
      if (!agent || typeof agent.followup !== 'function') {
        log({ kind: 'no-session', nonce, sessionId })
        return finish(409)
      }
      seen.set(nonce, { status: 'pending', at: Date.now() })
      let timer
      try {
        const message = createUserMessage({
          content: [{ type: 'text', text: noticeText }],
          source: { kind: 'lan-signal', form: 'notice', summary: 'LAN pilot mail' },
        })
        await Promise.race([
          Promise.resolve().then(() => agent.followup(message)),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), FOLLOWUP_TIMEOUT_MS) }),
        ])
      } catch {
        // The recipient may have accepted the message before failing or timing out.
        seen.set(nonce, { status: 'unknown', at: Date.now() })
        log({ kind: 'unknown', nonce })
        return finish(500)
      } finally {
        clearTimeout(timer)
      }
      seen.set(nonce, { status: 'queued', at: Date.now() })
      const now = Date.now()
      for (const [key, entry] of seen) {
        if (now - Number(entry?.at ?? 0) > RETENTION_MS) seen.delete(key)
      }
      for (const key of [...seen.keys()].slice(0, Math.max(0, seen.size - MAX_NONCES))) seen.delete(key)
      log({ kind: 'queued', nonce, sessionId })
      finish(202)
    } catch {
      log({ kind: 'error' })
      finish(500)
    }
  })
  server.requestTimeout = 5000
  server.headersTimeout = 5000
  server.once('error', () => log({ kind: 'listen-error' }))

  ctx.effect(() => {
    server.listen({ host: '127.0.0.1', port, exclusive: true })
    log({ kind: 'started', port })
    return () => {
      stopping = true
      return new Promise((resolve) => server.close(() => resolve()))
    }
  })
}
