// Electron UI 를 Windows x64 폴더로 패키징한다. scripts/build_msi.py 가 호출한다.
// 사용: node scripts/package.mjs <출력 폴더> <앱 버전>
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { packager } from '@electron/packager';

const uiDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const assets = path.resolve(uiDir, '..', 'assets');
const [outDir = path.resolve(uiDir, '..', 'build', 'electron'), appVersion = '1.0.0'] = process.argv.slice(2);

const paths = await packager({
  dir: uiDir,
  name: 'EduGuard',
  executableName: 'EduGuard',
  platform: 'win32',
  arch: 'x64',
  out: outDir,
  overwrite: true,
  asar: true,
  prune: false,
  appVersion,
  buildVersion: appVersion,
  icon: path.join(assets, 'eduguard.ico'),
  extraResource: [path.join(assets, 'eduguard.ico'), path.join(assets, 'eduguard.png')],
  // UI 는 런타임 의존성이 없으므로 node_modules(빌드 도구)는 통째로 뺀다.
  ignore: [/^\/src($|\/)/, /^\/scripts($|\/)/, /^\/node_modules($|\/)/, /^\/package-lock\.json$/],
  win32metadata: {
    CompanyName: 'EduGuard',
    FileDescription: 'EduGuard',
    ProductName: 'EduGuard',
    InternalName: 'EduGuard',
    OriginalFilename: 'EduGuard.exe',
    'requested-execution-level': 'requireAdministrator',
  },
});
console.log(paths[0]);
