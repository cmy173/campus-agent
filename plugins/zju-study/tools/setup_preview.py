"""下载官方 LibreOffice，解包到项目 var/runtime，供本地离线预览使用。

不修改默认办公软件、不注册文件关联。已有系统 LibreOffice 时无需运行。
官方发行包固定 SHA256 验证；Windows MSI administrative extraction。
"""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://download.documentfoundation.org/libreoffice/stable/26.8.1/win/x86_64/LibreOffice_26.8.1_Win_x86-64.msi'
SHA256 = 'c6298b10cfa1748bcbedad94c62ce771e240e5021bfd6d7a967cd571b04b5312'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--msi', type=Path, help='使用已下载的官方安装包（仍验证哈希）')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('请通过系统包管理器安装 LibreOffice，或设置 STUDY_SOFFICE_PATH。')
    target = (ROOT / 'var' / 'runtime' / 'libreoffice').resolve()
    if not target.is_relative_to(ROOT.resolve()):
        raise SystemExit('运行库目录必须位于项目内')
    target.mkdir(parents=True, exist_ok=True)
    if next(target.glob('**/program/soffice.exe'), None):
        print('本地预览运行库已就绪。'); return
    installer = args.msi or (ROOT / 'var' / 'runtime' / 'LibreOffice-26.8.1.msi')
    if not installer.is_file():
        print('正在下载官方本地转换器（约 358MB），文件不会发送至任何转换网站。', flush=True)
        with urllib.request.urlopen(URL, timeout=45) as response, installer.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    with installer.open('rb') as input_file:
        digest = hashlib.file_digest(input_file, 'sha256').hexdigest()
    if digest != SHA256:
        raise SystemExit('官方安装包哈希不匹配，未运行；请重新下载。')
    print('校验通过，正在解包本地运行库…', flush=True)
    result = subprocess.run(['msiexec.exe', '/a', str(installer.resolve()), '/qn', f'TARGETDIR={target}'],
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=600)
    converter = next(target.glob('**/program/soffice.exe'), None)
    if result.returncode or converter is None:
        raise SystemExit(f'解包未完成（退出码 {result.returncode}）。也可以安装官方 LibreOffice 后重试预览。')
    print('本地离线预览运行库已就绪。')


if __name__ == '__main__':
    main()
