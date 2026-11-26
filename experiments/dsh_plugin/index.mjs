import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { startNoticeServer } from './core.mjs';

export const name = 'hardline-notice-experiment';
export const inject = ['agents'];

export async function apply(ctx, config) {
  await ctx.effect(async () => {
    const service = await startNoticeServer({ ctx, config, createUserMessage });
    return () => service.close();
  });
}
