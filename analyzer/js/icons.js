// SVG icon helper. The icons themselves are CSS classes in css/icons.css
// (generated from tools/icons/*.svg by tools/build_icons.py).
//
//   el.innerHTML = KestrelIcon('gear') + ' Settings';
//   button.prepend(KestrelIcon.el('upload'));
(function () {
  const NAME = /^[a-z0-9-]+$/;

  function cls(name) {
    if (!NAME.test(name)) throw new Error(`KestrelIcon: bad icon name "${name}"`);
    return `k-icon k-icon--${name}`;
  }

  /** Markup for an icon, decorative (hidden from screen readers). */
  function KestrelIcon(name) {
    return `<i class="${cls(name)}" aria-hidden="true"></i>`;
  }

  /** The same icon as a DOM element. */
  KestrelIcon.el = function (name) {
    const i = document.createElement('i');
    i.className = cls(name);
    i.setAttribute('aria-hidden', 'true');
    return i;
  };

  /** Point an existing icon element at another icon, keeping its other classes. */
  KestrelIcon.set = function (el, name) {
    if (!el) return;
    cls(name);
    for (const c of [...el.classList]) if (c.startsWith('k-icon--')) el.classList.remove(c);
    el.classList.add('k-icon', `k-icon--${name}`);
  };

  window.KestrelIcon = KestrelIcon;
})();
