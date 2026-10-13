import { parseGcode } from './gcode.js';

self.onmessage = ({ data }) => {
  try {
    const model = parseGcode(data.text, data.name);
    self.postMessage({ id: data.id, model }, [model.extrusion.buffer, model.travel.buffer,
      model.extrusionPrefix.buffer, model.travelPrefix.buffer]);
  } catch (error) { self.postMessage({ id: data.id, error: error.message }); }
};
