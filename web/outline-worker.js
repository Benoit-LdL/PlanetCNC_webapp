// Builds the job outline off the main thread, so a big program doesn't freeze the dashboard.
import { buildOutline } from './gcode.js';

self.onmessage = (e) => {
  const { seq, text } = e.data;
  let outline = null;
  try {
    outline = buildOutline(text);
  } catch (err) {
    console.error('outline failed', err);
  }
  self.postMessage({ seq, outline }, outline ? [outline.positions.buffer] : []);
};
