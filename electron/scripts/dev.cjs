const { spawn } = require('node:child_process');

(async () => {
  const { createServer } = await import('vite');
  const server = await createServer();
  await server.listen();
  const electron = require('electron');
  const childEnv = { ...process.env, STUDIO_DEV_URL: 'http://127.0.0.1:5173' };
  delete childEnv.ELECTRON_RUN_AS_NODE;
  const child = spawn(electron, ['.'], { stdio: 'inherit', windowsHide: true,
    env: childEnv });
  child.on('exit', async (code) => { await server.close(); process.exit(code || 0); });
})().catch((error) => { console.error(error); process.exit(1); });
