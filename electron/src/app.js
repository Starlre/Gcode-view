import './style.css';
import { createIcons, Box, Layers, FolderOpen, Download, Play, Pause, RotateCcw, ChevronLeft,
  ChevronRight, Plus, Minus, Move3d, Scan, Eye, Sun, Moon, HelpCircle, Check, ArrowUp,
  ArrowDown, Settings2, SlidersHorizontal, Cuboid, Crosshair, Maximize, PackageOpen,
  MousePointer2, X, FileCode2, CircleCheck, LoaderCircle } from 'lucide';
import { Viewport } from './viewport.js';
import { formatTime, moveAtTime } from './gcode.js';
import { setupLayout } from './layout.js';

const icons = { Box, Layers, FolderOpen, Download, Play, Pause, RotateCcw, ChevronLeft,
  ChevronRight, Plus, Minus, Move3d, Scan, Eye, Sun, Moon, HelpCircle, Check, ArrowUp,
  ArrowDown, Settings2, SlidersHorizontal, Cuboid, Crosshair, Maximize, PackageOpen,
  MousePointer2, X, FileCode2, CircleCheck, LoaderCircle };
const icon = (name) => `<i data-lucide="${name}" aria-hidden="true"></i>`;
const $ = (id) => document.getElementById(id);
const defaults = { layer_height: 0.05, first_layer_height: 0.05, fill_spacing: 1, gap: 1,
  xy_feed_speed: 180, xy_travel_speed: 1200, feed_speed: 65, travel_speed: 150,
  support_layers: 8, support_dwell: 50, exposure: 6, bed_x: 80, bed_y: 45, margin: 3,
  rotation_x: 0, rotation_y: 0, rotation_z: 0, auto_scale: true, center_model: true,
  fill_enabled: true, slice_direction: [0, 0, 1] };
let saved = {};
try { saved = JSON.parse(localStorage.getItem('hkuprint-settings') || '{}'); } catch { /* Reset invalid saved data. */ }
const state = { settings: { ...defaults, ...saved, slice_direction: [0, 0, 1] },
  mesh: null, model: null, layer: 0, time: 0, playing: false, speed: 1,
  busy: false, dirty: false, imported: false, mode: 'model', directionLocked: false };
const speedSteps = [0.25, 0.5, 1, 2, 4, 8, 16, 32];

const fields = {
  layer_height: ['普通层高', 'mm', 0.01, 20, 0.01], first_layer_height: ['首层层高', 'mm', 0.01, 20, 0.01],
  fill_spacing: ['回字形圈距', 'mm', 0.01, 20, 0.1], xy_feed_speed: ['走刀速度', 'mm/min', 0.1, 3000, 10],
  xy_travel_speed: ['空移速度', 'mm/min', 0.1, 3000, 50], feed_speed: ['抬升速度', 'mm/min', 0.1, 3000, 5],
  travel_speed: ['下降速度', 'mm/min', 0.1, 3000, 5], support_layers: ['底层数量', '层', 0, 10000, 1],
  support_dwell: ['底层曝光', 's', 0, 86400, 1], exposure: ['普通层曝光', 's', 0, 86400, 0.1],
  bed_x: ['工作区 X', 'mm', 1, 1000, 1], bed_y: ['工作区 Y', 'mm', 1, 1000, 1],
  margin: ['边距', 'mm', 0, 100, 0.5], rotation_x: ['X 轴旋转', '°', -360, 360, 5],
  rotation_y: ['Y 轴旋转', '°', -360, 360, 5], rotation_z: ['Z 轴旋转', '°', -360, 360, 5],
};

function field(key) {
  const [label, unit, min, max, step] = fields[key];
  return `<div class="field"><label for="${key}">${label}</label><div class="input-shell">
    <input id="${key}" data-setting="${key}" type="number" min="${min}" max="${max}" step="${step}"
      value="${state.settings[key]}" aria-describedby="error-${key}" /><span>${unit}</span></div>
    <span id="error-${key}" class="field-error"></span></div>`;
}

$('app').innerHTML = `
  <a class="skip-link" href="#viewport-toolbar">跳到预览操作</a>
  <header class="app-header">
    <div class="brand"><span class="brand-mark">${icon('layers')}</span><div><h1>HKUprint <span>Studio</span></h1><span class="brand-caption">切片与走刀工作台</span></div></div>
    <div class="header-file"><span id="document-name">新建作业</span><span id="document-note">STL / OBJ / G-code</span></div>
    <div class="header-actions"><button id="open-file" class="button">${icon('folder-open')}<span>导入文件</span><kbd>Ctrl O</kbd></button>
      <button id="export-job" class="button primary" disabled>${icon('download')}<span>导出打印作业</span></button></div>
  </header>
  <div class="workspace-bar">
    <nav class="workflow" aria-label="工作步骤"><button id="model-tab" class="workflow-tab selected" aria-pressed="true"><span class="step-number">1</span>模型准备</button>
      <span class="workflow-line"></span><button id="path-tab" class="workflow-tab" aria-pressed="false" disabled><span class="step-number">2</span>走刀预览</button></nav>
    <div class="workspace-tools"><span class="local-badge">${icon('circle-check')}本地作业</span>
      <button id="layout-toggle" class="button compact" popovertarget="layout-popover">${icon('settings-2')}布局</button>
      <button id="theme-toggle" class="icon-button" title="切换深色界面" aria-label="切换深色界面">${icon('moon')}</button>
      <button id="help-button" class="icon-button" title="操作帮助" aria-label="操作帮助">${icon('help-circle')}</button></div>
  </div>
  <div id="layout-popover" class="layout-popover" popover role="dialog" aria-labelledby="layout-heading">
    <div class="layout-heading"><label id="layout-heading" for="sidebar-percent">参数区占比</label>
      <div class="input-shell"><input id="sidebar-percent" type="number" step="1" aria-describedby="layout-hint" /><span>%</span></div></div>
    <input id="sidebar-width" type="range" step="1" aria-label="调整参数区占比" />
    <p id="layout-hint" class="helper">也可拖动中间分隔线，双击恢复默认。宽度会自动记住。</p>
    <button id="reset-layout" class="button full">${icon('rotate-ccw')}恢复默认布局</button>
  </div>
  <main id="workspace" class="workspace">
    <aside id="sidebar" class="sidebar" aria-label="模型与切片设置">
      <div class="sidebar-tabs" role="tablist" aria-label="设置分类"><button id="parameters-tab" class="selected" role="tab" aria-controls="parameters-panel" aria-selected="true">${icon('sliders-horizontal')}切片参数</button>
        <button id="object-tab" role="tab" aria-controls="object-panel" aria-selected="false">${icon('box')}模型</button></div>
      <div class="sidebar-scroll">
        <section class="file-section"><div class="section-caption">当前对象</div><div class="object-row"><span class="object-icon">${icon('cuboid')}</span>
          <div><strong id="object-name">尚未导入模型</strong><span id="object-detail">打开文件，或拖入预览区</span></div><span id="object-check" hidden>${icon('check')}</span></div></section>
        <form id="settings-form" novalidate>
          <div id="parameters-panel" role="tabpanel" aria-labelledby="parameters-tab">
            <section class="setting-section"><h2>分层与填充<span class="section-tag">回字形</span></h2>
              <div class="field-grid">${field('layer_height')}${field('first_layer_height')}</div>${field('fill_spacing')}
              <div class="path-rule">${icon('layers')}最小内圈 → 逐圈向外 · 对角切换</div></section>
            <details class="setting-section" open><summary>运动速度</summary><div class="field-grid">${field('xy_feed_speed')}${field('xy_travel_speed')}${field('feed_speed')}${field('travel_speed')}</div></details>
            <details class="setting-section"><summary>曝光与底层</summary>${field('support_layers')}<div class="field-grid">${field('support_dwell')}${field('exposure')}</div></details>
            <details class="setting-section"><summary>工作区</summary><label class="select-label" for="bed-preset">工作区方向</label>
              <select id="bed-preset"><option value="80,45">80 × 45 mm · 投影坐标</option><option value="45,80">45 × 80 mm · 机器坐标</option><option value="custom">自定义</option></select>
              <div class="field-grid">${field('bed_x')}${field('bed_y')}</div>${field('margin')}
              <label class="check-row"><input id="auto_scale" type="checkbox" data-setting="auto_scale" ${state.settings.auto_scale ? 'checked' : ''} />超出工作区时自动适配</label>
              <label class="check-row"><input id="center_model" type="checkbox" data-setting="center_model" ${state.settings.center_model ? 'checked' : ''} />模型居中</label></details>
          </div>
          <div id="object-panel" role="tabpanel" aria-labelledby="object-tab" hidden>
            <section class="setting-section"><h2>模型尺寸</h2><div class="dimensions"><div><span>X</span><strong id="size-x">—</strong></div><div><span>Y</span><strong id="size-y">—</strong></div><div><span>Z</span><strong id="size-z">—</strong></div></div><p class="helper">尺寸单位 mm；导入后可旋转并选择切片方向。</p></section>
            <section class="setting-section"><h2>模型姿态</h2>${field('rotation_x')}${field('rotation_y')}${field('rotation_z')}<button id="reset-rotation" class="button full" type="button">${icon('rotate-ccw')}重置旋转</button></section>
            <section class="setting-section"><h2>切片方向</h2><button id="lock-direction" class="button full" type="button">${icon('scan')}当前视角作为 Z</button>
              <p id="direction-note" class="helper">沿世界 Z 方向切片</p><button id="reset-direction" type="button" class="text-button">恢复世界 Z</button></section>
          </div>
        </form>
        <section class="job-section"><div class="section-caption">作业概览</div><div class="job-stats"><div><span>总层数</span><strong id="stat-layers">—</strong></div><div><span>预计时间</span><strong id="stat-time">—</strong></div><div><span>走刀长度</span><strong id="stat-distance">—</strong></div><div><span>路径段数</span><strong id="stat-moves">—</strong></div></div>
          <p class="helper" id="job-note">切片后生成 run.gcode 与逐层曝光 PNG。</p><button id="reveal-job" class="text-button" hidden>${icon('folder-open')}打开输出文件夹</button></section>
      </div>
      <div class="sidebar-action"><p id="slice-note">导入模型，开始你的打印作业</p><button id="slice-button" class="button primary full" disabled>${icon('layers')}开始切片</button></div>
    </aside>
    <div id="workspace-divider" class="workspace-divider" role="separator" tabindex="0" aria-label="调整参数区与预览区宽度"
      aria-controls="sidebar" aria-orientation="vertical" title="拖动调整宽度 · 双击恢复默认 · 方向键微调"><span aria-hidden="true"></span></div>
    <section class="preview" aria-label="三维预览">
      <div id="viewport-toolbar" class="viewport-toolbar" tabindex="-1"><div class="view-presets" aria-label="预览视角"><button class="icon-button" data-view="iso" title="等轴视图" aria-label="等轴视图">${icon('box')}</button>
        <button class="view-button" data-view="top">俯视</button><button class="view-button" data-view="front">正视</button><button class="view-button" data-view="side">侧视</button>
        <span class="toolbar-divider"></span><button id="fit-view" class="icon-button" title="适应窗口 · F" aria-label="适应窗口">${icon('maximize')}</button></div>
        <div class="view-tools"><button id="pick-face" class="button compact" aria-pressed="false">${icon('mouse-pointer-2')}选面正视</button>
          <button id="direction-shortcut" class="button compact">${icon('scan')}视角作为 Z</button></div></div>
      <div class="preview-stage" id="drop-target">
        <div class="layer-rail" id="layer-rail" hidden><span class="rail-label">层</span><output id="layer-value">1</output>
          <button id="layer-up" class="icon-button" title="上一高度层" aria-label="增加一层">${icon('plus')}</button>
          <input id="layer-slider" type="range" min="1" max="1" value="1" aria-label="选择预览层" />
          <button id="layer-down" class="icon-button" title="下一高度层" aria-label="减少一层">${icon('minus')}</button><span id="layer-total">/ 1</span></div>
        <div id="viewport" class="viewport" role="img" aria-label="模型与走刀三维预览，支持旋转、平移和缩放"></div>
        <div class="viewport-caption" id="viewport-caption"><span class="caption-dot"></span><span id="preview-title">模型工作区</span><span id="bed-label">80 × 45 mm</span></div>
        <div class="empty-state" id="empty-state"><span class="empty-icon">${icon('package-open')}</span><h2>从一个模型开始</h2><p>拖入 STL、OBJ 或 G-code<br />在三维空间里检查每一层走刀</p>
          <button id="empty-import" class="button primary">${icon('folder-open')}导入文件</button><button id="load-demo" class="text-button">试用 15 mm 示例方块 ${icon('chevron-right')}</button></div>
        <div class="drop-overlay" id="drop-overlay" hidden>${icon('download')}松开以导入文件</div>
        <div class="stage-bottom"><span id="view-hint">左键旋转 · 右键平移 · 滚轮缩放</span><span id="layer-height" hidden>Z 0.000 mm</span></div>
        <div class="path-legend" id="path-legend" hidden><span><i class="legend-line completed"></i>已走过</span><span><i class="legend-line future"></i>当前层待走</span>
          <label><input id="show-travel" type="checkbox" /><i class="legend-line travel"></i>空移</label><label><input id="show-needle" type="checkbox" checked />针头</label></div>
      </div>
      <div class="playback" id="playback">
        <div class="progress-row"><label for="progress-slider">层内走刀</label><input id="progress-slider" type="range" min="0" max="1000" step="1" value="0" aria-label="当前层走刀进度" disabled /><output id="progress-value">0%</output></div>
        <div class="transport-row"><div class="transport-buttons"><button id="reset-playback" class="icon-button" title="回到本层起点" aria-label="回到本层起点" disabled>${icon('rotate-ccw')}</button>
          <button id="step-back" class="icon-button" title="上一步 · ←" aria-label="上一步" disabled>${icon('chevron-left')}</button>
          <button id="play-button" class="play-button" title="播放 · 空格" aria-label="播放当前层" disabled>${icon('play')}</button>
          <button id="step-forward" class="icon-button" title="下一步 · →" aria-label="下一步" disabled>${icon('chevron-right')}</button>
          <span class="playback-time"><span id="current-time">00:00</span><span>/</span><span id="layer-duration">00:00</span></span></div>
          <div class="speed-control"><span>播放速度</span><button id="speed-down" class="icon-button" title="减速" aria-label="减慢播放速度">${icon('minus')}</button><output id="speed-value">1×</output><button id="speed-up" class="icon-button" title="加速" aria-label="加快播放速度">${icon('plus')}</button></div></div>
        <div class="telemetry"><span><b>X</b><output id="position-x">—</output></span><span><b>Y</b><output id="position-y">—</output></span><span><b>Z</b><output id="position-z">—</output></span><span class="command" id="current-command">导入 G-code 或切片后开始预览</span></div>
      </div>
    </section>
  </main>
  <footer class="statusbar"><div class="status-message" role="status" aria-live="polite"><span id="status-indicator" class="status-indicator"></span><span id="status-text">就绪 · 导入模型或 G-code 文件</span></div><span id="status-detail">HKUprint Studio · 0.1</span></footer>
  <dialog id="help-dialog"><div class="dialog-heading"><h2>操作帮助</h2><button id="close-help" class="icon-button" aria-label="关闭帮助">${icon('x')}</button></div>
    <p>导入 STL / OBJ → 调整姿态与参数 → 开始切片 → 检查走刀 → 导出打印作业。</p><dl class="shortcuts"><dt>导入文件</dt><dd>Ctrl + O</dd><dt>播放 / 暂停</dt><dd>空格</dd><dt>前后单步</dt><dd>← / →</dd><dt>切换层</dt><dd>Page Up / Page Down</dd><dt>适应窗口</dt><dd>F</dd></dl>
    <p class="helper">左键旋转，右键平移，滚轮缩放。开启“选面正视”后单击模型面，再选择“视角作为 Z”即可沿该方向切片。预览时间依据 G-code 速度估算。</p>
    <p class="helper">拖动参数区与预览区之间的分隔线，或在顶部“布局”里设置占比。双击分隔线恢复默认；分隔线获得焦点后，左右方向键微调，Shift + 方向键加大调整幅度，Enter 恢复默认。</p></dialog>
`;

function refreshIcons() { createIcons({ icons, attrs: { 'stroke-width': 2 } }); }
refreshIcons();
setupLayout();
const viewport = new Viewport($('viewport'), () => status('已正对选中面；可将当前视角设为 Z 切片方向'));
const parser = new Worker(new URL('./parse-worker.js', import.meta.url), { type: 'module' });
let parseId = 0;
const parsing = new Map();
parser.onmessage = ({ data }) => {
  const task = parsing.get(data.id); if (!task) return;
  parsing.delete(data.id); data.error ? task.reject(new Error(data.error)) : task.resolve(data.model);
};
parser.onerror = (event) => { for (const task of parsing.values()) task.reject(new Error(event.message)); parsing.clear(); };
const parse = (text, name) => new Promise((resolve, reject) => {
  const id = ++parseId; parsing.set(id, { resolve, reject }); parser.postMessage({ id, text, name });
});
async function call(name, ...args) {
  if (!window.studio) throw new Error('请通过 Electron 启动此工作台');
  const result = await window.studio[name](...args);
  if (!result.ok) throw new Error(result.error);
  return result.data;
}
function status(message, error = false) {
  $('status-text').textContent = message; $('status-indicator').classList.toggle('error', error);
}
function setBusy(busy, message) {
  state.busy = busy; document.body.classList.toggle('busy', busy); if (message) status(message);
  syncButtons();
}
function syncButtons() {
  $('open-file').disabled = $('empty-import').disabled = $('load-demo').disabled = state.busy;
  $('slice-button').disabled = state.busy || !state.mesh;
  $('slice-button').innerHTML = state.busy ? `${icon('loader-circle')}处理中…` : `${icon('layers')}${state.dirty && state.model ? '重新切片' : '开始切片'}`;
  $('export-job').disabled = state.busy || !state.model || state.dirty || state.imported;
  $('path-tab').disabled = !state.model;
  $('model-tab').disabled = state.imported && !state.mesh;
  document.querySelectorAll('#settings-form input, #settings-form select, #settings-form button').forEach((input) => { input.disabled = state.busy; });
  for (const id of ['progress-slider', 'play-button', 'step-back', 'step-forward', 'reset-playback']) $(id).disabled = !state.model || state.busy;
  for (const id of ['pick-face', 'direction-shortcut']) $(id).disabled = !state.mesh || state.busy || state.mode !== 'model';
  for (const id of ['lock-direction', 'reset-rotation']) $(id).disabled = !state.mesh || state.busy;
  $('slice-note').textContent = state.busy ? '正在处理作业，预览仍可旋转' : state.dirty && state.model ? '参数已修改，请重新切片后导出' : state.mesh ? '回字形填充已开启 · 由内向外' : '导入模型，开始你的打印作业';
  refreshIcons();
}
function setMode(mode) {
  if (mode === 'path' && !state.model) return;
  state.mode = mode; if (mode === 'model') pause(); viewport.setMode(mode);
  $('model-tab').classList.toggle('selected', mode === 'model'); $('path-tab').classList.toggle('selected', mode === 'path');
  $('model-tab').setAttribute('aria-pressed', mode === 'model'); $('path-tab').setAttribute('aria-pressed', mode === 'path');
  $('layer-rail').hidden = $('layer-height').hidden = $('path-legend').hidden = mode !== 'path';
  $('preview-title').textContent = mode === 'model' ? '模型工作区' : '分层走刀预览';
  $('playback').classList.toggle('inactive', mode === 'model');
  $('pick-face').disabled = mode !== 'model' || !state.mesh;
  $('direction-shortcut').disabled = mode !== 'model' || !state.mesh;
}
function setPanel(name) {
  const object = name === 'object';
  $('parameters-panel').hidden = object; $('object-panel').hidden = !object;
  $('parameters-tab').classList.toggle('selected', !object); $('object-tab').classList.toggle('selected', object);
  $('parameters-tab').setAttribute('aria-selected', !object); $('object-tab').setAttribute('aria-selected', object);
}
function clearStats() {
  for (const id of ['stat-layers', 'stat-time', 'stat-distance', 'stat-moves']) $(id).textContent = '—';
}
function markDirty() {
  state.dirty = true; pause(); syncButtons();
  try { localStorage.setItem('hkuprint-settings', JSON.stringify(state.settings)); } catch { /* Storage can be disabled. */ }
}
function validateFields() {
  let firstInvalid;
  for (const key of Object.keys(fields)) {
    const input = $(key), [, , min, max] = fields[key], value = input.valueAsNumber;
    const valid = Number.isFinite(value) && value >= min && value <= max && (key !== 'support_layers' || Number.isInteger(value));
    input.setAttribute('aria-invalid', !valid);
    $(`error-${key}`).textContent = valid ? '' : key === 'support_layers' ? '请输入有效整数层数' : `请输入 ${min}–${max} 范围内的数值`;
    if (!valid) firstInvalid ||= input;
    else state.settings[key] = value;
  }
  if (firstInvalid) {
    setPanel(firstInvalid.closest('#object-panel') ? 'object' : 'parameters');
    const details = firstInvalid.closest('details'); if (details) details.open = true;
    firstInvalid.focus(); return false;
  }
  if (state.settings.margin * 2 >= Math.min(state.settings.bed_x, state.settings.bed_y)) {
    $('margin').closest('details').open = true;
    $('error-margin').textContent = '边距必须小于工作区短边的一半'; $('margin').setAttribute('aria-invalid', true); $('margin').focus(); return false;
  }
  return true;
}
async function acceptFile(data) {
  if (!data) return;
  pause(); $('reveal-job').hidden = true;
  if (data.kind === 'model') {
    state.mesh = data; state.model = null; state.imported = false; state.dirty = true;
    state.directionLocked = false; state.settings.slice_direction = [0, 0, 1];
    $('direction-note').textContent = '沿世界 Z 方向切片';
    viewport.clearToolpath(); viewport.setBed(state.settings.bed_x, state.settings.bed_y); viewport.setMesh(data, state.settings);
    clearStats(); setMode('model');
    const dimensions = [data.bounds[1] - data.bounds[0], data.bounds[3] - data.bounds[2], data.bounds[5] - data.bounds[4]];
    ['x', 'y', 'z'].forEach((axis, index) => { $(`size-${axis}`).textContent = dimensions[index].toFixed(2); });
    $('object-detail').textContent = `${data.triangles.toLocaleString()} 个三角面 · ${dimensions.map((v) => v.toFixed(1)).join(' × ')} mm`;
    status(`已导入 ${data.name}，调整参数后开始切片`);
  } else {
    status('正在解析三维走刀…');
    const model = await parse(data.gcode, data.name);
    if (!model.layers.length) throw new Error('没有找到可预览的层');
    if (data.imported) {
      state.mesh = null; viewport.setMesh(null); state.imported = true;
      for (const axis of ['x', 'y', 'z']) $(`size-${axis}`).textContent = '—';
    } else state.imported = false;
    state.model = model; state.dirty = false;
    if (data.imported && Number(model.metadata.machineX) > 0 && Number(model.metadata.machineY) > 0) {
      viewport.setBed(Number(model.metadata.machineX), Number(model.metadata.machineY));
      $('bed-label').textContent = `${model.metadata.machineX} × ${model.metadata.machineY} mm`;
    }
    viewport.setToolpath(model); setMode('path'); setLayer(0);
    $('stat-layers').textContent = model.layers.length.toLocaleString(); $('stat-time').textContent = formatTime(model.totalTime);
    $('stat-distance').textContent = `${(model.totalDistance / 1000).toFixed(2)} m`;
    $('stat-moves').textContent = model.moves.length.toLocaleString();
    $('object-detail').textContent = `${model.layers.length} 层 · ${model.lineCount.toLocaleString()} 行 G-code`;
    $('job-note').textContent = data.imported ? '已载入走刀文件，可分层检查与播放。' : '导出 run.gcode + 3840 × 2160 逐层曝光 PNG。';
    status(`走刀已就绪 · ${model.layers.length} 层${model.warnings.length ? ` · 未模拟指令：${model.warnings.join('、')}` : ''}`);
  }
  $('document-name').textContent = $('object-name').textContent = data.name;
  $('document-note').textContent = data.imported ? 'G-code 走刀文件' : '模型切片作业';
  $('object-check').hidden = false; $('empty-state').hidden = true; syncButtons();
}
async function openFile(file) {
  if (state.busy) return;
  setBusy(true, '正在导入文件…');
  try { await acceptFile(await call(file ? 'importFile' : 'openFile', ...(file ? [file] : []))); }
  catch (error) { status(error.message, true); }
  finally { setBusy(false); }
}
async function loadDemo() {
  setBusy(true, '正在创建示例方块…');
  try {
    for (const key of ['layer_height', 'first_layer_height', 'fill_spacing']) state.settings[key] = $(key).value = 1;
    state.settings.support_layers = $('support_layers').value = 2;
    await acceptFile(await call('demo'));
  } catch (error) { status(error.message, true); }
  finally { setBusy(false); }
}
async function slice() {
  if (state.busy || !state.mesh || !validateFields()) return;
  pause(); setBusy(true, '正在切片…');
  try { await acceptFile(await call('slice', state.settings)); }
  catch (error) { status(error.message, true); }
  finally { setBusy(false); }
}
async function exportJob() {
  if (state.dirty) { status('参数已经修改，请重新切片后导出', true); return; }
  setBusy(true, '选择打印作业保存位置…');
  try {
    const job = await call('exportJob');
    if (job) {
      $('reveal-job').hidden = false; $('job-note').textContent = `已导出：${job.directory}`;
      status(`导出完成 · run.gcode 与 ${job.layers} 张曝光 PNG`);
    } else status('已取消导出');
  } catch (error) { status(error.message, true); }
  finally { setBusy(false); }
}
function setLayer(index) {
  if (!state.model) return;
  pause(); state.layer = Math.max(0, Math.min(state.model.layers.length - 1, index));
  const layer = state.model.layers[state.layer];
  state.time = layer.startTime;
  $('layer-slider').max = state.model.layers.length; $('layer-slider').value = state.layer + 1;
  $('layer-value').textContent = state.layer + 1; $('layer-total').textContent = `/ ${state.model.layers.length}`;
  $('layer-height').textContent = `Z ${layer.z.toFixed(3)} mm`;
  $('layer-up').disabled = state.layer >= state.model.layers.length - 1; $('layer-down').disabled = state.layer === 0;
  viewport.setLayer(state.layer); updateProgress();
}
function updateProgress() {
  if (!state.model) return;
  const layer = state.model.layers[state.layer], duration = layer.endTime - layer.startTime;
  const ratio = duration ? Math.max(0, Math.min(1, (state.time - layer.startTime) / duration)) : 0;
  const current = viewport.setProgress(state.time);
  $('progress-slider').value = Math.round(ratio * 1000); $('progress-value').textContent = `${Math.round(ratio * 100)}%`;
  $('progress-slider').style.setProperty('--progress', `${ratio * 100}%`);
  $('current-time').textContent = formatTime(state.time - layer.startTime); $('layer-duration').textContent = formatTime(duration);
  ['x', 'y', 'z'].forEach((axis, index) => { $(`position-${axis}`).textContent = current.point[index].toFixed(3); });
  $('current-command').textContent = `N${current.move.line}  ${current.move.raw}`;
  $('current-command').title = current.move.raw;
  $('status-detail').textContent = `第 ${state.layer + 1} / ${state.model.layers.length} 层 · ${current.move.feed.toFixed(0)} mm/min`;
}
function pause() {
  state.playing = false;
  $('play-button').innerHTML = icon('play'); $('play-button').setAttribute('aria-label', '播放当前层'); refreshIcons();
}
let previousTimestamp = 0, playbackFrame;
function togglePlay() {
  if (!state.model || state.busy) return;
  if (state.playing) { pause(); return; }
  setMode('path');
  const layer = state.model.layers[state.layer];
  if (state.time >= layer.endTime) state.time = layer.startTime;
  state.playing = true; previousTimestamp = 0;
  $('play-button').innerHTML = icon('pause'); $('play-button').setAttribute('aria-label', '暂停播放'); refreshIcons();
  if (playbackFrame) cancelAnimationFrame(playbackFrame);
  const tick = (timestamp) => {
    if (!state.playing) { playbackFrame = null; return; }
    if (!previousTimestamp) previousTimestamp = timestamp;
    state.time = Math.min(layer.endTime, state.time + (timestamp - previousTimestamp) / 1000 * state.speed);
    previousTimestamp = timestamp; updateProgress();
    if (state.time >= layer.endTime) { pause(); status(`第 ${state.layer + 1} 层播放完成`); }
    else playbackFrame = requestAnimationFrame(tick);
  };
  playbackFrame = requestAnimationFrame(tick);
}
function step(direction) {
  if (!state.model) return;
  pause(); const layer = state.model.layers[state.layer], current = moveAtTime(state.model, state.time);
  let index = direction > 0 ? current.index : current.index - 1;
  if (direction > 0 && state.time >= current.move.t1 - 1e-7) index++;
  index = Math.max(layer.first, Math.min(layer.last, index));
  state.time = direction > 0 ? state.model.moves[index].t1 : state.model.moves[index].t0; updateProgress();
}
function changeSpeed(direction) {
  const index = Math.max(0, Math.min(speedSteps.length - 1, speedSteps.indexOf(state.speed) + direction));
  state.speed = speedSteps[index]; $('speed-value').textContent = `${state.speed}×`;
  $('speed-down').disabled = index === 0; $('speed-up').disabled = index === speedSteps.length - 1;
}
function lockDirection() {
  if (!state.mesh) return;
  state.settings.slice_direction = viewport.direction(); state.directionLocked = true;
  $('direction-note').textContent = `已锁定：(${state.settings.slice_direction.map((v) => v.toFixed(2)).join(', ')})`;
  markDirty(); status('已设定 Z 切片方向，下一次切片将应用当前视角');
}

$('open-file').onclick = $('empty-import').onclick = () => openFile();
$('load-demo').onclick = loadDemo; $('slice-button').onclick = slice; $('export-job').onclick = exportJob;
$('reveal-job').onclick = () => call('revealJob').catch((error) => status(error.message, true));
$('model-tab').onclick = () => setMode('model'); $('path-tab').onclick = () => setMode('path');
$('parameters-tab').onclick = () => setPanel('parameters'); $('object-tab').onclick = () => setPanel('object');
for (const id of ['parameters-tab', 'object-tab']) $(id).addEventListener('keydown', (event) => {
  if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    const object = event.key === 'End' || (event.key !== 'Home' && id === 'parameters-tab');
    setPanel(object ? 'object' : 'parameters'); $(object ? 'object-tab' : 'parameters-tab').focus();
  }
});
document.querySelectorAll('[data-view]').forEach((button) => { button.onclick = () => viewport.fit(button.dataset.view); });
$('fit-view').onclick = () => viewport.fit();
$('pick-face').onclick = () => {
  viewport.pickMode = !viewport.pickMode; $('pick-face').setAttribute('aria-pressed', viewport.pickMode);
  $('pick-face').classList.toggle('selected', viewport.pickMode);
  status(viewport.pickMode ? '单击模型表面，自动正对该面' : '已退出选面模式');
};
$('direction-shortcut').onclick = $('lock-direction').onclick = lockDirection;
$('reset-direction').onclick = () => { state.settings.slice_direction = [0, 0, 1]; state.directionLocked = false; $('direction-note').textContent = '沿世界 Z 方向切片'; markDirty(); };
$('reset-rotation').onclick = () => {
  for (const axis of ['x', 'y', 'z']) state.settings[`rotation_${axis}`] = $(`rotation_${axis}`).value = 0;
  viewport.updateMeshSettings(state.settings); markDirty();
};
document.querySelectorAll('[data-setting]').forEach((input) => {
  input.addEventListener('change', () => {
    state.settings[input.dataset.setting] = input.type === 'checkbox' ? input.checked : input.valueAsNumber;
    if (validateFields()) {
      if (['bed_x', 'bed_y'].includes(input.id)) viewport.setBed(state.settings.bed_x, state.settings.bed_y);
      $('bed-label').textContent = `${state.settings.bed_x} × ${state.settings.bed_y} mm`;
      viewport.updateMeshSettings(state.settings);
    }
    markDirty();
  });
});
$('bed-preset').value = `${state.settings.bed_x},${state.settings.bed_y}`;
if (!$('bed-preset').value) $('bed-preset').value = 'custom';
$('bed-preset').onchange = () => {
  if ($('bed-preset').value === 'custom') return;
  const [x, y] = $('bed-preset').value.split(',').map(Number);
  state.settings.bed_x = $('bed_x').value = x; state.settings.bed_y = $('bed_y').value = y;
  viewport.setBed(x, y); viewport.updateMeshSettings(state.settings); $('bed-label').textContent = `${x} × ${y} mm`; markDirty();
};
$('layer-slider').oninput = () => setLayer(Number($('layer-slider').value) - 1);
$('layer-up').onclick = () => setLayer(state.layer + 1); $('layer-down').onclick = () => setLayer(state.layer - 1);
$('progress-slider').oninput = () => {
  if (!state.model) return;
  pause(); const layer = state.model.layers[state.layer];
  state.time = layer.startTime + (layer.endTime - layer.startTime) * Number($('progress-slider').value) / 1000;
  updateProgress();
};
$('play-button').onclick = togglePlay; $('step-back').onclick = () => step(-1); $('step-forward').onclick = () => step(1);
$('reset-playback').onclick = () => setLayer(state.layer);
$('speed-down').onclick = () => changeSpeed(-1); $('speed-up').onclick = () => changeSpeed(1);
$('show-travel').onchange = () => { viewport.showTravel = $('show-travel').checked; if (viewport.travelLines) viewport.travelLines.visible = viewport.showTravel; viewport.invalidate(); };
$('show-needle').onchange = () => { viewport.showNeedle = $('show-needle').checked; viewport.needle.visible = viewport.showNeedle; viewport.invalidate(); };
function applyTheme() {
  const dark = document.documentElement.dataset.theme === 'dark';
  viewport.theme(dark);
  const label = dark ? '切换浅色界面' : '切换深色界面';
  $('theme-toggle').innerHTML = icon(dark ? 'sun' : 'moon');
  $('theme-toggle').setAttribute('aria-label', label);
  $('theme-toggle').title = label;
  refreshIcons();
}
try { document.documentElement.dataset.theme = localStorage.getItem('hkuprint-theme') || 'light'; } catch { /* Optional preference. */ }
applyTheme(); window.addEventListener('studio:theme', applyTheme);
$('theme-toggle').onclick = () => {
  const dark = document.documentElement.dataset.theme !== 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  try { localStorage.setItem('hkuprint-theme', dark ? 'dark' : 'light'); } catch { /* Optional preference. */ }
  applyTheme();
};
$('help-button').onclick = () => $('help-dialog').showModal(); $('close-help').onclick = () => $('help-dialog').close();
$('settings-form').onsubmit = (event) => { event.preventDefault(); slice(); };
window.studio?.onProgress((event) => status(event.message));
let dragDepth = 0;
$('drop-target').addEventListener('dragenter', (event) => { event.preventDefault(); dragDepth++; $('drop-overlay').hidden = false; });
$('drop-target').addEventListener('dragover', (event) => event.preventDefault());
$('drop-target').addEventListener('dragleave', () => { dragDepth--; if (dragDepth <= 0) $('drop-overlay').hidden = true; });
$('drop-target').addEventListener('drop', (event) => { event.preventDefault(); dragDepth = 0; $('drop-overlay').hidden = true; if (event.dataTransfer.files[0]) openFile(event.dataTransfer.files[0]); });
document.addEventListener('dragover', (event) => event.preventDefault());
document.addEventListener('drop', (event) => event.preventDefault());
window.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'o') { event.preventDefault(); openFile(); return; }
  if (event.defaultPrevented || event.target.matches('input,select,textarea,[role="separator"]') || $('help-dialog').open || $('layout-popover').matches(':popover-open')) return;
  const actions = { ' ': togglePlay, ArrowRight: () => step(1), ArrowLeft: () => step(-1),
    PageUp: () => setLayer(state.layer + 1), PageDown: () => setLayer(state.layer - 1), f: () => viewport.fit() };
  if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
});
syncButtons();
call('info').then(() => status('就绪 · 导入模型或 G-code 文件')).catch((error) => status(error.message, true));

window.studioLayoutProbe = () => ({
  horizontalOverflow: document.documentElement.scrollWidth > innerWidth,
  controlOverflow: ['.viewport-toolbar', '.transport-row', '.progress-row'].some((selector) => {
    const element = document.querySelector(selector); return element.scrollWidth > element.clientWidth + 1;
  }),
  sidebarWidth: $('sidebar').getBoundingClientRect().width,
  previewWidth: document.querySelector('.preview').getBoundingClientRect().width,
  sidebarPercent: Number($('workspace-divider').getAttribute('aria-valuenow')),
  viewport: { width: $('viewport').clientWidth, height: $('viewport').clientHeight },
  fontSize: getComputedStyle(document.body).fontSize,
  canvasCount: $('viewport').querySelectorAll('canvas').length,
});
window.studioLayoutSmoke = async () => {
  const settle = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const check = () => {
    const layout = window.studioLayoutProbe();
    if (layout.horizontalOverflow || layout.controlOverflow || layout.sidebarWidth < 339 || layout.previewWidth < 679) throw new Error('面板调整后出现溢出或宽度越界');
    if (Math.abs(viewport.camera.aspect - $('viewport').clientWidth / $('viewport').clientHeight) > 0.001) throw new Error('三维视口没有同步调整');
    return layout;
  };
  $('reset-layout').click(); await settle();
  const initial = check(), time = state.time, dirty = state.dirty;
  const camera = viewport.camera.position.clone(), target = viewport.controls.target.clone();
  const key = (name, shiftKey = false) => $('workspace-divider').dispatchEvent(new KeyboardEvent('keydown', { key: name, shiftKey, bubbles: true, cancelable: true }));
  key('ArrowRight'); await settle();
  if (check().sidebarWidth <= initial.sidebarWidth || state.time !== time) throw new Error('分隔线方向键与走刀快捷键冲突');
  key('End'); await settle(); const widest = check();
  key('Home'); await settle(); const narrowest = check();
  if (widest.sidebarWidth <= narrowest.sidebarWidth) throw new Error('面板宽度不可调整');
  key('ArrowRight', true); await settle(); check();
  $('layout-toggle').click(); await settle();
  if (!$('layout-popover').matches(':popover-open')) throw new Error('布局菜单没有打开');
  for (const value of ['100', '-10', '']) {
    $('sidebar-percent').value = value; $('sidebar-percent').dispatchEvent(new Event('change')); await settle(); check();
  }
  $('sidebar-width').value = 35; $('sidebar-width').dispatchEvent(new Event('input')); await settle();
  const customized = check();
  if (state.time !== time || state.dirty !== dirty || camera.distanceTo(viewport.camera.position) > 0.001 || target.distanceTo(viewport.controls.target) > 0.001) throw new Error('调整面板影响了走刀状态或观察角度');
  $('layout-popover').hidePopover();
  $('reset-layout').click(); await settle(); check();
  if (Number(localStorage.getItem('hkuprint-sidebar-ratio')) !== 0.28) throw new Error('布局默认偏好未保存');
  return { initial, widest, narrowest, customized, preservedViewAndPlayback: true };
};
window.studioLayoutScreenshot = async () => {
  $('sidebar-percent').value = 35; $('sidebar-percent').dispatchEvent(new Event('change'));
  $('layout-popover').showPopover();
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
};
window.studioRenderProbe = () => {
  viewport.renderNow();
  const context = viewport.renderer.getContext();
  const width = context.drawingBufferWidth, height = context.drawingBufferHeight;
  const pixels = new Uint8Array(width * height * 4);
  context.readPixels(0, 0, width, height, context.RGBA, context.UNSIGNED_BYTE, pixels);
  const background = pixels.slice(0, 3);
  let different = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    if (Math.abs(pixels[i] - background[0]) + Math.abs(pixels[i + 1] - background[1]) + Math.abs(pixels[i + 2] - background[2]) > 35) different++;
  }
  return { differentPixels: different, totalPixels: width * height };
};
window.runStudioSmokeTest = async () => {
  await loadDemo();
  if (!state.mesh || state.mesh.triangles !== 12) throw new Error('示例模型导入失败');
  if (!viewport.smokePickFace()) throw new Error('模型面选择失败');
  lockDirection();
  if (Math.abs(state.settings.slice_direction[2] - 1) > 0.001) throw new Error('选面切片方向不正确');
  $('reset-direction').click();
  await slice();
  if (!state.model || state.model.layers.length !== 15) throw new Error('切片结果不正确');
  setLayer(11);
  $('progress-slider').value = 620; $('progress-slider').dispatchEvent(new Event('input'));
  changeSpeed(1); changeSpeed(1);
  const before = viewport.completed.geometry.instanceCount;
  const previousLayers = state.model.extrusionPrefix[state.model.layers[11].first];
  if (before < previousLayers) throw new Error('下层轨迹丢失');
  for (const value of [100, 900, 250, 750, 620]) {
    $('progress-slider').value = value; $('progress-slider').dispatchEvent(new Event('input'));
  }
  setMode('model'); $('rotation_x').value = 15; $('rotation_x').dispatchEvent(new Event('change'));
  if (!state.dirty || !$('export-job').disabled) throw new Error('参数变更没有使旧切片失效');
  $('reset-rotation').click(); await slice(); setLayer(11);
  $('progress-slider').value = 620; $('progress-slider').dispatchEvent(new Event('input'));
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return { ...window.studioLayoutProbe(), layers: state.model.layers.length, moves: state.model.moves.length,
    completedSegments: viewport.completed.geometry.instanceCount, lowerLayerSegments: previousLayers,
    speed: state.speed, webgl: viewport.renderer.getContext().getParameter(viewport.renderer.getContext().VERSION) };
};
window.studioModelScreenshot = () => {
  setMode('model'); setPanel('object'); viewport.fit(); viewport.renderNow();
};
window.studioPathScreenshot = () => { setMode('path'); setPanel('parameters'); viewport.fit(); viewport.renderNow(); };
window.studioImportSmoke = async (data) => {
  await acceptFile(data);
  if (!state.model || state.mesh || !state.imported || !$('export-job').disabled) throw new Error('G-code 导入状态错误');
  setLayer(state.model.layers.length - 1);
  $('progress-slider').value = 500; $('progress-slider').dispatchEvent(new Event('input'));
  return { name: state.model.name, layers: state.model.layers.length, moves: state.model.moves.length };
};
