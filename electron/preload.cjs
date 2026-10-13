const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('studio', {
  openFile: () => ipcRenderer.invoke('studio:open'),
  importFile: (file) => ipcRenderer.invoke('studio:import', webUtils.getPathForFile(file)),
  demo: () => ipcRenderer.invoke('studio:demo'),
  slice: (settings) => ipcRenderer.invoke('studio:slice', settings),
  exportJob: () => ipcRenderer.invoke('studio:export'),
  revealJob: () => ipcRenderer.invoke('studio:reveal'),
  info: () => ipcRenderer.invoke('studio:info'),
  onProgress: (callback) => {
    const listener = (_event, data) => callback(data);
    ipcRenderer.on('studio:progress', listener);
    return () => ipcRenderer.removeListener('studio:progress', listener);
  },
});
