// Crosshair + tooltip for the typical-price line. Snaps to the nearest day.
document.querySelectorAll("figure.chart").forEach((fig) => {
  const pts = JSON.parse(fig.dataset.points);
  const svg = fig.querySelector("svg"), hit = fig.querySelector(".hit");
  const cross = fig.querySelector(".crosshair"), dot = fig.querySelector(".dot"), tip = fig.querySelector(".tip");
  function show(evt) {
    const box = svg.getBoundingClientRect();
    const x = (evt.clientX - box.left) * (svg.viewBox.baseVal.width / box.width);
    const p = pts.reduce((a, b) => (Math.abs(b.x - x) < Math.abs(a.x - x) ? b : a));
    cross.setAttribute("x1", p.x); cross.setAttribute("x2", p.x);
    dot.setAttribute("cx", p.x); dot.setAttribute("cy", p.y);
    vis(true);
    tip.textContent = `${p.day}: ${p.median} (${p.samples} samples)`;
    const left = (p.x / svg.viewBox.baseVal.width) * box.width;
    tip.style.left = `${Math.min(Math.max(left, 70), box.width - 70)}px`;
  }
  // SVG elements have no .hidden property, so toggle visibility
  function vis(on) { for (const el of [cross, dot]) el.setAttribute("visibility", on ? "visible" : "hidden"); tip.hidden = !on; }
  function hide() { vis(false); }
  hide();
  hit.addEventListener("pointermove", show);
  hit.addEventListener("pointerdown", show);
  hit.addEventListener("pointerleave", hide);
});
