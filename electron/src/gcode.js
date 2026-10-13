const commandPattern = /^([GMT])\s*(\d+)/i;
const numberPattern = /([XYZEFIJKRPS])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?)/gi;
const distance = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);

function arcPoints(start, end, args, clockwise) {
  let cx, cy;
  if ('I' in args || 'J' in args) {
    cx = start[0] + (args.I || 0); cy = start[1] + (args.J || 0);
  } else if ('R' in args) {
    const dx = end[0] - start[0], dy = end[1] - start[1], chord = Math.hypot(dx, dy);
    const radius = Math.abs(args.R);
    if (chord < 1e-8 || chord > radius * 2) return [end];
    const height = Math.sqrt(Math.max(0, radius * radius - chord * chord / 4));
    const sign = (clockwise ? -1 : 1) * (args.R < 0 ? -1 : 1);
    cx = (start[0] + end[0]) / 2 - dy / chord * height * sign;
    cy = (start[1] + end[1]) / 2 + dx / chord * height * sign;
  } else return [end];
  const begin = Math.atan2(start[1] - cy, start[0] - cx);
  let sweep = Math.atan2(end[1] - cy, end[0] - cx) - begin;
  if (clockwise && sweep >= -1e-10) sweep -= Math.PI * 2;
  if (!clockwise && sweep <= 1e-10) sweep += Math.PI * 2;
  const radius = Math.hypot(start[0] - cx, start[1] - cy);
  const count = Math.max(8, Math.min(1024, Math.ceil(Math.abs(sweep) * radius / 0.3)));
  const points = [];
  for (let i = 1; i <= count; i++) {
    const t = i / count, angle = begin + sweep * t;
    points.push(i === count ? end : [cx + radius * Math.cos(angle), cy + radius * Math.sin(angle),
      start[2] + (end[2] - start[2]) * t, start[3] + (end[3] - start[3]) * t]);
  }
  return points;
}

export function parseGcode(text, name = 'run.gcode') {
  const moves = [], layers = [], metadata = {}, warnings = new Set();
  let position = [0, 0, 0, 0], offsets = [0, 0, 0], absolute = true, absoluteE = true;
  let units = 1, feed = 1200, active = false, light = 0, totalTime = 0, totalDistance = 0;
  let currentLayer = -1, implicitZ = null, material = 'A';
  const explicitLayers = /;\s*(?:LAYER_START|LAYER)\s*:/i.test(text);
  const register = (end, code, raw, line, extrude, duration) => {
    const length = distance(position, end);
    if (currentLayer < 0 && !explicitLayers && (extrude || layers.length === 0)) {
      currentLayer = layers.length;
      layers.push({ name: String(currentLayer + 1), z: end[2], first: moves.length, last: moves.length });
      implicitZ = end[2];
    } else if (!explicitLayers && extrude && Math.abs(end[2] - implicitZ) > 0.001) {
      currentLayer = layers.length;
      layers.push({ name: String(currentLayer + 1), z: end[2], first: moves.length, last: moves.length });
      implicitZ = end[2];
    }
    moves.push({ start: position.slice(0, 3), end: end.slice(0, 3), code, raw, line, feed,
      extrude, dwell: code === 'G4', light, material, layer: currentLayer,
      t0: totalTime, t1: totalTime + duration, duration, distance: length });
    if (currentLayer >= 0) {
      layers[currentLayer].last = moves.length - 1;
      if (extrude) layers[currentLayer].z = end[2];
    }
    totalTime += duration; totalDistance += length; position = end;
  };
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i].trim();
    const meta = raw.match(/^;\s*([^:]+):\s*(.*)$/);
    if (meta) metadata[meta[1].trim()] = meta[2].trim();
    const layerMatch = raw.match(/;\s*(?:LAYER_START|LAYER)\s*:\s*(-?\d+)/i);
    if (layerMatch) {
      currentLayer = layers.length;
      layers.push({ name: String(Number(layerMatch[1]) + 1), z: position[2], first: moves.length, last: moves.length - 1 });
    }
    const materialMatch = raw.match(/;\s*MATERIAL\s*:\s*([AB])/i);
    if (materialMatch) material = materialMatch[1].toUpperCase();
    const code = raw.split(';')[0].replace(/\([^)]*\)/g, '').replace(/^N\d+\s*/i, '').split('*')[0].trim();
    const match = code.match(commandPattern);
    if (!match) continue;
    const command = match[1].toUpperCase() + Number(match[2]);
    const args = Object.fromEntries([...code.slice(match[0].length).matchAll(numberPattern)].map(([_, key, value]) => [key.toUpperCase(), Number(value)]));
    if (Object.values(args).some((v) => !Number.isFinite(v))) throw new Error(`第 ${i + 1} 行包含无效数值`);
    if (command === 'G20') units = 25.4;
    else if (command === 'G21') units = 1;
    else if (command === 'G90') absolute = true;
    else if (command === 'G91') absolute = false;
    else if (command === 'M82') absoluteE = true;
    else if (command === 'M83') absoluteE = false;
    else if (command === 'M101') active = true;
    else if (command === 'M102') active = false;
    else if (command === 'M106' || command === 'M3') light = args.S ?? 255;
    else if (command === 'M107' || command === 'M5') light = 0;
    else if (command === 'T0' || command === 'T1') material = command === 'T0' ? 'A' : 'B';
    else if (command === 'M2' || command === 'M30') break;
    else if (command === 'G28') {
      const axes = ['X', 'Y', 'Z'].filter((axis) => axis in args || new RegExp(`\\b${axis}\\b`, 'i').test(code));
      for (const axis of axes.length ? axes : ['X', 'Y', 'Z']) {
        const index = 'XYZ'.indexOf(axis); position[index] = 0; offsets[index] = 0;
      }
    } else if (command === 'G92') {
      for (let axis = 0; axis < 3; axis++) if ('XYZ'[axis] in args) offsets[axis] = position[axis] - args['XYZ'[axis]] * units;
      if ('E' in args) position[3] = args.E * units;
    } else if (command === 'G4') {
      const seconds = Math.max(0, args.P !== undefined ? args.P / 1000 : args.S || 0);
      if (seconds) register(position.slice(), command, raw, i + 1, false, seconds);
    } else if (['G0', 'G1', 'G2', 'G3'].includes(command)) {
      if ('F' in args) feed = args.F * units;
      if (feed <= 0) throw new Error(`第 ${i + 1} 行速度必须大于零`);
      const end = position.map((value, axis) => {
        const key = 'XYZE'[axis];
        if (!(key in args)) return value;
        const isAbsolute = axis === 3 ? absoluteE : absolute;
        return isAbsolute ? args[key] * units + (offsets[axis] || 0) : value + args[key] * units;
      });
      const extrude = command !== 'G0' && (active || end[3] > position[3] + 1e-8);
      const scaledArgs = Object.fromEntries(Object.entries(args).map(([key, value]) => [key, value * units]));
      const points = command === 'G2' || command === 'G3' ? arcPoints(position, end, scaledArgs, command === 'G2') : [end];
      for (const point of points) {
        const length = distance(position, point);
        if (length > 1e-8) register(point, command, raw, i + 1, extrude, length / feed * 60);
      }
      position = end;
    } else if (!['G94', 'M18', 'M84', 'M104', 'M109', 'M140', 'M190', 'M6054'].includes(command)) warnings.add(command);
  }
  if (!moves.length) throw new Error('文件中没有可以预览的走刀或停留指令');
  for (const layer of layers) {
    layer.startTime = moves[layer.first]?.t0 || 0;
    layer.endTime = moves[layer.last]?.t1 || layer.startTime;
  }
  const extrusion = [], travel = [], extrusionPrefix = new Uint32Array(moves.length + 1), travelPrefix = new Uint32Array(moves.length + 1);
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  let extrusionCount = 0, travelCount = 0;
  moves.forEach((move, index) => {
    extrusionPrefix[index] = extrusionCount; travelPrefix[index] = travelCount;
    if (!move.dwell) {
      (move.extrude ? extrusion : travel).push(...move.start, ...move.end);
      move.extrude ? extrusionCount++ : travelCount++;
      for (const point of [move.start, move.end]) for (let axis = 0; axis < 3; axis++) {
        min[axis] = Math.min(min[axis], point[axis]); max[axis] = Math.max(max[axis], point[axis]);
      }
    }
  });
  extrusionPrefix[moves.length] = extrusionCount; travelPrefix[moves.length] = travelCount;
  if (!Number.isFinite(min[0])) min.fill(0), max.fill(0);
  return { name, moves, layers, metadata, totalTime, totalDistance, lineCount: lines.length,
    warnings: [...warnings], bounds: { min, max }, extrusion: new Float32Array(extrusion),
    travel: new Float32Array(travel), extrusionPrefix, travelPrefix };
}

export function moveAtTime(model, time) {
  let lo = 0, hi = model.moves.length - 1;
  while (lo < hi) {
    const middle = (lo + hi) >>> 1;
    if (model.moves[middle].t1 <= time) lo = middle + 1;
    else hi = middle;
  }
  const move = model.moves[lo];
  const fraction = move.duration ? Math.max(0, Math.min(1, (time - move.t0) / move.duration)) : 1;
  return { index: lo, move, fraction, point: move.start.map((value, axis) => value + (move.end[axis] - value) * fraction) };
}

export function formatTime(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return `${hours ? hours + ':' : ''}${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}
