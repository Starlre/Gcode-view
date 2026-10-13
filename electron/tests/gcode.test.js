import test from 'node:test';
import assert from 'node:assert/strict';
import { parseGcode, moveAtTime } from '../src/gcode.js';

test('两层的沉积、空移、曝光及时间边界可以分别预览', () => {
  const model = parseGcode(`G21\nG90\nG28 X Y Z\n;LAYER_START:0\nG0 X1 Y1 Z0.2 F600\nM101\nG1 X11 Y1 F600\nG1 X11 Y11\nM102\nM106 S255\nG4 P1000\nM106 S0\n;LAYER_START:1\nG0 Z0.4\nM101\nG1 X1 Y11\nM102`);
  assert.equal(model.layers.length, 2);
  assert.equal(model.moves.filter((move) => move.extrude).length, 3);
  assert.equal(model.moves.find((move) => move.dwell).light, 255);
  assert.equal(model.layers[0].z, 0.2);
  assert.equal(model.layers[1].z, 0.4);
  const move = model.moves[1];
  assert.deepEqual(moveAtTime(model, move.t0 + 0.5).point, [6, 1, 0.2]);
  assert.equal(model.extrusionPrefix[model.layers[1].first], 2);
  assert.equal(model.extrusion.length / 6, 3);
});

test('G92 改变坐标读数，物理路径仍然连续', () => {
  const model = parseGcode('G90\nG0 X10 F600\nG92 X0\nM101\nG1 X5');
  assert.equal(model.moves.at(-1).end[0], 15);
  assert.equal(model.moves.at(-1).distance, 5);
});

test('相对坐标、相对挤出和英寸能正确换算', () => {
  const model = parseGcode('G20\nG91\nM83\nG1 X1 E0.1 F60\nG1 Y1 E0.1');
  assert.deepEqual(model.moves.at(-1).end, [25.4, 25.4, 0]);
  assert.equal(model.moves.every((move) => move.extrude), true);
  assert.equal(model.totalTime, 2);
});

test('完整圆弧闭合，并保留总长度', () => {
  const model = parseGcode('G90\nG0 X5 Y0 F600\nM101\nG3 X5 Y0 I-5 J0');
  assert.deepEqual(model.moves.at(-1).end, [5, 0, 0]);
  const arcLength = model.moves.filter((move) => move.extrude).reduce((sum, move) => sum + move.distance, 0);
  assert.ok(Math.abs(arcLength - 10 * Math.PI) < 0.01);
});

test('没有层注释的 FDM 文件按沉积高度分层', () => {
  const model = parseGcode('G0 Z0.2\nG1 X10 E1 F600\nG0 Z0.4\nG1 Y10 E2');
  assert.deepEqual(model.layers.map((layer) => layer.z), [0.2, 0.4]);
});

test('拒绝没有走刀的文件和无效速度', () => {
  assert.throws(() => parseGcode('M101\nM102'), /没有/);
  assert.throws(() => parseGcode('G1 X10 F0'), /速度/);
});
