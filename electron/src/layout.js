const preferenceKey = 'hkuprint-sidebar-ratio';
const defaultRatio = 0.28;

export function setupLayout() {
  const $ = (id) => document.getElementById(id);
  const workspace = $('workspace'), sidebar = $('sidebar');
  const divider = $('workspace-divider'), popover = $('layout-popover');
  const percent = $('sidebar-percent'), slider = $('sidebar-width');
  let ratio = defaultRatio, bounds, drag, frame = 0;
  try {
    const stored = Number(localStorage.getItem(preferenceKey));
    if (stored >= 0.1 && stored <= 0.5) ratio = stored;
  } catch { /* Layout preferences are optional. */ }

  function measure() {
    const rect = workspace.getBoundingClientRect(), style = getComputedStyle(workspace);
    const left = parseFloat(style.paddingLeft), right = parseFloat(style.paddingRight);
    const available = workspace.clientWidth - left - right - divider.offsetWidth;
    const min = Math.min(340, available / 2);
    const max = Math.max(min, Math.min(available / 2, available - 680));
    return { available, min, max, left: rect.left + left };
  }

  function positionPopover() {
    if (!popover.matches(':popover-open')) return;
    const rect = $('layout-toggle').getBoundingClientRect();
    popover.style.left = `${Math.max(12, Math.min(innerWidth - popover.offsetWidth - 12, rect.right - popover.offsetWidth))}px`;
    popover.style.top = `${rect.bottom + 8}px`;
  }

  function apply() {
    bounds = measure();
    const width = Math.max(bounds.min, Math.min(bounds.max, ratio * bounds.available));
    workspace.style.setProperty('--sidebar-width', `${width}px`);
    const value = Math.round(width / bounds.available * 100);
    divider.setAttribute('aria-valuemin', Math.round(bounds.min / bounds.available * 100));
    divider.setAttribute('aria-valuemax', Math.round(bounds.max / bounds.available * 100));
    divider.setAttribute('aria-valuenow', value);
    divider.setAttribute('aria-valuetext', `参数区 ${value}%，预览区 ${100 - value}%`);
    percent.min = slider.min = Math.round(bounds.min / bounds.available * 100);
    percent.max = slider.max = Math.round(bounds.max / bounds.available * 100);
    percent.value = slider.value = value;
    positionPopover();
  }

  function save() {
    try { localStorage.setItem(preferenceKey, String(ratio)); } catch { /* Optional preference. */ }
  }

  function setRatio(value) {
    if (!Number.isFinite(value)) { apply(); return; }
    bounds = measure();
    ratio = Math.max(bounds.min, Math.min(bounds.max, value * bounds.available)) / bounds.available;
    apply(); save();
  }

  function reset() { ratio = defaultRatio; apply(); save(); }

  function finishDrag(event) {
    if (!drag || (event && event.pointerId !== drag.pointerId)) return;
    const pointerId = drag.pointerId;
    drag = null;
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
    apply(); save();
    document.body.classList.remove('resizing-panels');
    if (divider.hasPointerCapture(pointerId)) divider.releasePointerCapture(pointerId);
  }

  divider.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || drag) return;
    event.preventDefault();
    divider.focus();
    bounds = measure();
    drag = { pointerId: event.pointerId, startX: event.clientX, width: sidebar.getBoundingClientRect().width, ...bounds };
    divider.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing-panels');
  });
  function moveDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    ratio = Math.max(drag.min, Math.min(drag.max, drag.width + event.clientX - drag.startX)) / drag.available;
    workspace.style.setProperty('--sidebar-width', `${Math.max(drag.min, Math.min(drag.max, drag.width + event.clientX - drag.startX))}px`);
    if (!frame) frame = requestAnimationFrame(() => { frame = 0; apply(); });
  }
  document.addEventListener('pointermove', moveDrag, true);
  document.addEventListener('pointerup', finishDrag, true);
  document.addEventListener('pointercancel', finishDrag, true);
  divider.addEventListener('lostpointercapture', finishDrag);
  window.addEventListener('blur', () => finishDrag());
  divider.addEventListener('dblclick', reset);
  divider.addEventListener('keydown', (event) => {
    const current = sidebar.getBoundingClientRect().width / bounds.available;
    const step = event.shiftKey ? 0.05 : 0.01;
    if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); reset(); return; }
    const values = { ArrowLeft: current - step, ArrowRight: current + step,
      Home: bounds.min / bounds.available, End: bounds.max / bounds.available };
    if (Object.hasOwn(values, event.key)) {
      event.preventDefault(); event.stopPropagation(); setRatio(values[event.key]);
    }
  });
  percent.addEventListener('change', () => setRatio(percent.value === '' ? NaN : percent.valueAsNumber / 100));
  slider.addEventListener('input', () => setRatio(slider.valueAsNumber / 100));
  $('reset-layout').onclick = reset;
  popover.addEventListener('toggle', positionPopover);
  new ResizeObserver(() => {
    if (!frame) frame = requestAnimationFrame(() => { frame = 0; apply(); });
  }).observe(workspace);
  apply();
}
