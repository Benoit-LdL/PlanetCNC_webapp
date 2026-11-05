// Builds the toolpath off the main thread, so a big program doesn't freeze the dashboard.
import { buildToolpath } from './gcode.js';

self.onmessage = (e) => {
  const { seq, text } = e.data;
  let path = null;
  try {
    path = buildToolpath(text);
  } catch (err) {
    console.error('toolpath failed', err);
  }
  self.postMessage({ seq, path }, path ? [path.positions.buffer, path.rapid.buffer] : []);
};
