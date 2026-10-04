// Read rendered editor content, excluding only editor-owned decorative widgets.
// innerText inserts a layout newline before the new autolink icon; do not erase
// arbitrary whitespace from the user's question to compensate for that artifact.
function bridgeComposerText(element) {
  if (typeof element.value === 'string') return element.value.trim();
  const read = node => {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return '';
    if (node.classList.contains('ProseMirror-trailingBreak') ||
        (node.classList.contains('ProseMirror-widget') && node.getAttribute('aria-hidden') === 'true')) return '';
    if (node.tagName === 'BR') return '\n';
    return [...node.childNodes].map(read).join('');
  };
  const blocks = [...element.children];
  if (blocks.length && blocks.every(node => node.tagName === 'P'))
    return blocks.map(read).join('\n\n').trim();
  // Unknown rich editor layouts are not guessed. Preserve the earlier gate.
  return (element.innerText || '').trim();
}
