const { spawnSync } = require('node:child_process');
const path = require('node:path');

function run(command, args) {
  const result = spawnSync(command, args, { stdio: 'inherit', windowsHide: true, shell: false });
  if (result.status !== 0) process.exit(result.status || 1);
}
run(process.env.HKUPRINT_PYTHON || 'python', ['-B', 'scripts/make_icon.py']);
run(process.env.HKUPRINT_PYTHON || 'python', ['-m', 'PyInstaller', '--noconfirm', '--onefile',
  '--name', 'hku-slice-backend', '--paths', '..', '--distpath', 'backend-dist',
  '--workpath', 'backend-build', 'backend/hku_backend.py']);
run(process.execPath, [path.join('node_modules', 'vite', 'bin', 'vite.js'), 'build']);
run(process.execPath, [path.join('node_modules', 'electron-builder', 'cli.js'), '--win', '--dir']);
