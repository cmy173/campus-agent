/** Observe the public SDK/native Goal driver; never enqueue continuation prompts here. */
export async function runWithGoalLifecycle(harness, text, { sessionId, readGoal, onNotification, onGoalState,
  isCancelled = () => false }) {
  const subscription = harness.client.subscribeSessionTree(sessionId);
  let response = '', eventCount = 0, closing = false, transportError, turnError;
  const observeTurnError = event => {
    if (event?.type !== 'turn/end' || event.data?.reason?.kind !== 'error') return;
    // The SDK resolves at idle even when a native turn ended with an error.
    // Retain only its machine code; provider messages may contain sensitive text.
    const code = event.data.reason.error?.code;
    turnError = Object.assign(new Error('Harness native turn failed'), {
      code: typeof code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(code) ? code : 'HARNESS_FAILED',
    });
  };
  const notifications = (async () => {
    try {
      for await (const notification of subscription) {
        onNotification?.(notification);
        if (notification.method !== 'session.event') continue;
        eventCount++;
        const { sessionId: source, event } = notification.params;
        if (source === sessionId) observeTurnError(event);
        if (source === sessionId && event?.type === 'assistant/message') {
          const value = (event.data?.message?.content ?? []).filter(block => block.type === 'text')
            .map(block => block.text).join('\n');
          if (value.trim()) response = value;
        }
      }
    } catch (error) { if (!closing && !isCancelled()) transportError = error; }
  })();
  try {
    const result = await harness.run(text, { sessionId });
    for (const event of result.events ?? []) observeTurnError(event);
    response ||= result.finalResponse;
    let previous = '';
    while (!isCancelled()) {
      if (transportError) throw transportError;
      if (turnError) throw turnError;
      const state = await readGoal();
      const encoded = JSON.stringify(state);
      if (state.goal && encoded !== previous) onGoalState?.(state);
      previous = encoded;
      // run() settles at the initial idle boundary. The native round driver
      // alone owns subsequent prompts. Wait through its admission/idle gap.
      if (!state.goal || (state.agent_status === 'idle' &&
        (state.goal.phase !== 'active' || state.goal.activation === 'disarmed'))) break;
      await new Promise(done => setTimeout(done, 100));
    }
    if (transportError) throw transportError;
    if (turnError) throw turnError;
    return { ...result, finalResponse: response, eventCount };
  } finally {
    closing = true;
    subscription.close();
    await notifications;
  }
}
