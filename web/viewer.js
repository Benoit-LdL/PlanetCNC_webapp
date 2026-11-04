import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { segmentIndexForLine } from './gcode.js';

const css = (name) => new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue(name).trim());
const MAX_TRACE_POINTS = 200000;

export class Viewer {
  constructor(container) {
    this.container = container;
    this.colors = {
      todo: css('--path-todo'), done: css('--path-done'), rapid: css('--path-rapid'),
      rapidDone: css('--path-rapid').multiplyScalar(0.45), trace: css('--trace'),
    };

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x11161b);

    this.persp = new THREE.PerspectiveCamera(45, 1, 0.1, 100000);
    this.ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, -100000, 100000);
    for (const c of [this.persp, this.ortho]) c.up.set(0, 0, 1);
    this.camera = this.persp;
    this.view = '3d';

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.screenSpacePanning = true;

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(1, -1, 2);
    this.scene.add(sun);

    this.world = new THREE.Group();   // grid, axes - rebuilt per program
    this.scene.add(this.world);
    this.path = null;
    this.segLines = null;
    this.segRapid = null;
    this.doneIdx = 0;
    this.bounds = { min: [0, 0, 0], max: [100, 100, 10] };
    this.bed = null;     // machine travel in work coordinates, from TNG's motor limits

    this.tool = this._makeTool();
    this.scene.add(this.tool);
    this._buildWorld();

    const traceGeom = new THREE.BufferGeometry();
    traceGeom.setAttribute('position', new THREE.BufferAttribute(new Float32Array(MAX_TRACE_POINTS * 3), 3));
    traceGeom.setDrawRange(0, 0);
    this.trace = new THREE.Line(traceGeom, new THREE.LineBasicMaterial({ color: this.colors.trace }));
    this.trace.frustumCulled = false;
    this.traceCount = 0;
    this.scene.add(this.trace);

    this.follow = false;
    new ResizeObserver(() => this._resize()).observe(container);
    this._resize();
    this.setView('3d');
    this.renderer.setAnimationLoop(() => this._render());
  }

  // ---------------- scene setup ----------------

  _makeTool() {
    const g = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({ color: 0xd8dde3, metalness: 0.6, roughness: 0.35 });
    const tip = new THREE.Mesh(new THREE.ConeGeometry(1, 2, 24), mat);
    tip.rotation.x = -Math.PI / 2;   // point down -Z
    tip.position.z = 1;
    const shank = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 6, 24), mat);
    shank.rotation.x = Math.PI / 2;
    shank.position.z = 5;
    const holder = new THREE.Mesh(new THREE.CylinderGeometry(2.2, 2.2, 4, 24),
      new THREE.MeshStandardMaterial({ color: 0x3fc1e6, metalness: 0.3, roughness: 0.5 }));
    holder.rotation.x = Math.PI / 2;
    holder.position.z = 10;
    g.add(tip, shank, holder);
    return g;
  }

  _buildWorld() {
    for (const c of [...this.world.children]) {
      this.world.remove(c);
      c.geometry?.dispose();
      c.material?.dispose();
    }
    const { min, max } = this.bounds;
    const bed = this.bed;
    let x0, y0, x1, y1, gz, step;
    if (bed) {
      // Grid covers the machine's whole travel, at the bottom of the Z travel (like TNG's table).
      step = niceStep(Math.max(bed.max[0] - bed.min[0], bed.max[1] - bed.min[1]) / 10);
      [x0, y0, x1, y1, gz] = [bed.min[0], bed.min[1], bed.max[0], bed.max[1], bed.min[2]];
    } else {
      step = niceStep(Math.max(max[0] - min[0], max[1] - min[1], 10) / 10);
      x0 = Math.floor(Math.min(min[0], 0) / step) * step;
      y0 = Math.floor(Math.min(min[1], 0) / step) * step;
      x1 = Math.ceil(Math.max(max[0], 0) / step) * step;
      y1 = Math.ceil(Math.max(max[1], 0) / step) * step;
      gz = 0;
    }

    // Grid lines on round work-coordinate values, plus the edges
    const pts = [];
    const lines = (lo, hi) => {
      const v = [lo];
      for (let t = Math.ceil(lo / step) * step; t < hi - 1e-6; t += step) if (t > lo + 1e-6) v.push(t);
      return v.concat(hi);
    };
    for (const x of lines(x0, x1)) pts.push(x, y0, gz, x, y1, gz);
    for (const y of lines(y0, y1)) pts.push(x0, y, gz, x1, y, gz);
    const grid = new THREE.LineSegments(
      new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(pts, 3)),
      new THREE.LineBasicMaterial({ color: 0x2a3540 }));
    this.world.add(grid);

    // Machine travel volume
    if (bed) {
      const box = new THREE.Box3(new THREE.Vector3(...bed.min), new THREE.Vector3(...bed.max));
      this.world.add(new THREE.Box3Helper(box, 0x55626e));
    }

    // Work zero axes (X red, Y green, Z blue) like the PlanetCNC origin marker
    const a = step * 1.5;
    const axes = new THREE.LineSegments(
      new THREE.BufferGeometry()
        .setAttribute('position', new THREE.Float32BufferAttribute([0, 0, 0, a, 0, 0, 0, 0, 0, 0, a, 0, 0, 0, 0, 0, 0, a], 3))
        .setAttribute('color', new THREE.Float32BufferAttribute([1, .3, .3, 1, .3, .3, .3, 1, .3, .3, 1, .3, .3, .5, 1, .3, .5, 1], 3)),
      new THREE.LineBasicMaterial({ vertexColors: true }));
    this.world.add(axes);

    // Toolpath bounding box
    if (this.path) {
      const box = new THREE.Box3(new THREE.Vector3(...min), new THREE.Vector3(...max));
      const helper = new THREE.Box3Helper(box, 0x4a5763);
      this.world.add(helper);
    }

    this.tool.scale.setScalar(Math.max(step / 6, 0.5));
  }

  /** Machine travel in work coordinates ({min, max} or null); redraws only when it really changed. */
  setBed(bed) {
    const same = (a, b) => (!a && !b) || (a && b && [0, 1, 2].every((k) =>
      Math.abs(a.min[k] - b.min[k]) < 0.01 && Math.abs(a.max[k] - b.max[k]) < 0.01));
    if (same(bed, this.bed)) return;
    const first = !this.bed && bed;
    this.bed = bed;
    this._buildWorld();
    if (first) this.fit();
  }

  /** What Fit shows: the program and the machine bed together. */
  _fitBounds() {
    const b = this.path || !this.bed ? this.bounds : this.bed;
    if (!this.bed) return b;
    return {
      min: [0, 1, 2].map((k) => Math.min(b.min[k], this.bed.min[k])),
      max: [0, 1, 2].map((k) => Math.max(b.max[k], this.bed.max[k])),
    };
  }

  // ---------------- program ----------------

  setProgram(parsed) {
    if (this.path) {
      this.scene.remove(this.path);
      this.path.geometry.dispose();
      this.path.material.dispose();
      this.path = null;
    }
    this.segLines = parsed?.lines ?? null;
    this.segRapid = parsed?.rapid ?? null;
    this.doneIdx = 0;
    if (parsed && parsed.count) {
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.BufferAttribute(parsed.positions, 3));
      geom.setAttribute('color', new THREE.BufferAttribute(new Float32Array(parsed.count * 6), 3));
      this.path = new THREE.LineSegments(geom, new THREE.LineBasicMaterial({ vertexColors: true }));
      this.path.frustumCulled = false;
      this.scene.add(this.path);
      this._colorRange(0, parsed.count, false);
      this.bounds = parsed.bounds;
    } else {
      this.bounds = { min: [0, 0, 0], max: [100, 100, 10] };
    }
    this._buildWorld();
    this.clearTrace();
    this.fit();
  }

  _colorRange(from, to, done) {
    const col = this.path.geometry.attributes.color;
    const arr = col.array;
    for (let i = from; i < to; i++) {
      const c = this.segRapid[i]
        ? (done ? this.colors.rapidDone : this.colors.rapid)
        : (done ? this.colors.done : this.colors.todo);
      const o = i * 6;
      arr[o] = arr[o + 3] = c.r;
      arr[o + 1] = arr[o + 4] = c.g;
      arr[o + 2] = arr[o + 5] = c.b;
    }
    col.needsUpdate = true;
  }

  /** Color everything before the given source line as done. */
  setCurrentLine(lineNo) {
    if (!this.path) return;
    const idx = segmentIndexForLine(this.segLines, lineNo);
    if (idx > this.doneIdx) this._colorRange(this.doneIdx, idx, true);
    else if (idx < this.doneIdx) this._colorRange(idx, this.doneIdx, false);
    this.doneIdx = idx;
  }

  // ---------------- tool + trace ----------------

  setToolPosition(x, y, z, recordTrace) {
    this.tool.position.set(x, y, z);
    if (recordTrace) this._addTrace(x, y, z);
    if (this.follow) this._centerOn(this.tool.position);
  }

  _addTrace(x, y, z) {
    const attr = this.trace.geometry.attributes.position;
    if (this.traceCount > 0) {
      const o = (this.traceCount - 1) * 3;
      const a = attr.array;
      if (Math.abs(a[o] - x) + Math.abs(a[o + 1] - y) + Math.abs(a[o + 2] - z) < 1e-4) return;
    }
    if (this.traceCount >= MAX_TRACE_POINTS) {
      // Drop the oldest half
      const half = MAX_TRACE_POINTS / 2;
      attr.array.copyWithin(0, half * 3);
      this.traceCount = half;
    }
    attr.array.set([x, y, z], this.traceCount * 3);
    this.traceCount++;
    this.trace.geometry.setDrawRange(0, this.traceCount);
    attr.needsUpdate = true;
  }

  clearTrace() {
    this.traceCount = 0;
    this.trace.geometry.setDrawRange(0, 0);
  }

  setTraceVisible(v) { this.trace.visible = v; }
  setFollow(v) { this.follow = v; if (v) this._centerOn(this.tool.position); }

  // ---------------- camera ----------------

  setView(view) {
    this.view = view;
    const useOrtho = view !== '3d';
    const cam = useOrtho ? this.ortho : this.persp;
    if (cam !== this.camera) {
      this.camera = cam;
      this.controls.object = cam;
    }
    this.controls.enableRotate = !useOrtho;
    this._resize();
    this.fit();
  }

  fit() {
    const { min, max } = this._fitBounds();
    const center = new THREE.Vector3((min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2);
    const size = new THREE.Vector3(max[0] - min[0], max[1] - min[1], max[2] - min[2]);
    const radius = Math.max(size.length() / 2, 5);
    const dir = {
      '3d': new THREE.Vector3(-0.6, -1.2, 1.0),
      top: new THREE.Vector3(0, 0, 1),
      front: new THREE.Vector3(0, -1, 0),
      right: new THREE.Vector3(1, 0, 0),
    }[this.view].normalize();

    // Looking straight down needs a non-parallel up vector
    this.camera.up.set(0, this.view === 'top' ? 1 : 0, this.view === 'top' ? 0 : 1);

    this.controls.target.copy(center);
    if (this.camera.isPerspectiveCamera) {
      const dist = radius / Math.sin((this.persp.fov * Math.PI) / 360) * 1.05;
      this.camera.position.copy(center).addScaledVector(dir, dist);
    } else {
      this.camera.position.copy(center).addScaledVector(dir, radius * 10);
      const aspect = this._aspect();
      const w = this.view === 'top' ? size.x : this.view === 'front' ? size.x : size.y;
      const h = this.view === 'top' ? size.y : size.z;
      const half = Math.max(w / 2 / aspect, h / 2, 5) * 1.15;
      this.ortho.zoom = 1;
      this._orthoHalf = half;
      this._applyOrtho();
    }
    this.camera.lookAt(center);
    this.controls.update();
  }

  _centerOn(p) {
    const d = p.clone().sub(this.controls.target);
    this.controls.target.add(d);
    this.camera.position.add(d);
  }

  _aspect() {
    const r = this.container.getBoundingClientRect();
    return Math.max(r.width, 1) / Math.max(r.height, 1);
  }

  _applyOrtho() {
    const half = this._orthoHalf ?? 100;
    const aspect = this._aspect();
    Object.assign(this.ortho, { left: -half * aspect, right: half * aspect, top: half, bottom: -half });
    this.ortho.updateProjectionMatrix();
  }

  _resize() {
    const r = this.container.getBoundingClientRect();
    const w = Math.max(r.width, 1), h = Math.max(r.height, 1);
    this.renderer.setSize(w, h, false);
    this.persp.aspect = w / h;
    this.persp.updateProjectionMatrix();
    this._applyOrtho();
  }

  _render() {
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / p;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * p;
}
