// Observe only the clipboard API call produced by one visible Copy action.
// Forward the real operation unchanged and restore every descriptor in finally.
// No ChatGPT application state, hidden APIs, or network traffic is read.
async function bridgeCopyWithProof(id, role, expectedUrl, expectedOwner, ownerKey) {
  const assertIdentity = () => {
    if (location.href !== expectedUrl || sessionStorage.getItem(ownerKey) !== expectedOwner)
      throw Error('Copy owner/URL changed');
    if (!document.hasFocus()) throw Error('Copy document lost focus');
  };
  assertIdentity();
  const record = bridgePinnedMessage(id, role);
  const body = bridgeMessageBody(record), button = bridgeCopyButton(record);
  if (!body || !button || button.disabled || !button.getClientRects().length)
    throw Error('Pinned Copy control unavailable');
  const bodyText = body.textContent;
  const clipboard = navigator.clipboard;
  if (!clipboard) throw Error('Clipboard API unavailable for proven Copy');
  const descriptors = new Map();
  let writes = 0, copied = null, failed = null, method = '';
  try {
    for (const name of ['writeText', 'write']) {
      const original = clipboard[name];
      if (typeof original !== 'function') continue;
      descriptors.set(name, Object.getOwnPropertyDescriptor(clipboard, name));
      Object.defineProperty(clipboard, name, {configurable:true, writable:true, value:async function(value) {
        writes += 1;
        try {
          const result = await Reflect.apply(original, this, [value]);
          let text;
          if (name === 'writeText') text = String(value);
          else {
            const plain = [...value].filter(item => item.types.includes('text/plain'));
            if (plain.length !== 1) throw Error('Copy does not have one plain-text payload');
            text = await (await plain[0].getType('text/plain')).text();
          }
          copied = text; method = name;
          return result;
        } catch (error) { failed = String(error); throw error; }
      }});
    }
    if (!descriptors.size) throw Error('Clipboard write observation unsupported');
    button.click();
    const deadline = Date.now() + 10000;
    while (copied === null && failed === null && Date.now() < deadline)
      await new Promise(resolve => setTimeout(resolve, 25));
    assertIdentity();
    const current = bridgeMessageBody(bridgePinnedMessage(id, role));
    if (!current || current.textContent !== bodyText) throw Error('Pinned message changed during Copy');
    if (failed) throw Error('Copy write failed: ' + failed);
    if (writes !== 1 || copied === null || !copied.trim())
      throw Error('One successful clipboard write was not proven');
    return {text:copied, message_id:id, role, method, writes, provenance:'visible-copy-write/v1',
      literal_user_text:role === 'user' && !!body.querySelector('[data-markdown-text-tone="user-message"]')};
  } finally {
    for (const [name, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(clipboard, name, descriptor);
      else delete clipboard[name];
    }
  }
}
