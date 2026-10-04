"""Reference helper. Solutions themselves are generated per task, not static gold files."""
from pathlib import Path
import shutil


def materialize(task_dir, workspace_dir, reply_file):
    """Copy a generated reference into an empty worker workspace and reply file."""
    task, workspace, reply = Path(task_dir), Path(workspace_dir), Path(reply_file)
    source = task/'reference'/'workspace'
    if not source.is_dir() or source.is_symlink() or any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('reference workspace is missing or contains a symlink')
    if workspace.is_symlink() or (workspace.exists() and any(workspace.iterdir())):
        raise ValueError('reference destination must be empty')
    if reply.is_symlink():
        raise ValueError('reference reply destination must not be a symlink')
    shutil.copytree(source,workspace,dirs_exist_ok=True)
    reply.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(task/'reference'/'reply.txt',reply)
