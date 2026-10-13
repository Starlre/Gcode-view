import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { moveAtTime } from './gcode.js';

function dispose(object) {
  object?.traverse((child) => {
    child.geometry?.dispose();
    if (Array.isArray(child.material)) child.material.forEach((m) => m.dispose());
    else if (!child.material?.userData.shared) child.material?.dispose();
  });
  object?.removeFromParent();
}

export class Viewport {
  constructor(host, onFace) {
    this.host = host; this.onFace = onFace; this.width = 80; this.height = 45;
    this.scene = new THREE.Scene(); this.scene.background = new THREE.Color('#edf1f4');
    this.camera = new THREE.PerspectiveCamera(38, 1, 0.05, 2000);
    this.camera.up.set(0, 0, 1);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.host.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true; this.controls.dampingFactor = 0.12;
    this.controls.minDistance = 2; this.controls.maxDistance = 1000;
    this.controls.addEventListener('change', () => this.invalidate());
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x73818e, 3));
    const light = new THREE.DirectionalLight(0xffffff, 3);
    light.position.set(-30, -60, 140); this.scene.add(light);
    this.materials = [];
    this.completedMaterial = this.wideMaterial(0x11955b, 2.0);
    this.futureMaterial = this.wideMaterial(0x527e96, 1.6);
    this.partialMaterial = this.wideMaterial(0x11955b, 2.6);
    this.needle = this.makeNeedle(); this.scene.add(this.needle); this.needle.visible = false;
    this.gizmoScene = new THREE.Scene();
    this.gizmoCamera = new THREE.PerspectiveCamera(35, 1, 0.1, 20);
    this.gizmoCamera.up.set(0, 0, 1);
    this.gizmoScene.add(new THREE.AxesHelper(0.85));
    const colors = ['#ce4451', '#188250', '#346cdb'];
    for (let axis = 0; axis < 3; axis++) {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 64;
      const context = canvas.getContext('2d'); context.font = 'bold 36px Segoe UI';
      context.textAlign = 'center'; context.textBaseline = 'middle'; context.fillStyle = colors[axis];
      context.fillText('XYZ'[axis], 32, 32);
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(canvas), depthTest: false }));
      sprite.position.setComponent(axis, 1.15); sprite.scale.set(0.5, 0.5, 1);
      this.gizmoScene.add(sprite);
    }
    this.setBed(80, 45); this.fit();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.renderer.domElement.addEventListener('pointerdown', (event) => { this.down = [event.clientX, event.clientY]; });
    this.renderer.domElement.addEventListener('pointerup', (event) => this.pick(event));
    this.renderer.domElement.addEventListener('pointermove', () => {
      this.renderer.domElement.style.cursor = this.pickMode && this.modelMesh?.visible ? 'crosshair' : 'grab';
    });
  }

  wideMaterial(color, linewidth) {
    const material = new LineMaterial({ color, linewidth, depthTest: false, depthWrite: false });
    material.userData.shared = true;
    this.materials.push(material); return material;
  }

  lineObject(positions, material) {
    const geometry = new LineSegmentsGeometry();
    geometry.setPositions(positions.length ? positions : new Float32Array(6));
    geometry.instanceCount = positions.length / 6;
    const lines = new LineSegments2(geometry, material);
    lines.frustumCulled = false;
    return lines;
  }

  setBed(width, height) {
    this.width = width; this.height = height;
    dispose(this.bed);
    this.bed = new THREE.Group();
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(width, height),
      new THREE.MeshStandardMaterial({ color: 0xf9fafb, roughness: 0.85, metalness: 0.04, side: THREE.DoubleSide }));
    plane.position.set(width / 2, height / 2, -0.04); this.bed.add(plane);
    const points = [];
    for (let x = 0; x <= width; x += 5) points.push(x, 0, 0, x, height, 0);
    for (let y = 0; y <= height; y += 5) points.push(0, y, 0, width, y, 0);
    const grid = new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(points, 3)),
      new THREE.LineBasicMaterial({ color: 0xc4d0d8, transparent: true, opacity: 0.7 }));
    this.bed.add(grid);
    const outline = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0, 0), new THREE.Vector3(width, 0, 0),
      new THREE.Vector3(width, height, 0), new THREE.Vector3(0, height, 0)]),
    new THREE.LineBasicMaterial({ color: 0x9baebb }));
    this.bed.add(outline);
    const axes = new THREE.AxesHelper(12); axes.position.z = 0.06; this.bed.add(axes);
    this.scene.add(this.bed); this.theme(this.dark ?? false); this.invalidate();
  }

  setMesh(data, settings) {
    this.meshData = data;
    dispose(this.modelGroup); dispose(this.selection); this.selectedNormal = null;
    if (!data) { this.modelGroup = this.modelMesh = null; this.invalidate(); return; }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(data.positions, 3));
    const b = data.bounds;
    geometry.translate(-(b[0] + b[1]) / 2, -(b[2] + b[3]) / 2, -(b[4] + b[5]) / 2);
    geometry.computeVertexNormals();
    this.modelMesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
      color: 0x668fa4, roughness: 0.55, metalness: 0.12, side: THREE.DoubleSide,
      transparent: true, opacity: 1, flatShading: true }));
    this.modelGroup = new THREE.Group(); this.rotationGroup = new THREE.Group();
    this.rotationGroup.add(this.modelMesh);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 28),
      new THREE.LineBasicMaterial({ color: 0x355e76, transparent: true, opacity: 0.36 }));
    this.rotationGroup.add(edges); this.modelGroup.add(this.rotationGroup); this.scene.add(this.modelGroup);
    this.updateMeshSettings(settings); this.setMode('model'); this.fit();
  }

  updateMeshSettings(settings) {
    if (!this.modelGroup) return;
    this.rotationGroup.rotation.set(...['rotation_x', 'rotation_y', 'rotation_z'].map((key) => THREE.MathUtils.degToRad(settings[key] || 0)), 'ZYX');
    this.modelGroup.scale.set(1, 1, 1); this.modelGroup.position.set(0, 0, 0);
    this.modelGroup.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(this.modelGroup), size = box.getSize(new THREE.Vector3());
    const scale = settings.auto_scale ? Math.min(1, (settings.bed_x - 2 * settings.margin) / Math.max(size.x, 0.001),
      (settings.bed_y - 2 * settings.margin) / Math.max(size.y, 0.001)) : 1;
    this.modelGroup.scale.set(Math.max(scale, 0.001), Math.max(scale, 0.001), 1);
    const cx = settings.center_model ? settings.bed_x / 2 : settings.margin + size.x * scale / 2;
    const cy = settings.center_model ? settings.bed_y / 2 : settings.margin + size.y * scale / 2;
    this.modelGroup.position.set(cx, cy, -box.min.z);
    this.modelGroup.updateMatrixWorld(true);
    dispose(this.selection); this.selection = null; this.selectedNormal = null;
    this.invalidate();
  }

  setToolpath(model) {
    dispose(this.paths); this.paths = new THREE.Group(); this.model = model; this.activeLayer = -1;
    this.completed = this.lineObject(model.extrusion, this.completedMaterial);
    this.completed.geometry.instanceCount = 0; this.completed.renderOrder = 3;
    this.paths.add(this.completed);
    this.partial = this.lineObject(new Float32Array(6), this.partialMaterial); this.partial.renderOrder = 4;
    this.paths.add(this.partial); this.scene.add(this.paths); this.setMode('path');
    this.setLayer(0); this.setProgress(model.layers[0]?.startTime || 0); this.fit();
  }

  clearToolpath() { dispose(this.paths); this.paths = null; this.model = null; this.needle.visible = false; }

  setLayer(index) {
    if (!this.model || this.activeLayer === index) return;
    this.activeLayer = index;
    const layer = this.model.layers[index];
    dispose(this.future); dispose(this.travelLines);
    const first = this.model.extrusionPrefix[layer.first], last = this.model.extrusionPrefix[layer.last + 1];
    this.future = this.lineObject(this.model.extrusion.subarray(first * 6, last * 6), this.futureMaterial);
    this.future.renderOrder = 2; this.paths.add(this.future);
    const ta = this.model.travelPrefix[layer.first], tb = this.model.travelPrefix[layer.last + 1];
    this.travelLines = new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position',
      new THREE.BufferAttribute(this.model.travel.slice(ta * 6, tb * 6), 3)),
    new THREE.LineBasicMaterial({ color: 0x8f9da7, transparent: true, opacity: 0.5, depthTest: false }));
    this.travelLines.visible = this.showTravel ?? false; this.paths.add(this.travelLines);
    this.invalidate();
  }

  setProgress(time) {
    if (!this.model) return;
    let current = moveAtTime(this.model, time);
    const layer = this.model.layers[this.activeLayer];
    if (layer && current.index > layer.last) {
      const move = this.model.moves[layer.last];
      current = { index: layer.last, move, fraction: 1, point: move.end.slice() };
    }
    this.completed.geometry.instanceCount = this.model.extrusionPrefix[current.index] + (current.fraction >= 1 && current.move.extrude ? 1 : 0);
    this.partial.visible = current.move.extrude && current.fraction < 1;
    if (this.partial.visible) {
      const buffer = this.partial.geometry.attributes.instanceStart.data;
      buffer.array.set([...current.move.start, ...current.point]);
      buffer.needsUpdate = true;
    }
    this.needle.position.set(...current.point);
    this.needle.visible = this.mode === 'path' && (this.showNeedle ?? true);
    this.invalidate(); return current;
  }

  setMode(mode) {
    this.mode = mode;
    if (this.modelGroup) this.modelGroup.visible = mode === 'model';
    if (this.paths) this.paths.visible = mode === 'path';
    if (this.selection) this.selection.visible = mode === 'model';
    this.needle.visible = mode === 'path' && !!this.model && (this.showNeedle ?? true);
    this.invalidate();
  }

  makeNeedle() {
    const group = new THREE.Group();
    const metal = new THREE.MeshStandardMaterial({ color: 0xb2bdc7, metalness: 0.85, roughness: 0.25 });
    const tip = new THREE.Mesh(new THREE.ConeGeometry(0.24, 1.6, 16), metal);
    tip.rotation.x = -Math.PI / 2; tip.position.z = 0.8; group.add(tip);
    const shaft = new THREE.Mesh(new THREE.CylinderGeometry(0.24, 0.24, 3.5, 16), metal);
    shaft.rotation.x = Math.PI / 2; shaft.position.z = 3.35; group.add(shaft);
    const body = new THREE.Mesh(new THREE.CylinderGeometry(0.8, 0.55, 2, 24),
      new THREE.MeshStandardMaterial({ color: 0x344854, metalness: 0.3, roughness: 0.5 }));
    body.rotation.x = Math.PI / 2; body.position.z = 6; group.add(body);
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.65, 0.8, 32),
      new THREE.MeshBasicMaterial({ color: 0x11955b, side: THREE.DoubleSide, depthTest: false }));
    ring.position.z = 0.03; group.add(ring); return group;
  }

  fit(preset = 'iso') {
    const target = new THREE.Vector3(this.width / 2, this.height / 2, 0);
    let size = Math.max(this.width, this.height);
    if (this.modelGroup && this.mode === 'model') {
      const box = new THREE.Box3().setFromObject(this.modelGroup);
      box.getCenter(target); size = Math.max(20, box.getSize(new THREE.Vector3()).length());
    } else if (this.model && this.mode === 'path' && this.model.extrusion.length) {
      const box = new THREE.Box3();
      const point = new THREE.Vector3();
      for (let i = 0; i < this.model.extrusion.length; i += 3) {
        point.fromArray(this.model.extrusion, i); box.expandByPoint(point);
      }
      box.getCenter(target); size = Math.max(20, box.getSize(new THREE.Vector3()).length());
    }
    const directions = { iso: [0.9, -1.2, 0.95], top: [0, -0.001, 1], front: [0, -1, 0.001], side: [1, 0, 0.001] };
    const direction = new THREE.Vector3(...(directions[preset] || directions.iso)).normalize();
    this.controls.target.copy(target); this.camera.position.copy(target).addScaledVector(direction, size * 1.9 / Math.min(1, this.camera.aspect));
    this.controls.update(); this.invalidate();
  }

  direction() { return this.camera.position.clone().sub(this.controls.target).normalize().toArray(); }

  pick(event) {
    if (!this.pickMode || !this.modelMesh || this.mode !== 'model' || !this.down) return;
    if (Math.hypot(event.clientX - this.down[0], event.clientY - this.down[1]) > 5) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const point = new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    const raycaster = new THREE.Raycaster(); raycaster.setFromCamera(point, this.camera);
    const hit = raycaster.intersectObject(this.modelMesh)[0];
    if (!hit) return;
    this.selectedNormal = hit.face.normal.clone().applyNormalMatrix(new THREE.Matrix3().getNormalMatrix(this.modelMesh.matrixWorld)).normalize();
    dispose(this.selection);
    const positions = this.modelMesh.geometry.getAttribute('position');
    const vertices = [hit.face.a, hit.face.b, hit.face.c].map((index) => new THREE.Vector3().fromBufferAttribute(positions, index).applyMatrix4(this.modelMesh.matrixWorld));
    this.selection = new THREE.Mesh(new THREE.BufferGeometry().setFromPoints(vertices),
      new THREE.MeshBasicMaterial({ color: 0x25a76f, transparent: true, opacity: 0.5, side: THREE.DoubleSide, depthTest: false }));
    this.selection.renderOrder = 5; this.scene.add(this.selection); this.faceOn(); this.onFace?.();
  }

  faceOn() {
    if (!this.selectedNormal) return false;
    const distance = this.camera.position.distanceTo(this.controls.target);
    this.camera.position.copy(this.controls.target).addScaledVector(this.selectedNormal, distance);
    this.controls.update(); this.invalidate(); return true;
  }

  theme(dark) {
    this.dark = dark;
    this.scene.background.set(dark ? '#1b242c' : '#edf1f4');
    this.completedMaterial.color.set(dark ? '#4de098' : '#11955b');
    this.partialMaterial.color.copy(this.completedMaterial.color);
    this.futureMaterial.color.set(dark ? '#7dabc1' : '#527e96');
    this.bed.children[0].material.color.set(dark ? '#2c3a46' : '#f9fafb');
    this.bed.children[1].material.color.set(dark ? '#657b8c' : '#c4d0d8');
    this.invalidate();
  }

  resize() {
    const width = this.host.clientWidth, height = this.host.clientHeight;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height; this.camera.updateProjectionMatrix();
    for (const material of this.materials) material.resolution.set(width, height);
    this.invalidate();
  }

  invalidate() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null; this.renderNow();
    });
  }

  renderNow() {
      this.controls.update();
      const width = this.host.clientWidth, height = this.host.clientHeight;
      this.renderer.setViewport(0, 0, width, height); this.renderer.render(this.scene, this.camera);
      this.renderer.autoClear = false; this.renderer.clearDepth();
      this.gizmoCamera.position.copy(this.camera.position).sub(this.controls.target).normalize().multiplyScalar(4.5);
      this.gizmoCamera.lookAt(0, 0, 0);
      this.renderer.setViewport(width - 112, 18, 90, 90);
      this.renderer.render(this.gizmoScene, this.gizmoCamera); this.renderer.autoClear = true;
  }

  smokePickFace() {
    this.pickMode = true; this.fit('top'); this.renderNow();
    const screenPoint = this.controls.target.clone().project(this.camera);
    const rect = this.renderer.domElement.getBoundingClientRect();
    const x = rect.left + (screenPoint.x + 1) / 2 * rect.width;
    const y = rect.top + (1 - screenPoint.y) / 2 * rect.height;
    this.down = [x, y]; this.pick({ clientX: x, clientY: y });
    this.pickMode = false;
    return !!this.selectedNormal;
  }
}
