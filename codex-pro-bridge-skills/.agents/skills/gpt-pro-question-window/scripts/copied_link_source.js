// Read-only logical-link source observation for the new search-unit UI.
// This never clicks, navigates, expands a citation, or reads hidden network state.
function bridgeCopiedLinkSource(userId, assistantId, expectedUrl, expectedOwner, ownerKey, pageId) {
  if (location.href !== expectedUrl || sessionStorage.getItem(ownerKey) !== expectedOwner)
    throw Error('Copied-link source owner/URL conflict');
  const records = bridgeMessages();
  const user = records.filter(r => r.id === userId && r.role === 'user');
  const assistant = records.filter(r => r.id === assistantId && r.role === 'assistant');
  if (user.length !== 1 || assistant.length !== 1)
    throw Error('Copied-link source fixed records are missing or ambiguous');
  if (records.some(r => !['user', 'assistant'].includes(r.role)))
    throw Error('Copied-link source contains an unknown message role');

  const body = bridgeMessageBody(assistant[0]);
  if (!body || !body.textContent.trim()) throw Error('Copied-link source assistant body is incomplete');
  const stop = document.querySelector(
    '[data-testid="stop-button"],button[aria-label="Stop generating"],button[aria-label="停止回答"],button[aria-label="停止"],button[aria-label="Stop"]');
  const copy = bridgeCopyButton(assistant[0]);
  if (!copy || copy.disabled || !copy.getClientRects().length || stop)
    throw Error('Copied-link source assistant is not ready');

  // Legacy message nodes do not carry the new item/source shape. Preserve the
  // existing copied-link equality route for them without guessing source data.
  if (assistant[0].legacy || user[0].legacy)
    return {status: 'unsupported', reason: 'legacy-message-dom', url: location.href,
      owner: sessionStorage.getItem(ownerKey), page_id: String(pageId)};

  const compact = value => {
    try { return JSON.parse(JSON.stringify(value)); }
    catch (error) { throw Error('Copied-link source contains unserializable React data: ' + error); }
  };
  const locate = record => {
    let fiber = null;
    for (const key of Object.keys(record.node))
      if (key.startsWith('__reactFiber$')) { fiber = record.node[key]; break; }
    let itemFrame = null, turnFrame = null, entryFrame = null;
    for (let depth = 0; fiber && depth < 80; depth++, fiber = fiber.return) {
      const props = fiber.memoizedProps;
      if (!props || typeof props !== 'object') continue;
      if (!itemFrame && props.item && props.item.messageId === record.id)
        itemFrame = {depth, props};
      if (!turnFrame && props.turn && typeof props.turn === 'object')
        turnFrame = {depth, props};
      if (!entryFrame && props.entry && typeof props.entry === 'object')
        entryFrame = {depth, props};
    }
    if (!itemFrame || !turnFrame || !entryFrame)
      throw Error('Copied-link source React item/turn relationship is unavailable');
    const item = itemFrame.props.item;
    const turn = turnFrame.props.turn;
    const entry = entryFrame.props.entry;
    if (!entry.id || !turn || !Array.isArray(turn.items))
      throw Error('Copied-link source turn key/items are unavailable');
    const items = turn.items.map(value => {
      if (!value || typeof value !== 'object') throw Error('Copied-link source turn item is malformed');
      return {
        type: value.type || null,
        message_id: value.messageId || null,
        completed: value.completed === true,
        phase: value.phase || null,
      };
    });
    return {itemFrameDepth: itemFrame.depth, turnFrameDepth: turnFrame.depth,
      entryFrameDepth: entryFrame.depth, item, turn, entry, items};
  };
  const userSource = locate(user[0]);
  const assistantSource = locate(assistant[0]);
  const turnIdentity = source => ({
    key: source.entry.id,
    entry_id: source.entry.id,
    entry_turn_key: source.entry.turnKey || '',
    status: source.turn.status || '',
    items: source.items,
  });
  const userTurn = turnIdentity(userSource);
  const assistantTurn = turnIdentity(assistantSource);
  if (JSON.stringify(userTurn) !== JSON.stringify(assistantTurn))
    throw Error('Copied-link source user/assistant turn or entry relation changed');
  const userItem = userSource.item;
  const assistantItem = assistantSource.item;
  const turn = assistantSource.turn;
  const turnItems = assistantSource.items;
  const domAnchors = [...body.querySelectorAll('a')].map(node => ({
    href: node.href || '', text: (node.innerText || '').trim(),
    aria: node.getAttribute('aria-label') || '', testid: node.getAttribute('data-testid') || '',
  }));
  const resourceRows = [...body.querySelectorAll('[class~="group/resource-row"]')]
    .filter(node => !node.closest('a'))
    .map(node => ({
      filename: node.querySelector('[title]')?.getAttribute('title') || '',
      titles: [...node.querySelectorAll('[title]')].map(n => n.getAttribute('title') || ''),
      download_buttons: [...node.querySelectorAll('button[aria-label="下载文件"],button[aria-label="Download file"]')].length,
      has_library_icon: !!node.querySelector('[data-testid="library-file-icon"]'),
    }));
  const sandboxButtons = [...body.querySelectorAll('button')]
    .filter(node => !node.closest('a') && node.querySelector('[data-testid="library-file-icon"]')).length;
  return {
    status: 'available', schema: 'copied-link-source/v1', source_route: 'new-ui-fiber-item',
    conversation_url: location.href, owner_token: sessionStorage.getItem(ownerKey),
    page_id: String(pageId), observed_at: new Date().toISOString(), complete: true, truncated: false,
    records: {user: userId, assistant: assistantId},
    user: {id: userId, type: userItem.type || '', messageId: userItem.messageId || '',
      message: typeof userItem.message === 'string' ? userItem.message : ''},
    assistant: {id: assistantId, type: assistantItem.type || '',
      completed: assistantItem.completed === true, phase: assistantItem.phase || '',
      latest_message_id: assistantItem.latestMessageId || '',
      source_message_ids: Array.isArray(assistantItem.sourceMessageIds) ? assistantItem.sourceMessageIds : [],
      content: typeof assistantItem.content === 'string' ? assistantItem.content : '',
      content_references: Array.isArray(assistantItem.contentReferences) ? compact(assistantItem.contentReferences) : [],
      content_reference_message_ids: Array.isArray(assistantItem.contentReferenceMessageIds) ? assistantItem.contentReferenceMessageIds : [],
      content_reference_message_statuses: Array.isArray(assistantItem.contentReferenceMessageStatuses) ? assistantItem.contentReferenceMessageStatuses : [],
      turn_exchange_id: assistantItem.turnExchangeId || '',
    },
    turn: {...assistantTurn,
      first_server_message_id: turn.firstServerMessageId || '', message_ids: Array.isArray(turn.messageIds) ? turn.messageIds : []},
    user_turn: userTurn,
    dom: {anchors: domAnchors, resource_rows: resourceRows, sandbox_buttons: sandboxButtons,
      structural_links: domAnchors.length + resourceRows.length + sandboxButtons},
    readiness: {assistant_body_found: true, assistant_body_text_length: body.textContent.length,
      assistant_id: assistantId, copy_enabled: true, generating: false},
  };
}
