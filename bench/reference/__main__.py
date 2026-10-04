"""Offline oracle command for smoke-testing the grid plumbing."""
import argparse
from pathlib import Path
from . import materialize

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('task',type=Path)
parser.add_argument('workspace',type=Path)
parser.add_argument('reply',type=Path)
args = parser.parse_args()
materialize(args.task,args.workspace,args.reply)
print(args.reply.read_text(encoding='utf-8'),end='')
