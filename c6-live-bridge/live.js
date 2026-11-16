/* Live feeds: lists that keep growing while someone reads them.

   Each feed is a bounded scroll area, so new rows never push the rest of the page down.
   Rows are rebuilt on every update; each row carries a stable `rowKey` (and `rowOpen` when it
   is expanded but is not a <details>). LiveFeed.render() redraws a feed without moving what
   the reader is looking at:
   - an expanded row that is visible stays at the same place on screen, and the scroll
     position of the boxes inside it is carried over to the rebuilt row;
   - once the reader has scrolled into the feed, the first visible row is held instead;
   - at the top of a feed with nothing expanded, new rows appear at the top as usual.
   A status bar above the feed says when the view is held and how many rows arrived above,
   with a button to jump back to the latest. */
var LiveFeed = (() => {
  const state = new WeakMap();
  const isOpen = r => !!(r.open || r.rowOpen);
  const keyed = list => [...list.children].filter(r => r.rowKey !== undefined);
  const scrolls = row => row.querySelectorAll ? [...row.querySelectorAll('pre,textarea')] : [];

  function bar(box) {
    let s = state.get(box);
    if (s) return s;
    s = {pending: 0, bar: null, text: null, list: null};
    state.set(box, s);
    if (!box.parentElement || !box.insertAdjacentElement) return s;
    s.bar = document.createElement('div'); s.bar.className = 'feed-bar';
    s.text = document.createElement('span'); s.text.setAttribute('role', 'status');
    const jump = document.createElement('button'); jump.type = 'button'; jump.className = 'plain'; jump.textContent = 'Jump to latest';
    jump.onclick = () => { box.scrollTop = 0; s.pending = 0; show(box, s); };
    s.bar.append(s.text, jump);
    box.insertAdjacentElement('beforebegin', s.bar);
    box.addEventListener('scroll', () => { if (box.scrollTop <= 1 && s.pending) { s.pending = 0; } show(box, s); }, {passive: true});
    show(box, s);
    return s;
  }
  function show(box, s) {
    if (!s.bar) return;
    const held = box.scrollTop > 1 || keyed(s.list || box).some(isOpen);
    s.bar.classList.toggle('held', s.pending > 0);
    s.text.textContent = s.pending > 0 ? `Holding your place · ${s.pending} new above` :
      held ? 'Holding your place while you read' : 'Live · newest first';
  }

  // box: the bounded scroll area. list: the element whose children are the rows (box itself, or a tbody inside it).
  function render(box, list, draw) {
    if (!box || typeof box.getBoundingClientRect !== 'function') return draw();
    const s = bar(box); s.list = list;
    const before = keyed(list), seen = new Set(before.map(r => r.rowKey));
    const area = box.getBoundingClientRect(), page = document.documentElement;
    const visible = r => { const b = r.getBoundingClientRect(); return b.height > 0 && b.bottom > area.top && b.top < area.bottom; };
    const anchor = before.find(r => isOpen(r) && visible(r)) || (box.scrollTop > 1 ? before.find(visible) : null);
    const top = anchor ? anchor.getBoundingClientRect().top : 0;
    const inner = new Map(before.filter(isOpen).map(r => [r.rowKey, scrolls(r).map(x => [x.scrollTop, x.scrollLeft])]));
    // The reader is looking below this feed when it starts above the screen or ends in the top half of it.
    const readerBelow = area.height > 0 && (area.top < 0 || area.bottom < (window.innerHeight || page.clientHeight) / 2), height = area.height;

    draw();

    const after = keyed(list);
    for (const r of after) {
      const saved = inner.get(r.rowKey);
      if (saved && isOpen(r)) scrolls(r).forEach((x, i) => { if (saved[i]) [x.scrollTop, x.scrollLeft] = saved[i]; });
    }
    const now = anchor && after.find(r => r.rowKey === anchor.rowKey);
    if (now) {
      // Scroll the feed first; whatever the feed cannot absorb (it has not filled up yet) goes to the page.
      let shift = now.getBoundingClientRect().top - top;
      if (shift) { box.scrollTop += shift; shift = now.getBoundingClientRect().top - top; }
      if (Math.abs(shift) >= 1) window.scrollBy(0, shift);
      s.pending += after.filter(r => !seen.has(r.rowKey) && !r.rowDetail).length;
    } else {
      if (box.scrollTop <= 1) s.pending = 0;
      // Until the feed fills its height it grows; keep what the reader is looking at below it still.
      const grew = box.getBoundingClientRect().height - height;
      if (readerBelow && grew) window.scrollBy(0, grew);
    }
    show(box, s);
  }
  return {render};
})();
