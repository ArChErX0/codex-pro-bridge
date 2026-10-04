// Shared by submission, passive waiting and Copy capture. Do not infer identity
// from text, position, or the search-unit index; only accept a unique message ID.
function bridgeMessages() {
  const legacy = Array.from(document.querySelectorAll('[data-message-author-role][data-message-id]'));
  if (legacy.length) return legacy.map(node => ({node,
    id: node.getAttribute('data-message-id'), role: node.getAttribute('data-message-author-role'), legacy: true}));
  const records = Array.from(document.querySelectorAll('[data-chatgpt-search-message-ids][data-chatgpt-search-unit-key]')).map(node => {
    const role = (node.getAttribute('data-chatgpt-search-unit-key') || '').split(':').pop();
    const ids = [...new Set((node.getAttribute('data-chatgpt-search-message-ids') || '').trim().split(/\s+/).filter(Boolean))];
    if (!['user', 'assistant'].includes(role)) return null;
    if (ids.length !== 1) throw Error('Ambiguous message IDs');
    return {node, id: ids[0], role, legacy: false};
  }).filter(Boolean);
  if (new Set(records.map(r => r.id)).size !== records.length) throw Error('Duplicate message identity');
  return records;
}

function bridgePinnedMessage(id, role) {
  const found = bridgeMessages().filter(r => r.id === id && r.role === role);
  if (found.length !== 1) throw Error('Pinned message missing or ambiguous');
  return found[0];
}

function bridgeMessageBody(record) {
  if (record.legacy) return record.node.querySelector(record.role === 'assistant' ? '.markdown' : '.whitespace-pre-wrap') ||
    (record.role === 'user' ? record.node : null);
  if (record.role === 'user') return record.node.querySelector('[data-content-search-unit-key]');
  const bodies = Array.from(record.node.querySelectorAll('[data-chatgpt-selection-message-id]'))
    .filter(n => n.getAttribute('data-chatgpt-selection-message-id') === record.id);
  return bodies.length === 1 ? bodies[0] : null;
}

function bridgeCopyButton(record) {
  if (record.legacy) return record.node.closest('[data-testid^="conversation-turn-"]')?.querySelector(
    'button[data-testid="copy-turn-action-button"],button[aria-label="Copy response"],button[aria-label="复制回复"]');
  const selector = record.role === 'user' ? 'button[aria-label="复制消息"],button[aria-label="Copy message"]' :
    'button[aria-label="复制"],button[aria-label="Copy"],button[aria-label="复制回复"],button[aria-label="Copy response"]';
  const peers = bridgeMessages().filter(r => r.role === record.role && r.id !== record.id);
  let scope = record.role === 'user' ? record.node : record.node.parentElement;
  for (let depth = 0; scope && depth < 6; depth++, scope = scope.parentElement) {
    if (peers.some(r => scope.contains(r.node))) return null;
    const buttons = Array.from(scope.querySelectorAll(selector))
      .filter(n => (record.role === 'user' || !record.node.contains(n)) && n.getClientRects().length && !n.disabled);
    if (buttons.length > 1) return null;
    if (buttons.length === 1) return buttons[0];
  }
  return null;
}

async function waitForReply(config) {
  const batchEnd = Date.now() + 55000;
  const deadline = config.deadline ? Date.parse(config.deadline) : null;
  const result = (status, extra = {}) => ({
    status, attempt_id: config.attempt_id, remote_turn_id: config.remote_turn_id,
    ...extra
  });
  while (true) {
    if (location.href !== config.conversation_url ||
        sessionStorage.getItem('codex-pro-bridge.tab-owner.v1') !== config.tab_owner_token) {
      return result('identity-changed');
    }
    const messages = bridgeMessages();
    const userIndex = messages.findIndex(node =>
      node.role === 'user' && node.id === config.remote_turn_id);
    if (userIndex < 0) return result('target-not-visible');
    const answers = [];
    for (const node of messages.slice(userIndex + 1)) {
      const role = node.role;
      if (role === 'user') break;
      if (role === 'assistant') answers.push(node);
    }
    if (answers.length > 1) return result('ambiguous-answer');
    if (answers.length === 1) {
      const answer = answers[0];
      const copy = bridgeCopyButton(answer);
      const body = bridgeMessageBody(answer);
      const generating = document.querySelector(
        '[data-testid="stop-button"],button[aria-label="Stop generating"],button[aria-label="停止回答"],button[aria-label="停止"],button[aria-label="Stop"]');
      if (copy && !copy.disabled && body?.textContent.trim() && !generating) {
        return result('ready-for-capture', {
          assistant_turn_id: answer.id,
          observed_at: new Date().toISOString()
        });
      }
    }
    if (deadline !== null && Date.now() >= deadline) return result('deadline');
    if (Date.now() >= batchEnd) return result('pending');
    const remaining = deadline === null
      ? batchEnd - Date.now()
      : Math.min(deadline - Date.now(), batchEnd - Date.now());
    await new Promise(resolve => setTimeout(resolve,
      Math.min(1000, remaining)));
  }
}
