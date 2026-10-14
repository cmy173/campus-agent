/** Observe the public SDK/native Goal driver; never enqueue continuation prompts here. */
export async function runWithGoalLifecycle(harness, text, { sessionId, readGoal, onNotification, onGoalState,
  isCancelled = () => false }) {
  const subscription = harness.client.subscribeSessionTree(sessionId);
  let response = '', eventCount = 0, closing = false, transportError;
  const notifications = (async () => {
    try {
      for await (const notification of subscription) {
        onNotification?.(notification);
        if (notification.method !== 'session.event') continue;
        eventCount++;
        const { sessionId: source, event } = notification.params;
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
    response ||= result.finalResponse;
    let previous = '';
    while (!isCancelled()) {
      if (transportError) throw transportError;
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
    return { ...result, finalResponse: response, eventCount };
  } finally {
    closing = true;
    subscription.close();
    await notifications;
  }
}
