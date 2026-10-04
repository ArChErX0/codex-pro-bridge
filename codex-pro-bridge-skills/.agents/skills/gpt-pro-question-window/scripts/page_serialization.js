function bridgePageSerialized(userId, assistantId, url, owner, ownerKey) {
  if (location.href !== url || sessionStorage.getItem(ownerKey) !== owner)
    throw Error('Raw serialization owner/URL conflict');
  function record(id) {
    const found = [];
    for (const n of document.querySelectorAll('[data-message-id]')) {
      if (n.getAttribute('data-message-id') !== id) continue;
      const key = Object.keys(n).find(k => k.startsWith('__reactFiber$'));
      const props = n[key]?.memoizedProps?.children?.[0]?.props;
      const m = props?.message;
      if (!m || m.id !== id) continue;
      // These are raw serialized relationships; DOM proximity is never a parent.
      const node = props.node && props.node.message === m ? props.node : null;
      const value = JSON.parse(JSON.stringify({message: m, node}));
      if (!found.some(v => JSON.stringify(v) === JSON.stringify(value))) found.push(value);
    }
    if (found.length !== 1) throw Error('Raw message serialization unavailable or ambiguous');
    return found[0];
  }
  return {source: 'react-message-props/v1', conversation_url: location.href,
    owner_token: sessionStorage.getItem(ownerKey), observed_at: new Date().toISOString(),
    complete: true, truncated: false, messages: [record(userId), record(assistantId)]};
}
