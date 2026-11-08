import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const css = (name) => new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue(name).trim());
const MAX_TRACE_POINTS = 200000;

export class Viewer {
  constructor(container) {
    this.container = container;
    this.colors = { top: css('--path-top'), deep: css('--path-deep'), rapid: css('--rapid'), trace: css('--trace') };

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
    // Draw only when something changed: a big toolpath redrawn 60 times a second makes laptops crawl.
    this.dirty = true;
    this.controls.addEventListener('change', () => { this.dirty = true; });

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(1, -1, 2);
    this.scene.add(sun);

    this.world = new THREE.Group();   // grid, axes - rebuilt per program
    this.scene.add(this.world);
    this.hasProgram = false;
    this.path = null;
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

  /** The tool, with its tip at the group's origin. Without known dimensions it's a generic
   *  bit sized to the grid; with them (setToolShape) the cutter is drawn at its real size, with
   *  a small see-through holder above it. */
  _makeTool() {
    const g = new THREE.Group();
    this.metal = new THREE.MeshStandardMaterial({ color: 0xd8dde3, metalness: 0.6, roughness: 0.35, side: THREE.DoubleSide });
    this.generic = new THREE.Group();
    const tip = new THREE.Mesh(new THREE.ConeGeometry(1, 2, 24), this.metal);
    tip.rotation.x = -Math.PI / 2;   // point down -Z
    tip.position.z = 1;
    const shank = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 6, 24), this.metal);
    shank.rotation.x = Math.PI / 2;
    shank.position.z = 5;
    this.generic.add(tip, shank);
    this.holder = new THREE.Mesh(new THREE.CylinderGeometry(2.2, 2.2, 4, 24),
      new THREE.MeshStandardMaterial({ color: 0x3fc1e6, metalness: 0.3, roughness: 0.5 }));
    this.holder.geometry.rotateX(Math.PI / 2);
    this.holder.geometry.translate(0, 0, 2);   // bottom face at local z = 0
    this.cutter = null;
    this.cutterLength = 0;
    this.cutterRadius = 0;
    this.markerScale = 1;
    this.toolKey = '';
    g.add(this.generic, this.holder);
    return g;
  }

  _layoutTool() {
    const s = this.markerScale;
    this.generic.scale.setScalar(s);
    // With a real tool the holder belongs to it: a collet a bit wider than the cutter. (Sized to
    // the grid it was ~70 mm wide on a big machine, dwarfing a 6 mm bit.)
    this.holder.scale.setScalar(this.cutter ? Math.max(this.cutterRadius * 1.6, this.cutterRadius + 1.5) / 2.2 : s);
    // ...and see-through, so the cutter is what catches the eye.
    const m = this.holder.material;
    m.transparent = !!this.cutter;
    m.opacity = this.cutter ? 0.25 : 1;
    m.depthWrite = !this.cutter;
    m.needsUpdate = true;
    this.generic.visible = !this.cutter;
    this.holder.position.z = this.cutter ? this.cutterLength : 8 * s;
    this.dirty = true;
  }

  /** Draw the tool at its real size: {d: diameter, cr: corner radius, tipAngle: point angle in
   *  degrees (V-bits, chamfer mills, drills), kind}, in program units. null = generic marker. */
  setToolShape(shape) {
    const key = shape ? `${shape.d}|${shape.cr}|${shape.tipAngle}` : '';
    if (key === this.toolKey) return;
    this.toolKey = key;
    if (this.cutter) {
      this.tool.remove(this.cutter);
      this.cutter.geometry.dispose();
      this.cutter = null;
    }
    if (shape) {
      const r = shape.d / 2;
      const pts = [new THREE.Vector2(0, 0)];
      let flute = Math.max(shape.d * 3, 3);
      if (shape.tipAngle > 0 && shape.tipAngle < 179) {
        // Pointed: V-bit, chamfer mill, drill
        const h = r / Math.tan((shape.tipAngle / 2) * Math.PI / 180);
        pts.push(new THREE.Vector2(r, h));
        flute = Math.max(flute, h + shape.d);
      } else if (shape.cr > 0) {
        // Ball (corner radius = radius) or bull nose
        const cr = Math.min(shape.cr, r);
        for (let i = 0; i <= 8; i++) {
          const a = (i / 8) * Math.PI / 2;
          pts.push(new THREE.Vector2(r - cr + cr * Math.sin(a), cr - cr * Math.cos(a)));
        }
      } else {
        pts.push(new THREE.Vector2(r, 0));   // flat end mill
      }
      pts.push(new THREE.Vector2(r, flute), new THREE.Vector2(0, flute));
      const geom = new THREE.LatheGeometry(pts, 32);
      geom.rotateX(Math.PI / 2);   // lathe axis Y -> machine Z
      this.cutter = new THREE.Mesh(geom, this.metal);
      this.cutterLength = flute;
      this.cutterRadius = r;
      this.tool.add(this.cutter);
    }
    this._layoutTool();
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

    // Box around everything the program moves through
    if (this.hasProgram) {
      const box = new THREE.Box3(new THREE.Vector3(...min), new THREE.Vector3(...max));
      const helper = new THREE.Box3Helper(box, 0x4a5763);
      this.world.add(helper);
    }

    this.markerScale = Math.max(step / 6, 0.5);
    this._layoutTool();
    this.dirty = true;
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
    const b = this.hasProgram || !this.bed ? this.bounds : this.bed;
    if (!this.bed) return b;
    return {
      min: [0, 1, 2].map((k) => Math.min(b.min[k], this.bed.min[k])),
      max: [0, 1, 2].map((k) => Math.max(b.max[k], this.bed.max[k])),
    };
  }

  // ---------------- program ----------------

  /** Show a program's toolpath (from buildToolpath in gcode.js), or nothing for null.
   *  Feed moves are shaded by depth (light at the top, dark at the deepest cut). */
  setProgram(path) {
    if (this.path) {
      this.scene.remove(this.path);
      this.path.geometry.dispose();
      this.path.material.dispose();
      this.path = null;
    }
    this.hasProgram = !!path;
    this.programInches = !!path?.inches;   // the scene is in the program's units
    if (path?.count) {
      const pos = path.positions;
      const col = new Float32Array(pos.length);
      const [zTop, zLow] = path.cutZ;
      const range = zTop - zLow;
      const { top, deep, rapid } = this.colors;
      for (let v = 0; v < pos.length / 3; v++) {
        const o = v * 3;
        if (path.rapid[v >> 1]) {
          col[o] = rapid.r; col[o + 1] = rapid.g; col[o + 2] = rapid.b;
        } else {
          const t = range > 1e-9 ? Math.min(1, Math.max(0, (zTop - pos[o + 2]) / range)) : 0;
          col[o] = top.r + (deep.r - top.r) * t;
          col[o + 1] = top.g + (deep.g - top.g) * t;
          col[o + 2] = top.b + (deep.b - top.b) * t;
        }
      }
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      geom.setAttribute('color', new THREE.BufferAttribute(col, 3));
      this.path = new THREE.LineSegments(geom, new THREE.LineBasicMaterial({ vertexColors: true }));
      this.path.frustumCulled = false;
      this.scene.add(this.path);
    }
    this.bounds = path ? path.bounds : { min: [0, 0, 0], max: [100, 100, 10] };
    this._buildWorld();
    this.clearTrace();
    this.fit();
  }

  // ---------------- tool + trace ----------------

  setToolPosition(x, y, z, recordTrace) {
    if (this.tool.position.x !== x || this.tool.position.y !== y || this.tool.position.z !== z) this.dirty = true;
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
    this.dirty = true;
  }

  clearTrace() {
    this.traceCount = 0;
    this.trace.geometry.setDrawRange(0, 0);
    this.dirty = true;
  }

  setTraceVisible(v) { this.trace.visible = v; this.dirty = true; }
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
    this.dirty = true;
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
    this.dirty = true;
  }

  _render() {
    this.controls.update();   // fires 'change' while the view still glides (damping)
    if (!this.dirty) return;
    this.dirty = false;
    this.renderer.render(this.scene, this.camera);
  }
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / p;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * p;
}
