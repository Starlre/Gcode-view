const { spawn } = require('node:child_process');
const childEnv = { ...process.env };
delete childEnv.ELECTRON_RUN_AS_NODE;
const child = spawn(require('electron'), ['.', ...process.argv.slice(2)], {
  stdio: 'inherit', windowsHide: true, env: childEnv,
});
child.on('error', (error) => { console.error(error); process.exit(1); });
child.on('exit', (code) => process.exit(code || 0));
