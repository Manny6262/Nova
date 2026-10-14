"""Best-effort Python check guard. This is not an operating-system sandbox."""
from pathlib import Path
import os
import runpy
import sys

work=Path.cwd().resolve()
runtime=tuple({Path(sys.base_prefix).resolve(),Path(sys.prefix).resolve()})


def inside(path,roots):
    if isinstance(path,int): return False
    if not isinstance(path,(str,bytes,os.PathLike)): return False
    p=Path(os.fsdecode(path)).resolve()
    return any(p.is_relative_to(root) for root in roots)


def audit(event,args):
    if event=='import' and args and str(args[0]).split('.')[0] in {'ctypes','_ctypes','_winapi','winreg','mmap','_socket','socket'}:
        raise PermissionError('Native/process/network modules are unavailable to a guarded check.')
    if event.startswith(('socket.','subprocess.','ctypes.','winreg.','os.exec','os.spawn','os.startfile')) or event in {'os.system','os.kill','os.killpg','os.fork','os.forkpty','os.putenv','os.unsetenv'}:
        raise PermissionError('Network, process, native-library and environment actions are unavailable to a guarded check.')
    if event=='open':
        path,mode,flags=args
        writing=(isinstance(mode,str) and any(m in mode for m in 'wax+')) or bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
        roots=(work,) if writing else (work,*runtime)
        if not inside(path,roots): raise PermissionError('The check attempted file access outside its working copy or Python runtime.')
    if event in {'os.remove','os.rmdir','os.mkdir','os.chmod','os.utime','os.truncate','os.chdir','os.listdir','os.scandir'}:
        if not inside(args[0],(work,*runtime) if event in {'os.listdir','os.scandir'} else (work,)):
            raise PermissionError('The check attempted to change or inspect an unapproved folder.')
    if event in {'os.rename','os.link'}:
        if not all(inside(p,(work,)) for p in args[:2]): raise PermissionError('The check attempted a move outside its working copy.')
    if event=='os.symlink': raise PermissionError('Checks cannot create links.')


def main():
    test=sys.argv[1]
    if not inside(work/test,(work,)): raise ValueError('Invalid check path.')
    sys.path.insert(0,str(work))
    sys.dont_write_bytecode=True
    sys.addaudithook(audit)
    # A model-generated module must not silently terminate before assertions run.
    def no_exit(*args): raise PermissionError('Checks cannot terminate the process directly.')
    os._exit=no_exit
    try:
        runpy.run_path(test,run_name='__main__')
    except SystemExit as exc:
        raise RuntimeError('The check exited before returning. Use unittest.main(exit=False) and explicitly fail on unsuccessful results.') from exc
    print('NOVA_CHECK_RETURNED')


if __name__=='__main__': main()
