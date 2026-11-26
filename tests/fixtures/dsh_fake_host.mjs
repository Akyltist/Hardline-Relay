// Test process only: this is not a DeepSeek Harness host.
import { startNoticeServer } from '../../experiments/dsh_plugin/core.mjs';

let delivered = 0;
const ctx = { agents: { get(id) {
  if (id !== 'test-session') return undefined;
  return { followup(message) { delivered++; process.stdout.write(JSON.stringify({ event: 'followup', message }) + '\n'); } };
} } };
const service = await startNoticeServer({
  ctx,
  config: { sessionId: 'test-session', agent: process.env.TEST_NOTICE_AGENT ?? 'codex:berlin-test',
            secret: process.env.TEST_NOTICE_SECRET, port: 0 },
  createUserMessage: value => ({ ...value, role: 'user' }),
});
process.stdout.write(JSON.stringify({ event: 'ready', port: service.address.port }) + '\n');
process.stdin.resume();
process.stdin.on('end', async () => {
  await service.close();
  process.stdout.write(JSON.stringify({ event: 'closed', delivered }) + '\n');
  process.exit(0);
});
