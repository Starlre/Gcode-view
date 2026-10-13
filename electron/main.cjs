const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const { readFile, writeFile, mkdir } = require('node:fs/promises');
const { mkdtempSync } = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

let window, worker, workerError = '', lastJob = null, nextId = 0;
const pending = new Map();
const smoke = process.argv.includes('--smoke-test');
if (smoke) {
  app.setPath('userData', mkdtempSync(path.join(app.getPath('temp'), 'hkuprint-studio-smoke-')));
  app.commandLine.appendSwitch('disable-features', 'CalculateNativeWinOcclusion');
}

function rejectPending(message) {
  for (const entry of pending.values()) entry.reject(new Error(message));
  pending.clear();
}

function startBackend() {
  if (worker) return;
  workerError = '';
  const command = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', 'hku-slice-backend.exe')
    : process.env.HKUPRINT_PYTHON || 'python';
  const args = app.isPackaged ? [] : ['-u', '-B', path.join(__dirname, 'backend', 'hku_backend.py')];
  const child = spawn(command, args, { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' } });
  worker = child;
  readline.createInterface({ input: child.stdout }).on('line', (line) => {
    let response;
    try { response = JSON.parse(line); } catch { return; }
    if (response.event === 'progress') {
      window?.webContents.send('studio:progress', response);
      return;
    }
    const request = pending.get(response.id);
    if (!request) return;
    pending.delete(response.id);
    response.ok ? request.resolve(response.data) : request.reject(new Error(response.error));
  });
  child.stderr.on('data', (chunk) => { workerError = (workerError + chunk).slice(-2000); });
  child.on('error', (error) => {
    rejectPending(`切片引擎无法启动：${error.message}。开发版需要 Python，或设置 HKUPRINT_PYTHON。`);
    if (worker === child) worker = null;
  });
  child.on('exit', (code) => {
    rejectPending(`切片引擎已退出 (${code})：${workerError}`);
    if (worker === child) worker = null;
  });
  child.stdin.on('error', () => rejectPending('切片引擎连接中断，请重新打开应用。'));
}

function request(method, args = {}) {
  startBackend();
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    worker.stdin.write(JSON.stringify({ id, method, args }) + '\n');
  });
}

async function importFile(filePath) {
  const extension = path.extname(filePath).toLowerCase();
  if (extension === '.gcode' || extension === '.gco' || extension === '.gc') {
    const text = await readFile(filePath, 'utf8');
    if (Buffer.byteLength(text) > 100 * 1024 * 1024) throw new Error('G-code 超过 100 MB，请使用较小的文件。');
    return { kind: 'toolpath', name: path.basename(filePath), gcode: text, imported: true };
  }
  if (!['.stl', '.obj'].includes(extension)) throw new Error('请选择 STL、OBJ 或 G-code 文件。');
  return request('load', { path: filePath });
}

function registerIPC() {
  const handle = (name, action) => ipcMain.handle(`studio:${name}`, async (event, ...args) => {
    if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) {
      return { ok: false, error: '无效的窗口请求' };
    }
    try { return { ok: true, data: await action(...args) }; }
    catch (error) { return { ok: false, error: error.message }; }
  });
  handle('info', () => request('info'));
  handle('open', async () => {
    const result = await dialog.showOpenDialog(window, { title: '导入模型或 G-code', properties: ['openFile'],
      filters: [{ name: '模型与走刀文件', extensions: ['stl', 'obj', 'gcode', 'gco', 'gc'] }] });
    return result.canceled ? null : importFile(result.filePaths[0]);
  });
  handle('import', (filePath) => {
    if (typeof filePath !== 'string' || !path.isAbsolute(filePath)) throw new Error('文件路径无效');
    return importFile(filePath);
  });
  handle('demo', () => request('demo'));
  handle('slice', (settings) => request('slice', { settings }));
  handle('export', async () => {
    const result = await dialog.showOpenDialog(window, { title: '选择打印作业保存位置', properties: ['openDirectory', 'createDirectory'] });
    if (result.canceled) return null;
    lastJob = await request('export', { directory: result.filePaths[0] });
    return lastJob;
  });
  handle('reveal', async () => { if (lastJob) await shell.openPath(lastJob.directory); });
}

async function runSmokeTest() {
  const errors = [];
  window.webContents.on('console-message', (_event, _level, message) => {
    if (message.includes('Error') && !message.includes('Electron Security')) errors.push(message);
  });
  try {
    await new Promise((resolve) => window.webContents.once('did-finish-load', resolve));
    await window.webContents.insertCSS('* { transition: none !important; }');
    await window.webContents.executeJavaScript("document.documentElement.dataset.theme = 'light'; window.dispatchEvent(new Event('studio:theme'))");
    const result = await window.webContents.executeJavaScript('window.runStudioSmokeTest()');
    const artifactDir = process.env.STUDIO_ARTIFACT_DIR || (app.isPackaged
      ? path.join(app.getPath('temp'), 'hkuprint-studio-test') : path.join(__dirname, 'artifacts'));
    await mkdir(artifactDir, { recursive: true });
    const capture = async (name) => {
      await window.webContents.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
      await window.webContents.executeJavaScript('window.studioRenderProbe()');
      // Prime the hidden window's compositor before capturing the current state.
      await window.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
      await new Promise((resolve) => setTimeout(resolve, 500));
      await window.webContents.executeJavaScript('window.studioRenderProbe()');
      const screenshot = await window.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
      await writeFile(path.join(artifactDir, name), screenshot.toPNG());
    };
    await new Promise((resolve) => setTimeout(resolve, 600));
    const render = await window.webContents.executeJavaScript('window.studioRenderProbe()');
    if (render.differentPixels < 1000) throw new Error('三维渲染区域为空');
    await new Promise((resolve) => setTimeout(resolve, 350));
    await capture('studio-light.png');
    await window.webContents.executeJavaScript('window.studioModelScreenshot()');
    await new Promise((resolve) => setTimeout(resolve, 350));
    await capture('studio-model.png');
    await window.webContents.executeJavaScript('window.studioPathScreenshot()');
    await window.webContents.executeJavaScript("document.documentElement.dataset.theme = 'dark'; window.dispatchEvent(new Event('studio:theme'))");
    await new Promise((resolve) => setTimeout(resolve, 250));
    await capture('studio-dark.png');
    await window.webContents.executeJavaScript("document.documentElement.dataset.theme = 'light'; window.dispatchEvent(new Event('studio:theme'))");
    window.setSize(1180, 800);
    await new Promise((resolve) => setTimeout(resolve, 600));
    await window.webContents.executeJavaScript('window.studioRenderProbe()');
    await new Promise((resolve) => setTimeout(resolve, 350));
    const compact = await window.webContents.executeJavaScript('window.studioLayoutProbe()');
    await capture('studio-compact.png');
    const exportResult = await request('export', { directory: artifactDir });
    const imported = await importFile(exportResult.gcode);
    const importResult = await window.webContents.executeJavaScript(`window.studioImportSmoke(${JSON.stringify(imported)})`);
    const report = { ...result, render, compact, exportResult, importResult, packaged: app.isPackaged, errors };
    await writeFile(path.join(artifactDir, 'smoke-report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
    app.exit(errors.length || compact.horizontalOverflow ? 1 : 0);
  } catch (error) { console.error(error); app.exit(1); }
}

app.whenReady().then(() => {
  window = new BrowserWindow({ width: 1540, height: 1000, minWidth: 1120, minHeight: 760,
    title: 'HKUprint Studio', backgroundColor: '#d8e0e9', show: !smoke,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true,
      nodeIntegration: false, sandbox: true, backgroundThrottling: false } });
  window.setMenuBarVisibility(false);
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event) => event.preventDefault());
  registerIPC();
  if (smoke) runSmokeTest();
  if (process.env.STUDIO_DEV_URL) window.loadURL(process.env.STUDIO_DEV_URL);
  else window.loadFile(path.join(__dirname, 'dist', 'index.html'));
  if (!smoke) window.maximize();
});
app.on('before-quit', () => { worker?.kill(); rejectPending('应用已关闭'); });
app.on('window-all-closed', () => app.quit());
