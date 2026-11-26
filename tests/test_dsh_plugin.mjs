import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { test } from 'node:test';
import { startNoticeServer } from '../experiments/dsh_plugin/core.mjs';

const secret = randomBytes(32).toString('hex');
const config = { sessionId: 'isolated-test-session', agent: 'codex', route: 'test-route', secret, port: 0 };
const valid = { nonce: '0123456789abcdef', agent: 'codex', route: 'test-route' };

async function fixture(followup = () => {}, options = {}) {
  const lookedUp = [];
  const messages = [];
  const ctx = { agents: { get(id) { lookedUp.push(id); return options.missing ? undefined : { followup(message) { messages.push(message); return followup(message); } }; } } };
  const service = await startNoticeServer({ ctx, config, createUserMessage: input => ({ ...input, role: 'user' }) });
  const base = `http://127.0.0.1:${service.address.port}`;
  const send = async (body = valid, opts = {}) => fetch(`${base}${opts.path ?? '/notice'}`, {
    method: opts.method ?? 'POST',
    headers: { authorization: opts.auth ?? `Bearer ${secret}`, 'content-type': opts.type ?? 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  return { service, ctx, lookedUp, messages, base, send };
}

test('only loopback listens and valid request reaches only configured session', async t => {
  const f = await fixture();
  t.after(() => f.service.close());
  assert.equal(f.service.address.address, '127.0.0.1');
  assert.equal((await f.send()).status, 202);
  assert.deepEqual(f.lookedUp, [config.sessionId]);
  assert.equal(f.messages.length, 1);
  assert.equal(f.messages[0].source.kind, 'hardline');
  assert.equal(f.messages[0].source.form, 'notice');
  assert.equal(f.messages[0].role, 'user');
  assert.match(f.messages[0].content[0].text, /inbox\(agent='codex'/);
  assert.match(f.messages[0].content[0].text, /0123456789abcdef/);
  assert.match(f.messages[0].content[0].text, /auto_ack=false/);
  assert.match(f.messages[0].content[0].text, /receipt='0123456789abcdef'/);
});

test('repeated and concurrent notices with the same nonce wake only once', async t => {
  const f = await fixture(async () => { await new Promise(resolve => setTimeout(resolve, 30)); });
  t.after(() => f.service.close());
  const responses = await Promise.all([f.send(), f.send(), f.send()]);
  assert.deepEqual(responses.map(response => response.status), [202, 202, 202]);
  assert.equal(f.messages.length, 1);
  assert.equal((await f.send()).status, 202);
  assert.equal(f.messages.length, 1);
});

test('authentication, endpoint, content type and strict input reject without looking up a session', async t => {
  const f = await fixture();
  t.after(() => f.service.close());
  assert.equal((await f.send(valid, { auth: 'Bearer wrong' })).status, 401);
  assert.equal((await f.send(valid, { path: '/other' })).status, 404);
  assert.equal((await f.send(valid, { type: 'text/plain' })).status, 415);
  for (const body of ['{', '{}', 'null', '[]', '{"nonce":"0123456789abcdeF"}', '{"nonce":"0123"}', '{"nonce":"0123456789abcdef","message":"injected"}', JSON.stringify({ nonce: '0'.repeat(200) })]) {
    assert.ok([400, 413].includes((await f.send(body)).status));
  }
  assert.deepEqual(f.lookedUp, []);
  assert.equal(f.messages.length, 0);
});

test('unknown live target is not resumed and is rejected', async t => {
  const f = await fixture(undefined, { missing: true });
  t.after(() => f.service.close());
  assert.equal((await f.send()).status, 409);
  assert.equal(f.messages.length, 0);
  assert.deepEqual(f.lookedUp, [config.sessionId]);
});

test('different route or agent cannot wake this session', async t => {
  const f = await fixture();
  t.after(() => f.service.close());
  assert.equal((await f.send({ ...valid, route: 'other-route' })).status, 403);
  assert.equal((await f.send({ ...valid, agent: 'claude' })).status, 403);
  assert.deepEqual(f.lookedUp, []);
  assert.equal(f.messages.length, 0);
  assert.equal((await f.send()).status, 202);
  assert.equal(f.messages.length, 1);
});

test('followup errors and uncertain async acceptance return 500', async t => {
  const f = await fixture(async () => { throw new Error('private detail'); });
  t.after(() => f.service.close());
  const response = await f.send();
  assert.equal(response.status, 500);
  assert.equal(await response.text(), '');
  assert.equal(f.messages.length, 1);
  assert.equal((await f.send()).status, 500);
  assert.equal(f.messages.length, 1);
});

test('close shuts down listening endpoint', async () => {
  const f = await fixture();
  await f.service.close();
  await assert.rejects(f.send());
});

test('refuses missing or invalid config before listening', async () => {
  for (const patch of [{ secret: 'short' }, { sessionId: '' }, { agent: 'codex:other' }, { route: '' }, { route: 'bad route' }, { port: -1 }]) {
    await assert.rejects(startNoticeServer({ ctx: { agents: { get() {} } }, config: { ...config, ...patch }, createUserMessage: x => x }), TypeError);
  }
});
