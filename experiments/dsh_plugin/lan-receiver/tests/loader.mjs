export async function resolve(specifier, context, nextResolve) {
  if (specifier === '@deepseek-ai/dsh-llm') return { url: 'data:text/javascript,export const createUserMessage = x => x', shortCircuit: true }
  return nextResolve(specifier, context)
}
