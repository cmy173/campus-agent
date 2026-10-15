"""Isolated offline regression runner; never uses the real DB/files/secrets.

Precreated basetemp avoids Windows Python 3.12 mode=0700 ACL incompatibility
in restricted test environments. Only this process's UUID tree is affected.
"""
from pathlib import Path
import os
import sys
import uuid
root=Path(__file__).resolve().parents[1]
work=root/'.test-tmp'/('agent-audit-'+uuid.uuid4().hex)
work.mkdir(parents=True)
for folder in ('pytest','files','secrets','agent','workspace','campus','temp'):
    (work/folder).mkdir()
os.environ.update(STUDY_DATABASE_URL='sqlite:///'+(work/'domain.db').as_posix(),
                  STUDY_FILES_DIR=str(work/'files'),STUDY_SECRETS_DIR=str(work/'secrets'),
                  CAMPUS_AGENT_HOME=str(work/'agent'),CAMPUS_AGENT_WORKSPACE=str(work/'workspace'),
                  CAMPUS_WORKSPACE_DIR=str(work/'workspace'),
                  CAMPUS_DATA_DIR=str(work/'campus'),STUDY_WORKER_ENABLED='0',STUDY_WORKER_INPROC='0',
                  TMP=str(work/'temp'),TEMP=str(work/'temp'))
sys.path[:0]=[str(root/'apps'/'api'),str(root)]
import pytest
class SafeTemp:
    @pytest.hookimpl(trylast=True)
    def pytest_configure(self, config):
        factory=getattr(config,'_tmp_path_factory',None)
        if factory:
            factory._basetemp=work/'pytest'
            # The numbered helper also requests mode=0700 on Windows. Restrict
            # normalization to this synthetic tree, keeping production untouched.
            original=Path.mkdir
            def mkdir(path, mode=0o777, parents=False, exist_ok=False):
                if work in path.parents and mode==0o700: mode=0o777
                return original(path,mode=mode,parents=parents,exist_ok=exist_ok)
            Path.mkdir=mkdir
            original_os_mkdir=os.mkdir
            def os_mkdir(path, mode=0o777, *, dir_fd=None):
                candidate=Path(path)
                if work in candidate.parents and mode==0o700: mode=0o777
                return original_os_mkdir(path,mode,dir_fd=dir_fd)
            os.mkdir=os_mkdir
print('ISOLATED_WORKSPACE='+str(work),flush=True)
args=sys.argv[1:] or ['apps/api/tests','-q']
raise SystemExit(pytest.main(args,plugins=[SafeTemp()]))
